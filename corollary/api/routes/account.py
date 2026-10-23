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
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from corollary.api.deps import (
    LIVE_CREDENTIAL_ENV_VARS,
    AccountMode,
    AccountModeDep,
    ApiError,
    BrokerDep,
    CashSessionDep,
    ServiceRegistry,
    SessionDep,
    service_registry,
)
from corollary.api.operator import (
    AuditChange,
    cash_session_confirmed_notice,
    cash_session_ended_notice,
    NoticeSink,
    deliver_notice,
)
from corollary.api.schemas import (
    AccountResponse,
    ActivityItem,
    CashSessionConfirm,
    CashSessionState,
    ActivityStatus,
    EquityCurvePoint,
    MarginClassName,
    MarginSummary,
    Page,
    PortfolioHistoryResponse,
    Trend,
)
from corollary.db.models import (
    CASH_SESSION_ACTIVE_PREFIX,
    CASH_SESSION_AUDIT_FIELD,
    CASH_SESSION_INACTIVE,
    ENGINE_STATE_ID,
    AuditLog,
    EngineState,
)
from corollary.engine.cash_session import (
    MASKED_ACCOUNT_NUMBER,
    CashSession,
    CashSessionEndedWhileConfirming,
)
from corollary.engine.execution.interface import (
    Account,
    BrokerError,
    MarginClass,
    NonTradeActivity,
)
from corollary.engine.runtime import OperatorNotice
from corollary.wire import mask_account_number, vendor_detail

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
        account_number_masked=account.account_number_masked,
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


# --------------------------------------------------------------------------
# /api/account/cash-session -- rule 5's confirmed Cash session
# --------------------------------------------------------------------------

#: Every refusal below logs this, the inputs (masked only) and the instant.
_CASH_SESSION_RULE: Final = (
    "rule 5: Cash requires an explicit confirmation naming the live account, "
    "and the server checks the name the client echoed against the account it "
    "reads itself (rule 4)"
)

#: The 503's fixed words. Fixed, so nothing from the environment can reach
#: a response (rule 6); the variable names are on ``GET /api/account``.
CASH_SESSION_UNAVAILABLE: Final = (
    "Cash cannot be confirmed: the live Alpaca credentials are not configured "
    "on the server, so there is no live account to read or to confirm."
)


def cash_session_now() -> datetime:
    """The confirmation's clock, aware UTC. A dependency so a test can fix it."""
    return datetime.now(timezone.utc)


CashNowDep = Annotated[datetime, Depends(cash_session_now)]


def _echo_for_log(echo: str) -> str:
    """The number the client sent, in a form safe to log.

    A mask goes as-is. Anything else may be a full account number pasted in,
    so it is masked here, or replaced outright when even that is unsafe --
    a client's mistake must not put the number in the log (rule 6).
    """
    text = echo.strip()
    if MASKED_ACCOUNT_NUMBER.match(text):
        return text
    return mask_account_number(text) or "<not a masked account number>"


def _cash_session_refused(
    *,
    status: int,
    code: str,
    message: str,
    reason: str,
    correlation_id: str,
    **inputs: str,
) -> ApiError:
    """Rule 8: log the refusal -- rule, masked inputs, instant -- then build it."""
    _refuse(
        "cash_session_refused",
        message,
        _CASH_SESSION_RULE,
        reason=reason,
        status=status,
        correlation_id=correlation_id,
        **inputs,
    )
    return ApiError(status_code=status, code=code, message=message)


def _audit_value(session: CashSession | None) -> str:
    """``inactive``, or ``active ••••1234`` -- ``ck_audit_log_account_row``'s shapes."""
    if session is None:
        return CASH_SESSION_INACTIVE
    return CASH_SESSION_ACTIVE_PREFIX + session.account_number_masked


def _write_audit(session: Session, change: AuditChange, at: datetime) -> None:
    session.add(
        AuditLog(
            at=at,
            category=change.category,
            field=change.field,
            previous_value=change.previous,
            new_value=change.new,
        )
    )
    session.commit()


