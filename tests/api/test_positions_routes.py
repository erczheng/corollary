"""Logical positions and working orders, against the recorded paper account.

The account holds **13 broker rows**, and the whole point of this module is
that they are **9 positions**: four ``mleg`` verticals contributing two rows
each, and five singles contributing one. Nothing infers that. Each vertical is
proved by the ``mleg`` order that opened it, and the five singles are labelled
ungrouped because no order explains them -- three of which share one NVDA
expiry with mixed sides and different strikes, which is exactly the shape a
same-underlying/same-expiry heuristic would fuse into a spread nobody opened.

Why it is rule 4 rather than tidiness: a short leg rendered alone reports as
an **undefined-risk naked short**, so the AMD 470 put -- half of a defined-risk
credit spread -- would state the wrong risk class, and the risk manager would
then size against the wrong maximum loss.

Nothing here opens a socket. The broker replays ``tests/fixtures/alpaca/``
through the real ``AlpacaBroker`` parsers (``conftest.RecordedBroker``); the
market-data provider is a local stub, because a stub is the only way to make a
quote *absent* and see what the route says when it is.
"""

import json
import logging
from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.api.app import create_app
from corollary.api.deps import AccountMode, ServiceRegistry
from corollary.api.routes.positions import router as positions_router
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
    Trade,
)
from corollary.db.models import Fill
from corollary.engine.execution.interface import (
    BrokerAccount,
    BrokerPosition,
    Order,
    OrderClass,
    OrderQueryStatus,
    OrderSide,
    PositionIntent,
)
from corollary.instruments import OptionType, parse_occ_symbol

from .conftest import RecordedBroker

MODULE = (
    Path(__file__).resolve().parents[2]
    / "corollary"
    / "api"
    / "routes"
    / "positions.py"
)

#: Every OCC symbol the recorded account holds, and the underlying it is on.
HELD: dict[str, str] = {
    "AAPL261218C00340000": "AAPL",
    "AMD270115P00460000": "AMD",
    "AMD270115P00470000": "AMD",
    "NVDA260911C00205000": "NVDA",
    "NVDA260911C00240000": "NVDA",
    "NVDA260911P00230000": "NVDA",
    "NVDA261218P00200000": "NVDA",
    "NVDA261218P00205000": "NVDA",
    "QQQ270115C00740000": "QQQ",
    "SPY261130P00706000": "SPY",
    "SPY261130P00721000": "SPY",
    "TSLA261218P00330000": "TSLA",
    "TSLA261218P00340000": "TSLA",
}

UNDERLYING_PRICE: dict[str, Decimal] = {
    "AAPL": Decimal("324.99"),
    "AMD": Decimal("446.10"),
    "NVDA": Decimal("217.90"),
    "QQQ": Decimal("655.20"),
    "SPY": Decimal("757.41"),
    "TSLA": Decimal("331.05"),
}

#: The broker's own marks, read off ``positions.json`` so the two halves of
#: every assertion cannot drift.
MARK: dict[str, Decimal] = {
    "AAPL261218C00340000": Decimal("15.95"),
    "AMD270115P00460000": Decimal("33.4"),
    "AMD270115P00470000": Decimal("39.55"),
    "NVDA260911C00205000": Decimal("13.05"),
    "NVDA260911C00240000": Decimal("0"),
    "NVDA260911P00230000": Decimal("13.2"),
    "NVDA261218P00200000": Decimal("8.05"),
    "NVDA261218P00205000": Decimal("9.95"),
    "QQQ270115C00740000": Decimal("26.27"),
    "SPY261130P00706000": Decimal("6.21"),
    "SPY261130P00721000": Decimal("7.96"),
    "TSLA261218P00330000": Decimal("15.4"),
    "TSLA261218P00340000": Decimal("19.25"),
}

AMD_ORDER = "8dd6cb36-dc07-562d-90fb-4a5ed834fb63"
SPY_ORDER = "e2ce6562-ed9b-542b-a7ba-4ec15595330b"

