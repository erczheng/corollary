"""The account routes: balances, the equity curve, and cash movements.

Three things here are worth more than the rest of the file put together,
because each one is a number that would look right and be wrong.

**``position_market_value`` is gross, not net.** PRD §8.6 defines total equity
as *"cash + the market value of open positions"*, and the design spec names
``position_market_value`` as the broker's own version of that second term.
Measured against this account it is not: ``96886.08 + 20824`` is
``117710.08`` and the broker says equity is ``99728.08``. The term that
reconciles is ``long_market_value + short_market_value`` -- ``11833 + (-8991)
= 2842`` -- and ``96886.08 + 2842`` is ``99728.08`` exactly.
``position_market_value`` is ``|11833| + |8991| = 20824``, the *gross*
exposure. Using it as the equity term overstates equity by twice the short
market value, and on a book with no shorts the error is zero, so nothing would
have surfaced it until the first credit spread.

**The margin note is read from ``multiplier``, never asserted.** PRD §8.6 said
*"Paper is a margin account at 2× cash"*; this account reports ``4``. Wrong in
the direction that overstates capacity, which is the direction that matters.

**A ``description`` is vendor free text with an account number in it.** Rule
6's protection is written about field *names* and cannot see inside prose, so
the rule here is structural: no ``description`` reaches the wire, and nothing
logs one without :func:`corollary.wire.vendor_detail` first.
"""

import logging
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.api.app import create_app
from corollary.api.deps import AccountMode, ServiceRegistry
from corollary.api.routes import account as account_routes
from corollary.api.schemas import AccountResponse, AccountSnapshot
from corollary.data.providers.interface import MarketDataProvider
from corollary.db.models import ENGINE_STATE_ID, EngineState
from corollary.engine.execution.interface import (
    Account,
    Activity,
    ActivityCategory,
    BrokerAccount,
    BrokerError,
    BrokerPosition,
    EquityPoint,
    NonTradeActivity,
    Order,
    OrderQueryStatus,
    PortfolioHistory,
)

from .conftest import TEST_CREDENTIALS, RecordedBroker

ACCOUNT_MODULE = (
    Path(__file__).resolve().parents[2] / "corollary" / "api" / "routes" / "account.py"
)

#: An account number that has never existed, shaped like one Alpaca would
#: issue so :func:`corollary.wire.vendor_detail` recognises it. Rule 6: no real
#: identifier in a fixture, a test or a docstring -- and the real one does not
#: appear in the recordings either.
FAKE_ACCOUNT_NUMBER = "PAEXAMPLE000"


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def _no_provider() -> MarketDataProvider:
    """The account routes must never reach the market-data provider.

    Balances, the equity curve and cash movements all come from the trading
    host, which carries its own 200/min bucket. A route that quietly pulled a
    quote would spend the *other* bucket to answer a question nobody asked
    about prices.
    """
    raise AssertionError("the account routes must not reach the market data provider")


