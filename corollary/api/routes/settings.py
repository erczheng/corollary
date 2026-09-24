"""Settings: the six routes that make PRD §8.7 a page rather than a fixture.

Design spec decision 3, which is this module's whole brief:

    Risk limits, feed selection and notification routing persist to SQLite
    with §8.7's audit log. These are **config, not orders** -- rule 1
    untouched -- and they are the database's natural first customers, so
    Phase 6's risk manager reads a table that already exists.

    API key presence, feed status and options level must be server-backed
    regardless: **only the server can see the environment.**

Step 2 of the design spec claimed *"Settings goes server-backed"* and the
spec's own amendment list records that the sentence over-claimed: the three
config tables exist and are seeded, and nothing served them. This is that.

Five things here are rules rather than choices
----------------------------------------------

**Rule 4: the UI displays limits and the engine enforces them.** Never trust a
value that arrived from the client. ``validateRiskLimit`` in
``web/src/lib/settings.ts`` is the *client*; :func:`validate_risk_limit` in
``db.models`` is the authority, and the column carries
``ck_risk_limit_value`` underneath that for anything arriving as raw SQL.
Three layers, because a caller can only reach one of them.

**An unset ceiling is ``null``, never a substituted number.** The key is still
served -- a page cannot say "no ceiling configured" about a row it never
received -- but :attr:`RiskLimit.value` is ``None`` and nothing here invents a
default. ``riskLimitFor`` returns ``number | null`` for the same reason, and
the reason is not hypothetical: two order tickets once did this lookup
themselves with different fallbacks, ``?? 7`` in one and ``?? 0`` in the
other, so one reported a ceiling nobody had set and the other reported every
trade as over-limit.

**No ``Money`` column is ordered, compared or aggregated in SQL.** ``Money``
is TEXT on SQLite, so ``MAX(value)`` over the five seeded ceilings answers
``8``, ``MIN`` answers ``20``, ``WHERE value > '10'`` matches all five, and
``ORDER BY value`` gives 20, 25, 40, 7, 8. None of those is an error and every
one is a plausible number. :func:`~corollary.db.seed.risk_limits` loads the
rows and this module compares them as ``Decimal`` in Python; the guard in
``db.types`` raises on everything else, and working around it would be
working around the feature.

**Money crosses the request boundary as a string, never as a JSON float.**
Responses serialize ``Decimal`` to a JSON number because the API boundary is a
display boundary (design spec, *Three rule reinterpretations*). A request
value is *stored*, so it gets the treatment ``corollary.wire.as_decimal``
gives every vendor number: a string or a whole number parses exactly, and a
float **raises** rather than being converted.

**Rule 6: presence, and nothing else.** ``GET /keys`` never renders a key, not
partially, not in an error body, not as a last-four. ``PK****4F2A`` renders
four characters of one, and a wrong key cannot be diagnosed by squinting at
its suffix anyway.

What this module is not
-----------------------

**Config is not an order.** No broker is injected here, nothing is submitted
to one, and the execution half of the vendor surface does not exist until
Phase 6 -- which is what keeps rule 1 structural rather than disciplinary.
``tests/api/test_settings_routes.py`` greps this file for both of those names
and fails if either appears, so the absence is checked rather than trusted;
the two names live in that test rather than in this docstring for exactly
that reason. Rule 7's halt and flatten are likewise not here: halt is
``api/routes/engine.py``, and flatten does not exist at all.

**The vendor SDK is not imported.** ``corollary.data.providers.alpaca`` is
*our* module and owns which feed values each Alpaca endpoint accepts, so the
accepted sets are imported from it rather than restated. The feed *names* are
still read only inside that provider; what is written down here is which of
them a plan may be *offered*, which is a settings question rather than a
selection.
"""

import logging
import os
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Any, Final, NamedTuple, cast, get_args

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import BeforeValidator, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corollary.api.deps import (
    LIVE_CREDENTIAL_ENV_VARS,
    ApiError,
    ServiceRegistry,
    SessionDep,
    service_registry,
)
from corollary.api.operator import (
    AuditChange,
    OperatorEvent,
    notify_after_response,
    settings_notice,
)
from corollary.api.schemas import (
    ApiKeyPresence,
    ApiModel,
    AuditCategory,
    AuditLogEntry,
    DataFeed,
    DataSourceStatus,
    FeedKey,
    NotificationEvent,
    NotificationRoute,
    Page,
    RiskLimit,
    RiskLimitKey,
    RiskLimitUnit,
)
from corollary.data.providers.alpaca import (
    ALPACA_LIVE_KEY_ENV,
    ALPACA_LIVE_SECRET_ENV,
    ALPACA_OPTIONS_FEED_ENV,
    ALPACA_PAPER_KEY_ENV,
    ALPACA_PAPER_SECRET_ENV,
    ALPACA_STOCK_FEED_HISTORICAL_ENV,
    ALPACA_STOCK_FEED_REALTIME_ENV,
    OPTION_FEEDS,
    STOCK_HISTORICAL_FEEDS,
    STOCK_REALTIME_FEEDS,
)
from corollary.db.models import (
    ENGINE_STATE_ID,
    RISK_LIMIT_RANGES,
    AuditLog,
    EngineState,
    validate_risk_limit,
)
from corollary.db.models import DataFeed as DataFeedRow
from corollary.db.models import NotificationRoute as NotificationRouteRow
from corollary.db.models import RiskLimit as RiskLimitRow
from corollary.db.seed import NOTIFICATION_ROUTE_DEFAULTS, risk_limits
from corollary.wire import WireFormatError, as_decimal

