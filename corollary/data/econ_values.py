"""An economic release's first-print figure, from FRED's release-day vintage (spec R7).

**Pure.** No I/O, no clock, no logging: :func:`first_print` takes what
:meth:`corollary.data.providers.fred.FredProvider.vintage_window` returned and
the release's row of ``econ_release_series.csv``, and says what the figure is.
Storing it, scheduling the fetch, and showing it are later units.

Which observation
-----------------

The answer covers ``D-1 .. D`` for release day ``D``. FRED clips every row's
real-time period to that window (U6a's probe), so:

* **The vintage on D** is the rows with ``realtime_end == D``.
* **New or revised on D** is a date with a row starting on ``D`` and either no
  row ending on ``D-1`` or one whose value differs **as a Decimal**. FRED's
  formatting is not canonical -- one response holds ``'4060000.0'`` and
  ``'3930000'`` -- so ``'159128'`` against ``'159128.0'`` is the same number,
  not a revision. A value that became ``"."`` on ``D`` did change.
* **The release's observation**, x(t), is the latest-dated new-or-revised
  one. If there is none, FRED has not posted the release: :class:`NotPosted`.
  It must also be the **newest-dated observation in the D vintage**; if a
  newer one is there unchanged, D revised only an older month and the release
  is not posted either (:class:`NotPosted`, fail closed -- the catch-up
  retries). Every normal release, whose new period is the newest, is
  unaffected.
* **x(t-1) and x(t-2)** are the observations immediately before it **in the D
  vintage**, so a revision published that day is in the prior. Each must be
  exactly one period earlier at the seed row's ``frequency`` -- the same
  value the fetch's lookback was set from, and an answer fetched at another
  frequency is refused as a caller's bug. The spec says
  "immediately before"; requiring the period too means a gap in the vintage,
  or a monthly series read as quarterly, is :class:`NoValue` rather than a
  two-period change printed beside an "m/m" label.

The transforms
--------------

In ``Decimal``, every input parsed from FRED's text, in an explicit
:data:`_ARITHMETIC` context with ``InvalidOperation``, ``DivisionByZero`` and
``Overflow`` trapped -- no caller's global context changes a figure:

* ``level``: x(t). Prior x(t-1).
* ``change``: x(t) - x(t-1). Prior x(t-1) - x(t-2).
* ``pct_change``: (x(t) / x(t-1) - 1) x 100. Prior one period earlier.

Then ``scale``, then quantized to :data:`STORED_EXPONENT` (four places, the
seed's display ceiling, from which it is derived; ``ROUND_HALF_EVEN``). A
quantized negative zero is stored as ``0.0000``. The division runs at 50
significant digits, so its one rounding cannot move the four-place result: FRED's values carry a handful of digits, and a quotient of
two of them that does not terminate within 50 digits cannot lie within
10^-50 of a half-way point.

**Actual and prior together, or neither.** A ``"."`` or a missing row in any
observation either one needs, or a ``pct_change`` over zero, is
:class:`NoValue` with a reason naming the observation. Never an actual beside
an invented or zero prior. Display rounding to the seed's ``decimals``
(``ROUND_HALF_UP``) is not done here.

What an attempt recorded, and what a row shows (U6d)
----------------------------------------------------

Also pure, and here for the same reason: they are rules about these values,
not about storage. :func:`attempt_of` and :func:`attempt_of_error` turn one
fetch's result into the :class:`Attempt` that ``econ_release_fetch`` keeps
(``corollary.data.econ_store`` writes it). :func:`value_display` derives the
R8 display state from the release instant, whether a value is stored, and the
last attempt -- derived on every read, never stored.

**Exactly seven days** (:func:`unavailable_from`): the release instant on the
``America/New_York`` wall clock plus seven calendar days, so CPI at 08:30 ET
on the 14th becomes unavailable at 08:30 ET on the 21st -- at that instant,
and not one microsecond before. Across a daylight-saving change that is 167
or 169 elapsed hours, not 168: "calendar days", as the spec says. Every
release time in the seed is between 08:30 and 14:00 ET, so the result never
lands in the hour a clock change skips or repeats.

**A refusal is not lateness; an error is not FRED's silence.** A series FRED
refuses (not in ALFRED) is ``unavailable`` from its first refused attempt,
with the vendor's reason -- waiting seven days for a permanent answer would
only hide it. An ``error`` attempt stays ``awaiting`` for the week, and after
it says the fetch failed and names the error's type: a revoked key must not
read as "not published on FRED".
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
)
from typing import Final, Literal, TypeAlias

from corollary.data.providers.fred import (
    API_KEY_QUERY_PATTERN,
    FredRefusal,
    FredVintageRow,
    FredVintageWindow,
)
from corollary.data.seeds.calendar_seeds import (
    ET_ZONE,
    MAX_DISPLAY_DECIMALS,
    EconFrequency,
    EconReleaseSeries,
)
from corollary.wire import STORED_DETAIL_MAX, vendor_detail

__all__ = [
    "ATTEMPT_DETAIL_MAX",
    "COMPUTED_FROM_FRED",
    "DETAILED_OUTCOMES",
    "ERROR_DETAIL_MAX",
    "FETCH_OUTCOMES",
    "NOT_PUBLISHED_NOTE",
    "STORED_EXPONENT",
    "UNAVAILABLE_AFTER_DAYS",
    "VALUE_STATES",
    "Attempt",
    "FetchOutcome",
    "Figure",
    "FirstPrint",
    "NoValue",
    "NotPosted",
    "Refused",
    "ValueDisplay",
    "ValueState",
    "attempt_of",
    "attempt_of_error",
    "computed_label",
    "first_print",
    "unavailable_from",
    "value_display",
]

#: What every figure is labelled, with its series ID (spec R7, *Labelling*),
#: so a 0.1 difference from the agency's printed number has an explanation.
COMPUTED_FROM_FRED: Final = "computed from FRED's levels"

#: The stored figure's exponent: ``Decimal("0.0001")``, four places. Derived
#: from the seed's display ceiling rather than written twice, so a row can
#: never ask to display a place that was not stored. Built from its tuple, so
#: no context or string parse is involved.
STORED_EXPONENT: Final = Decimal((0, (1,), -MAX_DISPLAY_DECIMALS))

#: The transforms' context. Fifty digits: see the module docstring.
_ARITHMETIC: Final = Context(
    prec=50,
    rounding=ROUND_HALF_EVEN,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)

#: The quantize. It rounds, so ``Inexact`` is not trapped; the precision is
#: wide enough that any realistic coefficient fits.
_QUANTIZE: Final = Context(prec=80, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation])

_HUNDRED: Final = Decimal(100)
_ONE: Final = Decimal(1)

#: Months per period, and the months a period may start in.
_PERIOD_MONTHS: Final[dict[EconFrequency, int]] = {"monthly": 1, "quarterly": 3}

#: How x(t), x(t-1) and x(t-2) are named in a reason.
_NAMES: Final = ("x(t)", "x(t-1)", "x(t-2)")


def computed_label(series_id: str) -> str:
    """``computed from FRED's levels: <SERIES_ID>`` -- the label beside every figure."""
    return f"{COMPUTED_FROM_FRED}: {series_id}"


