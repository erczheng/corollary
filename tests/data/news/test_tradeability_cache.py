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
import logging
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
    MAX_IPO_LOOKUPS_PER_RUN,
    MAX_TICKERS_PER_RUN,
    RefreshResult,
    TradeabilityFailure,
    TradeabilityResult,
    refresh_tradeability,
    store_tradeability,
    tickers_needing_check,
)
from corollary.data.news.watchlist import watch_universe
from corollary.data.providers.alpaca import ADV_MAX_SYMBOLS
from corollary.data.providers.finnhub import FinnhubProvider
from corollary.data.providers.fundamentals import FundamentalsError
from corollary.data.providers.interface import (
    AssetDirectory,
    Bar,
    EquityAsset,
    ProviderError,
)
from corollary.db.models import (
    Base,
    TickerIpoDate,
    TickerTradeability,
)
from corollary.ratelimit import HostRateLimiter
from corollary.db.session import create_db_engine, sqlite_url
from tests.data.news.test_tradeability import history, sessions_before
from tests.data.providers.conftest import fixture_text, limiter, make_provider  # noqa: F401
from tests.data.providers.test_finnhub_provider import (
    TEST_CREDENTIALS,
    RecordingTransport,
    _never_sleep,
    by_symbol,
)

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
async def test_a_recent_listing_is_cached_with_its_partial_average_and_divisor(sessions):
    """Q10/Q12: listed three sessions ago -- the IPO date says so -- and the row
    says "over 3 sessions"."""

    def bars(symbol: str, session_date: date) -> list[Bar]:
        return history(
            3, before=session_date, symbol=symbol, volumes=[4_000_000, 2_000_000, 1_500_001]
        )

    provider = FakeInputs(bars=bars)
    ipos = FakeIpoDates({"NEWCO": sessions_before(SESSION, 3)[0]})
    result = await refresh(
        sessions, provider, ["NEWCO"], assets=directory("NEWCO"), ipo_dates=ipos
    )

    assert result.passed == ("NEWCO",)
    assert provider.root_calls == ["NEWCO"]
    row = rows(sessions)[("NEWCO", SESSION)]
    assert row.sessions_available == 3
    assert row.avg_volume_20d == 2_500_000  # 7,500,001 // 3
    assert row.passes is True


@pytest.mark.asyncio
async def test_no_completed_session_is_cached_under_its_own_name(sessions):
    provider = FakeInputs(bars=lambda symbol, session_date: [])
    await refresh(sessions, provider, ["NEWCO"], assets=directory("NEWCO"))

    row = rows(sessions)[("NEWCO", SESSION)]
    assert row.failures == "no_completed_session"
    assert row.sessions_available == 0
    assert row.avg_volume_20d is None
    assert row.last_close is None
    assert provider.root_calls == []


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



# --------------------------------- Q12: the real IPO date settles a partial window


class FakeIpoDates:
    """``IpoDateSource``: answers from a table, recorded, failing on request.

    ``answers[ticker]`` is a date, or ``None`` for a profile with no usable
    ``ipo``. A ticker in ``errors`` raises what Finnhub's provider raises
    when it could not be asked at all.
    """

    def __init__(
        self,
        answers: dict[str, date | None] | None = None,
        *,
        errors: Sequence[str] = (),
        provider_errors: Sequence[str] = (),
    ) -> None:
        self._answers = answers or {}
        self._errors = set(errors)
        self._provider_errors = set(provider_errors)
        self.calls: list[str] = []

    async def ipo_date(self, symbol: str) -> date | None:
        self.calls.append(symbol)
        if symbol in self._errors:
            raise FundamentalsError("GET /stock/profile2 returned 403: no access")
        if symbol in self._provider_errors:
            raise ProviderError("GET /stock/profile2 failed: timed out")
        return self._answers.get(symbol)


def listed_sessions_ago(count: int, session_date: date = SESSION) -> date:
    """The session ``count`` sessions before ``session_date`` -- a first bar's date."""
    return sessions_before(session_date, count)[0]