__all__ = [
    "ALPACA_DATA_PLAN_ENV",
    "BASIC_PLAN",
    "PAID_PLAN",
    "UNSET",
    "environment",
    "router",
]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: What an audit row records as the previous value when there was no row.
#:
#: The column is NOT NULL and the alternative is an empty cell, which reads as
#: a rendering bug rather than as a fact. It is not a value any setting can
#: legitimately hold -- limits are numeric text, feeds are feed names, routes
#: are on/off -- so it cannot be confused for one.
UNSET: Final = "(unset)"

#: Which Alpaca data plan this account is on. A **fact** about the account
#: rather than a preference -- you cannot select your way onto OPRA -- and the
#: only thing in this module that needs an environment variable of its own.
#:
#: Absent means :data:`BASIC_PLAN`, and that default is the safe direction
#: rather than a convenience. Defaulting to the paid plan would let Settings
#: store ``opra`` on a Basic account, and every options request would then 403
#: in the middle of a poll. Defaulting to Basic refuses an upgrade the account
#: has actually bought, which is visible, harmless, and fixed by setting the
#: variable.
ALPACA_DATA_PLAN_ENV: Final = "ALPACA_DATA_PLAN"

BASIC_PLAN: Final = "basic"
PAID_PLAN: Final = "algo_trader_plus"

#: Spelled as ``web/src/lib/types.ts`` spells ``DataPlan``, so a value moves
#: between the two without translation.
_PLAN_LABELS: Final[Mapping[str, str]] = {
    BASIC_PLAN: "Basic",
    PAID_PLAN: "Algo Trader Plus",
}

#: PRD §10's three critical events, mirroring ``settings.ts``'s
#: ``CRITICAL_EVENTS``. Silencing one everywhere is **permitted** -- the PRD is
#: explicit -- and the confirm that names what stops arriving is the client's
#: job. The server's job is to make sure the decision left a trace, because a
#: rule-9 alert routed nowhere is how the engine ends up halted and silent.
#:
#: ``stop_loss_hit`` is deliberately absent: a stop firing is a loss doing
#: exactly what it was told to do, which is the same category error as
#: colouring it ``error`` instead of ``bearish``.
_CRITICAL_EVENTS: Final[frozenset[str]] = frozenset(
    {"order_rejected", "daily_loss_halt", "engine_error"}
)

_KNOWN_LIMIT_KEYS: Final[frozenset[str]] = frozenset(get_args(RiskLimitKey))
_KNOWN_EVENTS: Final[frozenset[str]] = frozenset(get_args(NotificationEvent))


# --------------------------------------------------------------------------
# The environment, as a dependency
# --------------------------------------------------------------------------


def environment() -> Mapping[str, str]:
    """The process environment.

    A dependency rather than a bare ``os.environ`` read so a test can supply
    its own mapping and never depend on the developer's ``.env``. Read per
    request rather than captured at import, so a variable added to a reloaded
    process is seen on the next response rather than the next restart.

    Nothing under ``corollary/`` reads ``.env`` directly; the process
    environment is the input.
    """
    return os.environ


EnvDep = Annotated[Mapping[str, str], Depends(environment)]
RegistryDep = Annotated[ServiceRegistry, Depends(service_registry)]


def _is_set(env: Mapping[str, str], name: str) -> bool:
    """Whether a variable carries a usable value.

    Blank counts as absent, matching ``deps.missing_live_credentials``:
    ``FRED_API_KEY=`` in a ``.env`` is a variable somebody meant to fill in,
    and calling it present sends the diagnosis three layers downstream.
    """
    return bool((env.get(name) or "").strip())


# --------------------------------------------------------------------------
# Catalogues -- what the page shows, independent of what is stored
# --------------------------------------------------------------------------


class _LimitMeta(NamedTuple):
    """The wording of one ceiling. The range comes from ``RISK_LIMIT_RANGES``.

    Label and help live here rather than in the database because they are not
    configuration: rewording "Max risk per trade" is a code change with a
    review, and storing them would make the audit log's *field* ambiguous
    between the key and the label it happened to carry that day.
    """

    key: RiskLimitKey
    label: str
    help: str


#: CLAUDE.md rule 4's five ceilings, in the order the Settings page lists them.
#:
#: The help text is where *"what risk means"* stops being inferred per call
#: site: a defined-risk structure uses maximum loss at expiry, a long option
#: uses premium paid, and an undefined-risk structure uses a stress loss at
#: ±2σ of the underlying's 20-day realized volatility. A percentage ceiling is
#: uninterpretable without that, which is why PRD §8.7 requires the three
#: definitions on the page.
_LIMIT_CATALOGUE: Final[tuple[_LimitMeta, ...]] = (
    _LimitMeta(
        "max_risk_per_trade_pct",
        "Max risk per trade",
        "Ceiling on what one position may lose, as a share of account equity. "
        "Risk is maximum loss at expiry for a defined-risk structure, premium "
        "paid for a long option, and a ±2σ stress loss for anything "
        "undefined-risk.",
    ),
    _LimitMeta(
        "max_daily_loss_pct",
        "Max daily loss",
        "Realized + unrealized loss in one session that triggers an automatic "
        "halt.",
    ),
    _LimitMeta(
        "max_concurrent_positions",
        "Max concurrent positions",
        "How many positions may be open at once, across every strategy.",
    ),
    _LimitMeta(
        "max_exposure_per_underlying",
        "Max exposure per underlying",
        "Ceiling on combined risk across every position sharing one "
        "underlying.",
    ),
    _LimitMeta(
        "max_net_directional_pct",
        "Max net directional exposure",
        "Ceiling on net long-minus-short delta exposure, as a share of equity.",
    ),
)


class _FeedOption(NamedTuple):
    value: str
    #: True when the current plan cannot serve this value. Asking Alpaca for
    #: it returns an **auth error**, not empty data, so it has to be refused
    #: here rather than discovered in the middle of a poll.
    requires_upgrade: bool


