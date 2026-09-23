"""The account: balances, the equity curve, and cash movements.

Three routes, and five decisions that shape all of them.

**1. The margin figure is read, never asserted.** PRD §8.6 said *"Paper is a
margin account at 2× cash"* and the Account page rendered that sentence as a
constant. The live paper account reports ``multiplier: '4'`` -- a pattern
day-trader margin account -- so the sentence was wrong in the direction that
**overstates capacity**. :func:`_margin_summary` builds the words from the
number the broker actually sent, and the page renders them. The whole point of
§8.6's "the page says why" is that the figure be true.

The figure that actually binds an options trader is ``options_buying_power``,
never the margin figure, because **options are not marginable**. That sentence
is in every note this module produces, and when the broker sends no options
buying power the note says so rather than letting the margin figure stand in.

**2. Total equity has two sources and they must agree.** §8.6 defines it as
*"cash + the market value of open positions"*. The second term is
``long_market_value + short_market_value``. It is **not**
``position_market_value``, despite the name: on this account that field is
``20824`` while long plus short is ``2842``, because it reports the *gross*
exposure ``|11833| + |8991|``. Substituted for the net term it overstates
equity by twice the short market value -- and on a book holding no shorts the
error is exactly zero, so nothing would surface it until the first credit
spread. Both are served, named apart, and the reconciliation is computed and
reported rather than assumed.

**3. The equity curve is Alpaca's, with t₀ marked** -- decision 6. Reading the
broker's own record is not reconstruction; nothing is simulated. The ``t0``
marker is what preserves §8.1's *"no pre-Corollary reconstruction"*: this
chart sits beside a strategy win rate and must not claim credit for manual
trading that predates the engine. There is no ``equity_snapshot`` table and
there must not be one. Reading ``t0`` here is a **read** -- this module never
creates the singleton and never writes it, because ``t0`` is written once on
the first ever start and never rewritten.

**4. Deposits and withdrawals are not orders.** They arrive on the *non-trade*
activity branch, which is a different object shape with no ``side``, a
**signed** ``net_amount``, and ``symbol``/``qty`` documented as *"not present
for all activity types"*. The vendor is asked to narrow to the four cash
types; the answer is filtered again here, because a filter that lives only on
the vendor's side is a filter that puts a clearing fee in a column of deposits
the day the vendor widens it.

**5. A ``description`` is vendor free text with an account number inside it.**
A ``FEE`` row reads *"CAT fee for proceed of 15 trades on <date> by
PA…"*. Rule 6's protection is written about field *names* and cannot see
inside prose, so the rule here is structural: ``description`` is on no
response model this module serves, and nothing logs one without
:func:`corollary.wire.vendor_detail` first.

The vendor SDK is not imported here; ``alpaca`` lives in the two files
CLAUDE.md names and this is not one of them. Nothing here places, cancels or
replaces anything --
:class:`~corollary.engine.execution.interface.BrokerAccount` is the read half
of the vendor surface and ``BrokerExecution`` does not exist, so ``submit_order``
is not in a type these routes can name.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query

from corollary.api.deps import (
    LIVE_CREDENTIAL_ENV_VARS,
    AccountModeDep,
    ApiError,
    BrokerDep,
    ServiceRegistry,
    SessionDep,
    service_registry,
)
from corollary.api.schemas import (
    AccountResponse,
    ActivityItem,
    ActivityStatus,
    EquityCurvePoint,
    MarginClassName,
    MarginSummary,
    Page,
    PortfolioHistoryResponse,
    Trend,
)
from corollary.db.models import ENGINE_STATE_ID, EngineState
from corollary.engine.execution.interface import (
    Account,
    MarginClass,
    NonTradeActivity,
)
from corollary.wire import vendor_detail

__all__ = ["CASH_ACTIVITY_TYPES", "router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: The four activity types that move cash without being a trade, in the order
#: the design spec names them. Passed to the vendor to narrow the fetch, and
#: applied again locally because the local pass is the one that is correct.
#:
#: ``types`` and ``category`` are mutually exclusive on Alpaca's endpoint, and
#: ``category=non_trade_activity`` would also return every ``FEE``, ``OPEXP``
#: and ``OPASN`` row -- none of which is a transfer.
CASH_ACTIVITY_TYPES: Final[tuple[str, ...]] = ("TRANS", "CSD", "CSW", "JNLC")

#: What goes in the contract column of a row that has no contract. The Phase 1
#: fixtures render an em dash here (``contract: '—'`` on every ``DEPOSIT``
#: row), and matching them keeps the table a data-source swap rather than a
#: component rewrite. An empty string would render a blank cell where every
#: neighbouring row has a symbol, which reads as a missing value rather than
#: as an inapplicable one.
CASH_TRANSFER_CONTRACT: Final = "—"

#: Alpaca's ``status`` on a non-trade activity. ``correct`` is a *corrected*
#: row, which has settled, so it reads as filled.
_TRANSFER_STATUS: Final[Mapping[str, ActivityStatus]] = {
    "executed": "filled",
    "correct": "filled",
    "canceled": "canceled",
    "cancelled": "canceled",
    "pending": "pending",
}

#: ``<count><unit>``, where the unit is Alpaca's: D day, W week, M month, A
#: year. Note **A and not Y** -- ``period=1Y`` is a 422 here rather than a 400
#: from the vendor three layers away.
_PERIOD = re.compile(r"^[1-9][0-9]{0,2}[DWMA]$")

#: Alpaca's five portfolio-history resolutions.
_TIMEFRAMES: Final[frozenset[str]] = frozenset(
    {"1Min", "5Min", "15Min", "1H", "1D"}
)

#: Pagination bounds for the transfers table. The ceiling is a bound on one
#: response, not on the history: :attr:`Page.total` is the real total.
_MAX_PAGE_SIZE: Final = 200
_DEFAULT_PAGE_SIZE: Final = 50

#: Percentages are quantised before they cross the boundary. Not cosmetic:
#: ``day_change / last_equity`` is a 28-digit ``Decimal`` that no ``float``
#: round-trips, and ``JsonMoney``'s serializer logs a precision warning on
#: every such value -- one per request, for a number the page renders to two
#: places. Four places keeps a tenth of a basis point.
_PCT_PLACES: Final = Decimal("0.0001")

RegistryDep = Annotated[ServiceRegistry, Depends(service_registry)]


# --------------------------------------------------------------------------
# Logging helpers
# --------------------------------------------------------------------------


def _refuse(event: str, message: str, rule: str, **inputs: object) -> None:
    """Rule 8: the rule, the inputs and the timestamp, every time.

    A silent rejection is a bug -- you need this the first time the terminal
    shows fewer deposits than the bank statement does.
    """
    logger.warning(
        message,
        extra={
            "event": event,
            "rule": rule,
            "at": datetime.now(timezone.utc).isoformat(),
            **inputs,
        },
    )


def _safe_description(description: str | None) -> str | None:
    """Vendor free text, de-identified, or ``None``.

    The only sanctioned way a ``description`` may reach a log line.
    :func:`~corollary.wire.vendor_detail` blanks the account-number shape and
    bounds the length; a rule about field names cannot do either, because the
    identifier is inside a sentence.
    """
    if description is None:
        return None
    return vendor_detail(description)


# --------------------------------------------------------------------------
# GET /api/account
# --------------------------------------------------------------------------


def _plain(value: Decimal) -> str:
    """A ``Decimal`` as a human would write it -- ``4``, never ``4E+0``."""
    return format(value.normalize(), "f")


_MARGIN_LABELS: Final[Mapping[MarginClass, str]] = {
    MarginClass.CASH: "Cash account",
    MarginClass.REG_T: "Reg T margin account",
    MarginClass.PATTERN_DAY_TRADER: "Pattern day-trader margin account",
    MarginClass.UNKNOWN: "Margin class not recognised",
}

_MARGIN_CLASS_NAMES: Final[Mapping[MarginClass, MarginClassName]] = {
    MarginClass.CASH: "cash",
    MarginClass.REG_T: "reg_t",
    MarginClass.PATTERN_DAY_TRADER: "pdt",
    MarginClass.UNKNOWN: "unknown",
}


def _margin_summary(account: Account) -> MarginSummary:
    """The margin sentence, built from ``multiplier`` rather than asserted.

    Every branch quotes the real number. That is the whole design: a note that
    named a class without quoting the figure could drift back into being the
    2× constant it replaced, and nothing on screen would say which.
    """
    margin_class = account.margin_class
    multiplier = _plain(account.multiplier)

    if margin_class is MarginClass.CASH:
        note = (
            f"The broker reports a multiplier of {multiplier}: no margin, so "
            "buying power is settled cash."
        )
    elif margin_class is MarginClass.PATTERN_DAY_TRADER:
        note = (
            f"The broker reports a multiplier of {multiplier} — a pattern "
            f"day-trader margin account — so buying power is {multiplier}× cash."
        )
    elif margin_class is MarginClass.REG_T:
        note = (
            f"The broker reports a multiplier of {multiplier}, so buying power "
            f"is {multiplier}× cash."
        )
    else:
        note = (
            f"The broker reports a multiplier of {multiplier}, which is not 1, "
            "2 or 4, so the margin class cannot be named."
        )
        _refuse(
            "account_margin_class_unknown",
            "the account multiplier is not one of the three known margin classes",
            "a margin class is read from multiplier and never guessed; a wrong "
            "one is a wrong sizing ceiling",
            multiplier=multiplier,
        )

    if account.options_buying_power is None:
        note += (
            " Options are not marginable, so an option order sizes against "
            "options buying power rather than this figure — and the broker did "
            "not report one on this account."
        )
        _refuse(
            "account_options_buying_power_absent",
            "the account object carried no options buying power",
            "options are not marginable, so the margin figure must never be "
            "substituted for options buying power",
            multiplier=multiplier,
        )
    else:
        note += (
            " Options are not marginable, so an option order sizes against "
            "options buying power, never against this figure."
        )

    return MarginSummary(
        multiplier=account.multiplier,
        margin_class=_MARGIN_CLASS_NAMES[margin_class],
        label=_MARGIN_LABELS[margin_class],
        note=note,
    )


def _balance_trend(account: Account) -> Trend | None:
    """The day move as a percentage of the previous close, or ``None``.

    ``last_equity`` is equity at the previous close, so this is a day change
    for free. Zero previous equity is a brand-new account: the percentage is
    undefined and ``None`` says so, where ``0`` would claim a flat day.
    """
    if account.last_equity == 0:
        return None
    change_pct = (account.day_change / account.last_equity * 100).quantize(
        _PCT_PLACES, rounding=ROUND_HALF_UP
    )
    return Trend(change_pct=change_pct, compared_to="vs previous close")


@router.get("", summary="Balances, margin class and options entitlement")
async def read_account(
    broker: BrokerDep, mode: AccountModeDep, registry: RegistryDep
) -> AccountResponse:
    """One ``GET /v2/account``, and the arithmetic §8.6 asks for on top of it.

    Nothing here reaches the market-data provider. Balances live on the
    trading host, which carries its own 200/min budget; spending the data
    host's bucket to answer a question about cash would throttle the quote
    poll for nothing.
    """
    account = await broker.account()

    net_position_value = account.long_market_value + account.short_market_value
    derived_equity = account.cash + net_position_value
    difference = derived_equity - account.equity
    reconciles = difference == 0

    if not reconciles:
        _refuse(
            "account_equity_mismatch",
            "cash plus the net position value does not equal the broker's equity",
            "total equity has two sources and the page states whether they "
            "agree rather than picking one (PRD §8.6)",
            account=mode.value,
            cash=str(account.cash),
            net_position_value=str(net_position_value),
            derived_equity=str(derived_equity),
            broker_equity=str(account.equity),
            difference=str(difference),
        )

    missing = registry.missing_live_credentials
    return AccountResponse(
        account=mode,
        status=account.status,
        currency=account.currency,
        cash=account.cash,
        equity=account.equity,
        last_equity=account.last_equity,
        day_change=account.day_change,
        balance_trend=_balance_trend(account),
        buying_power=account.buying_power,
        options_buying_power=account.options_buying_power,
        long_market_value=account.long_market_value,
        short_market_value=account.short_market_value,
        net_position_value=net_position_value,
        # The broker's own `position_market_value`, which is gross rather than
        # net. Carried under a name that cannot be mistaken for the equity
        # term; see the module docstring.
        gross_position_value=account.position_market_value,
        derived_equity=derived_equity,
        equity_reconciles=reconciles,
        equity_difference=difference,
        margin=_margin_summary(account),
        options_approved_level=account.options_approved_level,
        options_trading_level=account.options_trading_level,
        trading_blocked=account.trading_blocked,
        account_blocked=account.account_blocked,
        transfers_blocked=account.transfers_blocked,
        cash_account_available=not missing,
        cash_account_unavailable_reason=_cash_unavailable_reason(missing),
        missing_live_credential_env_vars=list(missing),
    )


def _cash_unavailable_reason(missing: Sequence[str]) -> str | None:
    """Why the Cash toggle is disabled, in one sentence, or ``None``.

    Surfaced from the registry's own finding rather than recomputed --
    ``deps.missing_live_credentials`` is the one place that decides whether a
    blank variable counts as absent, and a second opinion here would be a
    second answer.

    Rule 5's second half is the argument the sentence makes: switching to Cash
    requires a confirmation naming the account **and its balance**, and there
    is no balance to name without credentials to read one with.
    """
    if not missing:
        return None
    verb = "is" if len(missing) == 1 else "are"
    return (
        f"Cash trading is unavailable: {' and '.join(missing)} {verb} not set "
        f"({' and '.join(LIVE_CREDENTIAL_ENV_VARS)} are both required). "
        "Selecting Cash requires an explicit confirmation naming the account "
        "and its balance, and there is no balance to read without them."
    )


# --------------------------------------------------------------------------
# GET /api/account/history
# --------------------------------------------------------------------------


def _check_window(period: str, timeframe: str) -> None:
    if not _PERIOD.match(period):
        _refuse(
            "account_history_window_rejected",
            "the requested portfolio-history period is not a shape Alpaca takes",
            "a window is validated here rather than sent and 400'd by the "
            "vendor three layers away",
            period=period,
            timeframe=timeframe,
            field="period",
        )
        raise ApiError(
            status_code=422,
            code="invalid_history_window",
            message=(
                f"period must be a count followed by D, W, M or A — {period!r} "
                "is not. Note the unit for a year is A, not Y."
            ),
        )
    if timeframe not in _TIMEFRAMES:
        _refuse(
            "account_history_window_rejected",
            "the requested portfolio-history timeframe is not one Alpaca serves",
            "a window is validated here rather than sent and 400'd by the "
            "vendor three layers away",
            period=period,
            timeframe=timeframe,
            field="timeframe",
        )
        raise ApiError(
            status_code=422,
            code="invalid_history_window",
            message=(
                f"timeframe must be one of {', '.join(sorted(_TIMEFRAMES))} — "
                f"{timeframe!r} is not."
            ),
        )


@router.get("/history", summary="Alpaca's equity curve, with t0 marked")
async def read_history(
    broker: BrokerDep,
    mode: AccountModeDep,
    session: SessionDep,
    period: Annotated[
        str, Query(description="Count plus D, W, M or A. A is a year, not Y.")
    ] = "1M",
    timeframe: Annotated[
        str, Query(description="1Min, 5Min, 15Min, 1H or 1D.")
    ] = "1D",
) -> PortfolioHistoryResponse:
    """The broker's own curve, and the marker that keeps it honest.

    Decision 6. Nothing is simulated and nothing is stored: this is a
    pass-through of ``/v2/account/portfolio/history`` plus ``engine_state.t0``.

    The ``t0`` read is a **read**. ``engine.state.engine_state()`` creates the
    singleton when it is missing, which is right for the question *"is the
    engine halted?"* -- absence of state is not evidence of a healthy engine --
    and wrong for this one. ``t0`` is written once on the first ever start and
    never rewritten, so a chart route that could create the row is a chart
    route that could move the marker.
    """
    _check_window(period, timeframe)
    history = await broker.portfolio_history(period=period, timeframe=timeframe)

    state = session.get(EngineState, ENGINE_STATE_ID)
    t0 = state.t0 if state is not None else None
    # `None` rather than 0 when there is no marker: unknown and none are
    # different claims, and 0 would say the whole curve is Corollary's.
    before = (
        None if t0 is None else sum(1 for point in history.points if point.at < t0)
    )

    return PortfolioHistoryResponse(
        account=mode,
        period=period,
        timeframe=history.timeframe or timeframe,
        base_value=history.base_value,
        base_value_asof=history.base_value_asof,
        points=[
            EquityCurvePoint(
                at=point.at,
                # A gap stays a gap. Zero here draws a line to the axis and
                # asserts the account was worth nothing that day.
                equity=point.equity,
                profit_loss=point.profit_loss,
                profit_loss_pct=point.profit_loss_pct,
            )
            for point in history.points
        ],
        t0=t0,
        points_before_t0=before,
    )


# --------------------------------------------------------------------------
# GET /api/account/transfers
# --------------------------------------------------------------------------


def _transfer_item(row: NonTradeActivity) -> ActivityItem | None:
    """One cash movement as a ledger row, or ``None`` if it cannot be one.

    Every rejection is logged with its rule, its inputs and the timestamp, and
    the row is dropped rather than rendered as a zero. ``$0.00`` in a column
    of deposits is a claim about the money; an absent row is a claim about the
    data, and only one of those is true.
    """
    amount = row.net_amount
    if amount is None:
        _refuse(
            "transfer_rejected",
            "a cash activity carried no net_amount",
            "a cash movement with no amount is dropped, never rendered as zero",
            activity_id=row.id,
            activity_type=row.activity_type,
            description=_safe_description(row.description),
        )
        return None
    if amount == 0:
        _refuse(
            "transfer_rejected",
            "a cash activity moved zero and has no direction",
            "a transfer is a deposit or a withdrawal; zero is neither",
            activity_id=row.id,
            activity_type=row.activity_type,
            description=_safe_description(row.description),
        )
        return None

    when = row.created_at
    if when is None:
        if row.activity_date is None:
            _refuse(
                "transfer_rejected",
                "a cash activity carried neither created_at nor a date",
                "a ledger row must be placeable in time; an undated one is "
                "dropped rather than dated to now",
                activity_id=row.id,
                activity_type=row.activity_type,
                description=_safe_description(row.description),
            )
            return None
        # A transfer you cannot see is worse than one dated to midnight of its
        # own settlement date, so the row survives and the substitution is on
        # the record. UTC, because the date is a date and localising it would
        # move it a day on any afternoon in New York.
        when = datetime.combine(row.activity_date, time.min, tzinfo=timezone.utc)
        logger.info(
            "a cash activity had no created_at; dated from its settlement date",
            extra={
                "event": "transfer_time_from_settlement_date",
                "rule": "a settlement date is a date, read as UTC midnight",
                "at": datetime.now(timezone.utc).isoformat(),
                "activity_id": row.id,
                "activity_date": row.activity_date.isoformat(),
            },
        )

    raw_status = (row.status or "").strip().lower()
    status = _TRANSFER_STATUS.get(raw_status)
    if status is None:
        status = "pending"
        _refuse(
            "transfer_status_unknown",
            "a cash activity carried a status this API does not model",
            "an unmodelled status reads as pending, and the real one is logged "
            "rather than dropped",
            activity_id=row.id,
            activity_type=row.activity_type,
            status=raw_status,
        )

    return ActivityItem(
        id=row.id,
        time=when,
        contract=CASH_TRANSFER_CONTRACT,
        # The sign is the fact. Deriving the action from anything else would
        # be a second source of truth about which way the money went.
        action="DEPOSIT" if amount > 0 else "WITHDRAWAL",
        price=None,
        quantity=None,
        # Money moved into the account is not money the account made, which is
        # why `amount` is its own field and never folded into `pnl`.
        pnl=None,
        pnl_pct=None,
        amount=amount,
        status=status,
        rejection_reason=None,
    )


@router.get("/transfers", summary="Deposits and withdrawals — never orders")
async def read_transfers(
    broker: BrokerDep,
    page: Annotated[int, Query(description="Zero-based.")] = 0,
    page_size: Annotated[
        int, Query(alias="pageSize", description="Rows per page.")
    ] = _DEFAULT_PAGE_SIZE,
) -> Page[ActivityItem]:
    """Cash movements for one book, newest first.

    ``TRANS`` / ``CSD`` / ``CSW`` / ``JNLC`` on the **non-trade** branch. This
    account's entire non-trade history is one ``JNLC`` funding journal, next
    to nineteen ``FEE`` rows that are not transfers and fourteen ``FILL`` rows
    that are a different object shape entirely.

    The order is newest first and ties break on the activity id, so the same
    history returns the same page every time. That is not decoration: a page
    boundary that moves between two requests silently drops or repeats a row.
    """
    if page < 0:
        _refuse(
            "transfers_page_rejected",
            "a negative page was requested",
            "pages are zero-based; a negative one has no rows to mean",
            page=page,
            page_size=page_size,
        )
        raise ApiError(
            status_code=422,
            code="invalid_page",
            message=f"page must be zero or greater — {page} is not.",
        )
    if not 0 < page_size <= _MAX_PAGE_SIZE:
        _refuse(
            "transfers_page_rejected",
            "a page size outside the served range was requested",
            "one response is bounded; the real total is reported separately "
            "so nothing has to page to learn it",
            page=page,
            page_size=page_size,
        )
        raise ApiError(
            status_code=422,
            code="invalid_page",
            message=(
                f"pageSize must be between 1 and {_MAX_PAGE_SIZE} — "
                f"{page_size} is not."
            ),
        )

    # The vendor filter narrows the fetch; the local one is what is correct.
    # `types` and `category` are mutually exclusive, and `category` would drag
    # in every fee and option event.
    rows = await broker.activities(types=CASH_ACTIVITY_TYPES)

    items: list[ActivityItem] = []
    for row in rows:
        # Two gates, and both are needed. The `isinstance` is structural: a
        # `FILL` is a different object shape with a different quantity
        # convention, and no vendor-side filter changes that. The membership
        # test is the one that catches a vendor filter that widened, which is
        # how a clearing fee ends up in a column of deposits.
        if not isinstance(row, NonTradeActivity):
            continue
        if row.activity_type.upper() not in CASH_ACTIVITY_TYPES:
            continue
        item = _transfer_item(row)
        if item is not None:
            items.append(item)

    items.sort(key=lambda row_item: (row_item.time, row_item.id), reverse=True)

    start = page * page_size
    window = items[start : start + page_size]
    return Page[ActivityItem](
        items=window,
        total=len(items),
        page=page,
        page_size=page_size,
        has_more=start + len(window) < len(items),
    )
