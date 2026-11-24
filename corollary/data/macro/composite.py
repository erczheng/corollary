"""Scoring and the Fear & Greed composite -- pure (step 8, U2).

Spec: ``docs/superpowers/specs/2026-11-17-phase-3-step-8-composite.md``,
decisions C6 to C10 and C14. :mod:`corollary.data.macro.components` (U1) turns
fetched inputs into one raw value per (session, component); this module turns
stored raw values into windows, z-scores, integer scores, the composite, its
as-of session, ``previous`` and the change.

What "pure" means here
----------------------

No I/O, no database, no provider, no environment, no clock. The imports are
:mod:`corollary.calendars`, :mod:`corollary.data.macro.components` and the
standard library, and a test parses this file and fails on anything else.
``now`` is an argument. :func:`read_composite` takes **rows the caller has
already loaded** (:class:`ComponentRow`, a plain frozen dataclass of the
``composite_component_value`` columns), never a database session: U3's store
loads them, and U7's routes call this function with them. C10 writes the
signature as ``read_composite(session, *, now)``; the session argument is
those loaded rows, so that this module stays testable by hand computation.

How much the caller loads, and ``loaded_from``
----------------------------------------------

A short load does not fail by itself: it silently shortens a window, which
moves z. The windows the read needs are today's, the 252 sessions ending *D*,
and ``previous``'s, the 252 ending the session before *D*; the second starts
one session earlier and so bounds the load. :func:`load_start` gives that
first session for a given as-of session.

The caller cannot size the load exactly in advance, because *D* depends on
the rows: when *D* is behind ``expected_as_of`` by k sessions (every Alpaca
component's rows stopped k sessions ago), its windows start k sessions
earlier than ``load_start(expected_as_of(now))``. So :func:`read_composite`
takes a required ``loaded_from``, the first session the caller's load covers
(every stored row dated on or after it is in ``rows``), and once *D* is known
it checks that ``load_start(D) >= loaded_from``. If not, it raises
:class:`IncompleteLoad`, whose ``needed_from`` is ``load_start(D)``. It never
computes on a truncated window. The intended caller (U7) does::

    loaded_from = load_start(expected_as_of(now))
    try:
        reading = read_composite(load(loaded_from), now=now, loaded_from=loaded_from)
    except IncompleteLoad as short:
        reading = read_composite(
            load(short.needed_from), now=now, loaded_from=short.needed_from
        )

The reload cannot raise again unless rows are deleted between the two loads.
*D* is a maximum over the Alpaca rows (Q29, below), and in the call above
every row in the first load is dated on or after its ``loaded_from``, so *D*
is too. The reload adds only rows dated before that, which are older than
*D*, and a maximum does not move for rows below it: the reload cannot move
*D*. A row written in between can move *D*, but only later, which needs a
later start, not an earlier one. Rows dated before ``loaded_from`` may be
passed and are read like any other; if a caller passes only some of them,
the reload can find a later *D* among the rest, never an earlier one, so it
still cannot raise again.

What ``loaded_from`` cannot fix: the verdicts that depend on there being *no*
row -- :attr:`ExclusionReason.NO_VALUE` and :attr:`UnavailableReason.NO_ROWS`
-- are statements about the rows loaded, and their details name
``loaded_from``. A component whose newest row predates the load reads as
``no_value`` rather than ``stale``, and when every Alpaca row predates the
load the result is ``NO_ROWS`` rather than a stale *D*.

Decimal, and the only two roundings
-----------------------------------

No float is accepted and none is produced. z is computed under an explicit
40-digit context with ``Decimal.sqrt`` (C6); Phi is the Taylor series of erf
under a 50-digit context (C7). The only quantizations are the two
``ROUND_HALF_UP`` integer roundings C7 names: a component's score, and the
composite as the mean of the integer scores. z is served as computed, never
clipped and never quantized.

Rule 8: nothing is dropped silently
-----------------------------------

Every one of the seven components appears in the result, always, in
:class:`ComponentKey` order. One that is not averaged carries an
:class:`ExclusionReason` and a detail naming its inputs. A missing composite
carries an :class:`UnavailableReason` and a detail.

The as-of rule (C8, Q28 option b, Q29 option ii), as implemented
----------------------------------------------------------------

* ``expected_as_of`` is the latest NYSE session whose close + 60 minutes is
  at or before ``now`` (C14). The close comes from the calendar, so a
  half-day's boundary is 14:00 ET. *Interpretation:* "has passed" is read as
  ``now >= close + 60 minutes``.
* *D* (``as_of``) is the newest session among the five Alpaca components'
  (momentum, strength, breadth, put_call, safe_haven) newest rows dated on
  or before ``expected_as_of`` (Q29). Rows dated after ``expected_as_of`` are
  not *D* candidates, which keeps C8's fixed rule that *D* is never a session
  whose close + 60 minutes has not passed. Forming components count: they
  have values. The five need not share a session, so one Alpaca component
  that stopped or never started does not hold *D* back for the other six.
* No Alpaca row at all on or before ``expected_as_of`` means no *D* and no
  composite: :attr:`UnavailableReason.NO_ROWS`, returned, never raised. FRED
  rows alone do not make a *D*.
* Every component, Alpaca or FRED, then uses its newest value dated on or
  before *D*. A value dated *D* has ``lag_sessions`` 0; the session before
  *D*, 1 (included, and makes the result ``provisional``, named in the note
  exactly as a VIX or junk lag is -- a put/call gap on *D* is one); more than
  one session older, ``stale``. No row on or before *D* at all is
  ``no_value``. Both exclusions drop the count ("N of 7").
* The window is the 252 NYSE sessions ending *D* (C6) for every component,
  lagged or not.
* ``stale`` is true when *D* is older than ``expected_as_of`` (C14). Under
  Q29 that happens only when every Alpaca component's rows stop short of
  ``expected_as_of``; one stopped component is ``stale`` on its own row
  instead.
* ``previous`` is the same derivation as of the session before *D* (C9):
  the same lag, ``stale`` and ``no_value`` rule, over windows ending there.
  The change is printed only when both days average the same component set;
  otherwise the note is :data:`COMPONENT_SET_CHANGED`.
* ``provisional_note`` names each lagged component by its key (C2's table
  gives no display name per component) with its value date, ISO-formatted;
  C8's own example is "VIX: Nov 13 close".
* ``change`` and ``change_note`` are both ``None`` when there is no composite
  today: there is nothing to compare, and no set changed to say so about.
  ``previous`` is still served, so U8 can show yesterday's figure.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import StrEnum
from typing import Final

from corollary.calendars import NYSE_TZ, nyse_session_close, nyse_sessions_between
from corollary.data.macro.components import (
    COMPONENT_SPECS,
    PRECISION,
    ComponentKey,
    Orientation,
)

__all__ = [
    "ALPACA_COMPONENTS",
    "AS_OF_DELAY",
    "COMPONENT_SET_CHANGED",
    "FORMING_NEEDS",
    "PHI_PRECISION",
    "PI",
    "SATURATION",
    "WINDOW_SESSIONS",
    "ComponentReading",
    "ComponentRow",
    "CompositeReading",
    "ExclusionReason",
    "IncompleteLoad",
    "PreviousComposite",
    "UnavailableReason",
    "composite_of",
    "expected_as_of",
    "load_start",
    "phi",
    "read_composite",
    "score",
]

#: C6: the window for as-of session *D* is the 252 NYSE sessions ending *D*.
WINDOW_SESSIONS: Final = 252

#: C6: fewer than this many values in the window is forming.
FORMING_NEEDS: Final = 60

#: C8: *D* is never a session whose close + this has not passed.
AS_OF_DELAY: Final = timedelta(minutes=60)

#: C7: Phi's Taylor series runs at this many digits.
PHI_PRECISION: Final = 50

#: C7: at or beyond this |z| the score is exactly 100 or 0.
SATURATION: Final = Decimal(7)

#: pi to 65 significant digits. A test checks it against a Machin series.
PI: Final = Decimal("3.1415926535897932384626433832795028841971693993751058209749445923")

#: C8 / Q29: the five components whose newest rows set *D*.
ALPACA_COMPONENTS: Final = (
    ComponentKey.MOMENTUM,
    ComponentKey.STRENGTH,
    ComponentKey.BREADTH,
    ComponentKey.PUT_CALL,
    ComponentKey.SAFE_HAVEN,
)

#: C9's note when the two days average different component sets.
COMPONENT_SET_CHANGED: Final = "component set changed"

_ONE: Final = Decimal(1)

#: Terms below this no longer move a 50-digit sum of order one.
_SERIES_EPSILON: Final = Decimal("1E-60")


class ExclusionReason(StrEnum):
    """Why a component is not averaged (rule 8). The values are what U7 serves."""

    #: No stored value dated on or before *D* among the rows loaded.
    NO_VALUE = "no_value"
    #: The newest value is more than one session older than *D* (C8).
    STALE = "stale"
    #: Fewer than :data:`FORMING_NEEDS` values in the window (C6).
    FORMING = "forming"
    #: The window's sample standard deviation is zero, so z does not exist (C6).
    NO_VARIANCE = "no_variance"


class UnavailableReason(StrEnum):
    """Why there is no composite. The detail beside it says it in words."""

    #: None of the five Alpaca components has a row on or before ``expected_as_of``.
    NO_ROWS = "no_rows"
    #: *D* exists, but every component is forming or excluded (C7).
    NO_USABLE_COMPONENTS = "no_usable_components"


class IncompleteLoad(Exception):
    """The rows passed do not reach back far enough for *D*'s windows.

    ``needed_from`` is :func:`load_start` of ``as_of``: load every stored row
    dated on or after it and read again. Raised rather than computing on a
    short window, which would silently move z (see the module docstring).
    """

    def __init__(self, *, needed_from: date, loaded_from: date, as_of: date) -> None:
        self.needed_from = needed_from
        self.loaded_from = loaded_from
        self.as_of = as_of
        super().__init__(
            f"incomplete load: as of {as_of}, the windows for it and for the "
            f"session before it start {needed_from}, but the rows were loaded "
            f"from {loaded_from}; reload from {needed_from}"
        )


# --- Inputs ------------------------------------------------------------------------


def _require_date(value: date, name: str) -> None:
    # ``datetime`` subclasses ``date``: an instant is not a session date.
    if type(value) is not date:
        raise TypeError(f"{name} must be a date, got {type(value).__name__} {value!r}")


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, got {value!r}")


def _require_decimal(value: Decimal, name: str) -> None:
    if type(value) is not Decimal:
        raise TypeError(f"{name} must be a Decimal, got {type(value).__name__} {value!r}")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite, got {value!r}")


@dataclass(frozen=True)
class ComponentRow:
    """One stored ``composite_component_value`` row, as the caller loads it."""

    component: ComponentKey
    session: date
    raw_value: Decimal
    source: str
    members: int | None
    universe_as_of: date | None
    computed_at: datetime

    def __post_init__(self) -> None:
        if type(self.component) is not ComponentKey:
            raise TypeError(f"component must be a ComponentKey, got {self.component!r}")
        _require_date(self.session, "session")
        _require_decimal(self.raw_value, "raw_value")
        if type(self.source) is not str or not self.source:
            raise ValueError("source must be a non-empty string")
        if self.members is not None and (type(self.members) is not int or self.members < 0):
            raise ValueError(f"members must be a non-negative int or None, got {self.members!r}")
        if self.universe_as_of is not None:
            _require_date(self.universe_as_of, "universe_as_of")
        _require_aware(self.computed_at, "computed_at")


# --- Results -----------------------------------------------------------------------


@dataclass(frozen=True)
class ComponentReading:
    """One component as of *D*: its value, window, z and score, or why not.

    ``value_date``, ``lag_sessions``, ``raw_value``, ``source``, ``members``,
    ``universe_as_of`` describe the value used (or, when ``stale``, the newest
    value that was too old); all are ``None`` for ``no_value``.
    ``lag_sessions`` is 0 or 1 for an included component; a stale one reports
    its actual lag. ``z`` is unoriented and unclipped; ``oriented_z`` is
    ``z * orientation``, the input to Phi.
    """

    key: ComponentKey
    orientation: Orientation
    value_date: date | None
    lag_sessions: int | None
    raw_value: Decimal | None
    source: str | None
    members: int | None
    universe_as_of: date | None
    window_start: date
    window_end: date
    observations: int
    forming_needs: int
    forming: bool
    z: Decimal | None
    oriented_z: Decimal | None
    score: int | None
    excluded_reason: ExclusionReason | None
    excluded_detail: str | None


@dataclass(frozen=True)
class PreviousComposite:
    """C9: the composite as of the session before *D*."""

    as_of: date
    composite: int | None
    components_averaged: int


@dataclass(frozen=True)
class CompositeReading:
    """Everything ``GET /api/news/composite`` serves (C10), derived on read.

    ``components`` is all seven readings in :class:`ComponentKey` order, or
    empty when there is no *D*. ``lagged`` pairs each included lagged
    component with its value date, in key order, for U7/U8 to phrase.
    """

    as_of: date | None
    expected_as_of: date
    stale: bool
    provisional: bool
    provisional_note: str | None
    lagged: tuple[tuple[ComponentKey, date], ...]
    composite: int | None
    components_averaged: int
    components_total: int
    unavailable_reason: UnavailableReason | None
    unavailable_detail: str | None
    previous: PreviousComposite | None
    change: int | None
    change_note: str | None
    computed_at: datetime | None
    components: tuple[ComponentReading, ...]


# --- Phi and the scores (C7) -------------------------------------------------------


def phi(z: Decimal) -> Decimal:
    """The standard normal CDF at ``z``, in ``Decimal``, from erf's Taylor series.

    Phi(z) = (1 + erf(z / sqrt 2)) / 2, with
    erf(x) = 2 / sqrt(pi) * sum_n (-1)^n x^(2n+1) / (n! (2n+1)),
    summed under a :data:`PHI_PRECISION`-digit context until a term drops below
    1E-60. Defined for ``|z| <`` :data:`SATURATION`; the series still converges
    beyond it, but the score never asks.
    """
    _require_decimal(z, "z")
    if abs(z) >= SATURATION:
        raise ValueError(f"phi is evaluated only for |z| < {SATURATION}, got {z}")
    with localcontext() as ctx:
        ctx.prec = PHI_PRECISION
        x = z / Decimal(2).sqrt()
        x_squared = x * x
        power = x  # (-1)^n x^(2n+1) / n!
        total = Decimal(0)
        n = 0
        while True:
            term = power / (2 * n + 1)
            total += term
            if abs(term) < _SERIES_EPSILON and n > 0:
                break
            n += 1
            power = -power * x_squared / n
        erf = 2 * total / PI.sqrt()
        return (_ONE + erf) / 2


def score(oriented_z: Decimal) -> int:
    """C7: ``ROUND_HALF_UP(100 * Phi(oriented z))``, an integer 0 to 100.

    At |z| >= 7 the score is exactly 100 or 0, because 100 * (1 - Phi(7)) is
    below 1.3E-10, which no integer rounding can see.
    """
    _require_decimal(oriented_z, "oriented_z")
    if oriented_z >= SATURATION:
        return 100
    if oriented_z <= -SATURATION:
        return 0
    with localcontext() as ctx:
        ctx.prec = PHI_PRECISION
        scaled = 100 * phi(oriented_z)
        return int(scaled.quantize(_ONE, rounding=ROUND_HALF_UP))


def composite_of(scores: Sequence[int]) -> int:
    """C7: ``ROUND_HALF_UP`` of the mean of the integer scores printed."""
    if not scores:
        raise ValueError("a composite needs at least one score")
    for value in scores:
        if type(value) is not int:
            raise TypeError(f"scores must be ints, got {type(value).__name__} {value!r}")
        if not 0 <= value <= 100:
            raise ValueError(f"scores run from 0 to 100, got {value}")
    with localcontext() as ctx:
        ctx.prec = PRECISION
        mean = Decimal(sum(scores)) / Decimal(len(scores))
        return int(mean.quantize(_ONE, rounding=ROUND_HALF_UP))


# --- Sessions ----------------------------------------------------------------------


def _sessions_ending(session: date, count: int) -> tuple[date, ...]:
    """The ``count`` NYSE sessions ending ``session``, from the calendar."""
    sessions = nyse_sessions_between(session - timedelta(days=2 * count + 14), session)
    return sessions[-count:]


def expected_as_of(now: datetime) -> date:
    """C14: the latest NYSE session whose close + 60 minutes is at or before ``now``."""
    _require_aware(now, "now")
    today = now.astimezone(NYSE_TZ).date()
    for session in reversed(nyse_sessions_between(today - timedelta(days=14), today)):
        close = nyse_session_close(session)
        if close is not None and close + AS_OF_DELAY <= now:
            return session
    raise ValueError(f"no NYSE session closed in the fortnight before {now}")


def load_start(as_of: date) -> date:
    """The first session a load must cover to read the composite as of ``as_of``.

    That is the first session of the :data:`WINDOW_SESSIONS`-session window
    ending the session before ``as_of`` -- ``previous``'s window (C9), which
    starts one session before today's (C6) and so contains it. Counted on the
    NYSE calendar. ``as_of`` must be an NYSE session.
    """
    _require_date(as_of, "as_of")
    if nyse_session_close(as_of) is None:
        raise ValueError(f"{as_of} is not an NYSE session")
    before = _sessions_ending(as_of, 2)[0]
    return _sessions_ending(before, WINDOW_SESSIONS)[0]


# --- The window and z (C6) ---------------------------------------------------------


def _z(values: Sequence[Decimal], x: Decimal) -> Decimal | None:
    """z of ``x`` against ``values`` (which include it), sample s; None when s = 0."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        n = len(values)
        mean = sum(values, Decimal(0)) / n
        variance = sum(((v - mean) ** 2 for v in values), Decimal(0)) / (n - 1)
        if variance == 0:
            return None
        return (x - mean) / variance.sqrt()