def partial(count: int, *, volume: int = 2_000_000) -> Callable[[str, date], list[Bar]]:
    """Bars on only the last ``count`` sessions: a partial window, no lookback bar."""

    def bars(symbol: str, session_date: date) -> list[Bar]:
        return history(count, before=session_date, symbol=symbol, volume=volume)

    return bars


def ipo_rows(sessions: Callable[[], Session]) -> dict[str, TickerIpoDate]:
    with sessions() as session:
        found = session.scalars(select(TickerIpoDate)).all()
        session.expunge_all()
    return {row.ticker: row for row in found}


@pytest.mark.asyncio
async def test_a_recent_ipo_passes_with_one_session_and_the_date_is_cached(sessions):
    """Owner test 1: listed yesterday, and the IPO date says so."""
    ipo = listed_sessions_ago(1)
    provider = FakeInputs(bars=partial(1, volume=1_500_000))
    ipos = FakeIpoDates({"NEWCO": ipo})
    result = await refresh(sessions, provider, ["NEWCO"], assets=directory("NEWCO"), ipo_dates=ipos)

    assert result.passed == ("NEWCO",)
    assert ipos.calls == ["NEWCO"]
    assert result.ipo_requests == 1
    assert provider.root_calls == ["NEWCO"]
    row = rows(sessions)[("NEWCO", SESSION)]
    assert (row.sessions_available, row.avg_volume_20d, row.passes) == (1, 1_500_000, True)
    stored = ipo_rows(sessions)["NEWCO"]
    assert stored.ipo_date == ipo
    assert stored.fetched_at == NOW


@pytest.mark.asyncio
async def test_an_old_ipo_with_a_partial_window_fails_as_missing_bars(sessions):
    """Owner test 2: listed in 1999, bars on five sessions -- zeros for the rest."""
    provider = FakeInputs(bars=partial(5))
    ipos = FakeIpoDates({"OLDCO": date(1999, 3, 10)})
    result = await refresh(sessions, provider, ["OLDCO"], assets=directory("OLDCO"), ipo_dates=ipos)

    assert result.passed == ()
    row = rows(sessions)[("OLDCO", SESSION)]
    assert row.failures == "low_volume"
    assert row.sessions_available == 20
    assert row.avg_volume_20d == 500_000
    assert row.standard_root is None
    assert provider.root_calls == []  # the verdict is settled; no root request
    # The date itself is a real answer, and it does not change: kept.
    assert ipo_rows(sessions)["OLDCO"].ipo_date == date(1999, 3, 10)


@pytest.mark.asyncio
async def test_a_missing_or_malformed_ipo_fails_closed_for_the_session_only(sessions, caplog):
    """Owner test 3: no usable date -- ``ipo_date_unavailable``, re-asked next session."""
    provider = FakeInputs(bars=partial(5))
    ipos = FakeIpoDates({"NEWCO": None})
    with caplog.at_level(logging.WARNING, logger="corollary.data.news.tradeability"):
        await refresh(sessions, provider, ["NEWCO"], assets=directory("NEWCO"), ipo_dates=ipos)

    row = rows(sessions)[("NEWCO", SESSION)]
    assert row.failures == "ipo_date_unavailable"
    assert row.passes is False
    assert row.avg_volume_20d is None
    assert row.sessions_available == 0
    assert row.standard_root is None
    assert provider.root_calls == []
    assert ipo_rows(sessions) == {}  # never stored as a permanent answer
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "tradeability_ipo_date_unavailable"]
    assert record.ticker == "NEWCO"
    assert "never assumed" in record.rule

    # Cached for this session like any failure, so not asked again today...
    with sessions() as session:
        assert tickers_needing_check(
            session, candidates=["NEWCO"], watch=UNIVERSE, session_date=SESSION
        ) == []
    # ...and asked again next session, where a real answer can still arrive.
    ipos_next = FakeIpoDates({"NEWCO": listed_sessions_ago(6, NEXT_SESSION)})
    result = await refresh(
        sessions, provider, ["NEWCO"], assets=directory("NEWCO"),
        session_date=NEXT_SESSION, ipo_dates=ipos_next,
    )
    assert ipos_next.calls == ["NEWCO"]
    assert result.passed == ("NEWCO",)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["errors", "provider_errors"])
