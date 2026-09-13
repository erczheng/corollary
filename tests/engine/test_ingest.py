"""Ingestion: the service that turns broker activity into rows.

``ledger.py`` and ``grouping.py`` are pure and complete; nothing wrote a
single row until this module's subject existed. What is under test here is
therefore not arithmetic -- ``test_ledger.py`` and ``test_ledger_recording.py``
own that -- but the four things a *service* can get wrong that a pure function
cannot:

* **Idempotency.** Ingestion runs on startup and on an interval, re-pulling an
  overlapping window every time. The second run must leave the tables exactly
  as the first did, and must not raise. ``fill(activity_id UNIQUE)`` is the
  whole mechanism, and a test that only ran ingestion once would never touch
  it.
* **The two-hop join.** A fill carries its *leg's* order id and the parent
  appears nowhere on it, so ``position_intent`` reaches the row only through
  ``?nested=true`` and a leg-id index. A one-hop join resolves the seven
  single-leg fills, silently leaves the mleg leg fills unintended, and nothing
  anywhere says so. Both halves are asserted.
* **Contract terms.** The matcher takes ``multiplier`` per contract and
  refuses to book a P&L it cannot state correctly, so a symbol whose terms
  were never fetched produces **no realized trade at all**. That is the same
  "terminal that believes you never win" failure as a bad ``net_amount``,
  arriving by refusal rather than by bad arithmetic. ``/v2/positions`` returns
  no multiplier field at all, so the contracts endpoint is the only source and
  100 is never substituted -- which the 50-multiplier test below proves by
  moving the money.
* **What must not become a fill.** 19 ``FEE`` rows and one ``JNLC`` carry no
  symbol and no ``order_id``. They are not lot movements, they must not be
  written, and they must not vanish either.

No live call, no network, no MCP. The activities and orders are the **real
recorded bytes** from ``tests/fixtures/alpaca/``, parsed by the real
``AlpacaBroker`` through an ``httpx.MockTransport`` and then handed to a
hand-written :class:`FakeBroker` -- so the shapes are Alpaca's and the control
surface (the ``since_id`` cursor, the call counts) is the test's.
"""

import dataclasses
import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.calendars import NYSE_TZ
from corollary.data.providers.alpaca import AlpacaCredentials
from corollary.data.providers.interface import (
    Bar,
    BarTimeframe,
    ContractStatus,
    MarketDataProvider,
    OptionContract,
    OptionSnapshot,
    ProviderError,
    Quote,
    StockSnapshot,
)
from corollary.db.models import Base, Fill, MlegGroup, MlegLeg, RealizedTrade
from corollary.db.session import create_db_engine, sqlite_url
from corollary.engine.execution.alpaca import AlpacaBroker
from corollary.engine.execution.interface import (
    Account,
    Activity,
    ActivityCategory,
    BrokerAccount,
    BrokerPosition,
    NonTradeActivity,
    Order,
    OrderClass,
    OrderQueryStatus,
    OrderSide,
    PortfolioHistory,
    PositionIntent,
    TradeActivity,
)
from corollary.engine.ingest import IngestResult, IngestRule, IngestService
from corollary.instruments import OptionType, parse_occ_symbol
from corollary.ratelimit import ALPACA_PAPER_TRADING_HOST, HostRateLimiter
from corollary.wire import REDACTED

from .ledger_support import at, contract, fee, fill, option_event

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alpaca"

PAPER = "paper"

#: Obviously fake, and pointed at the paper host. Rule 6: no key material in
#: tests, and rule 5: paper is the default everywhere.
CREDENTIALS = AlpacaCredentials(
    key_id="PKTESTTESTTESTTEST",
    secret_key="not-a-real-secret",
    trading_base_url=f"https://{ALPACA_PAPER_TRADING_HOST}",
    is_paper=True,
)

IWM_PUT = "IWM261218P00280000"

#: Long, out of the money at the close, so it expires worthless rather than
#: being auto-exercised. ``OPEXP`` closes at zero and needs no settlement
#: price, which leaves the contract's multiplier as the only thing standing
#: between this history and a booked realized loss.
NVDA_OTM_CALL = "NVDA260911C00240000"


# --------------------------------------------------------------------------
# The recording, parsed by the real broker
# --------------------------------------------------------------------------


def body_bytes(name: str) -> bytes:
    """A fixture's ``body``, sliced out of the file rather than re-serialised.

    The broker parses these bytes itself, so nothing on the money path is ever
    round-tripped through a Python float on the way in.
    """
    text = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    start = text.index('"body":') + len('"body":')
    while text[start].isspace():
        start += 1
    _, end = json.JSONDecoder().raw_decode(text, start)
    return text[start:end].encode("utf-8")


async def _never_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError(f"a replay test waited {seconds}s on the rate limiter")