class _FeedMeta(NamedTuple):
    key: FeedKey
    env_var: str
    label: str
    help: str
    #: What Alpaca's endpoint accepts, imported from the provider rather than
    #: restated. The provider is the authority on the enum.
    accepted: frozenset[str]
    #: What Settings *offers*, which is narrower: ``otc`` and ``boats`` are
    #: valid parameters and not choices anyone should make from a page.
    options: tuple[_FeedOption, ...]


#: The three ``ALPACA_*_FEED`` variables, mirroring ``feedOptionsFor`` in
#: ``settings.ts`` value for value.
#:
#: The one that surprises people: **historical SIP is free on Basic.** Any
#: request whose ``end`` is more than 15 minutes old may use it, so IEX is a
#: legal choice here and a quietly expensive one -- see :func:`_feed_warning`.
#: Only the latest/snapshot endpoints and the live stream are IEX-limited.
_FEED_CATALOGUE: Final[tuple[_FeedMeta, ...]] = (
    _FeedMeta(
        "options",
        ALPACA_OPTIONS_FEED_ENV,
        "Options quotes",
        "Indicative is a 15-minute-delayed derivative of OPRA, not OPRA "
        "itself.",
        OPTION_FEEDS,
        (_FeedOption("indicative", False), _FeedOption("opra", True)),
    ),
    _FeedMeta(
        "stockHistorical",
        ALPACA_STOCK_FEED_HISTORICAL_ENV,
        "Equity bars (historical)",
        "SIP is 100% of US volume and is free for anything older than 15 "
        "minutes.",
        STOCK_HISTORICAL_FEEDS,
        (_FeedOption("sip", False), _FeedOption("iex", False)),
    ),
    _FeedMeta(
        "stockRealtime",
        ALPACA_STOCK_FEED_REALTIME_ENV,
        "Equity quotes (real-time)",
        "Real-time SIP requires the paid plan; IEX is ~2.5% of US volume.",
        STOCK_REALTIME_FEEDS,
        (_FeedOption("iex", False), _FeedOption("sip", True)),
    ),
)

_FEED_BY_KEY: Final[Mapping[str, _FeedMeta]] = {
    meta.key: meta for meta in _FEED_CATALOGUE
}


class _KeyMeta(NamedTuple):
    env_var: str
    purpose: str
    #: ``True`` for a credential that is *meant* to be absent, so its absence
    #: renders as a fact rather than as a warning.
    optional: bool


#: Every credential named in ``.env.example``, with what it is for.
#:
#: ``COROLLARY_DATABASE_URL`` is absent on purpose: it is a SQLite path, not a
#: credential, and listing it under "API keys" would imply otherwise.
_KEY_CATALOGUE: Final[tuple[_KeyMeta, ...]] = (
    _KeyMeta(ALPACA_PAPER_KEY_ENV, "Paper execution + market data", False),
    _KeyMeta(ALPACA_PAPER_SECRET_ENV, "Paper execution + market data", False),
    _KeyMeta(ALPACA_LIVE_KEY_ENV, "Cash execution — absent until Phase 7", True),
    _KeyMeta(ALPACA_LIVE_SECRET_ENV, "Cash execution — absent until Phase 7", True),
    _KeyMeta("ANTHROPIC_API_KEY", "LLM enrichment + Research chat", False),
    _KeyMeta("FINNHUB_API_KEY", "News, sentiment, earnings calendar", False),
    _KeyMeta("FRED_API_KEY", "Macro series", False),
    _KeyMeta("MASSIVE_API_KEY", "Market data fallback — unused", True),
    _KeyMeta("DISCORD_WEBHOOK_URL", "Discord notification channel", True),
)

#: Event order for the routing matrix, taken from the shipped table rather
#: than restated. PRD §10's defaults are the order the page lists them in.
_ROUTE_EVENTS: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(event for event, _channel, _enabled in NOTIFICATION_ROUTE_DEFAULTS)
)


# --------------------------------------------------------------------------
# Money at the request boundary
# --------------------------------------------------------------------------