@dataclass(frozen=True, slots=True)
class Figure:
    """A release's figure and its prior, each quantized to four places.

    ``period`` is x(t)'s observation date (``2026-09-01`` for September's
    CPI); ``vintage_date`` is the release day whose vintage it was read from.
    Both values are in the seed's ``unit_label`` units, ``scale`` applied.
    """

    series_id: str
    period: date
    actual: Decimal
    prior: Decimal
    vintage_date: date


@dataclass(frozen=True, slots=True)
class NotPosted:
    """Nothing in the window is new or revised on the release day: FRED has not posted yet."""

    series_id: str
    vintage_date: date


@dataclass(frozen=True, slots=True)
class NoValue:
    """FRED posted, and the figure cannot be computed. ``reason`` says which observation."""

    series_id: str
    vintage_date: date
    reason: str


@dataclass(frozen=True, slots=True)
class Refused:
    """FRED declines this series' vintages. ``vendor_message`` is FRED's own reason."""

    series_id: str
    vintage_date: date
    vendor_message: str


#: Every outcome, so "not posted yet", "cannot be computed" and "FRED will
#: not serve it" stay three different things downstream.
FirstPrint: TypeAlias = Figure | NotPosted | NoValue | Refused


def _same_value(old: Decimal | None, new: Decimal | None) -> bool:
    """Equal as numbers, with ``None`` (FRED's ``"."``) equal only to itself."""
    if old is None or new is None:
        return old is None and new is None
    return old == new