def _route(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if "/activities" in path:
        name = "activities_mixed"
    elif "/orders" in path:
        name = "orders_nested"
    else:  # pragma: no cover - an unrouted request is a test bug
        raise AssertionError(f"no fixture routed for {request.method} {request.url}")
    return httpx.Response(
        status_code=200,
        content=body_bytes(name),
        headers={"content-type": "application/json"},
        request=request,
    )


async def recorded() -> tuple[list[Activity], list[Order]]:
    """The account's real activity and order history, as Alpaca sent it."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(_route))
    limiter = HostRateLimiter(
        requests_per_minute=10_000, clock=lambda: 0.0, sleep=_never_sleep
    )
    broker = AlpacaBroker(credentials=CREDENTIALS, client=client, limiter=limiter)
    try:
        return await broker.activities(), await broker.orders()
    finally:
        await client.aclose()


def option_symbols(activities: Sequence[Activity]) -> list[str]:
    """Every OCC contract symbol the activities mention, sorted."""
    found: set[str] = set()
    for row in activities:
        symbol = row.symbol
        if not symbol:
            continue
        try:
            parse_occ_symbol(symbol)
        except ValueError:
            continue
        found.add(symbol)
    return sorted(found)


# --------------------------------------------------------------------------
# The fakes
# --------------------------------------------------------------------------


class FakeBroker(BrokerAccount):
    """A ``BrokerAccount`` over a fixed history, with an observable cursor.

    ``honour_since_id`` is the whole point of the class. Set, the fake behaves
    as the vendor does and returns only what follows the cursor; unset, it
    returns the entire history every time -- which is the overlapping re-pull
    ingestion is explicitly allowed to make, and the case the upsert has to
    absorb.
    """

    def __init__(
        self,
        activities: Sequence[Activity],
        orders: Sequence[Order] = (),
        *,
        honour_since_id: bool = True,
    ) -> None:
        self._activities = list(activities)
        self._orders = list(orders)
        self.honour_since_id = honour_since_id
        self.activity_calls: list[str | None] = []
        self.order_calls: list[OrderQueryStatus] = []

    async def activities(
        self,
        *,
        types: Sequence[str] | None = None,
        category: ActivityCategory | None = None,
        after: datetime | None = None,
        until: datetime | None = None,
        since_id: str | None = None,
        page_size: int | None = None,
    ) -> list[Activity]:
        self.activity_calls.append(since_id)
        if since_id is None or not self.honour_since_id:
            return list(self._activities)
        ids = [row.id for row in self._activities]
        if since_id not in ids:
            return list(self._activities)
        return self._activities[ids.index(since_id) + 1 :]

    async def orders(
        self,
        *,
        status: OrderQueryStatus = OrderQueryStatus.ALL,
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[Order]:
        self.order_calls.append(status)
        return list(self._orders)

    async def account(self) -> Account:  # pragma: no cover - not on this path
        raise AssertionError("ingestion does not read the account object")

    async def positions(self) -> list[BrokerPosition]:  # pragma: no cover
        raise AssertionError("ingestion does not read /v2/positions")

    async def portfolio_history(
        self, *, period: str = "1M", timeframe: str = "1D"
    ) -> PortfolioHistory:  # pragma: no cover
        raise AssertionError("ingestion does not read the equity curve")


class FakeProvider(MarketDataProvider):
    """Contract terms and daily bars for what it was told about, and no more.

    A symbol it does not hold comes back absent rather than as a guess, which
    is the case requirement 4 exists for. ``fail_for`` makes the *fetch* fail
    instead, which is a different rule with the same consequence.

    ``retired`` is only served when ``status=ContractStatus.INACTIVE`` is
    asked for, which is how an **expired** contract behaves: it leaves the
    active list at exactly the moment the matcher needs its multiplier to book
    the expiry.

    ``bars`` maps ``(symbol, session date)`` to a closing price. Daily bars are
    stamped at the session's **opening** instant, which is midnight Eastern --
    04:00Z under EDT -- so a consumer that read the UTC date instead of the
    Eastern one would still pass here, and a consumer that read a *local* date
    would not. That is the trap the stamp is chosen to expose.
    """

    def __init__(
        self,
        contracts: Mapping[str, OptionContract] | None = None,
        *,
        retired: Mapping[str, OptionContract] | None = None,
        bars: Mapping[tuple[str, date], str] | None = None,
        fail_for: Sequence[str] = (),
        bars_fail_for: Sequence[str] = (),
    ) -> None:
        self._contracts = dict(contracts or {})
        self._retired = dict(retired or {})
        self._bars = {key: Decimal(value) for key, value in (bars or {}).items()}
        self._fail_for = set(fail_for)
        self._bars_fail_for = set(bars_fail_for)
        self.calls: list[tuple[str, date | None, date | None, bool]] = []
        self.statuses: list[ContractStatus] = []
        self.bar_calls: list[
            tuple[tuple[str, ...], BarTimeframe, datetime | None, datetime | None]
        ] = []

    async def option_contracts(
        self,
        underlying: str,
        *,
        expiration_lte: date | None = None,
        expiration_gte: date | None = None,
        strike_gte: Decimal | None = None,
        strike_lte: Decimal | None = None,
        option_type: OptionType | None = None,
        include_adjusted: bool = False,
        show_deliverables: bool = False,
        status: ContractStatus = ContractStatus.ACTIVE,
    ) -> list[OptionContract]:
        self.calls.append(
            (underlying, expiration_gte, expiration_lte, include_adjusted)
        )
        self.statuses.append(status)
        if underlying in self._fail_for:
            raise ProviderError(f"contracts endpoint unavailable for {underlying}")
        listed = (
            self._contracts if status is ContractStatus.ACTIVE else self._retired
        )
        found: list[OptionContract] = []
        for terms in listed.values():
            if terms.underlying_symbol != underlying:
                continue
            if expiration_gte is not None and terms.expiration < expiration_gte:
                continue
            if expiration_lte is not None and terms.expiration > expiration_lte:
                continue
            if terms.is_adjusted and not include_adjusted:
                continue
            found.append(terms)
        return found

    async def latest_stock_quotes(
        self, symbols: Sequence[str]
    ) -> dict[str, Quote]:  # pragma: no cover
        raise AssertionError("ingestion quotes nothing")

    async def stock_snapshots(
        self, symbols: Sequence[str]
    ) -> dict[str, StockSnapshot]:  # pragma: no cover
        raise AssertionError("ingestion quotes nothing")

    async def stock_bars(
        self,
        symbols: Sequence[str],
        *,
        timeframe: BarTimeframe = BarTimeframe.DAY,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, list[Bar]]:
        self.bar_calls.append((tuple(symbols), timeframe, start, end))
        for symbol in symbols:
            if symbol in self._bars_fail_for:
                raise ProviderError(f"bars unavailable for {symbol}")
        series: dict[str, list[Bar]] = {}
        for (symbol, session), close in sorted(self._bars.items()):
            if symbol not in symbols:
                continue
            # Midnight Eastern, which is where Alpaca stamps a daily bar.
            at_utc = datetime.combine(session, time(0), tzinfo=NYSE_TZ).astimezone(
                timezone.utc
            )
            if start is not None and at_utc < start:
                continue
            if end is not None and at_utc > end:
                continue
            series.setdefault(symbol, []).append(
                Bar(
                    symbol=symbol,
                    at=at_utc,
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1_000_000,
                    trade_count=1_000,
                    vwap=close,
                )
            )
        return series

    async def latest_option_quotes(
        self, symbols: Sequence[str]
    ) -> dict[str, Quote]:  # pragma: no cover
        raise AssertionError("ingestion quotes nothing")

    async def option_bars(
        self,
        symbols: Sequence[str],
        *,
        timeframe: BarTimeframe = BarTimeframe.DAY,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, list[Bar]]:  # pragma: no cover
        raise AssertionError("ingestion reads no bars in this step")

    async def option_chain(
        self,
        underlying: str,
        *,
        expiration_lte: date | None = None,
        expiration_gte: date | None = None,
        strike_gte: Decimal | None = None,
        strike_lte: Decimal | None = None,
        option_type: OptionType | None = None,
    ) -> dict[str, OptionSnapshot]:  # pragma: no cover
        raise AssertionError("ingestion reads no chain")


def standard_terms(
    symbols: Sequence[str], *, multiplier: str = "100"
) -> dict[str, OptionContract]:
    """Terms for each symbol: standard root, and the given multiplier."""
    return {symbol: contract(symbol, multiplier=multiplier) for symbol in symbols}


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """A file-backed SQLite database, because WAL is a no-op on ``:memory:``."""
    eng = create_db_engine(sqlite_url(tmp_path / "corollary.db"))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def fill_rows(engine: Engine) -> list[Fill]:
    with Session(engine) as session:
        return list(session.scalars(select(Fill)))


def trade_rows(engine: Engine) -> list[RealizedTrade]:
    with Session(engine) as session:
        return list(session.scalars(select(RealizedTrade)))


def table_snapshot(engine: Engine) -> dict[str, list[tuple[object, ...]]]:
    """Every ledger row, ids included, as comparable tuples.

    Ids are in deliberately: "identical table contents" has to mean the rows
    were left alone, not merely that a delete-and-reinsert happened to produce
    the same values under new keys.
    """
    with Session(engine) as session:
        fills = [
            (
                row.id,
                row.account,
                row.activity_id,
                row.order_id,
                row.group_id,
                row.symbol,
                row.side,
                row.position_intent,
                row.qty,
                row.price,
                row.at,
            )
            for row in session.scalars(select(Fill))
        ]
        trades = [
            (
                row.id,
                row.account,
                row.symbol,
                row.opened_at,
                row.closed_at,
                row.qty,
                row.open_price,
                row.close_price,
                row.pnl,
                row.pnl_pct,
                row.close_kind,
            )
            for row in session.scalars(select(RealizedTrade))
        ]
        groups = [
            (row.id, row.account, row.order_id, row.opened_at, row.net_price)
            for row in session.scalars(select(MlegGroup))
        ]
        legs = [
            (row.group_id, row.symbol, row.ratio, row.side, row.position_intent)
            for row in session.scalars(select(MlegLeg))
        ]
    return {
        "fill": sorted(fills, key=str),
        "realized_trade": sorted(trades, key=str),
        "mleg_group": sorted(groups, key=str),
        "mleg_leg": sorted(legs, key=str),
    }


async def ingest_the_recording(
    engine: Engine,
    *,
    multiplier: str = "100",
    honour_since_id: bool = True,
) -> tuple[IngestService, FakeBroker, FakeProvider, IngestResult]:
    activities, orders = await recorded()
    broker = FakeBroker(activities, orders, honour_since_id=honour_since_id)
    provider = FakeProvider(
        standard_terms(option_symbols(activities), multiplier=multiplier)
    )
    service = IngestService(
        broker=broker, provider=provider, engine=engine, account=PAPER
    )
    return service, broker, provider, await service.run()


# --------------------------------------------------------------------------
# The first run writes the rows
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_recording_becomes_fifteen_fills_one_trade_and_four_groups(
    engine: Engine,
) -> None:
    """Every count here is a fact about the recording, not a round number.

    35 activities: 15 ``FILL`` rows, 19 ``FEE`` rows and one ``JNLC``. One
    realized round trip, and four ``mleg`` verticals contributing two legs
    each.
    """
    _, _, _, result = await ingest_the_recording(engine)

    assert result.activities_pulled == 35
    assert result.fills_written == 15
    assert result.trades_written == 1
    assert result.mleg_groups_written == 4
    assert result.mleg_legs_written == 8

    assert len(fill_rows(engine)) == 15
    assert len(trade_rows(engine)) == 1


@pytest.mark.asyncio
async def test_the_one_real_round_trip_lands_in_the_table_at_minus_seven_dollars(
    engine: Engine,
) -> None:
    """IWM 280P, 8.21 in and 8.14 out, at a 100 multiplier read from the terms.

    The arithmetic ground truth for the whole ledger, asserted here on the
    **stored row** rather than on the matcher's output -- so the ``Money``
    columns, the CHECK constraints and the write path are all in the loop.
    """
    _, _, _, result = await ingest_the_recording(engine)

    (trade,) = trade_rows(engine)

    assert trade.account == PAPER
    assert trade.symbol == IWM_PUT
    assert trade.qty == 1
    assert trade.open_price == Decimal("8.21")
    assert trade.close_price == Decimal("8.14")
    assert trade.pnl == Decimal("-7.00")
    assert trade.pnl_pct == Decimal("-0.8526")
    assert trade.close_kind == "fill"
    assert result.rejections  # the JNLC, at minimum


@pytest.mark.asyncio
async def test_the_multiplier_comes_from_the_contract_terms_and_is_never_assumed(
    engine: Engine,
) -> None:
    """The same round trip at a 50 multiplier books -$3.50, not -$7.00.

    ``/v2/positions`` returns no multiplier field at all, so the contracts
    endpoint is the only source; a service that substituted 100 would report
    -$7.00 here and be wrong by the ratio on every adjusted contract.
    """
    await ingest_the_recording(engine, multiplier="50")

    (trade,) = trade_rows(engine)
    assert trade.pnl == Decimal("-3.50")


@pytest.mark.asyncio
async def test_terms_are_fetched_per_underlying_and_expiration_including_adjusted(
    engine: Engine,
) -> None:
    """One request per (underlying, expiration), and adjusted roots are asked for.

    Excluding adjusted contracts from *this* request would leave a held
    adjusted contract with no terms at all, which refuses its closing fills
    too -- where a premium difference times the multiplier is right whatever
    the deliverable. Asking for them lets the matcher refuse the *settlement*
    specifically, which is the narrower and correct refusal.
    """
    _, _, provider, _ = await ingest_the_recording(engine)

    assert provider.calls, "the contracts endpoint was never asked"
    assert all(include for _, _, _, include in provider.calls)
    # An exact expiration, not an open-ended range.
    assert all(gte is not None and gte == lte for _, gte, lte, _ in provider.calls)
    # One request per (underlying, expiration) pair, never one per symbol.
    pairs = {(underlying, gte) for underlying, gte, _, _ in provider.calls}
    assert len(provider.calls) == len(pairs)
    assert len(pairs) < len(option_symbols((await recorded())[0]))


@pytest.mark.asyncio
async def test_an_adjusted_roots_terms_are_found_under_the_underlying(
    engine: Engine,
) -> None:
    """``GME1`` is a modified root, not a ticker, and the endpoint files it
    under ``GME``.

    So the verbatim query returns nothing and a second one, with the numeric
    suffix stripped, finds it. The retry is safe in the only way that matters:
    the answer is matched back by **exact symbol** before anything is cached,
    so a wrong guess finds nothing rather than the wrong terms. Verbatim goes
    first so a ticker that really does end in a digit is never mangled.

    Without the retry this contract has no multiplier at all, which refuses
    its closing *fills* as well -- where a premium difference times the
    multiplier is right whatever the deliverable.
    """
    adjusted = "GME1261218C00030000"
    history = [
        fill(adjusted, "buy", 1, "2.00", when=at(14, 0), order_id="order-gme1"),
        fill(adjusted, "sell", 1, "2.50", when=at(15, 0), order_id="order-gme1-out"),
    ]
    orders = [
        an_order("order-gme1", adjusted, PositionIntent.BUY_TO_OPEN, "2.00"),
        an_order("order-gme1-out", adjusted, PositionIntent.SELL_TO_CLOSE, "2.50"),
    ]
    provider = FakeProvider({adjusted: contract(adjusted, underlying="GME")})
    service = IngestService(
        broker=FakeBroker(history, orders),
        provider=provider,
        engine=engine,
        account=PAPER,
    )
    result = await service.run()

    assert [underlying for underlying, _, _, _ in provider.calls] == ["GME1", "GME"]
    assert result.unfetched_terms == ()

    (trade,) = trade_rows(engine)
    assert trade.symbol == adjusted
    assert trade.pnl == Decimal("50.00")


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_ingestion_twice_is_a_no_op_the_second_time(
    engine: Engine,
) -> None:
    """Same rows, same ids, no exception -- and the cursor is the vendor's own.

    Ingestion runs on startup and on an interval, so the second run is the
    normal case rather than an edge one. The cursor handed back is the id of
    the **last activity the vendor returned**, never a ``MAX`` over a sorted
    column: neither the composite id nor ``transaction_time`` orders reliably.
    """
    service, broker, _, first = await ingest_the_recording(engine)
    before = table_snapshot(engine)

    second = await service.run()

    assert table_snapshot(engine) == before
    assert second.fills_written == 0
    assert second.trades_written == 0
    assert second.trades_removed == 0
    assert second.mleg_groups_written == 0

    activities, _ = await recorded()
    assert broker.activity_calls == [None, activities[-1].id]
    assert first.cursor == second.cursor == activities[-1].id


@pytest.mark.asyncio
async def test_a_full_repull_of_an_overlapping_window_changes_nothing(
    engine: Engine,
) -> None:
    """The upsert absorbs a re-pull, which is what makes overlap cheap.

    ``fill(activity_id UNIQUE)`` is the entire idempotency mechanism; a broker
    that ignores the cursor and hands back the whole history is the worst case
    it has to survive, and it is also what a cold start does deliberately.
    """
    service, broker, _, _ = await ingest_the_recording(engine, honour_since_id=False)
    before = table_snapshot(engine)

    second = await service.run()

    assert broker.activity_calls[1] is not None
    assert second.activities_pulled == 35
    assert second.fills_written == 0
    assert table_snapshot(engine) == before


@pytest.mark.asyncio
async def test_a_second_service_over_the_same_database_writes_nothing_new(
    engine: Engine,
) -> None:
    """A restart re-reads everything and still adds no row.

    The cursor and the terms cache are both in process, so the second service
    starts cold -- which is the restart path rather than the interval one, and
    the tables have to come out the same either way.
    """
    await ingest_the_recording(engine)
    before = table_snapshot(engine)

    _, _, _, again = await ingest_the_recording(engine)

    assert again.fills_written == 0
    assert again.trades_written == 0
    assert table_snapshot(engine) == before


@pytest.mark.asyncio
async def test_the_terms_cache_does_not_refetch_a_symbol_it_already_holds(
    engine: Engine,
) -> None:
    """Cached per symbol, so the second run asks the contracts endpoint nothing.

    It also matters after expiry: ``/v2/options/contracts`` is requested with
    ``status=active``, so an expired contract's terms may no longer be
    fetchable at all and the cache is what carries them across the event.
    """
    service, _, provider, _ = await ingest_the_recording(engine)
    first_pass = len(provider.calls)
    assert first_pass > 0

    await service.run()

    assert len(provider.calls) == first_pass


# --------------------------------------------------------------------------
# Requirement 4: terms that could not be fetched
# --------------------------------------------------------------------------


def expiry_history() -> list[Activity]:
    """One long call, opened and then expiring worthless out of the money.

    ``OPEXP`` is the out-of-the-money case only -- Alpaca auto-exercises ITM
    contracts absent a do-not-exercise instruction -- and it closes at
    **zero**, which needs no settlement price. So the contract's multiplier is
    the only thing standing between this history and a booked realized loss,
    which is exactly the variable under test.
    """
    return [
        fill(
            NVDA_OTM_CALL,
            "buy",
            1,
            "1.20",
            when=at(14, 0),
            order_id="order-nvda-otm",
        ),
        option_event("OPEXP", NVDA_OTM_CALL, -1, when=at(21, 0, day=11)),
    ]


def an_order(
    order_id: str, symbol: str, intent: PositionIntent, price: str
) -> Order:
    """A filled single-leg order, so the fill-to-order join has something to
    reach.

    ``buy`` is both buy-to-open and buy-to-close, so without this the matcher
    refuses the opening fill rather than guessing -- which is correct, and not
    what these tests are about.
    """
    return Order(
        id=order_id,
        symbol=symbol,
        asset_class="us_option",
        order_class=OrderClass.SIMPLE,
        side=OrderSide.BUY
        if intent.value.startswith("buy")
        else OrderSide.SELL,
        position_intent=intent,
        order_type="market",
        time_in_force="day",
        status="filled",
        quantity=Decimal(1),
        filled_quantity=Decimal(1),
        filled_avg_price=Decimal(price),
        limit_price=None,
        stop_price=None,
        ratio_qty=None,
        created_at=at(14, 0),
        submitted_at=at(14, 0),
        filled_at=at(14, 0),
        canceled_at=None,
        expired_at=None,
        updated_at=at(14, 0),
        extended_hours=False,
    )


def expiry_orders() -> list[Order]:
    """The order the opening buy joins to. ``buy`` is both BTO and BTC."""
    return [
        an_order(
            "order-nvda-otm", NVDA_OTM_CALL, PositionIntent.BUY_TO_OPEN, "1.20"
        )
    ]


@pytest.mark.asyncio
async def test_an_expiry_with_terms_books_the_full_loss(engine: Engine) -> None:
    """The control. Without this the refusal test below proves nothing.

    A long call bought for 1.20 and expiring worthless is a -$120 realized
    loss at a 100 multiplier, and it has to be *reachable* before "it did not
    happen" can mean anything.
    """
    service = IngestService(
        broker=FakeBroker(expiry_history(), expiry_orders()),
        provider=FakeProvider(standard_terms([NVDA_OTM_CALL])),
        engine=engine,
        account=PAPER,
    )
    result = await service.run()

    (trade,) = trade_rows(engine)
    assert trade.symbol == NVDA_OTM_CALL
    assert trade.close_kind == "expiry"
    assert trade.close_price == Decimal("0")
    assert trade.pnl == Decimal("-120.00")
    assert result.unfetched_terms == ()


@pytest.mark.asyncio
async def test_an_option_event_with_no_terms_books_nothing_and_is_reported(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """The whole point of requirement 4, asserted on both halves.

    The contracts endpoint answers, and simply does not carry this symbol.
    No realized trade is written -- the matcher refuses to book a P&L it
    cannot state correctly -- and the gap is **visible**: a refusal naming the
    rule, the inputs and the timestamp, on the result *and* in the log.

    A gap in lifetime P&L must be visible, not inferred. Refused silently this
    is the same failure as a wrong ``net_amount``: the terminal quietly
    believes you never win.
    """
    service = IngestService(
        broker=FakeBroker(expiry_history(), expiry_orders()),
        provider=FakeProvider(),  # answers, and holds nothing
        engine=engine,
        account=PAPER,
        correlation_id=lambda: "run-under-test",
    )

    with caplog.at_level(logging.WARNING, logger="corollary.engine.ingest"):
        result = await service.run()

    assert trade_rows(engine) == []

    (refusal,) = result.unfetched_terms
    assert refusal.rule is IngestRule.CONTRACT_TERMS_UNAVAILABLE
    assert refusal.symbol == NVDA_OTM_CALL
    assert refusal.underlying == "NVDA"
    assert refusal.carries_option_event
    assert "OPEXP" in refusal.activity_types
    assert refusal.at is not None
    assert refusal.activity_ids

    logged = [
        record
        for record in caplog.records
        if getattr(record, "rule", None) == IngestRule.CONTRACT_TERMS_UNAVAILABLE.value
    ]
    assert len(logged) == 1
    assert getattr(logged[0], "refusal_symbol", None) == NVDA_OTM_CALL
    assert getattr(logged[0], "correlation_id", None) == "run-under-test"
    assert getattr(logged[0], "at", None) is not None
    assert "OPEXP" in getattr(logged[0], "inputs", {})["activity_types"]


@pytest.mark.asyncio
async def test_a_failed_terms_fetch_is_its_own_rule_and_also_books_nothing(
    engine: Engine,
) -> None:
    """A provider that *raises* is a different rule with the same consequence.

    Distinguishing them matters to whoever reads the report: "the endpoint is
    down" and "this contract is not in the endpoint's answer" have different
    remedies, and only one of them heals on the next run.
    """
    service = IngestService(
        broker=FakeBroker(expiry_history(), expiry_orders()),
        provider=FakeProvider(fail_for=["NVDA"]),
        engine=engine,
        account=PAPER,
    )
    result = await service.run()

    assert trade_rows(engine) == []
    (refusal,) = result.unfetched_terms
    assert refusal.rule is IngestRule.CONTRACT_TERMS_FETCH_FAILED
    assert refusal.symbol == NVDA_OTM_CALL


@pytest.mark.asyncio
async def test_a_terms_refusal_does_not_delete_a_trade_already_booked(
    engine: Engine,
) -> None:
    """A provider outage must not erase lifetime P&L.

    The ledger is rebuilt from the whole history every run, so a run that
    cannot state a P&L produces fewer trades than the table holds. Deleting
    the difference would make a transient outage indistinguishable from a
    busted fill -- and it is the same "terminal that believes you never win",
    arriving by deletion.
    """
    history = expiry_history()
    orders = expiry_orders()
    seeing = IngestService(
        broker=FakeBroker(history, orders),
        provider=FakeProvider(standard_terms([NVDA_OTM_CALL])),
        engine=engine,
        account=PAPER,
    )
    await seeing.run()
    assert len(trade_rows(engine)) == 1

    blind = IngestService(
        broker=FakeBroker(history, orders),
        provider=FakeProvider(),
        engine=engine,
        account=PAPER,
    )
    result = await blind.run()

    assert len(trade_rows(engine)) == 1
    assert result.trades_removed == 0
    assert result.unfetched_terms


# --------------------------------------------------------------------------
# An expired contract's terms
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_expired_contracts_terms_come_from_the_retired_list(
    engine: Engine,
) -> None:
    """The active list is the wrong list at exactly the moment it matters.

    A contract leaves the active list when it expires, and the multiplier is
    needed to book *that expiry*. Asked only for ``status=active``, ingestion
    would refuse every expiry from a process started after it -- a gap in
    lifetime P&L that widens with every contract that ever runs to expiry,
    and one that no amount of retrying fixes because the answer never changes.

    The fallback asks the retired list second, not first: the overwhelming
    majority of symbols are live, and an extra request per live symbol every
    cycle would be a real cost against a 200/min budget.
    """
    provider = FakeProvider(retired={NVDA_OTM_CALL: contract(NVDA_OTM_CALL)})
    service = IngestService(
        broker=FakeBroker(expiry_history(), expiry_orders()),
        provider=provider,
        engine=engine,
        account=PAPER,
    )
    result = await service.run()

    assert provider.statuses == [ContractStatus.ACTIVE, ContractStatus.INACTIVE]
    assert result.unfetched_terms == ()

    (trade,) = trade_rows(engine)
    assert trade.symbol == NVDA_OTM_CALL
    assert trade.close_kind == "expiry"
    assert trade.pnl == Decimal("-120.00")


# --------------------------------------------------------------------------
# Settlement: what an exercise and an assignment close at
# --------------------------------------------------------------------------

#: The three contracts the paper account held into 2026-09-11, and NVDA's
#: close that day. Same ground truth as
#: ``test_ledger_option_events.py``'s predicted cases, deliberately: one set of
#: numbers, asserted in the pure matcher and again end to end through the
#: service, and both get replaced together when the real rows land.
NVDA_ITM_CALL = "NVDA260911C00205000"
NVDA_ITM_SHORT_PUT = "NVDA260911P00230000"
NVDA_SETTLEMENT_CLOSE = "218.17"
EXPIRATION = date(2026, 9, 11)


def nvda_bars() -> dict[tuple[str, date], str]:
    """NVDA's daily closes either side of the expiry.

    The neighbours are there so a consumer that grabbed the first or last bar
    of the window, rather than the one whose **Eastern** session date is the
    expiration, would get a different number and fail.
    """
    return {
        ("NVDA", date(2026, 9, 10)): "211.05",
        ("NVDA", EXPIRATION): NVDA_SETTLEMENT_CLOSE,
        ("NVDA", date(2026, 9, 14)): "224.90",
    }


@pytest.mark.asyncio
async def test_an_exercise_books_at_intrinsic_against_the_settlement_close(
    engine: Engine,
) -> None:
    """``NVDA260911C00205000``, bought at 14.20, auto-exercised at 218.17.

    The 205 call is worth ``218.17 - 205 = 13.17`` at settlement, so the trade
    loses $1.03 a share: **-$103.00**. The equity check that fixes the number
    independently -- cash goes -20,500 and 100 shares worth 21,817 arrive, a
    net +1,317 against the 1,420 premium already paid.

    Two wrong answers this rules out, both of which are well-formed numbers:
    ``net_amount`` is ``"0"`` on the event row, which books the whole premium
    as a loss; and the spec's own literal wording -- *"close at the strike"* --
    books **+$19,080**, since ``(205 - 14.20) x 100`` is what "the strike" says
    if you take it as a close price rather than as an input to one.
    """
    provider = FakeProvider(
        standard_terms([NVDA_ITM_CALL]), bars=nvda_bars()
    )
    service = IngestService(
        broker=FakeBroker(
            [
                fill(
                    NVDA_ITM_CALL, "buy", 1, "14.20", when=at(17, 37), order_id="o-itm"
                ),
                option_event("OPEXC", NVDA_ITM_CALL, -1, when=at(21, 0, day=11)),
            ],
            [an_order("o-itm", NVDA_ITM_CALL, PositionIntent.BUY_TO_OPEN, "14.20")],
        ),
        provider=provider,
        engine=engine,
        account=PAPER,
    )
    result = await service.run()

    (trade,) = trade_rows(engine)
    assert trade.close_kind == "exercise"
    assert trade.close_price == Decimal("13.17")
    assert trade.pnl == Decimal("-103.00")
    assert result.unpriced_settlements == ()

    # Daily bars, from the historical surface, over a window that brackets the
    # expiration. The feed is the provider's business and is `sip` by config;
    # what ingestion owns is asking for the right day at the right resolution.
    (symbols, timeframe, start, end) = provider.bar_calls[0]
    assert symbols == ("NVDA",)
    assert timeframe is BarTimeframe.DAY
    assert start is not None and end is not None
    assert start < datetime.combine(EXPIRATION, time(0), tzinfo=NYSE_TZ) < end


@pytest.mark.asyncio
async def test_an_unverifiable_exercise_is_written_with_no_price_at_all(
    engine: Engine,
) -> None:
    """The refusal reaches the stored price, not only the stored P&L.

    ``GME1`` is a modified root: the contract delivers 100 GME **plus**
    something else, and ``multiplier`` and ``size`` both say 100 anyway. So
    the matcher refuses the realized trade -- and the close price it would
    otherwise have stored is ``max(30.40 - 30, 0)``, an intrinsic value
    against the very strike the adjustment invalidated.

    Three things have to be true at once, and the middle one is what this
    test exists for:

    * no ``realized_trade`` row, because no dollar figure is derivable;
    * a ``fill`` row with ``price`` **NULL**, because the contracts really
      left the book and the Activity page's ``notBooked`` count reads this
      row -- but an estimate in a column of prices paid is what open question
      4 forbade one column over;
    * ``price`` is not 0, which is a price, and on a long books a total loss.
    """
    adjusted = "GME1260911C00030000"
    provider = FakeProvider(
        {adjusted: contract(adjusted, underlying="GME")},
        bars={("GME", EXPIRATION): "30.40"},
    )
    service = IngestService(
        broker=FakeBroker(
            [
                fill(adjusted, "buy", 1, "2.00", when=at(17, 37), order_id="o-gme1"),
                option_event("OPEXC", adjusted, -1, when=at(21, 0, day=11)),
            ],
            [an_order("o-gme1", adjusted, PositionIntent.BUY_TO_OPEN, "2.00")],
        ),
        provider=provider,
        engine=engine,
        account=PAPER,
    )

    await service.run()

    assert trade_rows(engine) == []
    exercise = next(
        row for row in fill_rows(engine) if row.position_intent == "sell_to_close"
    )
    assert exercise.price is None
    assert exercise.qty == 1
    # The opening fill is untouched: a premium paid is a measurement, and
    # nothing about the deliverable makes it less so.
    opening = next(
        row for row in fill_rows(engine) if row.position_intent == "buy_to_open"
    )
    assert opening.price == Decimal("2.00")


@pytest.mark.asyncio
async def test_an_assignment_on_a_short_put_inverts_the_sign(
    engine: Engine,
) -> None:
    """``NVDA260911P00230000``, sold at 11.55, assigned at a close of 218.17.

    The 230 put is worth ``230 - 218.17 = 11.83``, so the short loses $0.28 a
    share: **-$28.00**. A short inverts -- ``(open - close)``, not
    ``(close - open)`` -- and getting that backwards reports a losing
    assignment as a $28 win, which is the direction that flatters the book.

    ``qty`` is ``+1`` here, not ``-1``: a non-trade row carries a **signed**
    quantity, positive when a short is assigned away and negative when
    contracts leave a long. One ingest path, two conventions.
    """
    provider = FakeProvider(
        standard_terms([NVDA_ITM_SHORT_PUT]), bars=nvda_bars()
    )
    service = IngestService(
        broker=FakeBroker(
            [
                # `sell_short` is an opening sale and decides itself, so this
                # one needs no order to join to.
                fill(NVDA_ITM_SHORT_PUT, "sell_short", 1, "11.55", when=at(17, 37)),
                option_event("OPASN", NVDA_ITM_SHORT_PUT, 1, when=at(21, 0, day=11)),
            ]
        ),
        provider=provider,
        engine=engine,
        account=PAPER,
    )
    await service.run()

    (trade,) = trade_rows(engine)
    assert trade.close_kind == "assignment"
    assert trade.open_price == Decimal("11.55")
    assert trade.close_price == Decimal("11.83")
    assert trade.pnl == Decimal("-28.00")


@pytest.mark.asyncio
async def test_a_missing_settlement_bar_refuses_rather_than_booking(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """No settlement price, no trade -- and never a substituted one.

    The tempting fallbacks are all wrong and all well-formed: the strike
    books a five-figure phantom gain, ``net_amount`` books a total loss, and
    the last quote is a price for a contract that no longer exists. A refusal
    is a visible gap; an invented settlement price is a wrong number under the
    word "realized", which is what rule 4 exists to stop.
    """
    provider = FakeProvider(standard_terms([NVDA_ITM_CALL]))  # terms, no bars
    service = IngestService(
        broker=FakeBroker(
            [
                fill(
                    NVDA_ITM_CALL, "buy", 1, "14.20", when=at(17, 37), order_id="o-itm"
                ),
                option_event("OPEXC", NVDA_ITM_CALL, -1, when=at(21, 0, day=11)),
            ],
            [an_order("o-itm", NVDA_ITM_CALL, PositionIntent.BUY_TO_OPEN, "14.20")],
        ),
        provider=provider,
        engine=engine,
        account=PAPER,
        correlation_id=lambda: "run-under-test",
    )

    with caplog.at_level(logging.WARNING, logger="corollary.engine.ingest"):
        result = await service.run()

    assert trade_rows(engine) == []

    (refusal,) = result.unpriced_settlements
    assert refusal.rule is IngestRule.SETTLEMENT_PRICE_UNAVAILABLE
    assert refusal.symbol == NVDA_ITM_CALL
    assert refusal.underlying == "NVDA"
    assert refusal.carries_option_event
    assert refusal.at is not None
    assert refusal.inputs["settlement_date"] == EXPIRATION.isoformat()

    logged = [
        record
        for record in caplog.records
        if getattr(record, "rule", None)
        == IngestRule.SETTLEMENT_PRICE_UNAVAILABLE.value
    ]
    assert len(logged) == 1
    assert getattr(logged[0], "correlation_id", None) == "run-under-test"

    # And the matcher's own refusal is passed through alongside it, so the two
    # halves of the same gap are both on the result.
    assert any(
        rejection.rule.value == "unpriced_option_event"
        for rejection in result.rejections
    )


@pytest.mark.asyncio
async def test_a_failed_bars_fetch_is_its_own_rule(engine: Engine) -> None:
    """"The bars endpoint is down" heals next run; "there is no such bar" does
    not. Different rules, because different remedies."""
    provider = FakeProvider(
        standard_terms([NVDA_ITM_CALL]), bars_fail_for=["NVDA"]
    )
    service = IngestService(
        broker=FakeBroker(
            [
                fill(
                    NVDA_ITM_CALL, "buy", 1, "14.20", when=at(17, 37), order_id="o-itm"
                ),
                option_event("OPEXC", NVDA_ITM_CALL, -1, when=at(21, 0, day=11)),
            ],
            [an_order("o-itm", NVDA_ITM_CALL, PositionIntent.BUY_TO_OPEN, "14.20")],
        ),
        provider=provider,
        engine=engine,
        account=PAPER,
    )
    result = await service.run()

    assert trade_rows(engine) == []
    (refusal,) = result.unpriced_settlements
    assert refusal.rule is IngestRule.SETTLEMENT_PRICE_FETCH_FAILED


@pytest.mark.asyncio
async def test_an_expiry_needs_no_settlement_price_and_asks_for_none(
    engine: Engine,
) -> None:
    """``OPEXP`` closes at **zero**, which is a price rather than an absence.

    So the bars endpoint is never touched for one. Worth pinning: fetching a
    settlement for every option event would spend a request per expiry on a
    number the branch does not read, and would invent a refusal for a trade
    that books perfectly well without it.
    """
    provider = FakeProvider(standard_terms([NVDA_OTM_CALL]))
    service = IngestService(
        broker=FakeBroker(expiry_history(), expiry_orders()),
        provider=provider,
        engine=engine,
        account=PAPER,
    )
    result = await service.run()

    assert provider.bar_calls == []
    assert result.unpriced_settlements == ()
    (trade,) = trade_rows(engine)
    assert trade.close_price == Decimal("0")


@pytest.mark.asyncio
async def test_the_settlement_cache_survives_a_second_run(engine: Engine) -> None:
    """One bars request, however many times ingestion runs.

    Ingestion runs every interval and a settled close never changes, so
    re-fetching it is pure waste against the 200/min budget -- and the rows
    must come out identical regardless.
    """
    provider = FakeProvider(
        standard_terms([NVDA_ITM_CALL]), bars=nvda_bars()
    )
    service = IngestService(
        broker=FakeBroker(
            [
                fill(
                    NVDA_ITM_CALL, "buy", 1, "14.20", when=at(17, 37), order_id="o-itm"
                ),
                option_event("OPEXC", NVDA_ITM_CALL, -1, when=at(21, 0, day=11)),
            ],
            [an_order("o-itm", NVDA_ITM_CALL, PositionIntent.BUY_TO_OPEN, "14.20")],
        ),
        provider=provider,
        engine=engine,
        account=PAPER,
    )
    await service.run()
    before = table_snapshot(engine)
    assert len(provider.bar_calls) == 1

    second = await service.run()

    assert len(provider.bar_calls) == 1
    assert second.trades_written == 0
    assert table_snapshot(engine) == before


@pytest.mark.asyncio
async def test_a_supplied_settlement_is_authoritative_and_is_not_refetched(
    engine: Engine,
) -> None:
    """The seam for a caller that already knows the close.

    Mirrors ``build_ledger``'s own ``settlements`` argument, one convention
    rather than two, and it is what a backfill over a window the bars endpoint
    no longer covers would use.
    """
    provider = FakeProvider(standard_terms([NVDA_ITM_CALL]))  # no bars at all
    service = IngestService(
        broker=FakeBroker(
            [
                fill(
                    NVDA_ITM_CALL, "buy", 1, "14.20", when=at(17, 37), order_id="o-itm"
                ),
                option_event("OPEXC", NVDA_ITM_CALL, -1, when=at(21, 0, day=11)),
            ],
            [an_order("o-itm", NVDA_ITM_CALL, PositionIntent.BUY_TO_OPEN, "14.20")],
        ),
        provider=provider,
        engine=engine,
        account=PAPER,
    )
    result = await service.run(
        settlements={NVDA_ITM_CALL: Decimal(NVDA_SETTLEMENT_CLOSE)}
    )

    assert provider.bar_calls == []
    assert result.unpriced_settlements == ()
    (trade,) = trade_rows(engine)
    assert trade.pnl == Decimal("-103.00")


# --------------------------------------------------------------------------
# Settlement: *which session* an exercise or assignment is priced against
# --------------------------------------------------------------------------

#: The early-assignment case. A short call assigned on the Tuesday before a
#: Friday expiry is ordinary on American-style equity options -- it is what
#: ex-dividend assignment risk *is* -- and it is the case the contract's
#: expiration date gets wrong by three sessions.
NVDA_EARLY_SHORT_CALL = "NVDA260918C00205000"
EARLY_EXPIRATION = date(2026, 9, 18)
EARLY_EVENT_DATE = date(2026, 9, 15)


def nvda_early_bars() -> dict[tuple[str, date], str]:
    """NVDA's closes across the assignment and on through the expiration.

    The two that matter are three sessions and $17 apart, so a run that priced
    the Tuesday event against the Friday close could not come out right by
    accident. Every session in between is present too, so nothing is refused
    for want of a bar.
    """
    return {
        ("NVDA", date(2026, 9, 14)): "209.40",
        ("NVDA", EARLY_EVENT_DATE): "212.00",
        ("NVDA", date(2026, 9, 16)): "203.10",
        ("NVDA", date(2026, 9, 17)): "198.75",
        ("NVDA", EARLY_EXPIRATION): "195.00",
    }


@pytest.mark.asyncio
async def test_an_early_assignment_prices_against_its_own_session(
    engine: Engine,
) -> None:
    """A 2026-09-15 assignment is priced at the 2026-09-15 close. Not 09-18's.

    **This is a look-ahead bug, and it is worth naming as one.** Sourcing the
    settlement from the contract's expiration reads a bar three days in the
    *future* of the event that needs it, which is the ledger's version of an
    indicator reading an unclosed bar.

    The short 205 call was sold at 14.20 and assigned away with NVDA at
    212.00, so it was worth its intrinsic ``212.00 - 205 = 7.00`` and the
    short keeps ``(14.20 - 7.00) x 100 = +$720.00``.

    Priced against the expiration instead, NVDA's 195.00 close leaves the call
    worthless, the whole 14.20 credit is booked as kept, and the ledger
    reports **+$1,420** -- a $700 phantom gain, with no refusal, no rejection
    and no log line anywhere. The bar window is asserted below because the
    failure is not a bar chosen wrongly from a window: the correct session was
    never requested at all.
    """
    provider = FakeProvider(
        standard_terms([NVDA_EARLY_SHORT_CALL]), bars=nvda_early_bars()
    )
    service = IngestService(
        broker=FakeBroker(
            [
                # `sell_short` is an opening sale and decides itself.
                fill(NVDA_EARLY_SHORT_CALL, "sell_short", 1, "14.20", when=at(17, 37)),
                option_event(
                    "OPASN", NVDA_EARLY_SHORT_CALL, 1, when=at(20, 0, day=15)
                ),
            ]
        ),
        provider=provider,
        engine=engine,
        account=PAPER,
    )
    result = await service.run()

    (trade,) = trade_rows(engine)
    assert trade.close_kind == "assignment"
    assert trade.close_price == Decimal("7.00")
    assert trade.pnl == Decimal("720.00")
    assert result.unpriced_settlements == ()
    assert result.rejections == ()

    # The session that was actually asked for. A window that brackets the
    # expiration instead would not contain the event date at all, which is
    # what makes this assertion the one that catches the bug at its source.
    (symbols, timeframe, start, end) = provider.bar_calls[0]
    assert symbols == ("NVDA",)
    assert timeframe is BarTimeframe.DAY
    assert start is not None and end is not None
    event_midnight = datetime.combine(EARLY_EVENT_DATE, time(0), tzinfo=NYSE_TZ)
    assert start < event_midnight < end
    # And never reaches forward to the expiration, which has not happened yet
    # as far as this event is concerned.
    assert end < datetime.combine(EARLY_EXPIRATION, time(0), tzinfo=NYSE_TZ)


@pytest.mark.asyncio
async def test_a_friday_expiry_posted_on_monday_still_prices_against_friday(
    engine: Engine,
) -> None:
    """The other half of ``min(event_date, expiration)``, and the older half.

    An auto-exercise at a Friday expiry can post as a Monday activity, and
    Monday's close is a different number about a different day -- 224.90
    rather than 218.17 here, which would turn a -$103.00 loss into a +$970.00
    gain. Capping at the expiration is what keeps that correct, and it is why
    the fix for the early-exercise case is a cap rather than a swap.
    """
    provider = FakeProvider(standard_terms([NVDA_ITM_CALL]), bars=nvda_bars())
    service = IngestService(
        broker=FakeBroker(
            [
                fill(
                    NVDA_ITM_CALL, "buy", 1, "14.20", when=at(17, 37), order_id="o-itm"
                ),
                # Friday's expiry, posted Monday.
                option_event("OPEXC", NVDA_ITM_CALL, -1, when=at(13, 30, day=14)),
            ],
            [an_order("o-itm", NVDA_ITM_CALL, PositionIntent.BUY_TO_OPEN, "14.20")],
        ),
        provider=provider,
        engine=engine,
        account=PAPER,
    )
    result = await service.run()

    (trade,) = trade_rows(engine)
    assert trade.close_price == Decimal("13.17")
    assert trade.pnl == Decimal("-103.00")
    assert result.unpriced_settlements == ()

    (_, _, start, end) = provider.bar_calls[0]
    assert start is not None and end is not None
    assert start < datetime.combine(EXPIRATION, time(0), tzinfo=NYSE_TZ) < end
    assert end < datetime.combine(date(2026, 9, 14), time(0), tzinfo=NYSE_TZ)


@pytest.mark.asyncio
async def test_an_undated_event_falls_back_to_its_eastern_session(
    engine: Engine,
) -> None:
    """No ``date`` field, so the stamp decides -- and the stamp is an instant.

    An assignment at 20:30 Eastern is already *tomorrow* in UTC, so reading
    the stamp's UTC date moves the event a whole session forward: 203.10
    instead of 212.00 here, which takes the 205 call to worthless and books
    the whole 14.20 credit as kept. Market data is Eastern and a session date
    is an Eastern question; ``activity_date`` is preferred where the vendor
    supplies one precisely because it is a date rather than an instant.
    """
    undated = dataclasses.replace(
        option_event("OPASN", NVDA_EARLY_SHORT_CALL, 1, when=at(0, 30, day=16)),
        activity_date=None,
    )
    provider = FakeProvider(
        standard_terms([NVDA_EARLY_SHORT_CALL]), bars=nvda_early_bars()
    )
    service = IngestService(
        broker=FakeBroker(
            [
                fill(NVDA_EARLY_SHORT_CALL, "sell_short", 1, "14.20", when=at(17, 37)),
                undated,
            ]
        ),
        provider=provider,
        engine=engine,
        account=PAPER,
    )
    result = await service.run()

    (trade,) = trade_rows(engine)
    assert trade.close_price == Decimal("7.00")
    assert trade.pnl == Decimal("720.00")
    assert result.unpriced_settlements == ()


@pytest.mark.asyncio
async def test_an_unpriced_early_event_names_the_session_it_actually_needed(
    engine: Engine,
) -> None:
    """Refusing is right; refusing about the wrong day is a wild goose chase.

    The bars here cover the expiration and nothing else, which is exactly the
    shape that used to *look* fine. The refusal must name 2026-09-15 -- the
    session the event settled on -- rather than the expiration the event had
    nothing to do with.
    """
    provider = FakeProvider(
        standard_terms([NVDA_EARLY_SHORT_CALL]),
        bars={("NVDA", EARLY_EXPIRATION): "195.00"},
    )
    service = IngestService(
        broker=FakeBroker(
            [
                fill(NVDA_EARLY_SHORT_CALL, "sell_short", 1, "14.20", when=at(17, 37)),
                option_event(
                    "OPASN", NVDA_EARLY_SHORT_CALL, 1, when=at(20, 0, day=15)
                ),
            ]
        ),
        provider=provider,
        engine=engine,
        account=PAPER,
    )
    result = await service.run()

    assert trade_rows(engine) == []
    (refusal,) = result.unpriced_settlements
    assert refusal.rule is IngestRule.SETTLEMENT_PRICE_UNAVAILABLE
    assert refusal.inputs["settlement_date"] == EARLY_EVENT_DATE.isoformat()
    assert refusal.inputs["event_date"] == EARLY_EVENT_DATE.isoformat()
    assert refusal.inputs["expiration"] == EARLY_EXPIRATION.isoformat()
    assert EARLY_EVENT_DATE.isoformat() in refusal.detail


@pytest.mark.asyncio
async def test_events_on_two_sessions_refuse_rather_than_price_both_at_one(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Half a short called away Tuesday, the rest at Friday's expiry.

    One settlement price per symbol cannot be right for both, and whichever
    session were chosen the other event would book a well-formed wrong number
    -- the same silent, directional failure this whole section exists to
    stop. So the symbol is refused and reported, and the raw rows are kept:
    the gap is recomputable, a booked wrong number is not.
    """
    provider = FakeProvider(
        standard_terms([NVDA_EARLY_SHORT_CALL]), bars=nvda_early_bars()
    )
    service = IngestService(
        broker=FakeBroker(
            [
                fill(NVDA_EARLY_SHORT_CALL, "sell_short", 2, "14.20", when=at(17, 37)),
                option_event(
                    "OPASN", NVDA_EARLY_SHORT_CALL, 1, when=at(20, 0, day=15)
                ),
                option_event(
                    "OPASN", NVDA_EARLY_SHORT_CALL, 1, when=at(20, 0, day=18)
                ),
            ]
        ),
        provider=provider,
        engine=engine,
        account=PAPER,
        correlation_id=lambda: "run-under-test",
    )

    with caplog.at_level(logging.WARNING, logger="corollary.engine.ingest"):
        result = await service.run()

    assert trade_rows(engine) == []
    assert provider.bar_calls == []

    (refusal,) = result.unpriced_settlements
    assert refusal.rule is IngestRule.SETTLEMENT_SESSION_AMBIGUOUS
    assert refusal.symbol == NVDA_EARLY_SHORT_CALL
    assert refusal.inputs["settlement_sessions"] == "2026-09-15,2026-09-18"

    logged = [
        record
        for record in caplog.records
        if getattr(record, "rule", None)
        == IngestRule.SETTLEMENT_SESSION_AMBIGUOUS.value
    ]
    assert len(logged) == 1
    assert getattr(logged[0], "correlation_id", None) == "run-under-test"

    # And the matcher says so too, once per event it could not price.
    assert [rejection.rule.value for rejection in result.rejections] == [
        "unpriced_option_event",
        "unpriced_option_event",
    ]


# --------------------------------------------------------------------------
# Rule 6: vendor free text never reaches a log or a return value bare
# --------------------------------------------------------------------------

#: Account-number **shaped**, and deliberately not an account number. The
#: shape is the whole point: rule 6 is enforced by a substring pass precisely
#: because a rule written about field names cannot see inside prose.
PLACEHOLDER_ACCOUNT = "PA0EXAMPLE00"

FEE_PROSE = f"CAT fee for proceed of 15 trades on 2026-09-10 by {PLACEHOLDER_ACCOUNT}"


@pytest.mark.asyncio
async def test_a_fee_description_leaves_ingestion_redacted(engine: Engine) -> None:
    """``IngestResult.fees`` is a return value, and a return value travels.

    ``FeeRecord.description`` is vendor free text and a ``FEE`` row's is the
    one field known to carry the account number in prose. It is on its way to
    the API route that reports ingestion status, so it has to be clean before
    it gets there rather than after.
    """
    service = IngestService(
        broker=FakeBroker([fee("-0.21", when=at(20, 30), description=FEE_PROSE)]),
        provider=FakeProvider(),
        engine=engine,
        account=PAPER,
    )
    result = await service.run()

    (charge,) = result.fees
    assert charge.description is not None
    assert PLACEHOLDER_ACCOUNT not in charge.description
    assert REDACTED in charge.description
    # Substituted, not deleted -- the fee is still identifiable as a CAT fee.
    assert charge.description.startswith("CAT fee for proceed of 15 trades")


# --------------------------------------------------------------------------
# The two-hop join
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_position_intent_reaches_the_fill_row_through_the_two_hop_join(
    engine: Engine,
) -> None:
    """All 15 fills carry an intent, including the eight that are mleg legs.

    A fill's ``order_id`` is the *leg's* id and the parent appears nowhere on
    it, so the join has to index ``legs[]`` as well as the top level. The
    short legs are what prove it reached the right object: ``sell_to_open`` is
    the intent while the activity's own ``side`` says ``sell_short`` -- two
    vocabularies for one fill, and only the order's is unambiguous.
    """
    _, orders = await recorded()
    await ingest_the_recording(engine)

    leg_ids = {leg.id for order in orders for leg in order.legs}
    assert len(leg_ids) == 8

    stored = fill_rows(engine)
    assert len(stored) == 15
    assert all(row.position_intent is not None for row in stored)

    leg_fills = [row for row in stored if row.order_id in leg_ids]
    assert len(leg_fills) == 8
    assert {row.position_intent for row in leg_fills} == {
        "buy_to_open",
        "sell_to_open",
    }
    assert {row.position_intent for row in leg_fills if row.side == "sell_short"} == {
        "sell_to_open"
    }


@pytest.mark.asyncio
async def test_a_one_hop_join_leaves_the_leg_fills_unintended(
    engine: Engine,
) -> None:
    """The failure the two-hop join exists to prevent, made visible.

    Orders fetched without ``?nested=true`` carry no ``legs[]``, so the eight
    leg fills reach no ``position_intent``. The four whose ``side`` is
    ``sell_short`` still decide themselves -- an opening sale is unambiguous
    -- but a ``buy`` is both buy-to-open and buy-to-close, so those four are
    refused rather than guessed and their rows carry a NULL intent rather than
    an invented one.
    """
    activities, orders = await recorded()
    flattened = [dataclasses.replace(order, legs=()) for order in orders]

    service = IngestService(
        broker=FakeBroker(activities, flattened),
        provider=FakeProvider(standard_terms(option_symbols(activities))),
        engine=engine,
        account=PAPER,
    )
    result = await service.run()

    stored = fill_rows(engine)
    assert len(stored) == 15
    unintended = [row for row in stored if row.position_intent is None]
    assert len(unintended) == 4
    assert {row.side for row in unintended} == {"buy"}
    assert any(
        rejection.rule.value == "unknown_intent" for rejection in result.rejections
    )
    # And no group can be written from a history whose legs were dropped.
    assert result.mleg_groups_written == 0
    assert any(
        refusal.rule is IngestRule.MLEG_ORDER_HAS_NO_LEGS
        for refusal in result.refusals
    )


# --------------------------------------------------------------------------
# What must not become a fill
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_non_trade_rows_become_no_fills_and_are_not_dropped(
    engine: Engine,
) -> None:
    """19 ``FEE`` rows and one ``JNLC``: no symbol, no ``order_id``, no fill.

    They are still money and they are still accounted for -- every one is
    either counted as a fee or refused with a rule. A row absent from both
    sides is one that vanished, which is the thing being ruled out.
    """
    activities, _ = await recorded()
    _, _, _, result = await ingest_the_recording(engine)

    non_trade = [row for row in activities if isinstance(row, NonTradeActivity)]
    assert len(non_trade) == 20
    assert sum(1 for row in non_trade if row.activity_type == "FEE") == 19
    assert not any(row.symbol for row in non_trade)
    assert not any(row.extra.get("order_id") for row in non_trade)

    stored = {row.activity_id for row in fill_rows(engine)}
    assert not stored & {row.id for row in non_trade}

    assert result.fees_seen == 19
    accounted = {charge.activity_id for charge in result.fees} | {
        rejection.activity_id for rejection in result.rejections
    }
    assert {row.id for row in non_trade} <= accounted


@pytest.mark.asyncio
async def test_every_stored_fill_is_a_trade_activity_on_an_option_symbol(
    engine: Engine,
) -> None:
    """The positive half: 15 rows, one per ``FILL``, all OCC contracts.

    ``qty`` is stored unsigned with the direction in ``side``, which is the
    convention the matcher's queue is built on -- a negative value here means
    the normalisation did not happen.
    """
    activities, _ = await recorded()
    await ingest_the_recording(engine)

    fills = {row.id for row in activities if isinstance(row, TradeActivity)}
    stored = fill_rows(engine)

    assert {row.activity_id for row in stored} == fills
    assert all(row.qty > 0 for row in stored)
    assert all(row.side in {"buy", "sell", "sell_short"} for row in stored)
    for row in stored:
        parse_occ_symbol(row.symbol)


# --------------------------------------------------------------------------
# mleg groups
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_mleg_history_is_written_as_grouping_evidence(
    engine: Engine,
) -> None:
    """Four verticals, two legs each, direction carried by the signed net price.

    A credit vertical reports ``filled_avg_price: "-2.01"`` and the sign *is*
    the direction -- debit long, credit short -- so taking an absolute value
    anywhere here would invert every credit spread's risk class.
    """
    _, _, _, result = await ingest_the_recording(engine)
    _, orders = await recorded()

    mleg = [order for order in orders if order.order_class is OrderClass.MLEG]
    assert len(mleg) == 4

    with Session(engine) as session:
        groups = {row.order_id: row for row in session.scalars(select(MlegGroup))}
        legs = list(session.scalars(select(MlegLeg)))

    assert set(groups) == {order.id for order in mleg}
    assert len(legs) == 8
    assert all(group.account == PAPER for group in groups.values())

    for order in mleg:
        group = groups[order.id]
        assert group.net_price == order.filled_avg_price
        assert group.opened_at == order.filled_at
    assert any(
        group.net_price is not None and group.net_price < 0
        for group in groups.values()
    )

    by_group: dict[int, list[MlegLeg]] = {}
    for leg in legs:
        by_group.setdefault(leg.group_id, []).append(leg)
    assert all(len(members) == 2 for members in by_group.values())
    assert all(leg.ratio == 1 for leg in legs)
    assert {leg.side for leg in legs} == {"buy", "sell"}
    assert {leg.position_intent for leg in legs} == {"buy_to_open", "sell_to_open"}
    assert result.mleg_legs_written == 8


# --------------------------------------------------------------------------
# The book
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_account_must_be_a_book_the_schema_knows(engine: Engine) -> None:
    """``paper`` or ``cash``. Anything else is a typo that would fail a CHECK.

    Refused at construction rather than at the first INSERT, because the CHECK
    fires after a whole run's work and names a column rather than the
    argument.
    """
    with pytest.raises(ValueError, match="account"):
        IngestService(
            broker=FakeBroker([]),
            provider=FakeProvider(),
            engine=engine,
            account="live",
        )


@pytest.mark.asyncio
async def test_an_empty_history_writes_nothing_and_does_not_raise(
    engine: Engine,
) -> None:
    """The paper account's real state until the first order. Not an edge case."""
    service = IngestService(
        broker=FakeBroker([]), provider=FakeProvider(), engine=engine, account=PAPER
    )
    result = await service.run()

    assert result.activities_pulled == 0
    assert result.fills_written == 0
    assert result.cursor is None
    assert fill_rows(engine) == []