def _read_component(
    key: ComponentKey,
    by_session: Mapping[date, ComponentRow],
    as_of: date,
    window: tuple[date, ...],
    loaded_from: date,
) -> ComponentReading:
    orientation = COMPONENT_SPECS[key].orientation
    values = [by_session[d].raw_value for d in window if d in by_session]
    observations = len(values)
    forming = observations < FORMING_NEEDS
    dated = [d for d in by_session if d <= as_of]

    def reading(
        used: ComponentRow | None,
        lag: int | None,
        z: Decimal | None,
        oriented: Decimal | None,
        value_score: int | None,
        reason: ExclusionReason | None,
        detail: str | None,
    ) -> ComponentReading:
        return ComponentReading(
            key=key,
            orientation=orientation,
            value_date=None if used is None else used.session,
            lag_sessions=lag,
            raw_value=None if used is None else used.raw_value,
            source=None if used is None else used.source,
            members=None if used is None else used.members,
            universe_as_of=None if used is None else used.universe_as_of,
            window_start=window[0],
            window_end=window[-1],
            observations=observations,
            forming_needs=FORMING_NEEDS,
            forming=forming,
            z=z,
            oriented_z=oriented,
            score=value_score,
            excluded_reason=reason,
            excluded_detail=detail,
        )

    if not dated:
        return reading(
            None, None, None, None, None, ExclusionReason.NO_VALUE,
            f"{key.value} has no stored value on or before {as_of} in the rows "
            f"loaded from {loaded_from}",
        )
    used = by_session[max(dated)]
    lag = len(nyse_sessions_between(used.session, as_of)) - 1
    if lag > 1:
        return reading(
            used, lag, None, None, None, ExclusionReason.STALE,
            f"{key.value}'s newest value is dated {used.session}, {lag} sessions "
            f"before {as_of}; at most one is allowed",
        )
    if forming:
        return reading(
            used, lag, None, None, None, ExclusionReason.FORMING,
            f"forming, {observations} of {FORMING_NEEDS} sessions in the "
            f"{WINDOW_SESSIONS}-session window ending {as_of}",
        )
    z = _z(values, used.raw_value)
    if z is None:
        return reading(
            used, lag, None, None, None, ExclusionReason.NO_VARIANCE,
            f"no variance in the window: all {observations} values in the "
            f"{WINDOW_SESSIONS}-session window ending {as_of} equal {used.raw_value}",
        )
    # Exact: negation is not rounded, where ``z * -1`` would round to the
    # ambient context's 28 digits.
    oriented = z if orientation is Orientation.POSITIVE else z.copy_negate()
    return reading(used, lag, z, oriented, score(oriented), None, None)