def _months_back(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) - months
    return date(index // 12, index % 12 + 1, day.day)


def _starts_a_period(day: date, frequency: EconFrequency) -> bool:
    if day.day != 1:
        return False
    return frequency == "monthly" or (day.month - 1) % 3 == 0


def _transform(series: EconReleaseSeries, x: tuple[Decimal, ...]) -> tuple[Decimal, Decimal]:
    """``(actual, prior)`` before scale. ``x`` is x(t), x(t-1)[, x(t-2)]; no zero divisor."""
    if series.transform == "level":
        return x[0], x[1]
    if series.transform == "change":
        return _ARITHMETIC.subtract(x[0], x[1]), _ARITHMETIC.subtract(x[1], x[2])

    def pct(new: Decimal, base: Decimal) -> Decimal:
        ratio = _ARITHMETIC.divide(new, base)
        return _ARITHMETIC.multiply(_ARITHMETIC.subtract(ratio, _ONE), _HUNDRED)

    return pct(x[0], x[1]), pct(x[1], x[2])


def _stored(value: Decimal, scale: Decimal) -> Decimal:
    """``value x scale`` to four places, half-even, with no negative zero."""
    scaled = _ARITHMETIC.multiply(value, scale)
    quantized = scaled.quantize(STORED_EXPONENT, rounding=ROUND_HALF_EVEN, context=_QUANTIZE)
    if quantized.is_zero():
        return quantized.copy_abs()
    return quantized


def first_print(
    answer: FredVintageWindow | FredRefusal, series: EconReleaseSeries
) -> FirstPrint:
    """The release's figure, from FRED's ``D-1 .. D`` answer and its seed row.

    The frequency is the seed row's. Raises ``ValueError`` only for a
    caller's bug: an answer for a different series than the seed row names,
    or a window fetched at a different frequency than the row states (its
    lookback was set for another series).
    """
    if answer.series_id != series.series_id:
        raise ValueError(
            f"the answer is for {answer.series_id}, the seed row for {series.series_id}"
        )
    release_day = answer.release_day
    if isinstance(answer, FredRefusal):
        return Refused(series.series_id, release_day, answer.vendor_message)
    frequency = series.frequency
    if answer.frequency != frequency:
        raise ValueError(
            f"the answer for {series.series_id} was fetched as {answer.frequency}, the seed "
            f"row says {frequency}"
        )

    def no_value(reason: str) -> NoValue:
        return NoValue(series.series_id, release_day, reason)

    on_day: dict[date, FredVintageRow] = {}
    before: dict[date, FredVintageRow] = {}
    for row in answer.rows:
        if row.realtime_end == release_day:
            on_day[row.date] = row
        elif row.realtime_end == answer.day_before:
            before[row.date] = row

    changed = sorted(
        row.date
        for row in answer.rows
        if row.realtime_start == release_day
        and (row.date not in before or not _same_value(before[row.date].value, row.value))
    )
    if not changed:
        return NotPosted(series.series_id, release_day)

    period = changed[-1]
    # Carried fix P1: x(t) must also be the newest-dated observation in the D
    # vintage. A vintage that revises only an old month, with nothing newer
    # new, is a revision day, not the release -- reading it as one printed a
    # five-month-old period as the first print. Fails closed: NotPosted, and
    # the catch-up retries.
    if not on_day or period != max(on_day):
        return NotPosted(series.series_id, release_day)
    if not _starts_a_period(period, frequency):
        return no_value(
            f"x(t) is dated {period}, which does not start a {frequency} period"
        )

    needed = 2 if series.transform == "level" else 3
    vintage = sorted(on_day)
    chain = [period]
    while len(chain) < needed:
        later = chain[-1]
        expected = _months_back(later, _PERIOD_MONTHS[frequency])
        earlier = [d for d in vintage if d < later]
        name = _NAMES[len(chain)]
        if not earlier:
            return no_value(
                f"{name} ({expected}) is not in the {release_day} vintage: it holds "
                f"nothing before {later}"
            )
        if earlier[-1] != expected:
            return no_value(
                f"{name} should be {expected}, one {frequency} period before {later}, but "
                f"the observation before {later} in the {release_day} vintage is "
                f"{earlier[-1]}"
            )
        chain.append(expected)

    values: list[Decimal] = []
    for name, day in zip(_NAMES, chain):
        value = on_day[day].value
        if value is None:
            return no_value(
                f"{name} ({day}) is FRED's missing-observation marker '.' in the "
                f"{release_day} vintage"
            )
        values.append(value)

    if series.transform == "pct_change":
        for name, day, value in zip(_NAMES[1:], chain[1:], values[1:]):
            if value.is_zero():
                return no_value(f"{name} ({day}) is zero, so a percent change over it is undefined")

    actual, prior = _transform(series, tuple(values))
    return Figure(
        series_id=series.series_id,
        period=period,
        actual=_stored(actual, series.scale),
        prior=_stored(prior, series.scale),
        vintage_date=release_day,
    )


# --------------------------------------------------------------------------
# U6d: one attempt's record, and the display state (R8)
# --------------------------------------------------------------------------

#: What one fetch attempt came to: ``econ_release_fetch.outcome``'s CHECK.
FetchOutcome: TypeAlias = Literal["figure", "not_posted", "no_value", "refused", "error"]
FETCH_OUTCOMES: Final[tuple[FetchOutcome, ...]] = (
    "figure",
    "not_posted",
    "no_value",
    "refused",
    "error",
)

#: The outcomes whose attempt must say why: the vendor's message for
#: ``refused``, the computed reason for ``no_value``, the error's type and a
#: short message for ``error``. ``figure`` and ``not_posted`` carry none.
DETAILED_OUTCOMES: Final[frozenset[FetchOutcome]] = frozenset({"no_value", "refused", "error"})

#: The stored detail's bound, after the ``ledger_rejection.detail``
#: precedent: :data:`~corollary.wire.STORED_DETAIL_MAX` bounds the *content*,
#: and ``vendor_detail`` may append a truncation notice of at most 32
#: characters past it (a refusal's message is bounded that way by the provider).
ATTEMPT_DETAIL_MAX: Final = 1280

#: An ``error`` attempt's bound. Short: the full text is in the log record.
ERROR_DETAIL_MAX: Final = 300

#: What ``vendor_detail`` is asked to keep of an error's text. With its
#: truncation notice (at most 32 characters) that stays under
#: :data:`ERROR_DETAIL_MAX`.
_ERROR_TEXT_LIMIT: Final = 256

if STORED_DETAIL_MAX + 32 > ATTEMPT_DETAIL_MAX:  # pragma: no cover - a constant drifted
    raise AssertionError("ATTEMPT_DETAIL_MAX no longer holds a bounded vendor message")


@dataclass(frozen=True, slots=True)
class Attempt:
    """One fetch attempt's outcome and its detail, as ``econ_release_fetch`` stores it.

    Refused at construction in exactly the shapes the table's CHECKs refuse:
    an unknown outcome, a detail on ``figure``/``not_posted``, a missing or
    blank one on the other three, or one over :data:`ATTEMPT_DETAIL_MAX`.
    """

    outcome: FetchOutcome
    detail: str | None

    def __post_init__(self) -> None:
        if self.outcome not in FETCH_OUTCOMES:
            raise ValueError(f"outcome {self.outcome!r} is not one of {list(FETCH_OUTCOMES)}")
        if self.outcome not in DETAILED_OUTCOMES:
            if self.detail is not None:
                raise ValueError(f"a {self.outcome} attempt carries no detail")
            return
        if self.detail is None or not self.detail.strip():
            raise ValueError(f"a {self.outcome} attempt must say why: its detail is empty")
        if len(self.detail) > ATTEMPT_DETAIL_MAX:
            raise ValueError(
                f"a {self.outcome} attempt's detail is {len(self.detail)} characters, "
                f"over {ATTEMPT_DETAIL_MAX}"
            )


def attempt_of(result: FirstPrint | FredRefusal) -> Attempt:
    """The attempt one fetch came to, from :func:`first_print`'s result.

    A :class:`FredRefusal` straight from the provider maps as its
    :class:`Refused` would, so a caller need not run :func:`first_print` on it.
    """
    if isinstance(result, Figure):
        return Attempt("figure", None)
    if isinstance(result, NotPosted):
        return Attempt("not_posted", None)
    if isinstance(result, NoValue):
        return Attempt("no_value", result.reason)
    if isinstance(result, (Refused, FredRefusal)):
        return Attempt("refused", result.vendor_message)
    raise TypeError(f"not a fetch result: {type(result).__name__}")


def attempt_of_error(exc: Exception, *, secrets: Sequence[str]) -> Attempt:
    """An ``error`` attempt: the exception's type and a short, scrubbed message.

    ``secrets`` is the provider's (``FredProvider.secrets``). The provider
    already scrubs every message it composes; this scrubs again, with the
    same ``api_key=`` pattern, because the text is about to be stored.
    """
    name = type(exc).__name__
    if not str(exc).strip():
        return Attempt("error", name)
    detail = vendor_detail(
        f"{name}: {exc}",
        secrets=secrets,
        patterns=(API_KEY_QUERY_PATTERN,),
        limit=_ERROR_TEXT_LIMIT,
    )
    return Attempt("error", detail)


#: The four display states (R8), derived on read and never stored.
ValueState: TypeAlias = Literal["upcoming", "awaiting", "filled", "unavailable"]
VALUE_STATES: Final[tuple[ValueState, ...]] = ("upcoming", "awaiting", "filled", "unavailable")

#: How many calendar days a release may go unfilled before it reads
#: ``unavailable``. See the module docstring, *Exactly seven days*.
UNAVAILABLE_AFTER_DAYS: Final = 7

#: The ``unavailable`` note when no refusal, computed reason or error explains it.
NOT_PUBLISHED_NOTE: Final = f"not published on FRED within {UNAVAILABLE_AFTER_DAYS} days"

#: What a refused series' note leads with, before the vendor's own reason
#: (spec *Not verified*: "that release shows 'not available from FRED' with
#: the vendor's reason").
NOT_AVAILABLE_PREFIX: Final = "not available from FRED"

#: What an ``error`` attempt's note leads with, before the error's type. It
#: says the *fetch* failed: a revoked ``FRED_API_KEY`` fails every attempt for
#: a week, and must never read as FRED not publishing (U6d audit L2).
NOT_FETCHED_PREFIX: Final = "could not be fetched from FRED"


def _error_type(detail: str | None) -> str:
    """The exception type an ``error`` attempt's detail leads with.

    :func:`attempt_of_error` writes ``"<Type>: <message>"``, or ``"<Type>"``
    alone for an empty message; the type is the text before the first colon.
    """
    if detail is None:  # pragma: no cover - Attempt refuses an error without one
        return "unknown error"
    return detail.partition(":")[0].strip() or "unknown error"


@dataclass(frozen=True, slots=True)
class ValueDisplay:
    """What one economic row shows: its state, and the note beside it.

    ``note`` is the computed-from label on ``filled``, the reason on
    ``unavailable``, and ``None`` on ``upcoming`` and ``awaiting``.
    """

    state: ValueState
    note: str | None


def _aware(at: datetime, name: str) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime, got {at!r}")
    return at


def unavailable_from(release_at: datetime) -> datetime:
    """The first instant an unfilled release reads ``unavailable``, in UTC.

    ``release_at`` on the ET wall clock plus :data:`UNAVAILABLE_AFTER_DAYS`
    calendar days. Adding a ``timedelta`` to a ``ZoneInfo``-aware datetime is
    wall-clock arithmetic, so a daylight-saving change in between moves the
    elapsed hours, not the wall-clock time.
    """
    local = _aware(release_at, "release_at").astimezone(ET_ZONE)
    return (local + timedelta(days=UNAVAILABLE_AFTER_DAYS)).astimezone(UTC)


def value_display(
    release_at: datetime,
    *,
    value_series_id: str | None,
    last_attempt: Attempt | None,
    now: datetime,
) -> ValueDisplay:
    """The R8 display state of one economic row at ``now``.

    ``value_series_id`` is the stored value's series, ``None`` when no value
    is stored. In order:

    * a stored value is ``filled``, labelled with its series, whenever it is
      read;
    * before ``release_at`` is ``upcoming``;
    * a last attempt FRED ``refused`` is ``unavailable`` **at once**, with
      :data:`NOT_AVAILABLE_PREFIX` and the vendor's reason. A refusal is
      permanent (the series is not in ALFRED), not lateness, so the seven
      days do not apply to it (spec *Not verified*; U6e ruling);
    * from ``release_at`` up to, not including, :func:`unavailable_from` is
      ``awaiting`` -- an ``error`` inside that week too, since the next
      attempt may succeed;
    * from that instant on is ``unavailable``, with the computed reason if
      the last attempt was ``no_value``, :data:`NOT_FETCHED_PREFIX` and the
      error's type if it was ``error`` (never a claim that FRED did not
      publish: the fetch failed), and :data:`NOT_PUBLISHED_NOTE` otherwise.
    """
    release_at = _aware(release_at, "release_at")
    now = _aware(now, "now")
    if value_series_id is not None:
        return ValueDisplay("filled", computed_label(value_series_id))
    if now < release_at:
        return ValueDisplay("upcoming", None)
    if last_attempt is not None and last_attempt.outcome == "refused":
        return ValueDisplay("unavailable", f"{NOT_AVAILABLE_PREFIX}: {last_attempt.detail}")
    if now < unavailable_from(release_at):
        return ValueDisplay("awaiting", None)
    if last_attempt is not None and last_attempt.outcome == "no_value":
        return ValueDisplay("unavailable", last_attempt.detail)
    if last_attempt is not None and last_attempt.outcome == "error":
        return ValueDisplay("unavailable", f"{NOT_FETCHED_PREFIX}: {_error_type(last_attempt.detail)}")
    return ValueDisplay("unavailable", NOT_PUBLISHED_NOTE)
