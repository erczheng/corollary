"""Decision 21's tradeability cache: which tickers need a check, and the refresh that fills it.

The pure filter has its own file (``test_tradeability.py``). This one is the
cache around it: one ``ticker_tradeability`` row per ticker per session date,
filled only for off-watch tickers, never for a ticker whose inputs the vendor
failed to give, and never spending a request whose answer cannot change the
verdict. Fakes stand in for the provider except in the last section, which
replays the recorded ``p4_*`` fixtures -- including AIFU, a ``has_options``
underlying whose only live contracts are adjusted.
"""

import json
from collections.abc import Callable, Iterator, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.data.news.tradeability import (
    ADV_BATCH_SIZE,
    MAX_TICKERS_PER_RUN,
    RefreshResult,
    TradeabilityFailure,
    TradeabilityResult,
    recent_article_tickers,
    refresh_tradeability,
    store_tradeability,
    tickers_needing_check,
)
from corollary.data.news.watchlist import watch_universe
from corollary.data.providers.alpaca import ADV_MAX_SYMBOLS
from corollary.data.providers.interface import (
    AssetDirectory,
    Bar,
    EquityAsset,
    ProviderError,
)
from corollary.db.models import Base, NewsArticle, NewsArticleTicker, TickerTradeability
from corollary.db.session import create_db_engine, sqlite_url
from tests.data.news.test_tradeability import history
from tests.data.providers.conftest import fixture_text, limiter, make_provider  # noqa: F401

SESSION = date(2026, 9, 24)
NEXT_SESSION = date(2026, 9, 25)
NOW = datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc)