def _exclusion_summary(reading: ComponentReading) -> str:
    reason = reading.excluded_reason
    if reason is ExclusionReason.FORMING:
        return f"{reading.key.value} forming, {reading.observations} of {FORMING_NEEDS}"
    return f"{reading.key.value} {reason.value if reason is not None else 'excluded'}"


@dataclass(frozen=True)
class _Scored:
    """One as-of session's component readings and composite (C7, C8)."""

    readings: tuple[ComponentReading, ...]
    composite: int | None
    averaged: frozenset[ComponentKey]
    unavailable_detail: str | None
    computed_at: datetime | None


def _score_at(
    by_key: Mapping[ComponentKey, Mapping[date, ComponentRow]],
    as_of: date,
    loaded_from: date,
) -> _Scored:
    window = _sessions_ending(as_of, WINDOW_SESSIONS)
    readings = tuple(
        _read_component(key, by_key[key], as_of, window, loaded_from) for key in ComponentKey
    )
    included = [r for r in readings if r.score is not None]
    computed = [
        row.computed_at
        for key in ComponentKey
        for day, row in by_key[key].items()
        if window[0] <= day <= as_of
    ]
    value: int | None = None
    detail: str | None = None
    if included:
        value = composite_of([r.score for r in included if r.score is not None])
    else:
        detail = f"no usable component as of {as_of}: " + "; ".join(
            _exclusion_summary(r) for r in readings
        )
    return _Scored(
        readings=readings,
        composite=value,
        averaged=frozenset(r.key for r in included),
        unavailable_detail=detail,
        computed_at=max(computed) if computed else None,
    )