async def test_a_finnhub_failure_fails_closed_and_caches_nothing(sessions, failure):
    """Owner test 4: a 403 or a timeout is "could not ask" -- unchecked, retried."""
    provider = FakeInputs(bars=partial(5))
    ipos = FakeIpoDates(**{failure: ["NEWCO"]})
    result = await refresh(sessions, provider, ["NEWCO"], assets=directory("NEWCO"), ipo_dates=ipos)

    assert result.passed == ()
    assert rows(sessions) == {}
    assert ipo_rows(sessions) == {}
    (error,) = result.errors
    assert (error.ticker, error.stage) == ("NEWCO", "ipo_date")
    assert provider.root_calls == []

    # Unchecked, so the same session asks again -- and a later answer counts.
    with sessions() as session:
        again = tickers_needing_check(
            session, candidates=["NEWCO"], watch=UNIVERSE, session_date=SESSION
        )
    assert again == ["NEWCO"]
    recovered = FakeIpoDates({"NEWCO": listed_sessions_ago(5)})
    second = await refresh(sessions, provider, again, assets=directory("NEWCO"), ipo_dates=recovered)
    assert second.passed == ("NEWCO",)
    assert recovered.calls == ["NEWCO"]


@pytest.mark.asyncio
async def test_the_cache_prevents_a_second_call(sessions):
    """Owner test 5: a partial-window ticker costs one Finnhub call, ever."""
    provider = FakeInputs(bars=partial(5))
    ipos = FakeIpoDates({"NEWCO": listed_sessions_ago(5)})
    await refresh(sessions, provider, ["NEWCO"], assets=directory("NEWCO"), ipo_dates=ipos)
    assert ipos.calls == ["NEWCO"]

    later = await refresh(
        sessions, provider, ["NEWCO"], assets=directory("NEWCO"),
        session_date=NEXT_SESSION, ipo_dates=ipos,
    )
    assert ipos.calls == ["NEWCO"]  # still one
    assert later.ipo_requests == 0
    assert later.passed == ("NEWCO",)
    assert rows(sessions)[("NEWCO", NEXT_SESSION)].sessions_available == 6


@pytest.mark.asyncio
async def test_a_date_already_on_file_is_used_without_asking(sessions):
    with sessions() as session:
        session.add(TickerIpoDate(ticker="OLDCO", ipo_date=date(2004, 8, 19), fetched_at=NOW))
        session.commit()
    provider = FakeInputs(bars=partial(5))
    ipos = FakeIpoDates()
    await refresh(sessions, provider, ["OLDCO"], assets=directory("OLDCO"), ipo_dates=ipos)

    assert ipos.calls == []
    assert rows(sessions)[("OLDCO", SESSION)].failures == "low_volume"


@pytest.mark.asyncio
async def test_an_ipo_date_the_tape_contradicts_fails_closed_and_is_not_stored(sessions):
    """SYNTHETIC: the vendor's date is after the first completed bar. The
    ticker fails closed, and the date is not cached for good -- a later,
    corrected answer must still be asked for next session."""
    provider = FakeInputs(bars=partial(5, volume=1_500_000))
    ipos = FakeIpoDates({"NEWCO": listed_sessions_ago(2)})
    await refresh(sessions, provider, ["NEWCO"], assets=directory("NEWCO"), ipo_dates=ipos)

    row = rows(sessions)[("NEWCO", SESSION)]
    assert row.failures == "ipo_date_unavailable"
    assert row.passes is False
    assert ipo_rows(sessions) == {}


@pytest.mark.asyncio
async def test_no_ipo_source_fails_every_partial_window_closed(sessions):
    """``ipo_dates=None`` (the default): nobody to ask, so never assume an IPO."""
    provider = FakeInputs(bars=partial(5))
    result = await refresh(sessions, provider, ["NEWCO"], assets=directory("NEWCO"))

    assert result.passed == ()
    assert result.ipo_requests == 0
    assert rows(sessions)[("NEWCO", SESSION)].failures == "ipo_date_unavailable"
    assert ipo_rows(sessions) == {}