class StubBroker(BrokerAccount):
    """A ``BrokerAccount`` whose three answered calls are set by the test.

    :class:`~tests.api.conftest.RecordedBroker` serves the real recordings and
    is what proves the shapes; this one exists for the responses Alpaca has
    never sent us -- a gap in the equity curve, a withdrawal, a journal with no
    amount. Both are needed: the recordings prove the parse, and these prove
    the refusals.
    """

    def __init__(
        self,
        *,
        account_obj: Account | None = None,
        history: PortfolioHistory | None = None,
        activity_rows: Sequence[Activity] = (),
    ) -> None:
        self._account = account_obj
        self._history = history
        self._activities = list(activity_rows)
        self.activity_calls: list[dict[str, Any]] = []
        self.history_calls: list[dict[str, str]] = []

    async def account(self) -> Account:
        assert self._account is not None, "this test did not supply an account"
        return self._account

    async def positions(self) -> list[BrokerPosition]:
        raise AssertionError("the account routes must not read positions")

    async def orders(
        self,
        *,
        status: OrderQueryStatus = OrderQueryStatus.ALL,
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[Order]:
        raise AssertionError("the account routes must not read orders")

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
        self.activity_calls.append(
            {"types": tuple(types) if types is not None else None, "category": category}
        )
        return list(self._activities)

    async def portfolio_history(
        self, *, period: str = "1M", timeframe: str = "1D"
    ) -> PortfolioHistory:
        self.history_calls.append({"period": period, "timeframe": timeframe})
        assert self._history is not None, "this test did not supply a history"
        return self._history


def build(broker: BrokerAccount, db_engine: Engine, *, live_keys: bool = False) -> FastAPI:
    brokers: dict[AccountMode, Callable[[], BrokerAccount]] = {
        AccountMode.PAPER: lambda: broker
    }
    missing: tuple[str, ...] = ()
    if live_keys:
        brokers[AccountMode.CASH] = lambda: broker
    else:
        missing = ("ALPACA_LIVE_API_KEY", "ALPACA_LIVE_SECRET_KEY")
    registry = ServiceRegistry(
        brokers=brokers, provider=_no_provider, missing_live_credentials=missing
    )
    app = create_app(registry=registry, db_engine=db_engine)
    app.include_router(account_routes.router)
    return app


@pytest.fixture
def account_client(app: FastAPI) -> Iterator[TestClient]:
    """The shared fixtures' app, with this dispatch's router mounted.

    ``api/app.py`` belongs to the orchestrator, so the router is included here
    rather than there. Everything else -- the recorded broker, the SQLite file,
    the absent live keys -- is exactly what the shipped app gets.
    """
    app.include_router(account_routes.router)
    with TestClient(app) as client:
        yield client


def stub_account(**overrides: Any) -> Account:
    """This account's real numbers, with one field moved when a test needs it."""
    fields: dict[str, Any] = {
        "status": "ACTIVE",
        "currency": "USD",
        "created_at": datetime(2026, 8, 5, 3, 44, 56, tzinfo=timezone.utc),
        "cash": Decimal("96886.08"),
        "equity": Decimal("99728.08"),
        "last_equity": Decimal("99839.08"),
        "portfolio_value": Decimal("99728.08"),
        "buying_power": Decimal("279544.32"),
        "options_buying_power": Decimal("69886.08"),
        "effective_buying_power": Decimal("279544.32"),
        "non_marginable_buying_power": Decimal("69886.08"),
        "regt_buying_power": Decimal("139772.16"),
        "long_market_value": Decimal("11833"),
        "short_market_value": Decimal("-8991"),
        "position_market_value": Decimal("20824"),
        "initial_margin": Decimal("27000"),
        "maintenance_margin": Decimal("27000"),
        "last_maintenance_margin": Decimal("187059"),
        "sma": Decimal("78462"),
        "accrued_fees": Decimal("0"),
        "pending_reg_taf_fees": Decimal("0"),
        "intraday_adjustments": Decimal("0"),
        "multiplier": Decimal("4"),
        "options_approved_level": 3,
        "options_trading_level": 3,
        "balance_asof": None,
        "trading_blocked": False,
        "account_blocked": False,
        "transfers_blocked": False,
        "trade_suspended_by_user": False,
        "shorting_enabled": True,
    }
    fields.update(overrides)
    return Account(**fields)


def cash_row(
    *,
    identifier: str = "20260805000000000::4b47d1d0-f34f-51b3-aa09-1fb43a0eae70",
    activity_type: str = "JNLC",
    net_amount: Decimal | None = Decimal("100000"),
    created_at: datetime | None = datetime(2026, 8, 5, 3, 48, 40, tzinfo=timezone.utc),
    status: str | None = "executed",
    description: str | None = "",
) -> NonTradeActivity:
    return NonTradeActivity(
        id=identifier,
        activity_type=activity_type,
        created_at=created_at,
        net_amount=net_amount,
        status=status,
        currency="USD",
        description=description,
        extra=MappingProxyType({}),
    )


def refusals(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if getattr(record, "event", "") == event]


# --------------------------------------------------------------------------
# GET /api/account -- the real numbers
# --------------------------------------------------------------------------


def test_the_snapshot_reports_this_accounts_real_numbers(
    account_client: TestClient,
) -> None:
    body = account_client.get("/api/account").json()

    assert body["cash"] == 96886.08
    assert body["equity"] == 99728.08
    assert body["optionsBuyingPower"] == 69886.08
    assert body["buyingPower"] == 279544.32


def test_money_crosses_the_boundary_as_a_json_number(
    account_client: TestClient,
) -> None:
    """``Decimal`` everywhere computed, a JSON number at the display boundary.

    A string here would make every ``format.ts`` helper wrong; a string *test*
    would hide it.
    """
    body = account_client.get("/api/account").json()

    assert isinstance(body["cash"], float)
    assert isinstance(body["equity"], float)


def test_the_options_level_is_read_from_the_account_object(
    account_client: TestClient,
) -> None:
    """Both levels arrive as JSON **integers** while every money field is a
    string, so a parser that maps the whole object through ``Decimal(str)``
    breaks on exactly these two. Level 3 is confirmed rather than hardcoded.
    """
    body = account_client.get("/api/account").json()

    assert body["optionsApprovedLevel"] == 3
    assert body["optionsTradingLevel"] == 3
    assert isinstance(body["optionsApprovedLevel"], int)
    assert isinstance(body["optionsTradingLevel"], int)


# --------------------------------------------------------------------------
# The margin class, which the PRD got wrong in prose
# --------------------------------------------------------------------------


def test_the_margin_class_comes_from_the_multiplier(
    account_client: TestClient,
) -> None:
    body = account_client.get("/api/account").json()

    assert body["margin"]["multiplier"] == 4
    assert body["margin"]["marginClass"] == "pdt"


def test_the_margin_note_says_what_it_found_rather_than_2x(
    account_client: TestClient,
) -> None:
    """PRD §8.6 asserted 2×. The account says 4, and the page must say 4.

    The failure this pins is the one that overstates capacity: a sentence
    quoting 2× beside a 4× account understates the leverage in use by half.
    """
    margin = account_client.get("/api/account").json()["margin"]

    assert "4" in margin["note"]
    assert "2×" not in margin["note"]
    assert "2x" not in margin["note"].lower()


def test_the_note_points_at_options_buying_power_not_the_margin_figure(
    account_client: TestClient,
) -> None:
    """Options are not marginable, so the margin figure never binds them."""
    margin = account_client.get("/api/account").json()["margin"]

    assert "options buying power" in margin["note"].lower()
    assert "not marginable" in margin["note"].lower()


@pytest.mark.parametrize(
    ("multiplier", "expected"),
    [("1", "cash"), ("2", "reg_t"), ("4", "pdt"), ("3", "unknown")],
)
def test_every_multiplier_maps_to_its_own_class(
    db_engine: Engine, multiplier: str, expected: str
) -> None:
    broker = StubBroker(account_obj=stub_account(multiplier=Decimal(multiplier)))

    with TestClient(build(broker, db_engine)) as client:
        body = client.get("/api/account").json()

    assert body["margin"]["marginClass"] == expected
    assert multiplier in body["margin"]["note"]


def test_an_unknown_multiplier_is_logged_rather_than_read_as_one_of_the_three(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """A wrong margin class is a wrong sizing ceiling. Rule 8 applies."""
    broker = StubBroker(account_obj=stub_account(multiplier=Decimal("3")))

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.account"):
        with TestClient(build(broker, db_engine)) as client:
            client.get("/api/account")

    records = refusals(caplog, "account_margin_class_unknown")
    assert records, "an unrecognised multiplier must leave a record"
    assert getattr(records[0], "rule", None)
    assert getattr(records[0], "at", None)
    assert getattr(records[0], "multiplier", None) == "3"


def test_an_absent_options_buying_power_is_stated_not_substituted(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    broker = StubBroker(account_obj=stub_account(options_buying_power=None))

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.account"):
        with TestClient(build(broker, db_engine)) as client:
            body = client.get("/api/account").json()

    assert body["optionsBuyingPower"] is None
    assert "did not report" in body["margin"]["note"]
    assert refusals(caplog, "account_options_buying_power_absent")


# --------------------------------------------------------------------------
# Equity, and the two sources that must agree
# --------------------------------------------------------------------------


def test_equity_reconciles_against_cash_plus_the_net_position_value(
    account_client: TestClient,
) -> None:
    body = account_client.get("/api/account").json()

    assert body["netPositionValue"] == 2842.0
    assert body["derivedEquity"] == 99728.08
    assert body["derivedEquity"] == body["equity"]
    assert body["equityReconciles"] is True
    assert body["equityDifference"] == 0.0


def test_the_brokers_position_market_value_is_gross_and_is_not_the_equity_term(
    account_client: TestClient,
) -> None:
    """The trap, pinned with this account's own numbers.

    ``position_market_value`` is ``|long| + |short|``. Substituted for the net
    term it overstates equity by twice the short market value -- here by
    ``17982`` on a ``99728.08`` account -- and on a book holding no shorts the
    error is exactly zero, so nothing surfaces it until the first credit
    spread.
    """
    body = account_client.get("/api/account").json()

    assert body["grossPositionValue"] == 20824.0
    assert body["longMarketValue"] == 11833.0
    assert body["shortMarketValue"] == -8991.0
    assert body["grossPositionValue"] != body["netPositionValue"]
    assert body["cash"] + body["grossPositionValue"] != body["equity"]


def test_a_book_that_does_not_reconcile_says_so_and_logs_it(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    broker = StubBroker(account_obj=stub_account(equity=Decimal("99000")))

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.account"):
        with TestClient(build(broker, db_engine)) as client:
            body = client.get("/api/account").json()

    assert body["equityReconciles"] is False
    assert body["equityDifference"] == pytest.approx(728.08)
    assert refusals(caplog, "account_equity_mismatch")


def test_the_day_change_comes_from_last_equity(account_client: TestClient) -> None:
    body = account_client.get("/api/account").json()

    assert body["lastEquity"] == 99839.08
    assert body["dayChange"] == pytest.approx(-111.0)
    assert body["balanceTrend"]["changePct"] == pytest.approx(-0.1112, abs=1e-4)
    assert "previous close" in body["balanceTrend"]["comparedTo"]


def test_a_zero_previous_close_has_no_trend_rather_than_a_wrong_one(
    db_engine: Engine,
) -> None:
    """A brand-new account divides by zero. ``None`` says unknown; 0% lies."""
    broker = StubBroker(account_obj=stub_account(last_equity=Decimal("0")))

    with TestClient(build(broker, db_engine)) as client:
        body = client.get("/api/account").json()

    assert body["balanceTrend"] is None
    assert body["dayChange"] == 99728.08


# --------------------------------------------------------------------------
# Decision 9 -- there is no settlement breakdown, and none may reappear
# --------------------------------------------------------------------------


def test_there_is_no_settled_or_unsettled_cash_anywhere_in_the_response(
    account_client: TestClient,
) -> None:
    body = account_client.get("/api/account").json()

    assert not [key for key in body if "settle" in key.lower()]
    assert "cash" in body


# --------------------------------------------------------------------------
# Rule 5 -- paper by default, cash never faked
# --------------------------------------------------------------------------


def test_the_snapshot_is_paper_unless_asked_otherwise(
    account_client: TestClient, paper_broker: RecordedBroker
) -> None:
    body = account_client.get("/api/account").json()

    assert body["account"] == "paper"
    assert "account" in paper_broker.calls


def test_cash_without_credentials_is_a_409_and_paper_is_never_substituted(
    account_client: TestClient, paper_broker: RecordedBroker
) -> None:
    response = account_client.get("/api/account?account=cash")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "account_unavailable"
    assert paper_broker.calls == []


def test_the_snapshot_says_why_cash_is_unavailable(
    account_client: TestClient,
) -> None:
    """Rule 5's toggle disables *with a stated reason*, computed server-side."""
    body = account_client.get("/api/account").json()

    assert body["cashAccountAvailable"] is False
    assert body["missingLiveCredentialEnvVars"] == [
        "ALPACA_LIVE_API_KEY",
        "ALPACA_LIVE_SECRET_KEY",
    ]
    reason = body["cashAccountUnavailableReason"]
    assert "ALPACA_LIVE_API_KEY" in reason
    assert "ALPACA_LIVE_SECRET_KEY" in reason


def test_cash_is_available_once_the_live_keys_are_set(db_engine: Engine) -> None:
    broker = StubBroker(account_obj=stub_account())

    with TestClient(build(broker, db_engine, live_keys=True)) as client:
        body = client.get("/api/account").json()

    assert body["cashAccountAvailable"] is True
    assert body["cashAccountUnavailableReason"] is None
    assert body["missingLiveCredentialEnvVars"] == []


def test_the_cash_book_is_reported_as_cash_when_it_is_configured(
    db_engine: Engine,
) -> None:
    broker = StubBroker(account_obj=stub_account())

    with TestClient(build(broker, db_engine, live_keys=True)) as client:
        body = client.get("/api/account?account=cash").json()

    assert body["account"] == "cash"


# --------------------------------------------------------------------------
# Rule 6 -- nothing identifying leaves the process
# --------------------------------------------------------------------------


def test_no_credential_and_no_account_identifier_reach_the_wire(
    account_client: TestClient,
) -> None:
    text = account_client.get("/api/account").text

    assert TEST_CREDENTIALS.key_id not in text
    assert TEST_CREDENTIALS.secret_key not in text
    assert "account_number" not in text
    assert "accountNumber" not in text
    # The recorded object's own `id`, which `Account` deliberately never models.
    assert "d66d53bc" not in text


def test_a_broker_failure_is_a_stated_condition(db_engine: Engine) -> None:
    broker = RecordedBroker(label="paper", fail_with=BrokerError("upstream is down"))

    with TestClient(build(broker, db_engine)) as client:
        response = client.get("/api/account")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "broker_unavailable"


def test_the_module_does_not_import_the_vendor_sdk() -> None:
    """``alpaca`` lives in two files and this is not one of them."""
    source = ACCOUNT_MODULE.read_text(encoding="utf-8")

    assert "import alpaca" not in source
    assert "from alpaca" not in source


# --------------------------------------------------------------------------
# GET /api/account/history -- decision 6
# --------------------------------------------------------------------------


def test_the_curve_is_the_brokers_own_record(account_client: TestClient) -> None:
    body = account_client.get("/api/account/history").json()

    assert body["timeframe"] == "1D"
    assert body["baseValue"] == 100000.0
    assert body["baseValueAsof"] == "2026-08-10"
    assert len(body["points"]) == 22
    assert body["points"][0]["at"].startswith("2026-08-12T00:00:00")
    assert body["points"][0]["equity"] == 100000.0
    assert body["points"][-1]["equity"] == 99839.08
    assert body["points"][-1]["profitLoss"] == -160.92


def test_a_gap_in_the_curve_stays_a_gap(db_engine: Engine) -> None:
    """``None`` coerced to zero draws a line to the axis and asserts the
    account was worth nothing that day."""
    history = PortfolioHistory(
        timeframe="1D",
        base_value=Decimal("100000"),
        base_value_asof=None,
        points=(
            EquityPoint(
                at=datetime(2026, 9, 10, tzinfo=timezone.utc),
                equity=Decimal("100000"),
                profit_loss=Decimal("0"),
                profit_loss_pct=Decimal("0"),
            ),
            EquityPoint(
                at=datetime(2026, 9, 11, tzinfo=timezone.utc),
                equity=None,
                profit_loss=None,
                profit_loss_pct=None,
            ),
        ),
        cashflow=MappingProxyType({}),
    )
    broker = StubBroker(account_obj=stub_account(), history=history)

    with TestClient(build(broker, db_engine)) as client:
        body = client.get("/api/account/history").json()

    assert body["points"][1]["equity"] is None
    assert body["points"][1]["profitLoss"] is None


def test_t0_marks_where_corollary_started(
    account_client: TestClient, db_engine: Engine
) -> None:
    """Decision 6: the chart must not claim credit for manual trading.

    The whole recorded window predates this process's first start, so every
    point on it is somebody else's.
    """
    body = account_client.get("/api/account/history").json()

    assert body["t0"] is not None
    assert body["pointsBeforeT0"] == 22


def test_points_before_t0_counts_only_what_predates_it(
    account_client: TestClient, db_engine: Engine
) -> None:
    with Session(db_engine) as session:
        state = session.get(EngineState, ENGINE_STATE_ID)
        assert state is not None
        state.t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
        session.commit()

    body = account_client.get("/api/account/history").json()

    # The recording runs 2026-08-12 .. 2026-09-11; fourteen points fall before
    # September and eight on or after it.
    assert body["pointsBeforeT0"] == 14


def test_a_never_started_engine_claims_nothing(
    account_client: TestClient, db_engine: Engine
) -> None:
    """``t0`` absent means unknown, so the count is ``None`` rather than 0.

    Zero would say every point on the chart is Corollary's.
    """
    with Session(db_engine) as session:
        state = session.get(EngineState, ENGINE_STATE_ID)
        assert state is not None
        session.delete(state)
        session.commit()

    body = account_client.get("/api/account/history").json()

    assert body["t0"] is None
    assert body["pointsBeforeT0"] is None


def test_reading_the_history_never_writes_engine_state(
    account_client: TestClient, db_engine: Engine
) -> None:
    """A GET that recreates the singleton would resume nothing, but it would
    also mean the route can move ``t0`` -- and ``t0`` is written once, ever."""
    with Session(db_engine) as session:
        state = session.get(EngineState, ENGINE_STATE_ID)
        assert state is not None
        session.delete(state)
        session.commit()

    account_client.get("/api/account/history")

    with Session(db_engine) as session:
        assert session.get(EngineState, ENGINE_STATE_ID) is None


def test_the_window_is_passed_to_the_broker(db_engine: Engine) -> None:
    broker = StubBroker(
        account_obj=stub_account(),
        history=PortfolioHistory(
            timeframe="1D",
            base_value=None,
            base_value_asof=None,
            points=(),
            cashflow=MappingProxyType({}),
        ),
    )

    with TestClient(build(broker, db_engine)) as client:
        body = client.get("/api/account/history?period=3M&timeframe=1H").json()

    assert broker.history_calls == [{"period": "3M", "timeframe": "1H"}]
    assert body["period"] == "3M"


@pytest.mark.parametrize("query", ["timeframe=7Min", "period=1Y", "period=", "period=M1"])
def test_an_unsupported_window_is_refused_and_logged(
    account_client: TestClient, caplog: pytest.LogCaptureFixture, query: str
) -> None:
    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.account"):
        response = account_client.get(f"/api/account/history?{query}")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_history_window"
    records = refusals(caplog, "account_history_window_rejected")
    assert records
    assert getattr(records[0], "rule", None)
    assert getattr(records[0], "at", None)


def test_the_history_refuses_cash_without_credentials(
    account_client: TestClient,
) -> None:
    assert account_client.get("/api/account/history?account=cash").status_code == 409


# --------------------------------------------------------------------------
# GET /api/account/transfers -- deposits and withdrawals are not orders
# --------------------------------------------------------------------------


def test_the_only_cash_movement_on_this_account_is_its_funding_journal(
    account_client: TestClient,
) -> None:
    """The recording holds 14 fills and 19 fees alongside one ``JNLC``."""
    body = account_client.get("/api/account/transfers").json()

    assert body["total"] == 1
    (item,) = body["items"]
    assert item["action"] == "DEPOSIT"
    assert item["amount"] == 100000.0
    assert item["status"] == "filled"
    assert item["price"] is None
    assert item["quantity"] is None
    assert item["pnl"] is None
    assert item["pnlPct"] is None


def test_a_fill_is_never_a_transfer(account_client: TestClient) -> None:
    body = account_client.get("/api/account/transfers").json()

    assert {item["action"] for item in body["items"]} <= {"DEPOSIT", "WITHDRAWAL"}


def test_the_vendor_filter_is_requested_and_still_not_trusted(
    db_engine: Engine,
) -> None:
    """``activity_types`` narrows the fetch; the local filter is what is
    correct. A vendor that widened the filter would otherwise put a fee in a
    column of deposits."""
    broker = StubBroker(
        account_obj=stub_account(),
        activity_rows=[
            cash_row(),
            cash_row(
                identifier="20260910000000000::fee",
                activity_type="FEE",
                net_amount=Decimal("-0.03"),
                description="OCC Clearing Fee",
            ),
        ],
    )

    with TestClient(build(broker, db_engine)) as client:
        body = client.get("/api/account/transfers").json()

    assert broker.activity_calls[0]["types"] == ("TRANS", "CSD", "CSW", "JNLC")
    assert broker.activity_calls[0]["category"] is None
    assert body["total"] == 1


def test_a_negative_movement_is_a_withdrawal(db_engine: Engine) -> None:
    """``amount`` is signed and the action follows the sign, not the other way
    round -- re-deriving one from the other is a second source of truth about
    which way the money went."""
    broker = StubBroker(
        account_obj=stub_account(),
        activity_rows=[
            cash_row(
                identifier="20260906000000000::csw",
                activity_type="CSW",
                net_amount=Decimal("-1250"),
            )
        ],
    )

    with TestClient(build(broker, db_engine)) as client:
        (item,) = client.get("/api/account/transfers").json()["items"]

    assert item["action"] == "WITHDRAWAL"
    assert item["amount"] == -1250.0


def test_a_movement_with_no_amount_is_dropped_and_logged(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    broker = StubBroker(
        account_obj=stub_account(),
        activity_rows=[cash_row(identifier="20260906000000000::x", net_amount=None)],
    )

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.account"):
        with TestClient(build(broker, db_engine)) as client:
            body = client.get("/api/account/transfers").json()

    assert body["total"] == 0
    records = refusals(caplog, "transfer_rejected")
    assert records
    assert getattr(records[0], "rule", None)
    assert getattr(records[0], "at", None)
    assert getattr(records[0], "activity_id", None) == "20260906000000000::x"


def test_a_zero_amount_movement_has_no_direction_and_is_dropped(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    broker = StubBroker(
        account_obj=stub_account(),
        activity_rows=[cash_row(net_amount=Decimal("0"))],
    )

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.account"):
        with TestClient(build(broker, db_engine)) as client:
            body = client.get("/api/account/transfers").json()

    assert body["total"] == 0
    assert refusals(caplog, "transfer_rejected")


def test_a_description_never_reaches_the_wire(db_engine: Engine) -> None:
    """Alpaca embeds the account number in free text, where a rule written
    about field names cannot see it. The field is simply never serialized."""
    broker = StubBroker(
        account_obj=stub_account(),
        activity_rows=[
            cash_row(
                description=(
                    "CAT fee for proceed of 15 trades on 2026-09-10 by "
                    f"{FAKE_ACCOUNT_NUMBER}"
                )
            )
        ],
    )

    with TestClient(build(broker, db_engine)) as client:
        text = client.get("/api/account/transfers").text

    assert FAKE_ACCOUNT_NUMBER not in text
    assert "description" not in text


def test_a_logged_description_is_redacted(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    broker = StubBroker(
        account_obj=stub_account(),
        activity_rows=[
            cash_row(
                net_amount=None,
                description=f"journal reversed by {FAKE_ACCOUNT_NUMBER}",
            )
        ],
    )

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.account"):
        with TestClient(build(broker, db_engine)) as client:
            client.get("/api/account/transfers")

    (record,) = refusals(caplog, "transfer_rejected")
    logged = getattr(record, "description", "")
    assert FAKE_ACCOUNT_NUMBER not in logged
    assert "<redacted>" in logged


def test_an_unrecognised_status_is_pending_and_logged(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    broker = StubBroker(
        account_obj=stub_account(), activity_rows=[cash_row(status="reversing")]
    )

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.account"):
        with TestClient(build(broker, db_engine)) as client:
            (item,) = client.get("/api/account/transfers").json()["items"]

    assert item["status"] == "pending"
    assert refusals(caplog, "transfer_status_unknown")


def test_a_missing_timestamp_falls_back_to_the_settlement_date(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """A transfer you cannot see is worse than one dated to its own settlement
    date, so the row survives -- and the substitution is recorded."""
    from datetime import date

    row = NonTradeActivity(
        id="20260805000000000::nodate",
        activity_type="CSD",
        created_at=None,
        activity_date=date(2026, 8, 5),
        net_amount=Decimal("500"),
        status="executed",
    )
    broker = StubBroker(account_obj=stub_account(), activity_rows=[row])

    with caplog.at_level(logging.INFO, logger="corollary.api.routes.account"):
        with TestClient(build(broker, db_engine)) as client:
            (item,) = client.get("/api/account/transfers").json()["items"]

    assert item["time"].startswith("2026-08-05T00:00:00")
    assert refusals(caplog, "transfer_time_from_settlement_date")


def test_a_movement_with_no_time_at_all_is_dropped(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    broker = StubBroker(
        account_obj=stub_account(), activity_rows=[cash_row(created_at=None)]
    )

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.account"):
        with TestClient(build(broker, db_engine)) as client:
            body = client.get("/api/account/transfers").json()

    assert body["total"] == 0
    assert refusals(caplog, "transfer_rejected")


def test_transfers_are_newest_first_and_deterministic(db_engine: Engine) -> None:
    rows = [
        cash_row(
            identifier=f"2026090{day}000000000::row",
            activity_type="CSD",
            net_amount=Decimal("100"),
            created_at=datetime(2026, 9, day, tzinfo=timezone.utc),
        )
        for day in (1, 2, 3)
    ]
    broker = StubBroker(account_obj=stub_account(), activity_rows=rows)

    with TestClient(build(broker, db_engine)) as client:
        first = client.get("/api/account/transfers").json()
        second = client.get("/api/account/transfers").json()

    assert [item["time"][:10] for item in first["items"]] == [
        "2026-09-03",
        "2026-09-02",
        "2026-09-01",
    ]
    assert first == second


def test_transfers_paginate(db_engine: Engine) -> None:
    rows = [
        cash_row(
            identifier=f"202609{day:02d}000000000::row",
            activity_type="CSD",
            net_amount=Decimal("100"),
            created_at=datetime(2026, 9, day, tzinfo=timezone.utc),
        )
        for day in range(1, 6)
    ]
    broker = StubBroker(account_obj=stub_account(), activity_rows=rows)

    with TestClient(build(broker, db_engine)) as client:
        first = client.get("/api/account/transfers?page=0&pageSize=2").json()
        last = client.get("/api/account/transfers?page=2&pageSize=2").json()

    assert first["total"] == 5
    assert first["page"] == 0
    assert first["pageSize"] == 2
    assert first["hasMore"] is True
    assert len(first["items"]) == 2
    assert last["hasMore"] is False
    assert len(last["items"]) == 1


@pytest.mark.parametrize("query", ["page=-1", "pageSize=0", "pageSize=5000"])
def test_a_nonsense_page_is_a_422(account_client: TestClient, query: str) -> None:
    assert account_client.get(f"/api/account/transfers?{query}").status_code == 422


def test_transfers_refuse_cash_without_credentials(
    account_client: TestClient,
) -> None:
    assert account_client.get("/api/account/transfers?account=cash").status_code == 409


# --------------------------------------------------------------------------
# The contract, guarded against drift
# --------------------------------------------------------------------------


#: The two fields the account models deliberately type differently, and why.
#: Both are absences the route refuses to fill in:
#:
#: * ``options_buying_power`` is ``Decimal | None`` on the broker's own account
#:   object, so the route cannot promise a number -- and substituting one would
#:   overstate capacity in exactly the way the 2x sentence did.
#: * ``balance_trend`` has no percentage when the previous close was zero, which
#:   is what a brand-new account looks like. ``0%`` would claim a flat day.
#:
#: ``AccountSnapshot`` mirrors the frontend's declared shape and says both are
#: required; widening *that* belongs to the frontend dispatch, not this one.
DELIBERATE_WIDENING = frozenset({"options_buying_power", "balance_trend"})


def test_the_account_response_and_the_snapshot_agree_on_shared_fields() -> None:
    """``AccountSnapshot`` is the *store's* composite shape -- assembled on the
    client from four routes -- and this route serves the account-level half of
    it. Where the two name the same figure they must type it the same way, or
    one of them is quietly a different number.
    """
    shared = set(AccountResponse.model_fields) & set(AccountSnapshot.model_fields)

    assert {"cash", "buying_power", "options_buying_power"} <= shared
    for name in shared - DELIBERATE_WIDENING:
        assert (
            AccountResponse.model_fields[name].annotation
            == AccountSnapshot.model_fields[name].annotation
        ), name


@pytest.mark.parametrize("name", sorted(DELIBERATE_WIDENING))
def test_the_widened_fields_are_really_nullable(name: str) -> None:
    """Each widening is a real ``| None``, not a drift nobody noticed."""
    annotation = AccountResponse.model_fields[name].annotation

    assert annotation != AccountSnapshot.model_fields[name].annotation
    assert type(None) in getattr(annotation, "__args__", ())