def _state(session: CashSession | None) -> CashSessionState:
    if session is None:
        return CashSessionState(
            active=False, account_number_masked=None, confirmed_at=None, balance=None
        )
    return CashSessionState(
        active=True,
        account_number_masked=session.account_number_masked,
        confirmed_at=session.confirmed_at,
        balance=session.balance,
    )


@router.get("/cash-session", summary="Whether a Cash session is confirmed")
def read_cash_session(holder: CashSessionDep) -> CashSessionState:
    """In memory only: reads the holder, never the broker."""
    return _state(holder.current())


async def _notify_in_order(request: Request, notice: OperatorNotice) -> None:
    """Deliver ``notice`` now, under the writers' lock, off the event loop.

    Not ``notify_after_response``, for two reasons that are both rule 5's.
    A background task runs after the handler has released the lock, so a
    confirm's notice and the end queued behind it could reach the runtime in
    either order -- "ended" then "confirmed" reads as Cash on. And a
    background task is dropped when the handler raises, which is exactly when
    a fail-off end most needs saying. ``deliver_notice`` never raises, and the
    action it describes has already happened by the time this runs.
    """
    runtime: NoticeSink | None = getattr(request.app.state, "engine_runtime", None)
    await run_in_threadpool(deliver_notice, runtime, notice)