def _now() -> datetime:
    return NOW


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(sqlite_url(tmp_path / "tradeability.db"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(db_engine: Engine) -> Callable[[], Session]:
    return lambda: Session(db_engine)


def _asset(symbol: str, *, has_options: bool = True) -> EquityAsset:
    return EquityAsset(
        symbol=symbol, name=f"{symbol} Inc.", tradable=True, has_options=has_options,
        exchange="NASDAQ",
    )


def directory(*optionable: str, without_options: Sequence[str] = ()) -> AssetDirectory:
    return AssetDirectory(
        assets=tuple(_asset(s) for s in optionable)
        + tuple(_asset(s, has_options=False) for s in without_options)
    )


def liquid(symbol: str, session_date: date) -> list[Bar]:
    """25 sessions of 2,000,000 shares closing at $50 -- passes every bar check."""
    return history(25, before=session_date, symbol=symbol)


class FakeInputs:
    """The two tradeability requests, recorded, answering from per-ticker tables."""

    def __init__(
        self,
        *,
        bars: Callable[[str, date], list[Bar]] = liquid,
        roots: dict[str, bool] | None = None,
        root_errors: Sequence[str] = (),
        bar_errors: int = 0,
    ) -> None:
        self._bars = bars
        self._roots = roots or {}
        self._root_errors = set(root_errors)
        self._bar_errors = bar_errors
        self.bar_calls: list[list[str]] = []
        self.root_calls: list[str] = []

    async def adv_daily_bars(
        self, symbols: Sequence[str], *, session_date: date
    ) -> dict[str, list[Bar]]:
        assert len(symbols) <= ADV_MAX_SYMBOLS
        self.bar_calls.append(list(symbols))
        if self._bar_errors:
            self._bar_errors -= 1
            raise ProviderError("GET /v2/stocks/bars failed: 503")
        answer: dict[str, list[Bar]] = {}
        for symbol in symbols:
            bars = self._bars(symbol, session_date)
            if bars:  # Alpaca omits a symbol with no bars in range
                answer[symbol] = bars
        return answer

    async def has_standard_root(self, ticker: str) -> bool:
        self.root_calls.append(ticker)
        if ticker in self._root_errors:
            raise ProviderError(f"GET /v2/options/contracts failed for {ticker}: 503")
        return self._roots.get(ticker, True)


async def refresh(
    sessions: Callable[[], Session],
    provider: Any,
    tickers: Sequence[str],
    *,
    assets: AssetDirectory | None,
    session_date: date = SESSION,
    **kwargs: Any,
) -> RefreshResult:
    return await refresh_tradeability(
        provider=provider,
        assets=assets,
        tickers=tickers,
        session_date=session_date,
        session_factory=sessions,
        now=_now,
        **kwargs,
    )


def rows(sessions: Callable[[], Session]) -> dict[tuple[str, date], TickerTradeability]:
    with sessions() as session:
        found = session.scalars(select(TickerTradeability)).all()
        session.expunge_all()
    return {(row.ticker, row.session_date): row for row in found}


# ------------------------------------------------ which tickers need a check


UNIVERSE = watch_universe(markets=["AAPL"], leaders=["MSFT"], positions=["TSLA"], manual=["NVDA"])


def test_only_off_watch_candidates_need_a_check(sessions):
    candidates = ["AAPL", "acme", "MARKET", "nvda", "MSFT", "TSLA", "ZZZZ", "ACME"]
    with sessions() as session:
        needed = tickers_needing_check(
            session, candidates=candidates, watch=UNIVERSE, session_date=SESSION
        )
    # Normalised, de-duplicated, caller order kept; every watch reason and
    # MARKET excluded.
    assert needed == ["ACME", "ZZZZ"]


def test_a_ticker_cached_this_session_is_not_rechecked_but_is_next_session(sessions):
    with sessions() as session:
        session.add(
            TickerTradeability(
                ticker="ACME", session_date=SESSION, has_options=False, standard_root=None,
                avg_volume_20d=None, last_close=None, sessions_available=0, passes=False,
                failures="no_options", checked_at=NOW,
            )
        )
        session.commit()
        same = tickers_needing_check(
            session, candidates=["ACME", "ZZZZ"], watch=UNIVERSE, session_date=SESSION
        )
        nxt = tickers_needing_check(
            session, candidates=["ACME", "ZZZZ"], watch=UNIVERSE, session_date=NEXT_SESSION
        )
    assert same == ["ZZZZ"]
    assert nxt == ["ACME", "ZZZZ"]


def _add_article(session: Session, vendor_id: str, published: datetime, *tickers: str) -> None:
    article = NewsArticle(
        vendor="alpaca", vendor_id=vendor_id, feed="alpaca_news",
        url=f"https://example.com/{vendor_id}", url_key=f"example.com/{vendor_id}",
        headline=f"story {vendor_id}", headline_key=f"story {vendor_id}",
        summary=None, publisher="Benzinga", published_at=published, ingested_at=published,
    )
    session.add(article)
    session.flush()
    for ticker in tickers:
        session.add(NewsArticleTicker(article_id=article.id, ticker=ticker))


def test_recent_article_tickers_reads_tags_since_a_time_newest_first(sessions):
    since = NOW - timedelta(days=1)
    with sessions() as session:
        _add_article(session, "old", since - timedelta(seconds=1), "OLDY")
        _add_article(session, "a", since, "ACME", "BETA")
        _add_article(session, "b", since + timedelta(hours=2), "BETA", "MARKET")
        _add_article(session, "c", since + timedelta(hours=1), "CHAR")
        session.commit()
        found = recent_article_tickers(session, since)
    # ``since`` is inclusive. Ordered by each ticker's latest article, newest
    # first, ties by symbol -- so a capped run checks the freshest news first.
    assert found == ["BETA", "MARKET", "CHAR", "ACME"]


def test_recent_article_tickers_refuses_a_naive_since(sessions):
    with sessions() as session, pytest.raises(ValueError):
        recent_article_tickers(session, datetime(2026, 9, 24))


# ------------------------------------------------------------------ refresh


@pytest.mark.asyncio
async def test_a_passing_ticker_is_cached_with_every_field(sessions):
    def bars(symbol: str, session_date: date) -> list[Bar]:
        return history(25, before=session_date, symbol=symbol, last_close="50.25")

    provider = FakeInputs(bars=bars)
    result = await refresh(sessions, provider, ["ACME"], assets=directory("ACME"))

    assert result.skipped is None
    assert result.errors == ()
    assert [r.ticker for r in result.results] == ["ACME"]
    assert provider.root_calls == ["ACME"]
    row = rows(sessions)[("ACME", SESSION)]
    assert row.has_options is True
    assert row.standard_root is True
    assert row.avg_volume_20d == 2_000_000
    # Money round-trips exactly: TEXT in, Decimal out, never a float.
    assert row.last_close == Decimal("50.25")
    assert isinstance(row.last_close, Decimal)
    assert row.sessions_available == 20
    assert row.passes is True
    assert row.failures == ""
    assert row.checked_at == NOW


@pytest.mark.asyncio
async def test_the_failures_column_is_the_results_failures_in_declaration_order(sessions):
    def bars(symbol: str, session_date: date) -> list[Bar]:
        return history(25, before=session_date, symbol=symbol, volume=10, last_close="1.10")

    result = await refresh(sessions, FakeInputs(bars=bars), ["ACME"], assets=directory("ACME"))

    (assessed,) = result.results
    row = rows(sessions)[("ACME", SESSION)]
    assert row.failures == ",".join(assessed.failures)
    assert row.failures == "low_volume,low_close"
    assert row.passes is False
    assert row.last_close == Decimal("1.10")
    assert row.avg_volume_20d == 10


@pytest.mark.asyncio
async def test_a_ticker_without_options_spends_no_root_request(sessions):
    provider = FakeInputs()
    await refresh(
        sessions, provider, ["ACME", "NOPT"],
        assets=directory("ACME", without_options=["NOPT"]),
    )

    assert provider.root_calls == ["ACME"]
    row = rows(sessions)[("NOPT", SESSION)]
    assert row.has_options is False
    assert row.standard_root is None  # not checked: the answer could not matter
    assert row.failures == "no_options"
    # Its bars were still read, so the row states its real volume and close
    # rather than an invented "insufficient history".
    assert row.avg_volume_20d == 2_000_000
    assert row.last_close == Decimal("50")


@pytest.mark.asyncio
async def test_a_ticker_missing_from_the_directory_reads_as_no_options(sessions):
    provider = FakeInputs()
    await refresh(sessions, provider, ["GONE"], assets=directory("ACME"))

    assert provider.root_calls == []
    assert rows(sessions)[("GONE", SESSION)].failures == "no_options"


@pytest.mark.asyncio
async def test_a_ticker_failing_on_its_bars_spends_no_root_request(sessions):
    def bars(symbol: str, session_date: date) -> list[Bar]:
        if symbol == "THIN":
            return history(25, before=session_date, symbol=symbol, volume=999_999)
        return liquid(symbol, session_date)

    provider = FakeInputs(bars=bars)
    await refresh(sessions, provider, ["ACME", "THIN"], assets=directory("ACME", "THIN"))

    assert provider.root_calls == ["ACME"]
    row = rows(sessions)[("THIN", SESSION)]
    assert row.standard_root is None
    assert row.failures == "low_volume"


@pytest.mark.asyncio
async def test_a_has_options_underlying_with_only_adjusted_contracts_fails(sessions):
    provider = FakeInputs(roots={"AIFU": False})
    result = await refresh(sessions, provider, ["AIFU"], assets=directory("AIFU"))

    assert provider.root_calls == ["AIFU"]
    (assessed,) = result.results
    assert assessed.failures == (TradeabilityFailure.NO_STANDARD_CONTRACT,)
    row = rows(sessions)[("AIFU", SESSION)]
    assert row.has_options is True
    assert row.standard_root is False
    assert row.failures == "no_standard_contract"


@pytest.mark.asyncio
async def test_an_adjusted_root_ticker_is_cached_without_a_root_request(sessions):
    provider = FakeInputs()
    await refresh(sessions, provider, ["AIFU1"], assets=directory("AIFU1"))

    # AIFU1 is not an equity symbol, so no bars request carries it either.
    assert provider.root_calls == []
    assert provider.bar_calls == []
    failures = rows(sessions)[("AIFU1", SESSION)].failures.split(",")
    assert failures[0] == "adjusted_root"


@pytest.mark.asyncio
async def test_a_malformed_ticker_spends_no_request_and_is_cached_as_malformed(sessions):
    provider = FakeInputs()
    await refresh(sessions, provider, ["BTC/USD", "ACME"], assets=directory("ACME"))

    assert provider.bar_calls == [["ACME"]]
    assert provider.root_calls == ["ACME"]
    row = rows(sessions)[("BTC/USD", SESSION)]
    assert row.failures.split(",")[0] == "malformed_ticker"


@pytest.mark.asyncio
async def test_market_is_never_checked_even_if_a_caller_passes_it(sessions):
    provider = FakeInputs()
    result = await refresh(sessions, provider, ["MARKET"], assets=directory("MARKET"))

    assert provider.bar_calls == []
    assert provider.root_calls == []
    assert rows(sessions) == {}
    assert result.results == ()


@pytest.mark.asyncio
async def test_no_asset_directory_skips_the_run_and_caches_nothing(sessions):
    provider = FakeInputs()
    result = await refresh(sessions, provider, ["ACME"], assets=None)

    assert result.skipped is not None and "asset" in result.skipped
    assert result.results == ()
    assert provider.bar_calls == [] and provider.root_calls == []
    assert rows(sessions) == {}


@pytest.mark.asyncio
async def test_a_failed_bars_request_caches_nothing_for_its_batch_and_is_retried(sessions):
    provider = FakeInputs(bar_errors=1)
    first = await refresh(sessions, provider, ["ACME", "BETA"], assets=directory("ACME", "BETA"))

    assert rows(sessions) == {}
    assert provider.root_calls == []  # no root request for a ticker that cannot be cached
    assert sorted(e.ticker for e in first.errors) == ["ACME", "BETA"]
    assert {e.stage for e in first.errors} == {"daily_bars"}
    assert all("503" in e.error for e in first.errors)

    with sessions() as session:
        again = tickers_needing_check(
            session, candidates=["ACME", "BETA"], watch=UNIVERSE, session_date=SESSION
        )
    assert again == ["ACME", "BETA"]
    second = await refresh(sessions, provider, again, assets=directory("ACME", "BETA"))
    assert second.errors == ()
    assert set(rows(sessions)) == {("ACME", SESSION), ("BETA", SESSION)}


@pytest.mark.asyncio
async def test_a_failed_root_check_is_not_cached_as_a_failure(sessions):
    provider = FakeInputs(root_errors=["BETA"])
    result = await refresh(sessions, provider, ["ACME", "BETA"], assets=directory("ACME", "BETA"))

    assert set(rows(sessions)) == {("ACME", SESSION)}
    (error,) = result.errors
    assert (error.ticker, error.stage) == ("BETA", "standard_root")
    assert "503" in error.error


@pytest.mark.asyncio
async def test_cached_then_not_rechecked_this_session_and_rechecked_the_next(sessions):
    provider = FakeInputs()
    await refresh(sessions, provider, ["ACME"], assets=directory("ACME"))

    with sessions() as session:
        same = tickers_needing_check(
            session, candidates=["ACME"], watch=UNIVERSE, session_date=SESSION
        )
        nxt = tickers_needing_check(
            session, candidates=["ACME"], watch=UNIVERSE, session_date=NEXT_SESSION
        )
    assert same == []
    assert nxt == ["ACME"]

    await refresh(sessions, provider, nxt, assets=directory("ACME"), session_date=NEXT_SESSION)
    assert set(rows(sessions)) == {("ACME", SESSION), ("ACME", NEXT_SESSION)}


@pytest.mark.asyncio
async def test_writing_the_same_ticker_twice_in_a_session_upserts_one_row(sessions):
    await refresh(sessions, FakeInputs(roots={"ACME": False}), ["ACME"], assets=directory("ACME"))
    await refresh(sessions, FakeInputs(), ["ACME"], assets=directory("ACME"))

    found = rows(sessions)
    assert list(found) == [("ACME", SESSION)]
    assert found[("ACME", SESSION)].passes is True


# ----------------------------------------- the store refuses an unproven pass


def _verdict(ticker: str, **overrides: Any) -> TradeabilityResult:
    fields: dict[str, Any] = {
        "ticker": ticker,
        "session_date": SESSION,
        "has_options": True,
        "standard_root": True,
        "avg_volume_20d": 2_000_000,
        "last_close": Decimal("50"),
        "sessions_available": 20,
        "failures": (),
    }
    fields.update(overrides)
    return TradeabilityResult(**fields)


@pytest.mark.parametrize(
    "overrides",
    [
        {"standard_root": None},  # the root check never ran
        {"standard_root": False},  # the root check ran and failed
        {"has_options": False},
    ],
    ids=["root-unchecked", "root-false", "no-options"],
)
def test_a_passing_result_without_a_proven_standard_root_is_refused(sessions, overrides):
    # A hand-built pass whose standard-contract check never ran is exactly how
    # an adjusted-root name would reach discovery with the wrong multiplier.
    good = _verdict("GOOD")
    unproven = _verdict("BAD", **overrides)
    assert unproven.passes
    with sessions() as session:
        with pytest.raises(ValueError, match="BAD"):
            store_tradeability(session, [good, unproven], checked_at=NOW)
        session.commit()
    # The whole store refuses: the good row ahead of it is not written either.
    assert rows(sessions) == {}


def test_a_proven_pass_and_a_failure_without_a_root_check_are_stored(sessions):
    moot = _verdict(
        "THIN", standard_root=None, failures=(TradeabilityFailure.LOW_VOLUME,)
    )
    with sessions() as session:
        written = store_tradeability(session, [_verdict("GOOD"), moot], checked_at=NOW)
        session.commit()
    assert written == 2
    found = rows(sessions)
    assert found[("GOOD", SESSION)].passes is True
    assert found[("GOOD", SESSION)].standard_root is True
    assert found[("THIN", SESSION)].standard_root is None


# ------------------------------------ a directory that cannot answer has_options


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assets",
    [
        AssetDirectory(assets=()),
        directory(without_options=["ACME", "BETA"]),
    ],
    ids=["empty", "options-less"],
)
async def test_a_directory_with_no_optionable_name_skips_the_run(sessions, assets):
    provider = FakeInputs()
    result = await refresh(sessions, provider, ["ACME", "BETA"], assets=assets)

    assert result.skipped is not None and "optionable" in result.skipped
    assert result.results == () and result.errors == () and result.deferred == ()
    assert provider.bar_calls == [] and provider.root_calls == []
    assert rows(sessions) == {}


def _with_missing_attributes(optionable: int, missing: int) -> AssetDirectory:
    names = _symbols(optionable + missing)
    return AssetDirectory(
        assets=tuple(_asset(s) for s in names[:optionable])
        + tuple(_asset(s, has_options=False) for s in names[optionable:]),
        missing_attributes=missing,
    )


@pytest.mark.asyncio
async def test_a_directory_mostly_missing_attributes_skips_the_run(sessions):
    # 3 of 5 rows carried no attributes: more than half -- a vendor schema
    # change, not a day on which most equities lost their options.
    provider = FakeInputs()
    result = await refresh(
        sessions, provider, ["ACME"], assets=_with_missing_attributes(2, 3)
    )

    assert result.skipped is not None and "attributes" in result.skipped
    assert provider.bar_calls == [] and provider.root_calls == []
    assert rows(sessions) == {}


@pytest.mark.asyncio
async def test_a_directory_exactly_half_missing_attributes_still_runs(sessions):
    assets = _with_missing_attributes(3, 3)
    ticker = _symbols(1)[0]  # optionable in this directory
    result = await refresh(sessions, FakeInputs(), [ticker], assets=assets)

    assert result.skipped is None
    assert set(rows(sessions)) == {(ticker, SESSION)}


# ------------------------------------------------- bars that are not one series


@pytest.mark.asyncio
async def test_bad_bar_data_caches_nothing_for_that_ticker_and_spends_no_root(sessions):
    def bars(symbol: str, session_date: date) -> list[Bar]:
        if symbol == "BAD":  # the vendor answered BAD with another symbol's bars
            return history(25, before=session_date, symbol="OTHER")
        return liquid(symbol, session_date)

    provider = FakeInputs(bars=bars)
    result = await refresh(sessions, provider, ["BAD", "ACME"], assets=directory("BAD", "ACME"))

    assert provider.root_calls == ["ACME"]
    assert set(rows(sessions)) == {("ACME", SESSION)}
    (error,) = result.errors
    assert error.ticker == "BAD"
    assert error.stage == "daily_bars"
    assert "OTHER" in error.error


def _symbols(count: int) -> list[str]:
    """``count`` distinct well-formed equity symbols: QAAA, QAAB, ..."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return [
        "Q" + letters[i // 676] + letters[(i // 26) % 26] + letters[i % 26]
        for i in range(count)
    ]


def test_the_batch_size_is_the_providers_ceiling():
    assert ADV_BATCH_SIZE == ADV_MAX_SYMBOLS == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(("count", "batches"), [(200, [200]), (201, [200, 1])])
async def test_bars_are_requested_in_batches_of_at_most_200(sessions, count, batches):
    tickers = _symbols(count)
    provider = FakeInputs()
    result = await refresh(
        sessions, provider, tickers, assets=directory(*tickers), max_tickers=count
    )

    assert [len(call) for call in provider.bar_calls] == batches
    assert [s for call in provider.bar_calls for s in call] == tickers
    assert len(rows(sessions)) == count
    assert result.deferred == ()


@pytest.mark.asyncio
async def test_a_run_is_bounded_and_the_rest_deferred_in_order(sessions):
    tickers = _symbols(MAX_TICKERS_PER_RUN + 3)
    provider = FakeInputs()
    result = await refresh(sessions, provider, tickers, assets=directory(*tickers))

    assert len(provider.root_calls) == MAX_TICKERS_PER_RUN
    assert len(rows(sessions)) == MAX_TICKERS_PER_RUN
    assert result.deferred == tuple(tickers[MAX_TICKERS_PER_RUN:])


@pytest.mark.asyncio
async def test_the_same_inputs_give_the_same_rows(tmp_path):
    # Deterministic: two fresh databases, identical inputs, identical rows.
    tickers = ["ZETA", "ACME", "NOPT", "THIN"]

    def bars(symbol: str, session_date: date) -> list[Bar]:
        volume = 5 if symbol == "THIN" else 2_000_000
        return history(25, before=session_date, symbol=symbol, volume=volume)

    snapshots = []
    for name in ("one", "two"):
        engine = create_db_engine(sqlite_url(tmp_path / f"{name}.db"))
        Base.metadata.create_all(engine)
        factory: Callable[[], Session] = lambda engine=engine: Session(engine)  # noqa: E731
        await refresh(
            factory, FakeInputs(bars=bars), tickers,
            assets=directory("ZETA", "ACME", "THIN", without_options=["NOPT"]),
        )
        snapshots.append(
            {
                key: (r.has_options, r.standard_root, r.avg_volume_20d, r.last_close,
                      r.sessions_available, r.passes, r.failures)
                for key, r in rows(factory).items()
            }
        )
        engine.dispose()
    assert snapshots[0] == snapshots[1]


# ------------------------------------------- recorded fixtures, real provider

#: The recording day of the p4 fixtures.
RECORDED_P4 = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)


def _recorded_bars_with_synthetic_aifu() -> str:
    """The recorded AAPL/SPY/XRX ADV bars, plus AAPL's rows re-keyed as AIFU.

    SYNTHETIC: AIFU's bars. Its recorded standard-root answer is the point of
    the test, and a real AIFU tape would fail on volume or close first -- so
    the root check would never be asked, which is itself correct but proves
    nothing about adjusted contracts.
    """
    recorded = json.loads(fixture_text("p4_stock_bars_adv"))
    body = recorded["body"]
    body["bars"]["AIFU"] = body["bars"]["AAPL"]
    return json.dumps(body)


@pytest.mark.asyncio
async def test_recorded_fixtures_adjusted_only_underlying_fails_and_aapl_passes(
    make_provider, sessions
):
    bars_body = _recorded_bars_with_synthetic_aifu()
    roots = {
        "AAPL": "p4_contracts_root_aapl",
        "AIFU": "p4_contracts_root_adjusted_only",
        "XRX": "p4_contracts_root_xrx",
    }

    def route(request: httpx.Request) -> str | tuple[int, str]:
        if request.url.path == "/v2/stocks/bars":
            return (200, bars_body)
        if request.url.path == "/v2/options/contracts":
            return roots[request.url.params["underlying_symbols"]]
        raise AssertionError(f"unexpected request {request.url}")

    provider, transport = make_provider(route, now=RECORDED_P4)
    result = await refresh(
        sessions, provider, ["AAPL", "AIFU", "XRX"],
        assets=directory("AAPL", "AIFU", "XRX"),
    )

    assert result.errors == ()
    found = rows(sessions)
    assert found[("AAPL", SESSION)].passes is True
    assert found[("AAPL", SESSION)].standard_root is True
    aifu = found[("AIFU", SESSION)]
    assert aifu.has_options is True
    assert aifu.standard_root is False
    assert aifu.failures == "no_standard_contract"
    # XRX closed at $3.47 on 2026-09-23: it fails on its bars, so no root
    # request was spent on it.
    xrx = found[("XRX", SESSION)]
    assert xrx.failures == "low_close"
    assert xrx.last_close == Decimal("3.47")
    contract_requests = [
        r.url.params["underlying_symbols"]
        for r in transport.requests
        if r.url.path == "/v2/options/contracts"
    ]
    assert sorted(contract_requests) == ["AAPL", "AIFU"]
    # One bars request for all three, on the historical feed.
    (bars_request,) = [r for r in transport.requests if r.url.path == "/v2/stocks/bars"]
    assert bars_request.url.params["feed"] == "sip"
