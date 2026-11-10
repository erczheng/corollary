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
* **x(t-1) and x(t-2)** are the observations immediately before it **in the D
  vintage**, so a revision published that day is in the prior. Each must be
  exactly one period earlier at the stated frequency. The spec says
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

Then ``scale``, then quantized to :data:`STORED_EXPONENT` (four places,
``ROUND_HALF_EVEN``). A quantized negative zero is stored as ``0.0000``. The
division runs at 50 significant digits, so its one rounding cannot move the
four-place result: FRED's values carry a handful of digits, and a quotient of
two of them that does not terminate within 50 digits cannot lie within
10^-50 of a half-way point.

**Actual and prior together, or neither.** A ``"."`` or a missing row in any
observation either one needs, or a ``pct_change`` over zero, is
:class:`NoValue` with a reason naming the observation. Never an actual beside
an invented or zero prior. Display rounding to the seed's ``decimals``
(``ROUND_HALF_UP``) is not done here.
"""

from dataclasses import dataclass
from datetime import date
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
)
from typing import Final, TypeAlias

from corollary.data.providers.fred import (
    FredRefusal,
    FredVintageRow,
    FredVintageWindow,
    VintageFrequency,
)
from corollary.data.seeds.calendar_seeds import EconReleaseSeries

__all__ = [
    "COMPUTED_FROM_FRED",
    "STORED_EXPONENT",
    "Figure",
    "FirstPrint",
    "NoValue",
    "NotPosted",
    "Refused",
    "computed_label",
    "first_print",
]

#: What every figure is labelled, with its series ID (spec R7, *Labelling*),
#: so a 0.1 difference from the agency's printed number has an explanation.
COMPUTED_FROM_FRED: Final = "computed from FRED's levels"

#: Four decimal places: the stored figure's exponent.
STORED_EXPONENT: Final = Decimal("0.0001")

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
_PERIOD_MONTHS: Final[dict[VintageFrequency, int]] = {"monthly": 1, "quarterly": 3}

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


def _starts_a_period(day: date, frequency: VintageFrequency) -> bool:
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

    Raises ``ValueError`` only for a caller's bug: an answer for a different
    series than the seed row names.
    """
    if answer.series_id != series.series_id:
        raise ValueError(
            f"the answer is for {answer.series_id}, the seed row for {series.series_id}"
        )
    release_day = answer.release_day
    if isinstance(answer, FredRefusal):
        return Refused(series.series_id, release_day, answer.vendor_message)

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

    frequency = answer.frequency
    period = changed[-1]
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