@pytest.mark.asyncio
async def test_the_ipo_date_is_asked_only_when_it_could_change_the_verdict(sessions):
    """Established names, and partial windows failing on anything else, cost nothing."""

    def bars(symbol: str, session_date: date) -> list[Bar]:
        if symbol == "OLDCO":  # established: 25 sessions, a bar before the window
            return history(25, before=session_date, symbol=symbol)
        if symbol == "CHEAP":
            return history(5, before=session_date, symbol=symbol, last_close="3")
        if symbol == "STALE":
            return history(5, before=session_date, symbol=symbol)[:-1]
        if symbol == "THIN":  # fails even over its own five sessions
            return history(5, before=session_date, symbol=symbol, volume=999_999)
        return history(5, before=session_date, symbol=symbol)

    provider = FakeInputs(bars=bars)
    ipos = FakeIpoDates()
    tickers = ["OLDCO", "CHEAP", "STALE", "THIN", "NOOPT"]
    await refresh(
        sessions, provider, tickers,
        assets=directory("OLDCO", "CHEAP", "STALE", "THIN", without_options=["NOOPT"]),
        ipo_dates=ipos,
    )

    assert ipos.calls == []
    cached = rows(sessions)
    assert cached[("OLDCO", SESSION)].passes is True
    assert cached[("CHEAP", SESSION)].failures == "low_close"
    assert cached[("STALE", SESSION)].failures == "stale_bars"
    assert cached[("THIN", SESSION)].failures == "low_volume"
    assert cached[("NOOPT", SESSION)].failures == "no_options"
    # Judged on the kindest reading (listed at its first bar) and failed
    # anyway: the recorded divisor is the one that average was taken over.
    assert cached[("THIN", SESSION)].sessions_available == 5


@pytest.mark.asyncio
async def test_ipo_lookups_are_bounded_per_run_and_the_rest_deferred(sessions):
    tickers = ["AAAA", "BBBB", "CCCC", "DDDD"]
    provider = FakeInputs(bars=partial(5))
    ipos = FakeIpoDates({t: listed_sessions_ago(5) for t in tickers})
    result = await refresh(
        sessions, provider, tickers, assets=directory(*tickers),
        ipo_dates=ipos, max_ipo_lookups=2,
    )

    assert ipos.calls == ["AAAA", "BBBB"]
    assert result.ipo_requests == 2
    assert result.passed == ("AAAA", "BBBB")
    assert result.deferred == ("CCCC", "DDDD")
    assert set(rows(sessions)) == {("AAAA", SESSION), ("BBBB", SESSION)}


def test_the_ipo_lookup_bound_is_twenty():
    assert MAX_IPO_LOOKUPS_PER_RUN == 20


@pytest.mark.asyncio
async def test_a_bad_ipo_lookup_bound_is_refused(sessions):
    with pytest.raises(ValueError):
        await refresh(sessions, FakeInputs(), ["ACME"], assets=directory("ACME"), max_ipo_lookups=-1)


@pytest.mark.asyncio
async def test_the_real_finnhub_provider_answers_the_ipo_question(sessions):
    """The recorded AAPL profile (``ipo`` 1980-12-12) through the real provider.

    SYNTHETIC: the bars -- five sessions of AAPL, a partial window no real
    AAPL tape has -- so the IPO question is asked at all. The recorded date
    is 45 years before the window, so the gap is missing bars and it fails.
    """
    transport = RecordingTransport(by_symbol({"AAPL": "profile2_aapl"}))
    finnhub = FinnhubProvider(
        credentials=TEST_CREDENTIALS,
        client=httpx.AsyncClient(transport=transport),
        limiter=HostRateLimiter(clock=lambda: 0.0, sleep=_never_sleep),
    )
    provider = FakeInputs(bars=partial(5))
    result = await refresh(sessions, provider, ["AAPL"], assets=directory("AAPL"), ipo_dates=finnhub)

    assert transport.symbols_requested() == ["AAPL"]
    assert result.passed == ()
    assert rows(sessions)[("AAPL", SESSION)].failures == "low_volume"
    assert ipo_rows(sessions)["AAPL"].ipo_date == date(1980, 12, 12)


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
