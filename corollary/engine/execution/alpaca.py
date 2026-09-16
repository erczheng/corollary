"""``AlpacaBroker`` — the Alpaca implementation of ``BrokerAccount``.

This is one of exactly two files in the codebase allowed to hold the Alpaca
vendor surface (the other is ``data/providers/alpaca.py``, for market data).
See CLAUDE.md, "Working with market data". Nothing outside these two files
should know an endpoint path or a JSON field name.

**Read-only, and structurally so.** ``BrokerExecution`` — submit, cancel,
replace — does not exist until Phase 6, so there is no order path in this
codebase for anything to reach. Rule 1 is satisfied by absence rather than by
discipline. Every request below is a ``GET``, and
``tests/engine/execution/test_no_order_path.py`` asserts that neither vendor
file contains another verb.

Why raw ``httpx`` and not the ``alpaca-py`` SDK
-----------------------------------------------

The same reason step 3 gave for the market-data provider, restated here
because it applies to this half too: ``alpaca-py`` annotates money as
``float``. CLAUDE.md's first convention is *"Money as ``Decimal``, never
``float``"*, and ``db/types.Money`` goes to the length of storing TEXT on
SQLite to keep that true at the database boundary. A vendor client that
converts prices to IEEE doubles on ingest defeats it at the *entry* boundary,
where the loss is unrecoverable.

On this host the argument has a specific edge. Most of the trading API returns
money as **strings** (``"avg_entry_price": "8.21"``), which parse straight to
``Decimal`` and make the whole thing easy. But ``GET
/v2/account/portfolio/history`` returns **bare JSON numbers** (``"base_value":
8413.04``, ``"equity": [8425.21, ...]``), so a parser written on the
reasonable belief that "the trading API sends strings" would put the entire
equity curve through doubles and nothing would say so. Hence
:func:`corollary.wire.decode_json` — ``json.loads(text,
parse_float=Decimal)`` — as the only entry point for a response body on this
path. ``httpx``'s ``response.json()`` offers no hook for it and is never
called here.

Two token buckets, not one
--------------------------

``data.alpaca.markets`` and ``paper-api.alpaca.markets`` each carry their own
200 requests per minute. This broker bills the trading host only, through the
process-wide :func:`~corollary.ratelimit.default_limiter`. A shared bucket
would throttle the process to half its real allowance, invisibly: nothing
errors, the poll just runs at half cadence.

Paper is the default
--------------------

:meth:`AlpacaBroker.from_env` builds paper credentials. Rule 5: every cold
start comes up in Paper, and nothing in this phase calls
``AlpacaCredentials.live_from_env``.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Final, TypeVar

import httpx

from corollary.data.providers.alpaca import AlpacaCredentials
from corollary.engine.execution.interface import (
    Account,
    Activity,
    ActivityCategory,
    BrokerAccount,
    BrokerAuthError,
    BrokerError,
    BrokerPosition,
    BrokerRateLimitedError,
    EquityPoint,
    FillSide,
    NonTradeActivity,
    Order,
    OrderClass,
    OrderQueryStatus,
    OrderSide,
    PortfolioHistory,
    PositionIntent,
    PositionSide,
    TradeActivity,
    TradeUpdate,
)
from corollary.ratelimit import HostRateLimiter, default_limiter
from corollary.sockets import (
    JSON_CODEC,
    SocketConnect,
    StreamActivityRecorder,
    VendorSocket,
    VendorStream,
    utcnow,
)
from corollary.wire import (
    ERROR_BODY_MAX,
    as_date,
    as_datetime,
    as_decimal,
    as_int,
    clean_params,
    decode_json,
    require_aware,
    rfc3339,
    translating,
    vendor_detail,
)

__all__ = ["AlpacaBroker", "ERROR_BODY_MAX"]

logger = logging.getLogger(__name__)

#: Alpaca's ceiling on the activities endpoint (``maximum: 100`` in the
#: OpenAPI document). The reason ingestion paginates at all.
ACTIVITIES_PAGE_MAX: Final = 100

#: The orders ceiling. Alpaca's own default is 50, which would silently
#: truncate any real order history — and a truncated order history is a
#: two-hop join missing its parents, which looks exactly like a book with no
#: spreads in it. Stated explicitly, and a full page is logged.
ORDERS_LIMIT_MAX: Final = 500

#: How many activity pages one call may follow before it gives up.
#:
#: Deliberately well under the per-host minute ceiling, for the same reason
#: the market-data provider caps its own pagination: a single runaway call
#: that consumed the whole 200/min budget would stall every concurrent poll
#: for the best part of a minute and only then raise. At 100 rows a page this
#: is 5,000 activities in one call; beyond that a caller passes ``since_id``
#: or ``after`` and resumes, which is what incremental ingestion does anyway.
_MAX_PAGES: Final = 50

#: Oldest first, always. Alpaca defaults to ``desc``, and the resume cursor
#: only works one way round: ``page_token`` is *"the ID of the last item on
#: your current page"*, so resuming from the newest id already held must page
#: **forwards** into newer rows. Descending, the same cursor would walk
#: backwards through history that is already stored.
_ASCENDING: Final = "asc"


# --------------------------------------------------------------------------
# What a vendor error body may write into a log
# --------------------------------------------------------------------------

#: :func:`~corollary.wire.vendor_detail` and its bound live in
#: :mod:`corollary.wire`, vendor-neutral, because the market-data provider
#: needs the identical treatment on its own ``_get`` -- it sends the same two
#: auth headers and its errors reach the same logs. They were here first, and
#: while they were, the provider had no redaction and no bound at all.
#:
#: :data:`~corollary.wire.ERROR_BODY_MAX` is re-exported rather than aliased:
#: it is part of this module's observable behaviour, and a caller reasoning
#: about how much of a body reaches a log should not have to know which module
#: the constant moved to.

# --------------------------------------------------------------------------
# Wire decoding
# --------------------------------------------------------------------------

#: The coercions live in :mod:`corollary.wire`, vendor-neutral, because the
#: market-data provider needs the identical decode path and ``as_datetime``'s
#: nanosecond truncation must not exist twice. Re-bound here and wrapped in
#: :func:`corollary.wire.translating`, so a malformed value surfaces as the
#: :class:`BrokerError` this module's callers handle. A ``TypeError`` is
#: deliberately not translated: a float reaching the money path is a bug in
#: this repository, not a vendor response.
_decode = translating(BrokerError, decode_json)
_as_decimal = translating(BrokerError, as_decimal)
_as_int = translating(BrokerError, as_int)
_as_datetime = translating(BrokerError, as_datetime)
_as_date = translating(BrokerError, as_date)


def _log_refusal(rule: str, **inputs: Any) -> str:
    """Log a refusal with the rule, the inputs and the time; return the detail.

    Rule 8 is written about rejected *orders*, and there are none in this
    phase. The principle it states — *"silent rejection is a bug — you will
    need this the first time the bot does nothing when you expected it to
    trade"* — applies just as squarely to a response this layer refuses to
    parse, because the visible symptom is identical: a page that is emptier
    than the account.

    Separate from :func:`_reject` because not every refusal is fatal to the
    response that carried it. A cashflow bucket whose length cannot be
    reconciled with the curve is dropped while the curve itself stands, and
    that drop has to leave the same record as a refusal that raises —
    otherwise it is precisely the silence rule 8 is about, and the symptom
    (an empty fee column) is indistinguishable from having paid no fees.
    """
    detail = ", ".join(f"{key}={value!r}" for key, value in sorted(inputs.items()))
    logger.warning(
        "broker rejected a response: %s (%s)",
        rule,
        detail,
        extra={
            "rule": rule,
            "inputs": inputs,
            "at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return detail


def _reject(rule: str, **inputs: Any) -> BrokerError:
    """Log the refusal, then build the error for the caller to raise."""
    return BrokerError(f"{rule} ({_log_refusal(rule, **inputs)})")


def _need(raw: Mapping[str, Any], key: str, what: str) -> Decimal:
    """A money field the vendor always sends. ``None`` here is a broken response."""
    value = _as_decimal(raw.get(key))
    if value is None:
        raise _reject(f"{what} is missing the required field {key!r}", keys=sorted(raw))
    return value


def _need_datetime(raw: Mapping[str, Any], key: str, what: str) -> datetime:
    """A timestamp the vendor always sends.

    ``raw[key]`` directly would raise ``KeyError``, which escapes the vendor
    boundary as the wrong type: rule 9's watchdog catches :class:`BrokerError`
    and a bare ``KeyError`` would propagate past it as a crash rather than a
    halt.
    """
    value = raw.get(key)
    if value is None:
        raise _reject(f"{what} is missing the required field {key!r}", keys=sorted(raw))
    return _as_datetime(value)


def _object(payload: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise _reject(
            f"{what} came back as {type(payload).__name__}, not an object",
            payload_type=type(payload).__name__,
        )
    return payload


def _array(payload: Any, what: str) -> list[Any]:
    if not isinstance(payload, list):
        raise _reject(
            f"{what} came back as {type(payload).__name__}, not an array",
            payload_type=type(payload).__name__,
        )
    return payload


_EnumT = TypeVar("_EnumT", bound=StrEnum)


def _enum_or_none(
    kind: type[_EnumT], value: Any, what: str
) -> _EnumT | None:
    """A vendor string as an enum member. Empty and absent both mean ``None``.

    An *unrecognised* value raises instead. A new ``order_class`` read as
    ``simple`` would group a structure nobody has modelled as a set of
    unrelated single legs, which is the invented-spread failure decision 5
    exists to avoid — so it is loud.
    """
    if value is None or value == "":
        return None
    try:
        return kind(value)
    except ValueError as exc:
        raise _reject(
            f"{what} is not a value this codebase models", value=value
        ) from exc


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------


def _account(payload: Any) -> Account:
    raw = _object(payload, "the account")
    return Account(
        status=str(raw.get("status", "")),
        currency=str(raw.get("currency", "")),
        created_at=_need_datetime(raw, "created_at", "the account"),
        cash=_need(raw, "cash", "the account"),
        equity=_need(raw, "equity", "the account"),
        last_equity=_need(raw, "last_equity", "the account"),
        portfolio_value=_need(raw, "portfolio_value", "the account"),
        buying_power=_need(raw, "buying_power", "the account"),
        options_buying_power=_as_decimal(raw.get("options_buying_power")),
        effective_buying_power=_as_decimal(raw.get("effective_buying_power")),
        non_marginable_buying_power=_as_decimal(
            raw.get("non_marginable_buying_power")
        ),
        regt_buying_power=_as_decimal(raw.get("regt_buying_power")),
        long_market_value=_need(raw, "long_market_value", "the account"),
        short_market_value=_need(raw, "short_market_value", "the account"),
        position_market_value=_as_decimal(raw.get("position_market_value")),
        initial_margin=_need(raw, "initial_margin", "the account"),
        maintenance_margin=_need(raw, "maintenance_margin", "the account"),
        last_maintenance_margin=_as_decimal(raw.get("last_maintenance_margin")),
        sma=_need(raw, "sma", "the account"),
        accrued_fees=_need(raw, "accrued_fees", "the account"),
        pending_reg_taf_fees=_as_decimal(raw.get("pending_reg_taf_fees")),
        intraday_adjustments=_as_decimal(raw.get("intraday_adjustments")),
        multiplier=_need(raw, "multiplier", "the account"),
        # These two arrive as JSON *integers* while every other numeric field
        # on the object is a string, so a uniform `Decimal(str)` map over the
        # whole payload breaks on exactly these. `as_int` takes either.
        options_approved_level=_as_int(raw.get("options_approved_level")),
        options_trading_level=_as_int(raw.get("options_trading_level")),
        balance_asof=_as_date(raw.get("balance_asof")),
        trading_blocked=bool(raw.get("trading_blocked", False)),
        account_blocked=bool(raw.get("account_blocked", False)),
        transfers_blocked=bool(raw.get("transfers_blocked", False)),
        trade_suspended_by_user=bool(raw.get("trade_suspended_by_user", False)),
        shorting_enabled=bool(raw.get("shorting_enabled", False)),
    )


def _position(payload: Any) -> BrokerPosition:
    raw = _object(payload, "a position")
    symbol = str(raw.get("symbol", ""))
    quantity = _need(raw, "qty", f"position {symbol}")
    side = _enum_or_none(PositionSide, raw.get("side"), f"position {symbol}.side")
    if side is None:
        raise _reject("a position has no side", symbol=symbol)

    # `qty` and `side` state the same fact twice. If they ever disagree, one
    # of them is wrong and there is no way to tell which -- and either choice
    # mis-states the risk class of the position, which is the failure rule 4
    # exists to prevent. So it raises rather than picking a winner.
    expected_negative = side is PositionSide.SHORT
    if quantity != 0 and (quantity < 0) != expected_negative:
        raise _reject(
            "a position's qty sign disagrees with its side",
            symbol=symbol,
            qty=str(quantity),
            side=side.value,
        )

    return BrokerPosition(
        symbol=symbol,
        asset_class=str(raw.get("asset_class", "")),
        quantity=quantity,
        # `or quantity` here would read an available quantity of zero -- a
        # fully committed position -- as the whole position being free.
        quantity_available=_first_not_none(
            _as_decimal(raw.get("qty_available")), quantity
        ),
        side=side,
        average_entry_price=_need(raw, "avg_entry_price", f"position {symbol}"),
        cost_basis=_need(raw, "cost_basis", f"position {symbol}"),
        market_value=_need(raw, "market_value", f"position {symbol}"),
        current_price=_as_decimal(raw.get("current_price")),
        lastday_price=_as_decimal(raw.get("lastday_price")),
        change_today=_as_decimal(raw.get("change_today")),
        unrealized_pl=_as_decimal(raw.get("unrealized_pl")),
        unrealized_plpc=_as_decimal(raw.get("unrealized_plpc")),
        unrealized_intraday_pl=_as_decimal(raw.get("unrealized_intraday_pl")),
        unrealized_intraday_plpc=_as_decimal(raw.get("unrealized_intraday_plpc")),
        asset_marginable=(
            None
            if raw.get("asset_marginable") is None
            else bool(raw["asset_marginable"])
        ),
        # Present and empty on an option, which is a different statement from
        # absent -- so the default is `""`, not `None`.
        exchange=str(raw.get("exchange", "")),
    )


def _order(payload: Any) -> Order:
    raw = _object(payload, "an order")
    order_id = str(raw.get("id", ""))
    if not order_id:
        raise _reject("an order has no id", keys=sorted(raw))

    # Alpaca spells a simple order both `"simple"` and `""`. Both mean one
    # thing, and nothing downstream should have to remember that.
    order_class = (
        _enum_or_none(OrderClass, raw.get("order_class"), "order_class")
        or OrderClass.SIMPLE
    )
    legs = tuple(_order(leg) for leg in (raw.get("legs") or ()))

    return Order(
        id=order_id,
        symbol=str(raw.get("symbol", "")),
        asset_class=str(raw.get("asset_class", "")),
        order_class=order_class,
        # Empty on an `mleg` parent: the parent is the structure, the legs are
        # the instruments. `None` rather than an empty member, so reading it
        # as an action is a type error rather than an empty string.
        side=_enum_or_none(OrderSide, raw.get("side"), f"order {order_id}.side"),
        position_intent=_enum_or_none(
            PositionIntent,
            raw.get("position_intent"),
            f"order {order_id}.position_intent",
        ),
        order_type=str(raw.get("type") or raw.get("order_type") or ""),
        time_in_force=str(raw.get("time_in_force", "")),
        status=str(raw.get("status", "")),
        quantity=_as_decimal(raw.get("qty")),
        filled_quantity=_first_not_none(
            _as_decimal(raw.get("filled_qty")), Decimal(0)
        ),
        # Signed on an `mleg` parent: negative is a net credit, and that sign
        # is what decision 5 reads direction from.
        filled_avg_price=_as_decimal(raw.get("filled_avg_price")),
        limit_price=_as_decimal(raw.get("limit_price")),
        stop_price=_as_decimal(raw.get("stop_price")),
        ratio_qty=_as_decimal(raw.get("ratio_qty")),
        created_at=_need_datetime(raw, "created_at", f"order {order_id}"),
        submitted_at=_optional_datetime(raw.get("submitted_at")),
        filled_at=_optional_datetime(raw.get("filled_at")),
        canceled_at=_optional_datetime(raw.get("canceled_at")),
        expired_at=_optional_datetime(raw.get("expired_at")),
        updated_at=_optional_datetime(raw.get("updated_at")),
        extended_hours=bool(raw.get("extended_hours", False)),
        legs=legs,
    )


def _first_not_none(value: Decimal | None, fallback: Decimal) -> Decimal:
    """``value`` unless it is absent. **Not** ``or`` -- zero is a value."""
    return fallback if value is None else value


def _optional_datetime(value: Any) -> datetime | None:
    return None if value is None else _as_datetime(value)


#: Everything :class:`NonTradeActivity` names. Anything else goes to
#: ``extra``, because the published schema is incomplete as a matter of
#: observed fact -- ``description``, ``price`` and ``execution_id`` are all
#: absent from it and all present in live responses.
_NON_TRADE_KNOWN: Final = frozenset(
    {
        "activity_sub_type",
        "activity_type",
        "created_at",
        "currency",
        "cusip",
        "date",
        "description",
        "execution_id",
        "group_id",
        "id",
        "net_amount",
        "per_share_amount",
        "price",
        "qty",
        "status",
        "symbol",
    }
)

#: Discriminates the two response shapes. ``PARTIAL_FILL`` is not in Alpaca's
#: published ``ActivityType`` enum -- a partial fill arrives as ``FILL`` with
#: ``type: "partial_fill"`` -- but it costs nothing to accept and would
#: otherwise be silently parsed as a non-trade row with no ``order_id``.
_TRADE_ACTIVITY_TYPES: Final = frozenset({"FILL", "PARTIAL_FILL"})


def _activity(payload: Any) -> Activity:
    raw = _object(payload, "an activity")
    activity_type = str(raw.get("activity_type", ""))
    if activity_type in _TRADE_ACTIVITY_TYPES:
        return _trade_activity(raw, activity_type)
    return _non_trade_activity(raw, activity_type)


def _trade_activity(raw: Mapping[str, Any], activity_type: str) -> TradeActivity:
    activity_id = str(raw.get("id", ""))
    side = _enum_or_none(FillSide, raw.get("side"), f"fill {activity_id}.side")
    if side is None:
        raise _reject("a fill has no side", activity_id=activity_id)
    order_id = str(raw.get("order_id") or "")
    if not order_id:
        # The join key. A fill without one cannot be attributed to an action
        # at all, and `side` alone cannot name it -- `buy` is both BTO and
        # BTC.
        raise _reject("a fill has no order_id", activity_id=activity_id)

    return TradeActivity(
        id=activity_id,
        activity_type=activity_type,
        # The **leg's** order id on a multi-leg order. See the interface's
        # module docstring for the second hop.
        order_id=order_id,
        order_status=str(raw.get("order_status", "")),
        symbol=str(raw.get("symbol", "")),
        side=side,
        quantity=_need(raw, "qty", f"fill {activity_id}"),
        price=_need(raw, "price", f"fill {activity_id}"),
        transaction_time=_need_datetime(
            raw, "transaction_time", f"fill {activity_id}"
        ),
        cumulative_quantity=_as_decimal(raw.get("cum_qty")),
        leaves_quantity=_as_decimal(raw.get("leaves_qty")),
        fill_type=str(raw.get("type") or "fill"),
    )


def _non_trade_activity(
    raw: Mapping[str, Any], activity_type: str
) -> NonTradeActivity:
    return NonTradeActivity(
        id=str(raw.get("id", "")),
        activity_type=activity_type,
        activity_sub_type=_optional_str(raw.get("activity_sub_type")),
        activity_date=_as_date(raw.get("date")),
        created_at=_optional_datetime(raw.get("created_at")),
        net_amount=_as_decimal(raw.get("net_amount")),
        per_share_amount=_as_decimal(raw.get("per_share_amount")),
        # **Signed** here, where a `FILL` row's is unsigned with a separate
        # `side`. Two conventions in one ingest path; the matcher normalises
        # them, and losing the sign at this boundary would make that
        # impossible.
        quantity=_as_decimal(raw.get("qty")),
        price=_as_decimal(raw.get("price")),
        symbol=_optional_str(raw.get("symbol")),
        cusip=_optional_str(raw.get("cusip")),
        group_id=_optional_str(raw.get("group_id")),
        execution_id=_optional_str(raw.get("execution_id")),
        status=_optional_str(raw.get("status")),
        currency=_optional_str(raw.get("currency")),
        description=_optional_str(raw.get("description")),
        extra=MappingProxyType(
            {key: value for key, value in raw.items() if key not in _NON_TRADE_KNOWN}
        ),
    )


def _optional_str(value: Any) -> str | None:
    """Absent stays absent. An empty string is a value; ``None`` is not one.

    ``description`` arrives as ``""`` on the funding journal and as prose on
    every fee, and those are different facts.
    """
    return None if value is None else str(value)


def _portfolio_history(payload: Any) -> PortfolioHistory:
    raw = _object(payload, "the portfolio history")
    stamps = _array(raw.get("timestamp"), "portfolio history timestamps")

    # Four parallel arrays are a time series only because their indices line
    # up. Zipped short, a mismatch is a silent off-by-one across the whole
    # chart, and this boundary is the only place it can be caught.
    columns: dict[str, list[Any]] = {}
    for name in ("equity", "profit_loss", "profit_loss_pct"):
        column = _array(raw.get(name) or [], f"portfolio history {name}")
        if len(column) != len(stamps):
            raise _reject(
                f"portfolio history {name} does not line up with timestamp",
                timestamps=len(stamps),
                **{name: len(column)},
            )
        columns[name] = column

    points = tuple(
        EquityPoint(
            at=datetime.fromtimestamp(int(stamp), tz=timezone.utc),
            equity=_as_decimal(columns["equity"][index]),
            profit_loss=_as_decimal(columns["profit_loss"][index]),
            profit_loss_pct=_as_decimal(columns["profit_loss_pct"][index]),
        )
        for index, stamp in enumerate(stamps)
    )

    # The **fifth** parallel array, and the one that used to go unmeasured.
    # Alpaca documents a bucket as *"accumulated value in dollar amount as of
    # the end of each time window"* — one value per window, exactly like
    # `equity` — and `PortfolioHistory.cashflow` promises its caller the
    # arrays are "aligned index-for-index with `points`". Passed through
    # unchecked, a bucket of the wrong length dates a fee or a deposit to the
    # wrong day in whatever zips it, and a consumer trusting that docstring
    # has nowhere else to catch it.
    #
    # **Dropped rather than raised**, which is the one way this differs from
    # the three arrays above. Those are `required` in Alpaca's schema and
    # *are* the curve: without them there is no answer to return at all.
    # `cashflow` is optional and annotates the curve, so a bucket that cannot
    # be dated does not make one equity value wrong — and raising would take
    # the whole chart down, and with it (rule 9's watchdog) the engine, over a
    # fee column. A bucket is already absent for ordinary reasons: this
    # account's `JNLC` is dated outside the recorded window and simply is not
    # in the map, so absence was never an assertion of zero. A mis-dated fee
    # is the one outcome with no honest reading, and dropping is what removes
    # it. The drop is logged with both lengths, per rule 8.
    cashflow: dict[str, tuple[Decimal | None, ...]] = {}
    cashflow_raw = _object(raw.get("cashflow") or {}, "the cashflow map")
    for bucket, values in cashflow_raw.items():
        name = str(bucket)
        column = _array(values, f"cashflow bucket {name}")
        if len(column) != len(points):
            _log_refusal(
                f"portfolio history cashflow bucket {name!r} does not line up "
                "with timestamp; dropped rather than mis-dated",
                bucket=name,
                values=len(column),
                timestamps=len(points),
            )
            continue
        cashflow[name] = tuple(_as_decimal(value) for value in column)

    return PortfolioHistory(
        timeframe=str(raw.get("timeframe", "")),
        base_value=_as_decimal(raw.get("base_value")),
        base_value_asof=_as_date(raw.get("base_value_asof")),
        points=points,
        cashflow=MappingProxyType(cashflow),
    )


# --------------------------------------------------------------------------
# The broker
# --------------------------------------------------------------------------


class AlpacaBroker(BrokerAccount):
    """Alpaca's trading API, read-only, with a per-host request budget.

    Construct with :meth:`from_env` in production. The explicit constructor
    exists so tests can inject an ``httpx.MockTransport`` client and a fake
    limiter, and therefore make **no live calls at all**.
    """

    def __init__(
        self,
        *,
        credentials: AlpacaCredentials,
        client: httpx.AsyncClient | None = None,
        limiter: HostRateLimiter | None = None,
    ) -> None:
        self._credentials = credentials
        self._client = client if client is not None else httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None
        # Shared unless one is passed in. A private limiter per component
        # means two components in one process each believe they hold 200/min
        # against a single server-side ceiling -- see
        # `ratelimit.default_limiter`.
        self._limiter = limiter if limiter is not None else default_limiter()
        self._host = httpx.URL(credentials.trading_base_url).host

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, **kwargs: Any
    ) -> "AlpacaBroker":
        """Build from the process environment. **Paper credentials**, per rule 5."""
        return cls(credentials=AlpacaCredentials.paper_from_env(env), **kwargs)

    def __repr__(self) -> str:
        """Masked, like :class:`AlpacaCredentials`.

        Rule 6: *"No keys in code, in tests, in fixtures, or in log output."*
        A plain repr would put the key into any traceback holding a broker,
        which is most of them.
        """
        kind = "paper" if self._credentials.is_paper else "LIVE"
        return f"AlpacaBroker({kind}, host={self._host!r})"

    __str__ = __repr__

    @property
    def base_url(self) -> str:
        return self._credentials.trading_base_url

    @property
    def is_paper(self) -> bool:
        return self._credentials.is_paper

    @property
    def limiter(self) -> HostRateLimiter:
        """The budget this broker spends against. Shared by default."""
        return self._limiter

    async def aclose(self) -> None:
        """Close the transport, but only if we opened it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "AlpacaBroker":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- HTTP

    def _detail(self, response: httpx.Response) -> str:
        """This response's body, bounded and de-identified for an error message.

        **Both halves of the key pair** go in as literals, because both are
        sent: :meth:`AlpacaCredentials.headers` puts the key id in
        ``APCA-API-KEY-ID`` and the secret in ``APCA-API-SECRET-KEY`` on every
        request. Alpaca echoes neither in an error body, and nothing about
        that is a guarantee — anything in front of it that reflects request
        headers into an error page (a WAF, a corporate proxy, a future error
        shape) writes a credential into a body this method then quotes into an
        exception message, and rule 6 covers log output. A 40-character secret
        sits well inside :data:`ERROR_BODY_MAX`, so the bound is no protection
        here; the redaction is.
        """
        return vendor_detail(
            response.text,
            secrets=(self._credentials.key_id, self._credentials.secret_key),
        )

    async def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """One GET, metered against the bucket for the trading host."""
        await self._limiter.acquire(self._host)

        try:
            response = await self._client.get(
                f"{self.base_url}{path}",
                params=clean_params(params),
                headers=self._credentials.headers(),
            )
        except httpx.HTTPError as exc:
            # Surfaced as a BrokerError rather than an httpx type: rule 9's
            # watchdog halts on a broker failure and should not have to know
            # what transport is underneath.
            raise BrokerError(f"GET {path} failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise BrokerAuthError(
                f"GET {path} returned {response.status_code}: "
                f"{self._detail(response)}. The API key pair was rejected — "
                "check which keys are in the environment before debugging "
                "the code. The Alpaca MCP server, for instance, holds "
                "non-paper keys and answers 401 on every trading endpoint."
            )
        if response.status_code == 429:
            raise BrokerRateLimitedError(
                f"GET {path} returned 429 despite the local budget. The "
                "server window and the local bucket disagree — another "
                "process may be sharing this key. Reset header: "
                f"{response.headers.get('X-RateLimit-Reset', 'absent')}"
            )
        if response.status_code >= 400:
            raise BrokerError(
                f"GET {path} returned {response.status_code}: "
                f"{self._detail(response)}"
            )
        # `wire.decode_json`, never `response.json()`: the portfolio history
        # endpoint sends money as bare JSON numbers, and `.json()` has no
        # hook for `parse_float=Decimal`.
        return _decode(response.text)

    # ------------------------------------------------------------ reading

    async def account(self) -> Account:
        return _account(await self._get("/v2/account"))

    async def positions(self) -> list[BrokerPosition]:
        payload = await self._get("/v2/positions")
        return [_position(row) for row in _array(payload, "the position list")]

    async def orders(
        self,
        *,
        status: OrderQueryStatus = OrderQueryStatus.ALL,
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[Order]:
        require_aware(after, "after")
        require_aware(until, "until")
        page = limit if limit is not None else ORDERS_LIMIT_MAX
        if not 0 < page <= ORDERS_LIMIT_MAX:
            raise ValueError(
                f"limit must be between 1 and {ORDERS_LIMIT_MAX}, got {limit!r}"
            )

        payload = await self._get(
            "/v2/orders",
            {
                "status": status,
                # Not optional. A fill carries its *leg's* order id and the
                # parent `mleg` id appears nowhere on it, so `legs[]` is the
                # only source for the leg-id to parent-id map the join needs.
                # Without this the join is one-hop and groups nothing --
                # silently, because on a simple order the two ids coincide.
                "nested": True,
                "limit": page,
                "direction": _ASCENDING,
                "after": rfc3339(after),
                "until": rfc3339(until),
                "symbols": ",".join(symbols) if symbols else None,
            },
        )
        rows = _array(payload, "the order list")
        if len(rows) >= page:
            # Not an error -- `after`/`until` are the caller's window -- but a
            # full page means the history may be cut off, and a cut-off order
            # history is a join missing parents rather than an empty screen.
            logger.warning(
                "order history filled the page; older orders may be missing",
                extra={"returned": len(rows), "limit": page},
            )
        return [_order(row) for row in rows]

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
        if types is not None and category is not None:
            # Alpaca: "Cannot be used with activity_types parameter."
            # Refused here rather than sent and 400'd three layers away.
            raise ValueError(
                "activities() takes `types` or `category`, not both — Alpaca "
                "refuses them together."
            )
        require_aware(after, "after")
        require_aware(until, "until")
        size = page_size if page_size is not None else ACTIVITIES_PAGE_MAX
        if not 0 < size <= ACTIVITIES_PAGE_MAX:
            raise ValueError(
                f"page_size must be between 1 and {ACTIVITIES_PAGE_MAX} "
                f"(Alpaca's documented maximum), got {page_size!r}"
            )

        base = {
            "activity_types": ",".join(types) if types else None,
            "category": category,
            "direction": _ASCENDING,
            "page_size": size,
            "after": rfc3339(after),
            "until": rfc3339(until),
        }

        rows: list[Activity] = []
        token = since_id
        for _ in range(_MAX_PAGES):
            payload = await self._get(
                "/v2/account/activities", {**base, "page_token": token}
            )
            page = _array(payload, "the activity list")
            if not page:
                return rows
            rows.extend(_activity(row) for row in page)
            # There is no `next_page_token` on this endpoint. Alpaca's cursor
            # *is* the last row's id, which is the other reason the composite
            # id has to survive intact.
            if len(page) < size:
                return rows
            last = _object(page[-1], "an activity").get("id")
            if not last:
                raise _reject(
                    "an activity page has no id to resume from",
                    page_size=len(page),
                )
            token = str(last)
        raise BrokerError(
            f"GET /v2/account/activities did not terminate within {_MAX_PAGES} "
            "pages. Refusing to return a partial result — pass `since_id` or "
            "`after` and resume, which is what incremental ingestion does."
        )

    async def portfolio_history(
        self, *, period: str = "1M", timeframe: str = "1D"
    ) -> PortfolioHistory:
        return _portfolio_history(
            await self._get(
                "/v2/account/portfolio/history",
                {
                    "period": period,
                    "timeframe": timeframe,
                    # Every bucket, so the Account page can name deposits and
                    # fees rather than only showing their effect on equity.
                    "cashflow_types": "ALL",
                },
            )
        )


# --------------------------------------------------------------------------
# The ``trade_updates`` websocket
# --------------------------------------------------------------------------
#
# The third vendor socket, and the only one that is not market data. It lives
# here rather than with the two quote streams because it is an order-lifecycle
# feed on the **trading** host: a different hostname, a separate rate-limit
# bucket, and paper or live decided by which key pair opened it. Everything
# transport-shaped is inherited from `corollary.sockets.VendorStream`, which
# owns rule 9's close attribution so that the three sockets cannot answer that
# question differently.
#
# Nothing here places an order. Rule 1 has exactly one path to `submit_order`
# and it is inside `RiskManager.approve()`; this socket *listens* to what
# happened to orders, which is the opposite direction and is what makes it
# safe to have on the trading host at all.

#: The one stream this socket listens to. Alpaca's trading websocket
#: multiplexes by name and ``trade_updates`` is the only name Corollary wants:
#: it is the order lifecycle. Account updates are polled.
TRADE_UPDATES_STREAM: Final = "trade_updates"

#: The path the trading websocket lives at, on whichever trading host the
#: credentials name.
TRADE_UPDATES_PATH: Final = "/stream"


def trade_updates_url(credentials: AlpacaCredentials) -> str:
    """The websocket URL for the account these credentials belong to.

    Derived from :attr:`AlpacaCredentials.trading_base_url` rather than
    selected here, which is rule 5 holding at the transport: a paper key pair
    carries the paper host, so pointing this socket at the live account takes
    a different key pair and not a different string literal.
    """
    base = credentials.trading_base_url
    for prefix in ("https://", "http://"):
        if base.startswith(prefix):
            base = base[len(prefix) :]
            break
    return f"wss://{base.rstrip('/')}{TRADE_UPDATES_PATH}"


def _whole(value: Decimal | None, what: str) -> int | None:
    """A quantity as an ``int``, refusing a fractional one.

    Alpaca sends quantities as strings, which parse to ``Decimal`` exactly. A
    non-integral one is **refused**, not rounded: this book trades contracts,
    the one asset class that fills fractionally is not traded here, and a
    rounded quantity is a position size that is wrong with nothing on screen
    to say so.
    """
    if value is None:
        return None
    if value != value.to_integral_value():
        raise BrokerError(
            f"{what} is {value}, which is not a whole number of contracts. A "
            "fractional quantity is refused rather than rounded: a rounded "
            "one is a position size that is wrong with nothing to say so"
        )
    return int(value)


def _trade_update(payload: Any) -> TradeUpdate:
    """One ``trade_updates`` event, as the domain object.

    The ``order`` member is *"the same as the order object that is returned
    from the REST API"*, so it goes through :func:`_order` -- the same
    translation, the same ``position_intent`` resolution, the same signed
    ``filled_avg_price``. A second parser for the same shape is a second
    place for the sign of a credit to be got wrong.
    """
    raw = _object(payload, "a trade update")
    order = _order(_object(raw.get("order"), "a trade update's order"))
    return TradeUpdate(
        event=str(raw.get("event") or ""),
        at=_need_datetime(raw, "timestamp", "a trade update"),
        order_id=order.id,
        symbol=order.symbol,
        status=order.status,
        # The order's own ``position_intent``, and never a guess from
        # ``side``: an ``mleg`` parent carries no intent at all, and inventing
        # one there is how a buy-to-close is booked as a new lot and doubles a
        # position the account already holds.
        action=order.position_intent,
        quantity=_whole(order.quantity, "the order quantity"),
        filled_quantity=_whole(order.filled_quantity, "the filled quantity") or 0,
        fill_price=_as_decimal(raw.get("price")),
        fill_quantity=_whole(_as_decimal(raw.get("qty")), "this event's quantity"),
        filled_avg_price=order.filled_avg_price,
        position_quantity=_whole(
            _as_decimal(raw.get("position_qty")), "the resulting position"
        ),
        order=order,
    )


class AlpacaTradeUpdateStream(VendorStream):
    """The order-lifecycle socket: ``trade_updates``, and nothing else.

    Authenticates with a message, listens to one stream by name, translates
    each event into a :class:`TradeUpdate`, and hands it to a synchronous
    sink -- which the composition root builds from ``api/fanout.py``, so this
    module does not import the browser's wire shapes.

    It is the second producer for rule 9's stream-closed condition. The
    attribution of a close, the reconnect and the backoff are the base
    class's, deliberately: the market-data sockets and this one have to give
    the same answer to *"was that close ours?"*, and two copies of that
    answer is one copy that goes wrong quietly.
    """

    def __init__(
        self,
        *,
        credentials: AlpacaCredentials,
        activity: StreamActivityRecorder,
        on_update: Callable[[TradeUpdate], None],
        connect: SocketConnect | None = None,
        sleep: Callable[[float], Any] | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        super().__init__(
            # The vendor's own name for the one stream this socket listens
            # to, which is also what rule 9's halt reason will say. Losing
            # this feed is losing fills, not quotes, and the record has to be
            # able to tell a reader which of the two happened.
            name=TRADE_UPDATES_STREAM,
            url=trade_updates_url(credentials),
            # JSON, which Alpaca's trading stream documents alongside
            # msgpack. The decoder reads whichever the *frame* is, because
            # the paper host answers in binary frames -- so an upgrade of the
            # vendor's default cannot silently stop this socket working.
            codec=JSON_CODEC,
            activity=activity,
            connect=connect,
            sleep=sleep,
            now=now,
            secrets=(credentials.key_id, credentials.secret_key),
        )
        self._credentials = credentials
        self._on_update = on_update
        self._listening = False

    @classmethod
    def from_env(
        cls,
        *,
        activity: StreamActivityRecorder,
        on_update: Callable[[TradeUpdate], None],
        env: Mapping[str, str] | None = None,
        paper: bool = True,
        **kwargs: Any,
    ) -> "AlpacaTradeUpdateStream":
        """The paper socket by default. Rule 5: paper is the default everywhere."""
        credentials = (
            AlpacaCredentials.paper_from_env(env)
            if paper
            else AlpacaCredentials.live_from_env(env)
        )
        return cls(
            credentials=credentials,
            activity=activity,
            on_update=on_update,
            **kwargs,
        )

    @property
    def listening(self) -> bool:
        """Has the server confirmed the ``listen``? Reported, never assumed."""
        return self._listening

    # -- the protocol ------------------------------------------------------

    def _handshake_headers(self) -> dict[str, str]:
        """Nothing. The trading stream authenticates with a message."""
        return {}

    def _auth_message(self) -> Mapping[str, Any]:
        """The auth frame. **Never logged**, by rule 6, anywhere in this class."""
        return {
            "action": "auth",
            "key": self._credentials.key_id,
            "secret": self._credentials.secret_key,
        }

    def _messages(self, frame: str | bytes) -> list[Mapping[str, Any]]:
        """One frame as a list of messages.

        The trading stream sends one object per frame, unlike the data
        streams' arrays. Both shapes are accepted rather than one being
        refused: the cost is two lines and the alternative is a feed that
        stops on a shape the vendor is free to change.
        """
        try:
            decoded = self._codec.decode(frame)
        except Exception as exc:
            logger.warning(
                "undecodable frame on the trade_updates stream: %s",
                self._detail(str(exc)),
                extra={
                    "event": "trade_updates_frame_undecodable",
                    "detail": self._detail(str(exc)),
                    "at": self._now().isoformat(),
                },
            )
            return []
        if isinstance(decoded, Mapping):
            return [decoded]
        if isinstance(decoded, list):
            return [item for item in decoded if isinstance(item, Mapping)]
        return []

    async def _handle(self, socket: VendorSocket, message: Mapping[str, Any]) -> None:
        name = message.get("stream")
        if name == "authorization":
            await self._authorized(socket, message)
        elif name == "listening":
            self._listening = True
        elif name == TRADE_UPDATES_STREAM:
            self._publish(message)
        # Anything else is a stream nobody listened to. It already counted as
        # a message, which is all rule 9 wanted from it.

    async def _authorized(
        self, socket: VendorSocket, message: Mapping[str, Any]
    ) -> None:
        data = message.get("data")
        status = str(data.get("status", "")) if isinstance(data, Mapping) else ""
        if status != "authorized":
            detail = self._detail(f"the trading stream answered {status!r}")
            logger.error(
                "the trade_updates stream refused our credentials: %s",
                detail,
                extra={
                    "event": "trade_updates_refused",
                    "rule": "vendor_refused",
                    "status": status,
                    "detail": detail,
                    "at": self._now().isoformat(),
                },
            )
            # A refusal that recurs identically on reconnect, and a lost feed
            # all the same: rule 9's condition is recorded either way, so the
            # engine halts rather than trading on blind.
            self._record_stream_closed(self._now(), detail)
            raise BrokerAuthError(
                f"the trade_updates stream refused the credentials: {status or 'unauthorized'}"
            )
        # Only now: an open socket that has not authorized carries nothing.
        self._record_stream_open(self._now())
        await self._codec.transmit(
            socket, {"action": "listen", "data": {"streams": [TRADE_UPDATES_STREAM]}}
        )

    def _publish(self, message: Mapping[str, Any]) -> None:
        """Translate one event and hand it to the sink.

        An event this fails on is logged and skipped rather than fatal: one
        unreadable message must not cost every *later* fill its notification,
        which is what a raised exception here would do. The catch is scoped to
        the translation for that reason.

        **The sink is inside the containment too**, in its own block: called
        after the ``try``, anything it raised propagated out of ``run()`` and
        killed the order feed with **no** ``record_stream_closed`` and no
        reconnect -- the engine blind to its own fills while rule 9 believed
        the socket healthy. ``api/fanout.py``'s ``wire_action`` is a ``dict``
        lookup with no exhaustiveness checking, so a fifth ``PositionIntent``
        member is a ``KeyError`` on this line.

        **The catch names the ``ArithmeticError`` family, not a member of
        it.** A non-finite ``Decimal`` can no longer be built out of a wire
        value -- ``corollary.wire._decimalise`` refuses one at the decode
        boundary -- and this is the second line of defence, because this path
        has now been broken twice by the same family arriving from different
        corners: ``InvalidOperation`` out of a comparison, then
        ``OverflowError`` out of ``int(Decimal('Infinity'))``, which was *not*
        in this tuple and so escaped every frame above it -- all of which
        catch only ``SocketClosed`` -- and killed the socket permanently with
        rule 9's close condition never recorded. ``ValueError`` covered its
        sibling ``int(Decimal('NaN'))``, which is exactly what made the path
        look covered.
        """
        try:
            update = _trade_update(message.get("data"))
        except (BrokerError, ArithmeticError, KeyError, TypeError, ValueError) as exc:
            detail = self._detail(str(exc))
            logger.warning(
                "unreadable trade update: %s",
                detail,
                extra={
                    "event": "trade_update_unreadable",
                    "rule": "unreadable_event",
                    "detail": detail,
                    "at": self._now().isoformat(),
                },
            )
            return
        try:
            # Synchronous by contract: `Fanout.publish` never awaits, so a
            # slow browser tab cannot stall this socket.
            self._on_update(update)
        except Exception as exc:  # the sink is the composition root's code
            detail = self._detail(str(exc))
            logger.error(
                "the trade_updates sink raised: %s",
                detail,
                extra={
                    "event": "trade_update_sink_failed",
                    "rule": "sink_raised",
                    "order_id": update.order_id,
                    "detail": detail,
                    "at": self._now().isoformat(),
                },
                exc_info=True,
            )