async def _record_end(
    *,
    ended: CashSession,
    session: Session,
    now: datetime,
    correlation_id: str,
    request: Request,
    cause: str,
) -> None:
    """Audit, log and notify a session that has *already* ended. Never raises.

    The end itself is the caller's, done first: ending is the safe direction
    and never waits on the database. If the audit row cannot be written the
    end still stands, the failure is an ERROR naming the rule and the
    correlation id, and the owner is still told the session ended -- a notice
    withheld because the bookkeeping failed would leave them believing Cash is
    on.
    """
    change = AuditChange(
        category="account",
        field=CASH_SESSION_AUDIT_FIELD,
        previous=_audit_value(ended),
        new=CASH_SESSION_INACTIVE,
    )
    try:
        await run_in_threadpool(_write_audit, session, change, now)
    except Exception as exc:
        logger.error(
            "the Cash session ended but its audit row could not be written; "
            "the end stands and is notified, the audit log is missing it",
            extra={
                "event": "cash_session_end_unrecorded",
                "rule": _CASH_SESSION_RULE,
                "cause": cause,
                "account": ended.account_number_masked,
                "error_type": type(exc).__name__,
                "at": now.isoformat(),
                "correlation_id": correlation_id,
            },
        )
    logger.info(
        "cash session ended for %s",
        ended.account_number_masked,
        extra={
            "event": "cash_session_ended",
            "cause": cause,
            "account": ended.account_number_masked,
            "confirmed_at": ended.confirmed_at.isoformat(),
            "at": now.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    await _notify_in_order(
        request,
        cash_session_ended_notice(
            account_number_masked=ended.account_number_masked,
            confirmed_at=ended.confirmed_at,
            at=now,
            correlation_id=correlation_id,
        ),
    )


@router.post(
    "/cash-session",
    summary="Confirm the live Cash account. Audit-logged.",
    responses={
        409: {
            "description": (
                "The echoed account number is not the live account's (any "
                "existing session is ended), or the session was ended while "
                "this confirmation was in flight"
            )
        },
        502: {"description": "The live account could not be read, or reported no number"},
        503: {"description": "No live credentials are configured"},
    },
)
async def confirm_cash_session(
    body: CashSessionConfirm,
    registry: RegistryDep,
    holder: CashSessionDep,
    session: SessionDep,
    now: CashNowDep,
    request: Request,
) -> CashSessionState:
    """Rule 5's confirmation, checked by the server.

    Reads the live account through the read-only Cash ``BrokerAccount`` and
    refuses unless the masked number the dialog showed -- echoed as
    ``accountNumber`` -- is the live account's. A success writes one
    ``account`` audit row, *then* starts (or restarts) the in-memory session,
    and notifies; a failed audit write therefore never has a visible session
    behind it, and ends any prior one (fail-off).

    Refusals record nothing, with one exception: a mismatch also ends any
    existing session, audited and notified, because a mismatch means the UI
    is wrong about which account is live (orchestrator decision P3).

    Holds the holder's writer lock for the whole body, so an end can never
    interleave with a confirm (audit M1); the generation check refuses a
    confirm overtaken by an end from any other path.

    Touches nothing else: no order path (rule 1), no halt state (rule 9).
    """
    correlation_id = str(uuid.uuid4())
    echoed = _echo_for_log(body.account_number)
    # Read at arrival, before waiting on the lock: an end that lands while
    # this request waits (or while its dependencies resolved) refuses it,
    # rather than this confirm reading the post-end generation and
    # switching Cash back on after the owner's later DELETE.
    generation = holder.generation
    async with holder.writer_lock:
        if not registry.configured(AccountMode.CASH):
            raise _cash_session_refused(
                status=503,
                code="cash_unavailable",
                message=CASH_SESSION_UNAVAILABLE,
                reason="no_live_credentials",
                correlation_id=correlation_id,
                echoed=echoed,
            )

        try:
            account = await registry.broker(AccountMode.CASH).account()
        except BrokerError as exc:
            # BrokerAuthError is a subclass. Logged as a Cash refusal (rule 8),
            # then re-raised into the app's vendor-error mapping, which
            # chooses the status and the words. Class name only: the vendor's
            # text is not ours to vouch for (rule 6).
            _refuse(
                "cash_session_refused",
                "Cash cannot be confirmed: the live account could not be read",
                _CASH_SESSION_RULE,
                reason="live_account_unreadable",
                error_type=type(exc).__name__,
                correlation_id=correlation_id,
                echoed=echoed,
            )
            raise
        live = account.account_number_masked
        if live is None:
            raise _cash_session_refused(
                status=502,
                code="cash_account_unnamed",
                message=(
                    "The live account reported no account number to confirm "
                    "against, so Cash cannot be confirmed. Nothing was recorded."
                ),
                reason="live_account_unnamed",
                correlation_id=correlation_id,
                echoed=echoed,
            )
        if body.account_number.strip() != live:
            ended = holder.end()
            refusal = _cash_session_refused(
                status=409,
                code="cash_account_mismatch",
                message=(
                    f"The account confirmed ({echoed}) is not the live account "
                    f"({live}). Cash is not confirmed"
                    + (
                        ", and the Cash session confirmed earlier has been ended"
                        if ended is not None
                        else ""
                    )
                    + "; reopen the confirmation and check which account the "
                    "live credentials belong to."
                ),
                reason="account_mismatch",
                correlation_id=correlation_id,
                echoed=echoed,
                live=live,
                ended_session=str(ended is not None),
            )
            if ended is not None:
                await _record_end(
                    ended=ended,
                    session=session,
                    now=now,
                    correlation_id=correlation_id,
                    request=request,
                    cause="account_mismatch",
                )
            raise refusal
        if holder.generation != generation:
            raise _ended_while_confirming(correlation_id, echoed)

        previous = holder.current()
        change = AuditChange(
            category="account",
            field=CASH_SESSION_AUDIT_FIELD,
            previous=_audit_value(previous),
            new=CASH_SESSION_ACTIVE_PREFIX + live,
        )
        try:
            await run_in_threadpool(_write_audit, session, change, now)
        except BaseException:
            # A session with no audit row is not allowed to stand. Ending the
            # prior one is the safe direction -- Cash off -- and it is said.
            ended = holder.end()
            logger.error(
                "the Cash session's audit row could not be written; no session "
                "was started, and any prior session was ended",
                extra={
                    "event": "cash_session_unrecorded",
                    "rule": _CASH_SESSION_RULE,
                    "account": live,
                    "ended_prior": ended is not None,
                    "at": now.isoformat(),
                    "correlation_id": correlation_id,
                },
            )
            if ended is not None:
                await _notify_in_order(
                    request,
                    cash_session_ended_notice(
                        account_number_masked=ended.account_number_masked,
                        confirmed_at=ended.confirmed_at,
                        at=now,
                        correlation_id=correlation_id,
                    ),
                )
            raise

        try:
            holder.confirm(
                account_number_masked=live,
                balance=account.equity,
                at=now,
                correlation_id=correlation_id,
                generation=generation,
            )
        except CashSessionEndedWhileConfirming:
            # An end from outside the lock landed during the audit write. It
            # stands; the row just written claims a session that never
            # started, so the correcting row goes beside it, best effort.
            await _correct_unstarted(
                session=session,
                started=change.new,
                now=now,
                correlation_id=correlation_id,
            )
            raise _ended_while_confirming(correlation_id, echoed) from None

        logger.info(
            "cash session confirmed for %s",
            live,
            extra={
                "event": "cash_session_confirmed",
                "account": live,
                "balance": str(account.equity),
                "replaced": previous is not None,
                "at": now.isoformat(),
                "correlation_id": correlation_id,
            },
        )
        await _notify_in_order(
            request,
            cash_session_confirmed_notice(
                account_number_masked=live,
                balance=account.equity,
                replaced=previous is not None,
                at=now,
                correlation_id=correlation_id,
            ),
        )
        return _state(holder.current())


def _ended_while_confirming(correlation_id: str, echoed: str) -> ApiError:
    return _cash_session_refused(
        status=409,
        code="cash_session_ended_while_confirming",
        message=(
            "The Cash session was ended while this confirmation was in "
            "flight; the end stands and Cash is not confirmed. Confirm again "
            "if Cash is still wanted."
        ),
        reason="ended_while_confirming",
        correlation_id=correlation_id,
        echoed=echoed,
    )


async def _correct_unstarted(
    *, session: Session, started: str, now: datetime, correlation_id: str
) -> None:
    """Follow an ``active`` audit row whose session never started. Never raises."""
    change = AuditChange(
        category="account",
        field=CASH_SESSION_AUDIT_FIELD,
        previous=started,
        new=CASH_SESSION_INACTIVE,
    )
    try:
        await run_in_threadpool(_write_audit, session, change, now)
    except Exception as exc:
        logger.error(
            "a Cash confirmation was overtaken by an end after its audit row "
            "was written, and the correcting row could not be written",
            extra={
                "event": "cash_session_correction_unrecorded",
                "rule": _CASH_SESSION_RULE,
                "error_type": type(exc).__name__,
                "at": now.isoformat(),
                "correlation_id": correlation_id,
            },
        )


@router.delete(
    "/cash-session",
    status_code=204,
    summary="End the Cash session. 204 whether or not one existed.",
)
async def end_cash_session(
    holder: CashSessionDep,
    session: SessionDep,
    now: CashNowDep,
    request: Request,
) -> Response:
    """End the session first, then record it.

    Ending is the safe direction, so it never waits on the database: if the
    audit write fails the session is still over, the response is still a
    204, the owner is still notified, and the missing row is an ERROR naming
    the rule and the correlation id. Ending nothing changes nothing, writes
    no row and notifies nobody.

    **Ends at once, before waiting on anything** (re-audit LOW-1): switching
    to Paper must never wait behind a confirm's slow live-account read, so
    ``end()`` runs before the writer lock. Its generation bump refuses an
    in-flight confirm, at the re-check after the read or at ``confirm()``.
    The lock is then taken only to record the end, so the ended notice still
    follows any notice the in-flight confirm already sent.
    """
    correlation_id = str(uuid.uuid4())
    ended = holder.end()
    if ended is None:
        # Nothing was active, so nothing to record or notify: answer at once.
        # The generation bump above still refuses any confirm in flight.
        logger.info(
            "no Cash session to end",
            extra={
                "event": "cash_session_end_noop",
                "at": now.isoformat(),
                "correlation_id": correlation_id,
            },
        )
        return Response(status_code=204)
    async with holder.writer_lock:
        await _record_end(
            ended=ended,
            session=session,
            now=now,
            correlation_id=correlation_id,
            request=request,
            cause="operator",
        )
        return Response(status_code=204)