def _wire_decimal(value: Any) -> Decimal:
    """A ceiling as an exact ``Decimal``. **A JSON float is refused.**

    ``json`` decodes ``7.5`` to an IEEE double, and this value is *stored* --
    which is exactly the path CLAUDE.md keeps floats off. A quoted string and
    a whole number both parse exactly, so refusing the third shape costs a
    caller one pair of quotes and closes the only way a double could get in.
    """
    if isinstance(value, float):
        logger.warning(
            "refused a JSON float at the settings request boundary",
            extra={
                "event": "settings_request_refused",
                "rule": (
                    "money crosses the request boundary as a string or a whole "
                    "number, never as a float (CLAUDE.md Conventions)"
                ),
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )
        raise ValueError(
            "send a risk ceiling as a JSON string like \"7.5\" or as a whole "
            "number. A JSON float is an IEEE double by the time it is parsed, "
            "and this value is stored exactly"
        )
    try:
        parsed = as_decimal(value)
    except (TypeError, WireFormatError) as exc:
        raise ValueError(f"{value!r} is not a number") from exc
    if parsed is None:
        raise ValueError("a risk ceiling must be a number, not an empty value")
    return parsed


WireDecimal = Annotated[Decimal, BeforeValidator(_wire_decimal)]


def _money_text(value: Decimal) -> str:
    """The audit-log spelling of a stored ceiling.

    ``format(..., "f")`` rather than ``str()``, matching what
    ``Money.process_bind_param`` writes: ``str(Decimal('1E+2'))`` is
    ``'1E+2'``, so the same number would reach the log under two spellings
    depending on how the ``Decimal`` was built.
    """
    return format(value, "f")


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------
#
# Local to this module rather than in ``api/schemas.py``, which is the
# *response* contract checked field by field against ``web/src/lib/types.ts``.
# A request body has no counterpart there to be checked against.


class RiskLimitUpdate(ApiModel):
    key: RiskLimitKey
    value: WireDecimal


class RiskLimitsUpdate(ApiModel):
    limits: Annotated[list[RiskLimitUpdate], Field(min_length=1)]


class DataFeedUpdate(ApiModel):
    key: FeedKey
    value: str


class DataFeedsUpdate(ApiModel):
    feeds: Annotated[list[DataFeedUpdate], Field(min_length=1)]


class NotificationRouteUpdate(ApiModel):
    event: NotificationEvent
    bell: bool
    discord: bool


class NotificationRoutesUpdate(ApiModel):
    routes: Annotated[list[NotificationRouteUpdate], Field(min_length=1)]


# --------------------------------------------------------------------------
# Refusals and the audit log
# --------------------------------------------------------------------------


def _refuse(
    *,
    code: str,
    message: str,
    rule: str,
    field: str,
    value: str,
    at: datetime,
    correlation_id: str,
) -> ApiError:
    """Build the 422, and record why it happened.

    Rule 8: *"A rejected order records the rule that rejected it, the inputs,
    and the timestamp. Silent rejection is a bug."* A refused configuration
    write has the same symptom -- a setting that will not take with nothing
    anywhere saying why -- so it gets the same treatment.

    Returns the error rather than raising it, so the call site reads
    ``raise _refuse(...)`` and the refusal is visible at the point of the
    decision.
    """
    logger.warning(
        "settings write refused: %s",
        message,
        extra={
            "event": "settings_rejected",
            "rule": rule,
            "code": code,
            "field": field,
            "value": value,
            "at": at.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    return ApiError(status_code=422, code=code, message=message)


def _audit(
    session: Session,
    *,
    category: str,
    field: str,
    previous: str,
    new: str,
    at: datetime,
    correlation_id: str,
) -> AuditChange:
    """Record one changed cell, in the one log that spans all three tables.

    Returns exactly what it wrote. The route's notification to the bell and
    Discord is built from these returns and nothing else, so the audit log and
    the message cannot disagree about what changed.

    PRD §8.7: *"one log rather than three -- on a bad day the question is
    simply whether anything changed first."*

    Only *raises* confirm on the client, because nagging on the safe direction
    trains people to dismiss the dialog that matters. The server audits every
    change **in both directions**: a record that covers half the changes is
    not a record.
    """
    session.add(
        AuditLog(
            at=at,
            category=category,
            field=field,
            previous_value=previous,
            new_value=new,
        )
    )
    logger.info(
        "settings changed: %s %s -> %s",
        field,
        previous,
        new,
        extra={
            "event": "settings_changed",
            "rule": "every config change is audit-logged, in both directions",
            "category": category,
            "field": field,
            "previous_value": previous,
            "new_value": new,
            "at": at.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    return AuditChange(category=category, field=field, previous=previous, new=new)


def _reject_duplicates(
    keys: list[str], *, what: str, code: str, at: datetime, correlation_id: str
) -> None:
    """Refuse a request that names the same thing twice.

    Which of the two wins is not something a caller should have to know, and
    last-one-wins is a rule nobody would write down on purpose.
    """
    seen: set[str] = set()
    repeated: set[str] = set()
    for key in keys:
        if key in seen:
            repeated.add(key)
        seen.add(key)

    duplicates = sorted(repeated)
    if duplicates:
        raise _refuse(
            code=code,
            message=(
                f"{what} named more than once in one request: "
                f"{', '.join(duplicates)}. Send each exactly once."
            ),
            rule="one value per key per request; last-one-wins is not a rule",
            field=", ".join(duplicates),
            value="",
            at=at,
            correlation_id=correlation_id,
        )


# --------------------------------------------------------------------------
# Risk limits
# --------------------------------------------------------------------------


def _limits(session: Session) -> list[RiskLimit]:
    """The five ceilings, with ``None`` where no row is configured.

    Reads through :func:`~corollary.db.seed.risk_limits`, which is the
    supported way to get at this table: it loads the rows and compares in
    Python because SQL cannot order or aggregate a ``Money`` column without
    lying about it.

    A row whose key is not one of the five is **logged and omitted** -- it
    cannot be typed, and the honest options are to drop it loudly or to widen
    the contract. Nothing in this app writes one.
    """
    configured = risk_limits(session)

    unknown = sorted(set(configured) - _KNOWN_LIMIT_KEYS)
    if unknown:
        logger.warning(
            "risk_limit holds %d key(s) the API contract does not declare",
            len(unknown),
            extra={
                "event": "settings_unknown_risk_limit",
                "rule": "an undeclared limit is omitted from the response, never renamed",
                "keys": unknown,
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )

    rows: list[RiskLimit] = []
    for meta in _LIMIT_CATALOGUE:
        limit_range = RISK_LIMIT_RANGES[meta.key]
        unit: RiskLimitUnit = "count" if limit_range.whole else "%"
        rows.append(
            RiskLimit(
                key=meta.key,
                label=meta.label,
                # None means no ceiling is configured. Never 0, never the
                # shipped default, never an omitted row.
                value=configured.get(meta.key),
                unit=unit,
                min=limit_range.low,
                max=limit_range.high,
                help=meta.help,
            )
        )
    return rows


@router.get("/limits", summary="The five risk ceilings, or null where unset")
def read_limits(session: SessionDep) -> list[RiskLimit]:
    return _limits(session)


@router.put("/limits", summary="Edit a ceiling. The engine still enforces it.")
def write_limits(
    body: RiskLimitsUpdate,
    session: SessionDep,
    request: Request,
    background: BackgroundTasks,
) -> list[RiskLimit]:
    """Validate every update, then apply them all or none of them.

    Atomic on purpose. Half-applying a set of ceilings leaves the engine
    enforcing a combination nobody chose and the page showing a state nobody
    saved -- and the half that applied would be the half that happened to sort
    first.

    Rule 4: this edits what is *stored*. ``RiskManager.approve()`` decides what
    is allowed, and it reads this table rather than anything the client sent.
    """
    at = datetime.now(timezone.utc)
    correlation_id = str(uuid.uuid4())

    _reject_duplicates(
        [update.key for update in body.limits],
        what="risk limit",
        code="duplicate_risk_limit",
        at=at,
        correlation_id=correlation_id,
    )

    for update in body.limits:
        try:
            validate_risk_limit(update.key, update.value)
        except ValueError as exc:
            raise _refuse(
                code="invalid_risk_limit",
                message=str(exc),
                rule=(
                    "a ceiling is validated server-side against its per-limit "
                    "range before it is stored (CLAUDE.md rule 4)"
                ),
                field=update.key,
                value=_money_text(update.value),
                at=at,
                correlation_id=correlation_id,
            ) from exc

    changes: list[AuditChange] = []
    for update in body.limits:
        row = session.get(RiskLimitRow, update.key)
        if row is None:
            session.add(RiskLimitRow(key=update.key, value=update.value))
            previous = UNSET
        elif row.value == update.value:
            # Equal money is equal, whatever its spelling. An audit log that
            # fills with "7 -> 7" is one nobody reads.
            continue
        else:
            previous = _money_text(row.value)
            row.value = update.value
        changes.append(
            _audit(
                session,
                category="risk",
                field=update.key,
                previous=previous,
                new=_money_text(update.value),
                at=at,
                correlation_id=correlation_id,
            )
        )

    session.commit()
    # After the commit, from the audit values, once for the whole request.
    notify_after_response(
        request,
        background,
        settings_notice(
            OperatorEvent.RISK_LIMITS_CHANGED,
            changes,
            at=at,
            correlation_id=correlation_id,
        ),
    )
    return _limits(session)


# --------------------------------------------------------------------------
# Feeds
# --------------------------------------------------------------------------


def _plan(env: Mapping[str, str]) -> str:
    """Which data plan this account is on, from the environment.

    An unrecognised value falls back to :data:`BASIC_PLAN` with a warning
    rather than raising. Refusing to serve the Settings page over a typo in a
    plan name would take down the page you would use to fix it, and Basic is
    the restrictive direction -- the failure is a refused upgrade, not an
    unlocked feed nobody paid for.
    """
    raw = (env.get(ALPACA_DATA_PLAN_ENV) or "").strip().lower()
    if not raw:
        return BASIC_PLAN
    if raw not in _PLAN_LABELS:
        logger.warning(
            "%s=%r is not a plan this app knows; reading it as %s",
            ALPACA_DATA_PLAN_ENV,
            raw,
            BASIC_PLAN,
            extra={
                "event": "settings_unknown_plan",
                "rule": (
                    "an unknown plan reads as basic -- the restrictive "
                    "direction, so a typo cannot unlock a paid feed"
                ),
                "accepted": sorted(_PLAN_LABELS),
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return BASIC_PLAN
    return raw


def _feed_help(meta: _FeedMeta, stored: str, env: Mapping[str, str]) -> str:
    """The static help, plus whatever the running process disagrees about.

    The provider reads these variables from the environment; Settings edits
    the table. Until the process restarts those are two different values, and
    a page that shows the stored one without saying so is claiming the engine
    is doing something it is not.
    """
    running = (env.get(meta.env_var) or "").strip().lower()
    if not running:
        return (
            f"{meta.help} {meta.env_var} is not set in this process, so the "
            "provider refuses to start rather than defaulting — a silent "
            "default here is how a volume threshold ends up measured against "
            "the wrong feed."
        )
    if running != stored:
        return (
            f"{meta.help} The running process is using {running!r} from "
            f"{meta.env_var}; this setting takes effect on restart."
        )
    return meta.help


def _feeds(session: Session, env: Mapping[str, str]) -> list[DataFeed]:
    stored = {row.key: row.value for row in session.scalars(select(DataFeedRow)).all()}

    rows: list[DataFeed] = []
    for meta in _FEED_CATALOGUE:
        value = stored.get(meta.env_var)
        if value is None:
            # Seeded on every startup, so reaching this means somebody deleted
            # the row. There is no way to say "unconfigured" in a non-nullable
            # contract field, so it is omitted loudly rather than defaulted --
            # a default here is the exact failure the feed rules exist to stop.
            logger.warning(
                "data_feed has no row for %s; omitting it from the response",
                meta.env_var,
                extra={
                    "event": "settings_missing_feed_row",
                    "rule": "an unconfigured feed is omitted, never defaulted",
                    "field": meta.env_var,
                    "at": datetime.now(timezone.utc).isoformat(),
                },
            )
            continue
        rows.append(
            DataFeed(
                key=meta.key,
                env_var=meta.env_var,
                label=meta.label,
                value=value,
                help=_feed_help(meta, value, env),
            )
        )
    return rows


def _feed_warning(meta: _FeedMeta, value: str, *, correlation_id: str) -> None:
    """Say what a legal choice costs, at the moment it is made.

    Historical bars on IEX is the dangerous one and it is dangerous *quietly*:
    ``min_avg_volume`` in a strategy YAML is compared against whatever feed
    produced the bars, so a 5,000,000 threshold measured on IEX is filtering on
    a fortieth of real volume. Nothing errors; the scanner just returns a
    different universe, and nothing in the strategy document records which feed
    it was measured against.

    Real-time IEX gets no warning: it is the only thing Basic serves live, so
    warning about it would be warning about the plan.
    """
    if meta.key == "stockHistorical" and value == "iex":
        logger.warning(
            "historical equity bars set to IEX",
            extra={
                "event": "settings_feed_warning",
                "rule": (
                    "min_avg_volume in a strategy is measured against whatever "
                    "feed produced the bars; IEX is ~2.5% of US volume and SIP "
                    "is free for anything older than 15 minutes"
                ),
                "field": meta.env_var,
                "value": value,
                "at": datetime.now(timezone.utc).isoformat(),
                "correlation_id": correlation_id,
            },
        )


@router.get("/feeds", summary="The three ALPACA_*_FEED settings")
def read_feeds(session: SessionDep, env: EnvDep) -> list[DataFeed]:
    return _feeds(session, env)


@router.put("/feeds", summary="Select a feed, within what the plan may serve")
def write_feeds(
    body: DataFeedsUpdate,
    session: SessionDep,
    env: EnvDep,
    request: Request,
    background: BackgroundTasks,
) -> list[DataFeed]:
    """Store a feed selection, refusing anything the plan cannot serve.

    The client marks upgrade-only values in the control; the server refuses
    them. That split is rule 4's shape applied to a feed: requesting ``opra``
    on Basic returns an **auth error** from Alpaca rather than empty data, so
    storing it moves the failure into the middle of a poll, several layers
    from its cause.
    """
    at = datetime.now(timezone.utc)
    correlation_id = str(uuid.uuid4())
    plan = _plan(env)

    _reject_duplicates(
        [update.key for update in body.feeds],
        what="feed",
        code="duplicate_feed",
        at=at,
        correlation_id=correlation_id,
    )

    resolved: list[tuple[_FeedMeta, str]] = []
    for update in body.feeds:
        meta = _FEED_BY_KEY[update.key]
        value = update.value.strip().lower()

        option = next((o for o in meta.options if o.value == value), None)
        if value not in meta.accepted or option is None:
            raise _refuse(
                code="invalid_feed_value",
                message=(
                    f"{value!r} is not a value {meta.label} can be set to. "
                    f"Accepted: {', '.join(o.value for o in meta.options)}."
                ),
                rule=(
                    "a feed name is validated against what the endpoint "
                    "accepts and what Settings offers, before it is stored"
                ),
                field=meta.env_var,
                value=value,
                at=at,
                correlation_id=correlation_id,
            )
        if option.requires_upgrade and plan != PAID_PLAN:
            raise _refuse(
                code="feed_requires_upgrade",
                message=(
                    f"{value!r} needs a plan upgrade. This account is on "
                    f"{_PLAN_LABELS[plan]}, and Alpaca answers an unentitled "
                    "feed with an auth error rather than empty data — so it is "
                    "refused here instead of failing mid-poll."
                ),
                rule=(
                    "a feed the plan cannot serve is refused server-side; the "
                    "client only marks it (CLAUDE.md rule 4)"
                ),
                field=meta.env_var,
                value=value,
                at=at,
                correlation_id=correlation_id,
            )
        resolved.append((meta, value))

    changes: list[AuditChange] = []
    for meta, value in resolved:
        _feed_warning(meta, value, correlation_id=correlation_id)
        row = session.get(DataFeedRow, meta.env_var)
        if row is None:
            session.add(DataFeedRow(key=meta.env_var, value=value))
            previous = UNSET
        elif row.value == value:
            continue
        else:
            previous = row.value
            row.value = value
        changes.append(
            _audit(
                session,
                category="feed",
                # The stored key, which for this table *is* the variable
                # name. ``models.AuditLog`` names ALPACA_OPTIONS_FEED as its
                # own example.
                field=meta.env_var,
                previous=previous,
                new=value,
                at=at,
                correlation_id=correlation_id,
            )
        )
        if (env.get(meta.env_var) or "").strip().lower() != value:
            logger.warning(
                "%s now stored as %r; the running process still reads %r",
                meta.env_var,
                value,
                (env.get(meta.env_var) or "").strip().lower(),
                extra={
                    "event": "settings_feed_divergence",
                    "rule": (
                        "the provider reads the environment and Settings edits "
                        "the table; the two diverge until a restart"
                    ),
                    "field": meta.env_var,
                    "at": at.isoformat(),
                    "correlation_id": correlation_id,
                },
            )

    session.commit()
    # Feed names and values only -- the audit values, never ``env``, so no key
    # or plan credential can reach the message (rule 6).
    notify_after_response(
        request,
        background,
        settings_notice(
            OperatorEvent.DATA_FEEDS_CHANGED,
            changes,
            at=at,
            correlation_id=correlation_id,
        ),
    )
    return _feeds(session, env)


# --------------------------------------------------------------------------
# Notification routing
# --------------------------------------------------------------------------


def _routes(session: Session) -> list[NotificationRoute]:
    """One row per event, both channels on it.

    The table stores a row per ``(event, channel)`` so adding SMS later is a
    data change; the page edits a matrix. A **missing** pair reads as off,
    which is the behavioural truth -- an unconfigured route delivers nothing --
    and is logged, because seeding guarantees a row for every event ×
    channel pair.
    """
    stored = {
        (row.event, row.channel): row.enabled
        for row in session.scalars(select(NotificationRouteRow)).all()
    }

    missing = [
        f"{event}.{channel}"
        for event in _ROUTE_EVENTS
        for channel in ("bell", "discord")
        if (event, channel) not in stored
    ]
    if missing:
        logger.warning(
            "notification_route is missing %d cell(s); reading them as off",
            len(missing),
            extra={
                "event": "settings_missing_route_row",
                "rule": "an unconfigured route delivers nothing, and says so",
                "cells": missing,
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )

    rows: list[NotificationRoute] = []
    for event in _ROUTE_EVENTS:
        if event not in _KNOWN_EVENTS:
            logger.warning(
                "notification_route holds an event the contract does not declare",
                extra={
                    "event": "settings_unknown_notification_event",
                    "rule": "an undeclared event is omitted, never renamed",
                    "field": event,
                    "at": datetime.now(timezone.utc).isoformat(),
                },
            )
            continue
        rows.append(
            NotificationRoute(
                # Narrowed by the guard directly above: ``_KNOWN_EVENTS`` is
                # the contract's own union, and a frozenset membership test is
                # not something a type checker can narrow through.
                event=cast(NotificationEvent, event),
                bell=stored.get((event, "bell"), False),
                discord=stored.get((event, "discord"), False),
            )
        )
    return rows


@router.get("/routes", summary="PRD §10's routing matrix")
def read_routes(session: SessionDep) -> list[NotificationRoute]:
    return _routes(session)


@router.put("/routes", summary="Route an event to a channel, or stop routing it")
def write_routes(
    body: NotificationRoutesUpdate,
    session: SessionDep,
    request: Request,
    background: BackgroundTasks,
) -> list[NotificationRoute]:
    """Apply a routing change, auditing the cells that actually changed.

    "Order filled changed" is not a change -- which channel it changed on is
    the whole content of the row -- so the audit field is ``event.channel``,
    exactly as ``notificationAuditField`` spells it.
    """
    at = datetime.now(timezone.utc)
    correlation_id = str(uuid.uuid4())

    _reject_duplicates(
        [update.event for update in body.routes],
        what="notification event",
        code="duplicate_notification_route",
        at=at,
        correlation_id=correlation_id,
    )

    changes: list[AuditChange] = []
    for update in body.routes:
        for channel, enabled in (("bell", update.bell), ("discord", update.discord)):
            row = session.get(NotificationRouteRow, (update.event, channel))
            if row is None:
                session.add(
                    NotificationRouteRow(
                        event=update.event, channel=channel, enabled=enabled
                    )
                )
                previous = UNSET
            elif row.enabled == enabled:
                continue
            else:
                previous = _on_off(row.enabled)
                row.enabled = enabled
            changes.append(
                _audit(
                    session,
                    category="notification",
                    field=f"{update.event}.{channel}",
                    previous=previous,
                    new=_on_off(enabled),
                    at=at,
                    correlation_id=correlation_id,
                )
            )

        if (
            update.event in _CRITICAL_EVENTS
            and not update.bell
            and not update.discord
        ):
            logger.warning(
                "%s is now routed to no channel at all",
                update.event,
                extra={
                    "event": "settings_critical_event_silenced",
                    "rule": (
                        "silencing a critical event everywhere is permitted and "
                        "recorded; a rule-9 alert routed nowhere is how the "
                        "engine ends up halted and silent"
                    ),
                    "notification_event": update.event,
                    "at": at.isoformat(),
                    "correlation_id": correlation_id,
                },
            )

    session.commit()
    # Scheduled after the commit, and routed when it runs: the new routing
    # governs the notification about itself. Switching Discord off for
    # ``notification_routes_changed`` means this change is not posted; the
    # audit log above is the complete record either way.
    notify_after_response(
        request,
        background,
        settings_notice(
            OperatorEvent.NOTIFICATION_ROUTES_CHANGED,
            changes,
            at=at,
            correlation_id=correlation_id,
        ),
    )
    return _routes(session)


def _on_off(enabled: bool) -> str:
    """How a routing cell reads in the audit log.

    ``on``/``off`` rather than ``true``/``false``: the log is read by a person
    asking what changed, and "Engine error — Discord: on → off" is a sentence.
    """
    return "on" if enabled else "off"


# --------------------------------------------------------------------------
# API keys -- presence, and nothing else
# --------------------------------------------------------------------------


@router.get("/keys", summary="Which credentials are configured. No values.")
def read_keys(env: EnvDep, registry: RegistryDep) -> list[ApiKeyPresence]:
    """Rule 6, as an endpoint.

    *"The Settings UI shows masked presence only — it never renders a key."*
    There is no value here, no mask and no last-four: ``PK****4F2A`` renders
    four characters of a secret, and a wrong key cannot be diagnosed by
    squinting at its suffix.

    **The live pair is answered by the service registry, not by this
    environment.** ``ServiceRegistry`` is what decides whether Cash is
    available at all, and if this panel answered from a second source the page
    could report a live key present while every cash request 409s. Two copies
    of one fact is how they come to disagree.
    """
    live_missing = set(registry.missing_live_credentials)
    return [
        ApiKeyPresence(
            env_var=meta.env_var,
            purpose=meta.purpose,
            present=(
                meta.env_var not in live_missing
                if meta.env_var in LIVE_CREDENTIAL_ENV_VARS
                else _is_set(env, meta.env_var)
            ),
            optional=meta.optional,
        )
        for meta in _KEY_CATALOGUE
    ]


# --------------------------------------------------------------------------
# The audit log
# --------------------------------------------------------------------------


@router.get("/audit", summary="PRD §8.7's one log across risk, feed and routing")
def read_audit(
    session: SessionDep,
    page: Annotated[int, Query(ge=0, description="Zero-based.")] = 0,
    page_size: Annotated[
        int, Query(ge=1, le=200, alias="pageSize", description="Rows per page.")
    ] = 50,
) -> Page[AuditLogEntry]:
    """Newest first, paginated, with ``total`` counting every row.

    The tie-break on ``id`` is not decoration: one request may change five
    ceilings, and those five rows carry the same ``at``. Ordering on ``at``
    alone would let them come back in whatever order SQLite happened to store
    them, which on a page that reads top-down looks like the changes happened
    in an order they did not.

    ``at`` and ``id`` are ordinary columns. Neither is ``Money``, so SQL may
    sort them -- the guard that raises applies to the money columns, and this
    table holds none.
    """
    total = session.scalar(select(func.count()).select_from(AuditLog)) or 0
    rows = session.scalars(
        select(AuditLog)
        .order_by(AuditLog.at.desc(), AuditLog.id.desc())
        .offset(page * page_size)
        .limit(page_size)
    ).all()

    return Page[AuditLogEntry](
        items=[
            AuditLogEntry(
                id=str(row.id),
                time=row.at,
                # ``ck_audit_log_category`` is a CHECK constraint on the
                # column, so the three values are enforced in the schema
                # rather than assumed here; pydantic re-checks on the way out.
                category=cast(AuditCategory, row.category),
                field=row.field,
                previous_value=row.previous_value,
                new_value=row.new_value,
            )
            for row in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page + 1) * page_size < total,
    )


# --------------------------------------------------------------------------
# Data sources -- decision 8's per-panel markers
# --------------------------------------------------------------------------


def _alpaca_source(session: Session, env: Mapping[str, str]) -> DataSourceStatus:
    """Alpaca's status, from the two facts the server actually has.

    ``connected`` here means *credentials are configured and the engine is not
    halted*. There is no live connection probe: the watchdog that owns that
    question lands with ``EngineRuntime``, and opening a vendor request every
    time somebody renders Settings would spend the rate limit on a status
    light. The detail says what the claim rests on rather than implying more.

    A missing ``engine_state`` row reads as halted, for the same reason
    ``api/routes/engine.py`` recreates it halted: **absence of state is not
    evidence of a healthy engine.**
    """
    missing = [
        name
        for name in (ALPACA_PAPER_KEY_ENV, ALPACA_PAPER_SECRET_ENV)
        if not _is_set(env, name)
    ]
    plan = _PLAN_LABELS[_plan(env)]
    name = "Alpaca (execution + data)"

    if missing:
        return DataSourceStatus(
            name=name,
            status="disconnected",
            detail=(
                f"{' and '.join(missing)} not set. No account, positions or "
                "quotes until they are."
            ),
        )

    state = session.get(EngineState, ENGINE_STATE_ID)
    if state is None:
        return DataSourceStatus(
            name=name,
            status="degraded",
            detail=(
                f"Paper account, {plan} plan. The engine state row is missing — "
                "absence of state is not evidence of a healthy engine."
            ),
        )
    if state.halted:
        reason = state.halted_reason or "no reason recorded"
        return DataSourceStatus(
            name=name,
            status="degraded",
            detail=(
                f"Paper account, {plan} plan. Engine halted — {reason}. "
                "Recovery is an explicit resume; nothing resumes itself."
            ),
        )
    return DataSourceStatus(
        name=name,
        status="connected",
        detail=(
            f"Paper account, {plan} plan. Credentials configured and the engine "
            "is not halted; connection health is the watchdog's to report."
        ),
    )


def _keyed_source(
    env: Mapping[str, str], *, name: str, env_var: str, waiting_for: str
) -> DataSourceStatus:
    """A source whose key is configured and whose integration is not built yet.

    Decision 8: Settings mixes real and mock within one page, so the markers
    sit on the affected panels. **A present key is not a wired integration**,
    and reporting one as ``connected`` would be the invented-number failure in
    a status light — so it reads ``disconnected`` and the detail says exactly
    which of the two is missing.
    """
    if not _is_set(env, env_var):
        return DataSourceStatus(
            name=name,
            status="disconnected",
            detail=f"{env_var} not set. {waiting_for}",
        )
    return DataSourceStatus(
        name=name,
        status="disconnected",
        detail=f"Key present; nothing reads it yet. {waiting_for}",
    )


@router.get("/sources", summary="What is live, and what is still a fixture")
def read_sources(session: SessionDep, env: EnvDep) -> list[DataSourceStatus]:
    """Decision 8, answerable rather than asserted.

    *"News, Research and Settings' sentiment readout carry a small marker in
    the same words as Research's scripted-chat marker. Settings mixes real and
    mock within one page, so its markers sit on the affected panels rather
    than the page title."*

    This is the route the affected panels read. Deterministic -- the same
    inputs produce the same list, in the same order -- because a status panel
    that reshuffles is one nobody trusts.
    """
    return [
        _alpaca_source(session, env),
        _keyed_source(
            env,
            name="Finnhub (news + calendar)",
            env_var="FINNHUB_API_KEY",
            waiting_for=(
                "Market cap, the news feed and the earnings calendar are later "
                "steps."
            ),
        ),
        _keyed_source(
            env,
            name="FRED (macro)",
            env_var="FRED_API_KEY",
            waiting_for=(
                "The macro series and the risk-free rate behind derived greeks "
                "are later steps."
            ),
        ),
        DataSourceStatus(
            name="StockTwits (social)",
            status="disconnected",
            detail=(
                "Not configured. Social attention is a later phase; the "
                "attention panel is sample data."
            ),
        ),
        DataSourceStatus(
            name="News sentiment (PRD §9)",
            status="disconnected",
            detail=(
                "Sample data — the accuracy table on this page is a fixture. "
                "No sentiment has been scored and none has been measured "
                "against realized return, so nothing here can be demoted by "
                "the floor. Alpaca and Finnhub both publish news without a "
                "sentiment field, and Finnhub's sentiment endpoint is "
                "paywalled."
            ),
        ),
    ]