AT = datetime(2026, 9, 10, 17, 11, 25, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# A market-data provider that can be made to not know things
# --------------------------------------------------------------------------


class StubProvider(MarketDataProvider):
    """Quotes, snapshots, bars and contract terms, each independently absent.

    Hand-built from *our* domain objects rather than from recorded vendor
    JSON, deliberately: the vendor parsing is already pinned in
    ``tests/data/providers/``, and what these tests need is the ability to
    delete one symbol's quote -- which a recording cannot do.
    """

    def __init__(
        self,
        *,
        quotes: dict[str, Quote] | None = None,
        prices: dict[str, Decimal] | None = None,
        bars: dict[str, list[Bar]] | None = None,
        contracts: dict[str, OptionContract] | None = None,
        fail_contracts: bool = False,
        fail_quotes: bool = False,
        fail_bars: bool = False,
    ) -> None:
        self.quotes = default_quotes() if quotes is None else quotes
        self.prices = dict(UNDERLYING_PRICE) if prices is None else prices
        self.bars = {} if bars is None else bars
        self.contracts = default_contracts() if contracts is None else contracts
        self.fail_contracts = fail_contracts
        self.fail_quotes = fail_quotes
        self.fail_bars = fail_bars
        self.calls: list[str] = []
        self.contract_queries: list[tuple[str, date | None]] = []
        self.bar_queries: list[tuple[tuple[str, ...], datetime | None]] = []

    async def latest_stock_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        raise AssertionError("the positions routes read snapshots, not raw quotes")

    async def stock_snapshots(
        self, symbols: Sequence[str]
    ) -> dict[str, StockSnapshot]:
        self.calls.append("stock_snapshots")
        found: dict[str, StockSnapshot] = {}
        for symbol in symbols:
            price = self.prices.get(symbol)
            if price is None:
                continue
            found[symbol] = StockSnapshot(
                symbol=symbol,
                latest_quote=None,
                latest_trade=Trade(symbol=symbol, price=price, size=1, at=AT),
                minute_bar=None,
                daily_bar=None,
                previous_daily_bar=None,
            )
        return found

    async def stock_bars(
        self,
        symbols: Sequence[str],
        *,
        timeframe: BarTimeframe = BarTimeframe.DAY,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, list[Bar]]:
        raise AssertionError("the positions routes do not read equity bars")

    async def latest_option_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        self.calls.append("latest_option_quotes")
        if self.fail_quotes:
            raise ProviderError("the options quote endpoint is unavailable")
        return {s: self.quotes[s] for s in symbols if s in self.quotes}

    async def option_bars(
        self,
        symbols: Sequence[str],
        *,
        timeframe: BarTimeframe = BarTimeframe.DAY,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, list[Bar]]:
        self.calls.append("option_bars")
        self.bar_queries.append((tuple(symbols), start))
        if self.fail_bars:
            raise ProviderError("the options bar endpoint is unavailable")
        return {s: self.bars[s] for s in symbols if s in self.bars}

    async def option_chain(
        self,
        underlying: str,
        *,
        expiration_lte: date | None = None,
        expiration_gte: date | None = None,
        strike_gte: Decimal | None = None,
        strike_lte: Decimal | None = None,
        option_type: OptionType | None = None,
    ) -> dict[str, OptionSnapshot]:
        raise AssertionError("the positions routes do not read a chain")

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
        self.calls.append("option_contracts")
        self.contract_queries.append((underlying, expiration_gte))
        if self.fail_contracts:
            raise ProviderError("the contracts endpoint is unavailable")
        return [
            contract
            for contract in self.contracts.values()
            if contract.underlying_symbol == underlying
            and (expiration_gte is None or contract.expiration >= expiration_gte)
            and (expiration_lte is None or contract.expiration <= expiration_lte)
        ]

    async def aclose(self) -> None:
        return None


def default_quotes() -> dict[str, Quote]:
    """A two-sided quote a penny either side of the broker's mark."""
    return {
        symbol: Quote(
            symbol=symbol,
            bid=mark - Decimal("0.05"),
            ask=mark + Decimal("0.05"),
            bid_size=10,
            ask_size=10,
            at=AT,
        )
        for symbol, mark in MARK.items()
    }


def default_contracts(
    *, multiplier: Decimal = Decimal(100), root_for: str | None = None
) -> dict[str, OptionContract]:
    contracts: dict[str, OptionContract] = {}
    for symbol, underlying in HELD.items():
        occ = parse_occ_symbol(symbol)
        contracts[symbol] = OptionContract(
            symbol=symbol,
            underlying_symbol=underlying,
            root_symbol=root_for if root_for == symbol else occ.root,
            expiration=occ.expiration,
            option_type=occ.option_type,
            strike=occ.strike,
            style="american",
            multiplier=multiplier,
            size=Decimal(100),
            open_interest=None,
            open_interest_date=None,
            close_price=None,
            close_price_date=None,
            tradable=True,
            status="active",
            name=symbol,
        )
    return contracts


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def build_app(
    *,
    broker: BrokerAccount,
    provider: MarketDataProvider,
    db_engine: Engine,
    cash: BrokerAccount | None = None,
) -> FastAPI:
    brokers: dict[AccountMode, Any] = {AccountMode.PAPER: lambda: broker}
    missing: tuple[str, ...] = ("ALPACA_LIVE_API_KEY", "ALPACA_LIVE_SECRET_KEY")
    if cash is not None:
        brokers[AccountMode.CASH] = lambda: cash
        missing = ()
    registry = ServiceRegistry(
        brokers=brokers, provider=lambda: provider, missing_live_credentials=missing
    )
    app = create_app(registry=registry, db_engine=db_engine)
    mounted = {getattr(route, "path", "") for route in app.routes}
    if "/api/positions" not in mounted:
        # The orchestrator owns ``api/app.py``; this test owns proving the
        # router works once it is wired, and must pass either side of that.
        app.include_router(positions_router)
    return app


@pytest.fixture
def stub_provider() -> StubProvider:
    return StubProvider()


@pytest.fixture
def positions_client(
    paper_broker: RecordedBroker, stub_provider: StubProvider, db_engine: Engine
) -> Iterator[TestClient]:
    app = build_app(
        broker=paper_broker, provider=stub_provider, db_engine=db_engine
    )
    with TestClient(app) as client:
        yield client


def positions_of(client: TestClient) -> list[dict[str, Any]]:
    response = client.get("/api/positions")

    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    return body


def by_id(rows: Sequence[dict[str, Any]], identifier: str) -> dict[str, Any]:
    for row in rows:
        if row["id"] == identifier:
            return row
    raise AssertionError(f"{identifier} not in {[row['id'] for row in rows]}")


# --------------------------------------------------------------------------
# The router's shape, which api/app.py depends on
# --------------------------------------------------------------------------


def test_the_router_exposes_exactly_two_read_routes() -> None:
    """Two GETs and nothing else. Decision 2: every write control is inert.

    A cancel was considered and rejected -- a cancel is not an order, but it
    puts the first broker write before the risk manager exists.
    """
    exposed = {
        (getattr(route, "path", ""), method)
        for route in positions_router.routes
        for method in getattr(route, "methods", set())
    }

    assert exposed == {("/api/positions", "GET"), ("/api/positions/working", "GET")}


def test_the_module_reaches_no_order_path() -> None:
    """Rule 1, structurally. ``BrokerAccount`` has no ``submit_order`` to call.

    The grep is the belt: a route that imported ``alpaca`` directly, or named
    a cancel, would compile fine and violate rules 1 and 3 silently.
    """
    source = MODULE.read_text(encoding="utf-8")

    assert "import alpaca" not in source
    assert "BrokerDep" in source
    for forbidden in ("submit_order", "cancel_order", "replace_order"):
        assert forbidden not in source, forbidden


def test_write_methods_are_not_routed(positions_client: TestClient) -> None:
    for method in ("post", "put", "patch", "delete"):
        response = getattr(positions_client, method)("/api/positions")

        assert response.status_code == 405, (method, response.text)


# --------------------------------------------------------------------------
# Grouping: 13 rows, 9 positions
# --------------------------------------------------------------------------


def test_thirteen_broker_rows_become_nine_logical_positions(
    positions_client: TestClient,
) -> None:
    rows = positions_of(positions_client)

    assert len(rows) == 9
    assert sum(1 for row in rows if len(row["legs"]) == 2) == 4
    assert sum(1 for row in rows if len(row["legs"]) == 1) == 5
    assert sum(len(row["legs"]) for row in rows) == 13


def test_a_credit_spread_is_one_position_with_both_legs(
    positions_client: TestClient,
) -> None:
    """The AMD vertical, keyed on the ``mleg`` order that proves it."""
    amd = by_id(positions_of(positions_client), AMD_ORDER)

    assert amd["symbol"] == "AMD"
    assert [(leg["symbol"], leg["side"], leg["ratio"]) for leg in amd["legs"]] == [
        ("AMD270115P00470000", "short", 1),
        ("AMD270115P00460000", "long", 1),
    ]
    assert amd["expiry"] == "2027-01-15"


def test_the_short_leg_of_a_spread_is_never_a_position_of_its_own(
    positions_client: TestClient,
) -> None:
    """Rule 4: alone, it reports as an undefined-risk naked short.

    The assertion is the *absence* of a row keyed on the short leg's symbol,
    which is what a partial or heuristic grouping would produce.
    """
    identifiers = {row["id"] for row in positions_of(positions_client)}

    assert "AMD270115P00470000" not in identifiers
    assert "SPY261130P00721000" not in identifiers
    assert AMD_ORDER in identifiers


def test_three_singles_on_one_expiry_do_not_rhyme_into_a_spread(
    positions_client: TestClient,
) -> None:
    """Decision 5 rejects a same-underlying/same-expiry heuristic, and this is
    the account's own counter-example: three unrelated NVDA singles expiring
    2026-09-11, mixed sides, different strikes."""
    rows = positions_of(positions_client)
    near = [row for row in rows if row["expiry"] == "2026-09-11"]

    assert len(near) == 3
    assert all(len(row["legs"]) == 1 for row in near)
    assert {row["id"] for row in near} == {
        "NVDA260911C00205000",
        "NVDA260911C00240000",
        "NVDA260911P00230000",
    }


def test_direction_comes_from_the_orders_net_price(
    positions_client: TestClient,
) -> None:
    """Debit long, credit short. All four verticals filled for a net credit."""
    rows = positions_of(positions_client)

    assert by_id(rows, AMD_ORDER)["direction"] == "short"
    assert by_id(rows, SPY_ORDER)["direction"] == "short"
    assert by_id(rows, "AAPL261218C00340000")["direction"] == "long"
    assert by_id(rows, "NVDA260911P00230000")["direction"] == "short"


def test_the_response_is_deterministic(positions_client: TestClient) -> None:
    """Same inputs, same bytes -- the standard the scanner is held to."""
    first = positions_client.get("/api/positions").text
    second = positions_client.get("/api/positions").text

    assert first == second


# --------------------------------------------------------------------------
# Signs, and money
# --------------------------------------------------------------------------


def test_a_shorts_negative_cost_basis_survives(
    positions_client: TestClient,
) -> None:
    """The broker sends ``cost_basis: -1155`` on the short NVDA put, and that
    is not a bug to fix: a credit received is a liability, which is the same
    rule ``orders.ts`` states as a negative ``openUnitValue``."""
    short = by_id(positions_of(positions_client), "NVDA260911P00230000")

    assert short["costBasis"] == -1155.0
    assert short["value"] == -1320.0
    assert short["quantity"] == 1
    assert short["direction"] == "short"


def test_a_credit_spread_nets_its_legs_signed(
    positions_client: TestClient,
) -> None:
    """AMD: ``-4155 + 3815`` basis, ``-3955 + 3340`` value, ``200 - 475`` P&L."""
    amd = by_id(positions_of(positions_client), AMD_ORDER)

    assert amd["costBasis"] == -340.0
    assert amd["value"] == -615.0
    assert amd["pnl"] == -275.0


def test_pnl_pct_denominates_on_the_magnitude_of_the_basis(
    positions_client: TestClient,
) -> None:
    """A short's basis is negative; a return measured against it would report
    a gain as a loss. AMD: ``-275 / 340`` is −80.88%."""
    amd = by_id(positions_of(positions_client), AMD_ORDER)

    assert amd["pnlPct"] == pytest.approx(-80.88, abs=0.01)


def test_money_is_a_json_number_not_a_string(positions_client: TestClient) -> None:
    """The API boundary is a display boundary; ``Decimal`` everywhere behind."""
    raw = json.loads(positions_client.get("/api/positions").text)

    for row in raw:
        for field in ("last", "underlying", "costBasis", "value", "pnl", "bid", "ask"):
            assert isinstance(row[field], (int, float)), (row["id"], field)
            assert not isinstance(row[field], str)


# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------


def test_bid_and_ask_bracket_last_on_a_long_single(
    positions_client: TestClient,
) -> None:
    aapl = by_id(positions_of(positions_client), "AAPL261218C00340000")

    assert aapl["bid"] == 15.90
    assert aapl["ask"] == 16.00
    assert aapl["bid"] <= aapl["last"] <= aapl["ask"]


def test_a_short_single_crosses_the_spread_the_other_way(
    positions_client: TestClient,
) -> None:
    """A short is bought back at the ask, so the structure's own bid is the
    negated leg **ask**. Reversed, every credit position would quote a better
    exit than the market offers."""
    short = by_id(positions_of(positions_client), "NVDA260911P00230000")

    assert short["bid"] == float(-(MARK["NVDA260911P00230000"] + Decimal("0.05")))
    assert short["ask"] == float(-(MARK["NVDA260911P00230000"] - Decimal("0.05")))
    assert short["bid"] <= short["last"] <= short["ask"]


def test_a_credit_spread_quotes_a_negative_net(
    positions_client: TestClient,
) -> None:
    """Long leg bid minus short leg ask, and it is still a number you pay."""
    amd = by_id(positions_of(positions_client), AMD_ORDER)
    expected_bid = (MARK["AMD270115P00460000"] - Decimal("0.05")) - (
        MARK["AMD270115P00470000"] + Decimal("0.05")
    )

    assert amd["bid"] == float(expected_bid)
    assert amd["bid"] <= amd["ask"]


def test_a_missing_quote_falls_back_to_the_mark_and_says_so(
    paper_broker: RecordedBroker, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 8: the substitution is on the record, with the rule and the symbol.

    The indicative feed is thin and 15 minutes behind; a contract with no
    two-sided quote is ordinary, not a failure. What is not allowed is a
    spread width nobody measured.
    """
    quotes = default_quotes()
    del quotes["QQQ270115C00740000"]
    provider = StubProvider(quotes=quotes)
    with TestClient(
        build_app(broker=paper_broker, provider=provider, db_engine=db_engine)
    ) as client:
        with caplog.at_level(logging.WARNING):
            qqq = by_id(positions_of(client), "QQQ270115C00740000")

    assert qqq["bid"] == qqq["ask"] == qqq["last"] == float(MARK["QQQ270115C00740000"])
    record = next(
        r for r in caplog.records if getattr(r, "event", "") == "position_quote_missing"
    )
    assert getattr(record, "symbol") == "QQQ270115C00740000"
    assert getattr(record, "rule")
    assert getattr(record, "at")


def one_sided(symbol: str, *, bid: Decimal | None, ask: Decimal | None) -> StubProvider:
    """Every quote as recorded, except one symbol's, replaced wholesale."""
    quotes = default_quotes()
    quotes[symbol] = Quote(
        symbol=symbol, bid=bid, ask=ask, bid_size=10, ask_size=10, at=AT
    )
    return StubProvider(quotes=quotes)


def test_a_stale_mark_above_a_one_sided_ask_is_held_to_the_ask(
    paper_broker: RecordedBroker, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """``bid <= last <= ask`` has to be true, not nearly true.

    The two prices come from two sources read at two instants: the broker's
    ``current_price`` and a 15-minute-delayed indicative quote. With no bid
    quoted the mark stands in for the bid *and* becomes the last, and nothing
    used to hold the last itself inside the one side that **was** measured --
    so a mark of 26.27 against a quoted ask of 26.00 printed a row whose last
    price was above its own ask. That is the number a reader stops trusting
    the rest of the row over, which is what the mid exists to prevent.
    """
    symbol = "QQQ270115C00740000"
    ask = Decimal("26.00")
    provider = one_sided(symbol, bid=None, ask=ask)
    with TestClient(
        build_app(broker=paper_broker, provider=provider, db_engine=db_engine)
    ) as client:
        with caplog.at_level(logging.WARNING):
            qqq = by_id(positions_of(client), symbol)

    assert MARK[symbol] > ask  # the condition under test, stated
    assert qqq["last"] == float(ask)
    assert qqq["bid"] <= qqq["last"] <= qqq["ask"]
    record = next(
        r
        for r in caplog.records
        if getattr(r, "event", "") == "position_mark_outside_quote"
    )
    assert getattr(record, "symbol") == symbol
    assert getattr(record, "rule")
    assert getattr(record, "at")


def test_a_stale_mark_below_a_one_sided_bid_is_held_to_the_bid(
    paper_broker: RecordedBroker, db_engine: Engine
) -> None:
    """The mirror case, which is no safer than the first one.

    With no ask quoted the mark fills the ask, so a mark *below* the quoted
    bid prints a last price under its own bid -- the same row, inverted.
    """
    symbol = "QQQ270115C00740000"
    bid = Decimal("26.50")
    provider = one_sided(symbol, bid=bid, ask=None)
    with TestClient(
        build_app(broker=paper_broker, provider=provider, db_engine=db_engine)
    ) as client:
        qqq = by_id(positions_of(client), symbol)

    assert MARK[symbol] < bid
    assert qqq["last"] == float(bid)
    assert qqq["bid"] <= qqq["last"] <= qqq["ask"]


def test_a_mark_already_inside_the_quoted_side_is_left_alone(
    paper_broker: RecordedBroker, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """The boundary: the clamp only moves a mark that is actually outside.

    A one-sided quote is ordinary on the indicative feed, and where the
    broker's mark agrees with the side that was quoted there is nothing to
    correct. Substituting the ask for the last there would throw away the one
    price the broker actually holds the position at.
    """
    symbol = "QQQ270115C00740000"
    ask = Decimal("26.50")
    provider = one_sided(symbol, bid=None, ask=ask)
    with TestClient(
        build_app(broker=paper_broker, provider=provider, db_engine=db_engine)
    ) as client:
        with caplog.at_level(logging.WARNING):
            qqq = by_id(positions_of(client), symbol)

    assert MARK[symbol] < ask
    assert qqq["last"] == float(MARK[symbol])
    assert qqq["bid"] == float(MARK[symbol])
    assert qqq["ask"] == float(ask)
    assert not [
        r
        for r in caplog.records
        if getattr(r, "event", "") == "position_mark_outside_quote"
    ]


def test_a_crossed_quote_is_not_a_market_and_the_mark_stands_on_both_sides(
    paper_broker: RecordedBroker, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """A bid above an ask is a data error wearing a price.

    ``Quote.mid`` already refuses to average one, which is what lands it here
    with both sides present. Passing the two crossed sides through to the row
    would make ``bid <= last <= ask`` unsatisfiable by arithmetic, and picking
    one side would launder the vendor's error into a plausible spread. The
    same rule the absent quote takes applies: the mark stands on both sides
    and the substitution is recorded.
    """
    symbol = "QQQ270115C00740000"
    provider = one_sided(symbol, bid=Decimal("26.50"), ask=Decimal("26.00"))
    with TestClient(
        build_app(broker=paper_broker, provider=provider, db_engine=db_engine)
    ) as client:
        with caplog.at_level(logging.WARNING):
            qqq = by_id(positions_of(client), symbol)

    assert qqq["bid"] == qqq["ask"] == qqq["last"] == float(MARK[symbol])
    record = next(
        r for r in caplog.records if getattr(r, "event", "") == "position_quote_crossed"
    )
    assert getattr(record, "symbol") == symbol
    assert getattr(record, "rule")
    assert getattr(record, "at")


def test_a_quote_outage_does_not_blank_the_book(
    paper_broker: RecordedBroker, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """The broker answers what is held; the provider answers what it is quoted
    at. Losing the second must not remove the first."""
    provider = StubProvider(fail_quotes=True)
    with TestClient(
        build_app(broker=paper_broker, provider=provider, db_engine=db_engine)
    ) as client:
        with caplog.at_level(logging.WARNING):
            rows = positions_of(client)

    assert len(rows) == 9
    assert any(
        getattr(r, "event", "") == "position_quotes_unavailable" for r in caplog.records
    )


def test_a_missing_underlying_price_is_stated_not_invented(
    paper_broker: RecordedBroker, db_engine: Engine
) -> None:
    """``underlying`` is what the payoff curve is drawn against. A zero there
    draws a catastrophe; there is no honest substitute, so the request fails
    with a stated reason naming the symbol."""
    prices = dict(UNDERLYING_PRICE)
    del prices["TSLA"]
    provider = StubProvider(prices=prices)
    with TestClient(
        build_app(broker=paper_broker, provider=provider, db_engine=db_engine)
    ) as client:
        response = client.get("/api/positions")

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "underlying_price_unavailable"
    assert "TSLA" in body["error"]["message"]


# --------------------------------------------------------------------------
# Multipliers: never 100 by assumption
# --------------------------------------------------------------------------


def test_multipliers_are_asked_of_the_contracts_endpoint(
    positions_client: TestClient, stub_provider: StubProvider
) -> None:
    """``/v2/positions`` returns no multiplier field at all, so there is
    nothing to fall back to and nowhere else to ask."""
    positions_of(positions_client)

    assert "option_contracts" in stub_provider.calls
    assert {underlying for underlying, _ in stub_provider.contract_queries} == set(
        UNDERLYING_PRICE
    )


def test_value_history_uses_the_contracts_multiplier_not_a_hundred(
    paper_broker: RecordedBroker, db_engine: Engine
) -> None:
    """An adjusted deliverable is why. A row priced at 100 when the contract
    says 50 states twice the money, and the risk manager sizes against it."""
    seed_fill(db_engine, "AAPL261218C00340000", at=AT - timedelta(days=2))
    provider = StubProvider(
        contracts=default_contracts(multiplier=Decimal(50)),
        bars={
            "AAPL261218C00340000": [
                daily_bar("AAPL261218C00340000", date(2026, 9, 9), Decimal("12.55")),
                daily_bar("AAPL261218C00340000", date(2026, 9, 10), Decimal("15.95")),
            ]
        },
    )
    with TestClient(
        build_app(broker=paper_broker, provider=provider, db_engine=db_engine)
    ) as client:
        aapl = by_id(positions_of(client), "AAPL261218C00340000")

    assert [point["value"] for point in aapl["valueHistory"]] == [627.5, 797.5]


def test_no_multiplier_means_no_value_history_rather_than_a_guess(
    paper_broker: RecordedBroker, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    seed_fill(db_engine, "AAPL261218C00340000", at=AT - timedelta(days=2))
    provider = StubProvider(
        contracts={},
        bars={
            "AAPL261218C00340000": [
                daily_bar("AAPL261218C00340000", date(2026, 9, 10), Decimal("15.95"))
            ]
        },
    )
    with TestClient(
        build_app(broker=paper_broker, provider=provider, db_engine=db_engine)
    ) as client:
        with caplog.at_level(logging.WARNING):
            aapl = by_id(positions_of(client), "AAPL261218C00340000")

    assert aapl["valueHistory"] == []
    assert any(
        getattr(r, "event", "") == "position_value_history_refused"
        for r in caplog.records
    )


# --------------------------------------------------------------------------
# Value history, whose entry date is the fill's
# --------------------------------------------------------------------------


def daily_bar(symbol: str, session: date, close: Decimal) -> Bar:
    return Bar(
        symbol=symbol,
        at=datetime(session.year, session.month, session.day, 4, tzinfo=timezone.utc),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1,
        trade_count=1,
        vwap=close,
    )


def seed_fill(
    engine: Engine,
    symbol: str,
    *,
    at: datetime,
    side: str = "buy",
    qty: int = 1,
    account: str = "paper",
    price: Decimal = Decimal("12.55"),
) -> None:
    with Session(engine) as session:
        session.add(
            Fill(
                account=account,
                activity_id=f"{at.strftime('%Y%m%d%H%M%S%f')[:17]}::{symbol}-{side}",
                order_id=None,
                group_id=None,
                symbol=symbol,
                side=side,
                position_intent=None,
                qty=qty,
                price=price,
                at=at,
            )
        )
        session.commit()


def test_value_history_starts_at_the_opening_fill_not_the_position(
    paper_broker: RecordedBroker, db_engine: Engine, stub_provider: StubProvider
) -> None:
    """A broker position carries **no open date at all**, so the entry date
    comes from the ``fill`` table or it does not exist."""
    entry = datetime(2026, 9, 8, 14, 30, tzinfo=timezone.utc)
    seed_fill(db_engine, "AAPL261218C00340000", at=entry)
    stub_provider.bars["AAPL261218C00340000"] = [
        daily_bar("AAPL261218C00340000", date(2026, 9, 8), Decimal("12.55")),
        daily_bar("AAPL261218C00340000", date(2026, 9, 9), Decimal("14.00")),
    ]
    with TestClient(
        build_app(broker=paper_broker, provider=stub_provider, db_engine=db_engine)
    ) as client:
        aapl = by_id(positions_of(client), "AAPL261218C00340000")

    assert [point["date"] for point in aapl["valueHistory"]] == [
        "2026-09-08",
        "2026-09-09",
    ]
    assert [point["value"] for point in aapl["valueHistory"]] == [1255.0, 1400.0]
    asked = {symbols: start for symbols, start in stub_provider.bar_queries}
    assert any(start == entry for start in asked.values())


def test_a_re_entered_symbol_measures_from_the_latest_entry(
    paper_broker: RecordedBroker, db_engine: Engine, stub_provider: StubProvider
) -> None:
    """A symbol bought, closed and bought again is a *new* position. Starting
    the series at the first purchase ever would chart a position that no
    longer existed by the middle of it."""
    seed_fill(db_engine, "AAPL261218C00340000", at=AT - timedelta(days=30))
    seed_fill(
        db_engine, "AAPL261218C00340000", at=AT - timedelta(days=29), side="sell"
    )
    reentry = AT - timedelta(days=2)
    seed_fill(db_engine, "AAPL261218C00340000", at=reentry)
    stub_provider.bars["AAPL261218C00340000"] = []
    with TestClient(
        build_app(broker=paper_broker, provider=stub_provider, db_engine=db_engine)
    ) as client:
        positions_of(client)

    assert any(start == reentry for _, start in stub_provider.bar_queries)


def test_no_fill_row_means_an_empty_series_rather_than_a_made_up_one(
    positions_client: TestClient,
) -> None:
    """Nothing has been ingested into ``fill`` here, which is the honest state
    of a fresh checkout: an empty series, not a series starting today."""
    rows = positions_of(positions_client)

    assert all(row["valueHistory"] == [] for row in rows)


def test_a_spread_charts_only_the_days_both_legs_priced(
    paper_broker: RecordedBroker, db_engine: Engine
) -> None:
    """A partial sum is not a position's value."""
    entry = AT - timedelta(days=3)
    seed_fill(db_engine, "AMD270115P00460000", at=entry)
    seed_fill(db_engine, "AMD270115P00470000", at=entry, side="sell_short")
    provider = StubProvider(
        bars={
            "AMD270115P00460000": [
                daily_bar("AMD270115P00460000", date(2026, 9, 9), Decimal("38.15")),
                daily_bar("AMD270115P00460000", date(2026, 9, 10), Decimal("33.40")),
            ],
            "AMD270115P00470000": [
                daily_bar("AMD270115P00470000", date(2026, 9, 10), Decimal("39.55")),
            ],
        }
    )
    with TestClient(
        build_app(broker=paper_broker, provider=provider, db_engine=db_engine)
    ) as client:
        amd = by_id(positions_of(client), AMD_ORDER)

    assert [point["date"] for point in amd["valueHistory"]] == ["2026-09-10"]
    assert amd["valueHistory"][0]["value"] == -615.0


def test_a_bar_outage_does_not_blank_the_book(
    paper_broker: RecordedBroker, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    seed_fill(db_engine, "AAPL261218C00340000", at=AT - timedelta(days=2))
    provider = StubProvider(fail_bars=True)
    with TestClient(
        build_app(broker=paper_broker, provider=provider, db_engine=db_engine)
    ) as client:
        with caplog.at_level(logging.WARNING):
            rows = positions_of(client)

    assert len(rows) == 9
    assert any(
        getattr(r, "event", "") == "position_bars_unavailable" for r in caplog.records
    )


def test_the_fill_lookup_is_scoped_to_the_book(
    paper_broker: RecordedBroker, db_engine: Engine, stub_provider: StubProvider
) -> None:
    """A cash fill must not date a paper position. Query keys on the client
    are account-scoped; the server has to be too."""
    seed_fill(
        db_engine, "AAPL261218C00340000", at=AT - timedelta(days=9), account="cash"
    )
    with TestClient(
        build_app(broker=paper_broker, provider=stub_provider, db_engine=db_engine)
    ) as client:
        aapl = by_id(positions_of(client), "AAPL261218C00340000")

    assert aapl["valueHistory"] == []
    assert stub_provider.bar_queries == []


# --------------------------------------------------------------------------
# Corollary's own fields, which are legitimately null
# --------------------------------------------------------------------------


def test_corollary_native_fields_are_null(positions_client: TestClient) -> None:
    """Nothing Corollary opened exists, so every position is *detached* rather
    than missing a strategy. Inventing an id that resolves to nothing is worse
    than an admitted absence."""
    for row in positions_of(positions_client):
        assert row["strategyId"] is None
        assert row["openedByStrategyId"] is None
        assert row["managedExit"] is None
        assert row["attachedExit"] is None


def test_expiry_is_a_date_not_an_instant(positions_client: TestClient) -> None:
    """A bare ``YYYY-MM-DD`` parses as UTC midnight on the client. Sending an
    instant re-opens the off-by-one where a Nov 21 expiry renders as Nov 20."""
    for row in positions_of(positions_client):
        assert len(row["expiry"]) == 10
        assert "T" not in row["expiry"]
        date.fromisoformat(row["expiry"])


def test_the_contract_label_names_the_structure(
    positions_client: TestClient,
) -> None:
    rows = positions_of(positions_client)

    assert by_id(rows, "AAPL261218C00340000")["contract"] == "$340 Call Dec 18"
    assert (
        by_id(rows, AMD_ORDER)["contract"]
        == "$470/$460 Put Credit Spread Jan 15"
    )


# --------------------------------------------------------------------------
# Account scoping
# --------------------------------------------------------------------------


def test_cash_is_refused_rather_than_served_from_paper(
    positions_client: TestClient,
) -> None:
    response = positions_client.get("/api/positions", params={"account": "cash"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "account_unavailable"


def test_the_requested_book_is_the_one_read(
    paper_broker: RecordedBroker,
    cash_broker: RecordedBroker,
    stub_provider: StubProvider,
    db_engine: Engine,
) -> None:
    app = build_app(
        broker=paper_broker,
        provider=stub_provider,
        db_engine=db_engine,
        cash=cash_broker,
    )
    with TestClient(app) as client:
        client.get("/api/positions", params={"account": "cash"})

    assert "positions" in cash_broker.calls
    assert paper_broker.calls == []


# --------------------------------------------------------------------------
# Declines are surfaced, never swallowed
# --------------------------------------------------------------------------


def _leg_intent(short: bool, closing: bool) -> PositionIntent:
    if closing:
        return (
            PositionIntent.BUY_TO_CLOSE if short else PositionIntent.SELL_TO_CLOSE
        )
    return PositionIntent.SELL_TO_OPEN if short else PositionIntent.BUY_TO_OPEN


def mleg_order(
    order_id: str,
    *symbols: str,
    net: Decimal = Decimal("-1.00"),
    shorts: tuple[int, ...] = (0,),
    status: str = "filled",
    order_type: str = "market",
    limit_price: Decimal | None = None,
    closing: tuple[int, ...] = (),
) -> Order:
    """A two-leg order proposing a group on ``symbols``.

    ``shorts`` names the leg indices opened short, ``closing`` the ones that
    close rather than open -- which is how a roll is built, and a roll is the
    shape that has no single action to report.
    """
    legs = tuple(
        Order(
            id=f"{order_id}-leg{index}",
            symbol=symbol,
            asset_class="us_option",
            order_class=OrderClass.SIMPLE,
            side=OrderSide.SELL if index in shorts else OrderSide.BUY,
            position_intent=_leg_intent(index in shorts, index in closing),
            order_type=order_type,
            time_in_force="day",
            status=status,
            quantity=Decimal(1),
            filled_quantity=Decimal(1),
            filled_avg_price=Decimal("1.00"),
            limit_price=limit_price,
            stop_price=None,
            ratio_qty=Decimal(1),
            created_at=AT,
            submitted_at=AT,
            filled_at=AT,
            canceled_at=None,
            expired_at=None,
            updated_at=AT,
            extended_hours=False,
        )
        for index, symbol in enumerate(symbols)
    )
    return Order(
        id=order_id,
        symbol="",
        asset_class="",
        order_class=OrderClass.MLEG,
        side=None,
        position_intent=None,
        order_type=order_type,
        time_in_force="day",
        status=status,
        quantity=Decimal(1),
        filled_quantity=Decimal(1),
        filled_avg_price=net,
        limit_price=limit_price,
        stop_price=None,
        ratio_qty=None,
        created_at=AT,
        submitted_at=AT,
        filled_at=AT,
        canceled_at=None,
        expired_at=None,
        updated_at=AT,
        extended_hours=False,
        legs=legs,
    )


class BrokerWithExtraOrders(RecordedBroker):
    """The recorded book, plus orders a recording cannot contain.

    The account has **no open orders at all**, so a working order has to be
    constructed. That is stated rather than hidden: these tests prove the
    mapping, and the first real working order is a reconciliation checkpoint.
    """

    def __init__(self, *, extra: Sequence[Order], label: str = "paper") -> None:
        super().__init__(label=label)
        self.extra = tuple(extra)

    async def orders(
        self,
        *,
        status: OrderQueryStatus = OrderQueryStatus.ALL,
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[Order]:
        recorded = await super().orders(
            status=status, after=after, until=until, limit=limit, symbols=symbols
        )
        return recorded + list(self.extra)


def test_a_partially_closed_spread_ungroups_and_is_logged(
    stub_provider: StubProvider, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """A spread with one leg gone no longer exists; its survivor falls back to
    an ungrouped row. Rule 8: the decline states its rule and its inputs."""
    broker = BrokerWithExtraOrders(
        extra=[mleg_order("ghost-order", "QQQ270115C00740000", "QQQ270115C00750000")]
    )
    with TestClient(
        build_app(broker=broker, provider=stub_provider, db_engine=db_engine)
    ) as client:
        with caplog.at_level(logging.INFO):
            rows = positions_of(client)

    assert by_id(rows, "QQQ270115C00740000")["legs"] == [
        {
            "symbol": "QQQ270115C00740000",
            "strike": 740.0,
            "right": "call",
            "side": "long",
            "ratio": 1,
        }
    ]
    declines = [
        r for r in caplog.records if getattr(r, "event", "") == "mleg_group_declined"
    ]
    assert any(getattr(r, "order_id", "") == "ghost-order" for r in declines)
    summary = next(
        r for r in caplog.records if getattr(r, "event", "") == "positions_assembled"
    )
    assert getattr(summary, "declined") == 1
    assert getattr(summary, "correlation_id")


# --------------------------------------------------------------------------
# Working orders
# --------------------------------------------------------------------------


def working_order(
    order_id: str,
    symbol: str,
    *,
    intent: PositionIntent = PositionIntent.BUY_TO_OPEN,
    order_type: str = "limit",
    status: str = "new",
    time_in_force: str = "day",
    limit_price: Decimal | None = Decimal("1.20"),
    stop_price: Decimal | None = None,
    quantity: Decimal | None = Decimal(1),
) -> Order:
    return Order(
        id=order_id,
        symbol=symbol,
        asset_class="us_option",
        order_class=OrderClass.SIMPLE,
        side=(
            OrderSide.BUY
            if intent
            in (PositionIntent.BUY_TO_OPEN, PositionIntent.BUY_TO_CLOSE)
            else OrderSide.SELL
        ),
        position_intent=intent,
        order_type=order_type,
        time_in_force=time_in_force,
        status=status,
        quantity=quantity,
        filled_quantity=Decimal(0),
        filled_avg_price=None,
        limit_price=limit_price,
        stop_price=stop_price,
        ratio_qty=None,
        created_at=AT,
        submitted_at=AT,
        filled_at=None,
        canceled_at=None,
        expired_at=None,
        updated_at=AT,
        extended_hours=False,
    )


def working_of(
    orders: Sequence[Order],
    provider: MarketDataProvider,
    db_engine: Engine,
) -> list[dict[str, Any]]:
    broker = BrokerWithExtraOrders(extra=orders)
    with TestClient(
        build_app(broker=broker, provider=provider, db_engine=db_engine)
    ) as client:
        response = client.get("/api/positions/working")

    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    return body


def test_the_recorded_account_has_no_working_orders(
    positions_client: TestClient,
) -> None:
    """All eleven recorded orders are ``filled``. A filled order rendered as
    working is a phantom resting in the market."""
    response = positions_client.get("/api/positions/working")

    assert response.status_code == 200
    assert response.json() == []


def test_an_opening_limit_order_rests_against_a_contract_key(
    stub_provider: StubProvider, db_engine: Engine
) -> None:
    rows = working_of(
        [working_order("w-1", "NVDA261218C00250000")], stub_provider, db_engine
    )

    assert len(rows) == 1
    assert rows[0]["id"] == "w-1"
    assert rows[0]["side"] == "BTO"
    assert rows[0]["orderType"] == "limit"
    assert rows[0]["contractKey"] == "NVDA261218C00250000"
    assert rows[0]["positionId"] is None
    assert rows[0]["limitPrice"] == 1.20
    assert rows[0]["timeInForce"] == "day"
    assert rows[0]["contract"] == "$250 Call Dec 18"
    assert rows[0]["quantity"] == 1


def test_a_closing_order_carries_the_logical_position_it_acts_on(
    stub_provider: StubProvider, db_engine: Engine
) -> None:
    """The AMD short leg belongs to the vertical, so a buy-to-close on it
    points at the **group**, not at the leg."""
    rows = working_of(
        [
            working_order(
                "w-2",
                "AMD270115P00470000",
                intent=PositionIntent.BUY_TO_CLOSE,
            )
        ],
        stub_provider,
        db_engine,
    )

    assert rows[0]["side"] == "BTC"
    assert rows[0]["positionId"] == AMD_ORDER
    assert rows[0]["contractKey"] is None


def test_exactly_one_of_position_id_and_contract_key_is_set(
    stub_provider: StubProvider, db_engine: Engine
) -> None:
    rows = working_of(
        [
            working_order("w-3", "NVDA261218C00250000"),
            working_order(
                "w-4", "AAPL261218C00340000", intent=PositionIntent.SELL_TO_CLOSE
            ),
        ],
        stub_provider,
        db_engine,
    )

    for row in rows:
        assert (row["positionId"] is None) != (row["contractKey"] is None)


def test_a_market_order_is_not_a_working_order(
    stub_provider: StubProvider, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """A market order fills; it does not sit and work. The contract's own type
    says so -- ``Exclude<OrderType, 'market'>``."""
    with caplog.at_level(logging.WARNING):
        rows = working_of(
            [working_order("w-5", "NVDA261218C00250000", order_type="market")],
            stub_provider,
            db_engine,
        )

    assert rows == []
    assert any(
        getattr(r, "event", "") == "working_order_unrepresentable"
        for r in caplog.records
    )


def test_an_order_with_no_stated_intent_is_refused_not_guessed(
    stub_provider: StubProvider, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """``buy`` is both buy-to-open and buy-to-close. Guessing is how a ledger
    books every close as a new lot; the same guess here would label an exit as
    an entry on the screen you check before intervening."""
    order = working_order("w-6", "NVDA261218C00250000")
    blind = Order(
        **{
            **{f: getattr(order, f) for f in order.__dataclass_fields__},
            "position_intent": None,
        }
    )
    with caplog.at_level(logging.WARNING):
        rows = working_of([blind], stub_provider, db_engine)

    assert rows == []
    record = next(
        r
        for r in caplog.records
        if getattr(r, "event", "") == "working_order_unrepresentable"
    )
    assert getattr(record, "order_id") == "w-6"
    assert getattr(record, "rule")


def test_a_filled_order_never_appears_as_working(
    stub_provider: StubProvider, db_engine: Engine
) -> None:
    rows = working_of(
        [working_order("w-7", "NVDA261218C00250000", status="filled")],
        stub_provider,
        db_engine,
    )

    assert rows == []


@pytest.mark.parametrize("status", ["canceled", "expired", "rejected", "replaced"])
def test_terminal_statuses_are_not_working(
    status: str, stub_provider: StubProvider, db_engine: Engine
) -> None:
    rows = working_of(
        [working_order("w-8", "NVDA261218C00250000", status=status)],
        stub_provider,
        db_engine,
    )

    assert rows == []


@pytest.mark.parametrize(
    "status", ["new", "accepted", "partially_filled", "pending_new", "held"]
)
def test_live_statuses_are_working(
    status: str, stub_provider: StubProvider, db_engine: Engine
) -> None:
    rows = working_of(
        [working_order("w-9", "NVDA261218C00250000", status=status)],
        stub_provider,
        db_engine,
    )

    assert [row["id"] for row in rows] == ["w-9"]


def test_a_stop_limit_order_carries_both_prices(
    stub_provider: StubProvider, db_engine: Engine
) -> None:
    rows = working_of(
        [
            working_order(
                "w-10",
                "AAPL261218C00340000",
                intent=PositionIntent.SELL_TO_CLOSE,
                order_type="stop_limit",
                limit_price=Decimal("10.00"),
                stop_price=Decimal("10.50"),
                time_in_force="gtc",
            )
        ],
        stub_provider,
        db_engine,
    )

    assert rows[0]["orderType"] == "stop_limit"
    assert rows[0]["limitPrice"] == 10.0
    assert rows[0]["stopPrice"] == 10.5
    assert rows[0]["timeInForce"] == "gtc"
    assert rows[0]["side"] == "STC"


def test_working_orders_read_the_requested_book(
    cash_broker: RecordedBroker, stub_provider: StubProvider, db_engine: Engine
) -> None:
    paper = RecordedBroker(label="paper")
    app = build_app(
        broker=paper, provider=stub_provider, db_engine=db_engine, cash=cash_broker
    )
    with TestClient(app) as client:
        client.get("/api/positions/working", params={"account": "cash"})

    assert "orders" in cash_broker.calls
    assert paper.calls == []


def test_working_orders_do_not_touch_market_data(
    stub_provider: StubProvider, db_engine: Engine
) -> None:
    """A resting order has no mark, no spread and no chart. Asking for one
    would spend two rate-limit buckets to display nothing."""
    working_of([working_order("w-11", "NVDA261218C00250000")], stub_provider, db_engine)

    assert stub_provider.calls == []


def test_a_working_order_carries_an_activity_handle(
    stub_provider: StubProvider, db_engine: Engine
) -> None:
    """Alpaca writes an activity row on a **fill**, so an unfilled order has
    none. The order's own id is the handle the pending row would key on -- a
    real id for the same event, not an invented one."""
    rows = working_of(
        [working_order("w-12", "NVDA261218C00250000")], stub_provider, db_engine
    )

    assert rows[0]["activityId"] == "w-12"


def test_a_contract_terms_outage_still_serves_the_book(
    paper_broker: RecordedBroker, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Losing the contracts endpoint loses the *multiplier*, not the book.

    The consequence is stated rather than papered over: no value series, and
    never a series computed against an assumed 100.
    """
    seed_fill(db_engine, "AAPL261218C00340000", at=AT - timedelta(days=2))
    provider = StubProvider(fail_contracts=True)
    with TestClient(
        build_app(broker=paper_broker, provider=provider, db_engine=db_engine)
    ) as client:
        with caplog.at_level(logging.WARNING):
            rows = positions_of(client)

    assert len(rows) == 9
    assert all(row["valueHistory"] == [] for row in rows)
    assert any(
        getattr(r, "event", "") == "position_contract_terms_unavailable"
        for r in caplog.records
    )


def test_a_costless_structure_reads_direction_from_the_signed_basis(
    stub_provider: StubProvider, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """``group_positions`` answers ``None`` for a net price of exactly zero --
    a costless structure is neither a debit nor a credit. The wire type has no
    third value, so the fallback is the broker's own signed cost basis, and it
    is logged where it is used rather than assumed."""
    broker = BrokerWithExtraOrders(
        extra=[
            mleg_order(
                "costless",
                "NVDA260911P00230000",
                "NVDA260911C00240000",
                net=Decimal(0),
            )
        ]
    )
    with TestClient(
        build_app(broker=broker, provider=stub_provider, db_engine=db_engine)
    ) as client:
        with caplog.at_level(logging.INFO):
            row = by_id(positions_of(client), "costless")

    assert row["costBasis"] == -1153.0
    assert row["direction"] == "short"
    record = next(
        r
        for r in caplog.records
        if getattr(r, "event", "") == "position_direction_from_basis"
    )
    assert getattr(record, "position_id") == "costless"


def test_an_mleg_working_order_reads_its_action_from_legs_and_price(
    stub_provider: StubProvider, db_engine: Engine
) -> None:
    """An ``mleg`` parent carries no intent of its own -- it is the structure,
    the legs are the instruments -- so the action is the legs' intents plus
    the **sign of the limit price**: negative is a credit."""
    rows = working_of(
        [
            mleg_order(
                "w-mleg",
                "NVDA261218P00215000",
                "NVDA261218P00210000",
                status="new",
                order_type="limit",
                limit_price=Decimal("-1.40"),
            )
        ],
        stub_provider,
        db_engine,
    )

    assert rows[0]["side"] == "STO"
    assert rows[0]["contract"] == "$215/$210 Put Credit Spread Dec 18"
    assert rows[0]["limitPrice"] == -1.40
    assert rows[0]["positionId"] is None


def test_a_roll_has_no_single_action_and_is_refused(
    stub_provider: StubProvider, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """One leg opening and one closing is two actions in one order. PRD §8.2
    defers rolling; reporting it as either half would be a wrong label on the
    screen somebody checks before intervening."""
    with caplog.at_level(logging.WARNING):
        rows = working_of(
            [
                mleg_order(
                    "w-roll",
                    "NVDA261218P00215000",
                    "NVDA261218P00210000",
                    status="new",
                    order_type="limit",
                    limit_price=Decimal("-1.40"),
                    closing=(1,),
                )
            ],
            stub_provider,
            db_engine,
        )

    assert rows == []
    assert any(
        getattr(r, "event", "") == "working_order_unrepresentable"
        for r in caplog.records
    )


def test_an_unrecognised_status_is_reported_rather_than_assumed_live(
    stub_provider: StubProvider, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        rows = working_of(
            [working_order("w-odd", "NVDA261218C00250000", status="teleported")],
            stub_provider,
            db_engine,
        )

    assert rows == []
    record = next(
        r
        for r in caplog.records
        if getattr(r, "event", "") == "working_order_status_unknown"
    )
    assert getattr(record, "status") == "teleported"