# --- The read (C8-C10, C14) --------------------------------------------------------


def _index(rows: Iterable[ComponentRow]) -> dict[ComponentKey, dict[date, ComponentRow]]:
    by_key: dict[ComponentKey, dict[date, ComponentRow]] = {key: {} for key in ComponentKey}
    for row in rows:
        if not isinstance(row, ComponentRow):
            raise TypeError(f"rows must be ComponentRow, got {type(row).__name__}")
        if nyse_session_close(row.session) is None:
            raise ValueError(
                f"{row.component.value} row dated {row.session} is not an NYSE session"
            )
        if row.session in by_key[row.component]:
            raise ValueError(f"duplicate {row.component.value} row for {row.session}")
        by_key[row.component][row.session] = row
    return by_key


def _names(keys: Sequence[ComponentKey]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    values = [key.value for key in keys]
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + " and " + values[-1]


def _provisional_note(as_of: date, lagged: Sequence[tuple[ComponentKey, date]]) -> str:
    """C8: name each lagged component and its date, grouped by date.

    Keys, not display names: C2's table gives none. One clause per value
    date, earliest first; within a clause, :class:`ComponentKey` order. An
    included component lags by at most one session, so in practice there is
    one clause.
    """
    by_day: dict[date, list[ComponentKey]] = {}
    for key, day in lagged:
        by_day.setdefault(day, []).append(key)
    clauses = [
        f"{_names(keys)} {'uses' if len(keys) == 1 else 'use'} the {day} close"
        for day, keys in sorted(by_day.items())
    ]
    return f"Provisional: as of the {as_of} close, " + "; ".join(clauses)


def _unavailable(
    expected: date, reason: UnavailableReason, detail: str
) -> CompositeReading:
    return CompositeReading(
        as_of=None,
        expected_as_of=expected,
        stale=False,
        provisional=False,
        provisional_note=None,
        lagged=(),
        composite=None,
        components_averaged=0,
        components_total=len(ComponentKey),
        unavailable_reason=reason,
        unavailable_detail=detail,
        previous=None,
        change=None,
        change_note=None,
        computed_at=None,
        components=(),
    )


def read_composite(
    rows: Iterable[ComponentRow], *, now: datetime, loaded_from: date
) -> CompositeReading:
    """The composite as of the latest completed close, derived from ``rows``.

    ``rows`` are the stored ``composite_component_value`` rows the caller
    (U3's store, through U7's routes) has loaded: every row dated on or after
    ``loaded_from``. ``now`` must be timezone-aware. Raises
    :class:`IncompleteLoad` when *D*'s windows start before ``loaded_from``
    (reload from its ``needed_from``; see the module docstring). Every as-of
    shape returns a result; none raises.
    """
    _require_aware(now, "now")
    _require_date(loaded_from, "loaded_from")
    expected = expected_as_of(now)
    by_key = _index(rows)

    # C8 / Q29: D is the newest session among the five Alpaca components'
    # newest rows. Rows dated after expected_as_of are not D candidates.
    candidates = [
        d for key in ALPACA_COMPONENTS for d in by_key[key] if d <= expected
    ]
    if not candidates:
        return _unavailable(
            expected,
            UnavailableReason.NO_ROWS,
            f"none of the five Alpaca components has a value on or before "
            f"{expected} in the rows loaded from {loaded_from}; the composite "
            "is first computed after a close",
        )
    as_of = max(candidates)

    # F3: never compute on a truncated window. previous's window starts
    # first and contains today's, so one check covers both.
    needed_from = load_start(as_of)
    if needed_from < loaded_from:
        raise IncompleteLoad(needed_from=needed_from, loaded_from=loaded_from, as_of=as_of)

    today = _score_at(by_key, as_of, loaded_from)
    before = _sessions_ending(as_of, 2)[0]
    yesterday = _score_at(by_key, before, loaded_from)

    lagged = tuple(
        (r.key, r.value_date)
        for r in today.readings
        if r.score is not None
        and r.lag_sessions is not None
        and r.lag_sessions > 0
        and r.value_date is not None
    )
    provisional_note = None if not lagged else _provisional_note(as_of, lagged)

    # C9: a change only between like sets. With no composite today there is
    # nothing to compare, and no set changed to say so about.
    change: int | None = None
    change_note: str | None = None
    if today.composite is not None:
        if yesterday.composite is not None and today.averaged == yesterday.averaged:
            change = today.composite - yesterday.composite
        else:
            change_note = COMPONENT_SET_CHANGED

    return CompositeReading(
        as_of=as_of,
        expected_as_of=expected,
        stale=as_of < expected,
        provisional=bool(lagged),
        provisional_note=provisional_note,
        lagged=lagged,
        composite=today.composite,
        components_averaged=len(today.averaged),
        components_total=len(ComponentKey),
        unavailable_reason=(
            None if today.composite is not None else UnavailableReason.NO_USABLE_COMPONENTS
        ),
        unavailable_detail=today.unavailable_detail,
        previous=PreviousComposite(
            as_of=before,
            composite=yesterday.composite,
            components_averaged=len(yesterday.averaged),
        ),
        change=change,
        change_note=change_note,
        computed_at=today.computed_at,
        components=today.readings,
    )
