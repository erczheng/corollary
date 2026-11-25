"""The seven raw components of the Fear & Greed composite -- pure (step 8, U1).

Spec: ``docs/superpowers/specs/2026-11-17-phase-3-step-8-composite.md``,
decisions C1 to C4. This module turns already-fetched inputs into one raw
value per (session, component). It scores nothing: windows, z, Phi and the
composite are ``composite.py`` (U2), and fetching and storing are U3 to U6.

What "pure" means here, and why it is tested
--------------------------------------------

No I/O, no database, no provider, no environment, and no import beyond
:mod:`corollary.calendars` and the standard library -- a test parses this
file's imports and fails on anything else. The inputs are plain frozen
dataclasses of ``Decimal``, ``int`` and ``date``, which is what lets every
expected value in the tests be computed by hand.

Every function returns a :class:`RawValue` or a :class:`NoValue`
------------------------------------------------------------------

Never ``None`` and never zero (rule 8). A missing input is a *stated* absence:
:class:`NoValue` carries a :class:`NoValueRule` and a human-readable detail,
and the caller (U4 to U6) logs both with the series and the session. A
zero-filled breadth on a day the bars did not arrive would score as a
perfectly neutral market, which is a figure nobody measured.

What raises instead
-------------------

A :class:`NoValue` is for inputs that are *absent*. Inputs that are *wrong*
raise, because computing on them would print a number from corrupt data:

* ``TypeError`` -- a ``float`` (or ``int``, or ``bool``) where a ``Decimal``
  belongs, a ``bool`` or ``float`` where a volume belongs, a ``datetime``
  where a session date belongs. No float is accepted, so none is produced.
* ``ValueError`` -- a non-finite or non-positive price, a negative volume, a
  series that is not strictly ascending, a duplicate symbol, a series of the
  wrong ``adjustment`` for its component, a mixed option chain, or an as-of
  date that is not an NYSE session.

Arithmetic
----------

Every computation runs under an explicit 40-digit ``localcontext`` and the
result is quantized to :data:`RAW_QUANTUM` (``1E-10``) with
``ROUND_HALF_EVEN`` (C1), so the stored text is deterministic.

Windows count sessions with a bar, or non-missing observations, never
calendar days (C2). The one window cut from the calendar is strength's
252-session year (C3), and the one calendar neighbour is breadth's "session
before" -- both from :func:`corollary.calendars.nyse_sessions_between`.

Interpretations this module makes, stated so they can be overridden
--------------------------------------------------------------------

* **safe_haven's 20-session return** is close(*t*) over the close 20 bars
  earlier, per series -- C2's "windows count sessions with a bar" applied to
  each of SPY and TLT on its own.
* **put_call takes the put/call flag and the OCC root already parsed.** C4
  says both come from the OCC symbol; parsing lives in
  ``corollary.instruments``, which this module may not import, so the caller
  (U6) parses and this module applies the adjusted-root rule
  (``root != underlying``, case-insensitive, as ``instruments.is_adjusted_root``).
"""

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Final

from corollary.calendars import nyse_sessions_between

__all__ = [
    "COMPONENT_SPECS",
    "MOMENTUM_CLOSES",
    "PRECISION",
    "RAW_QUANTUM",
    "SAFE_HAVEN_SESSIONS",
    "STRENGTH_SESSIONS",
    "VOLATILITY_OBSERVATIONS",
    "Adjustment",
    "BarSeries",
    "ComponentKey",
    "ComponentSpec",
    "DailyBar",
    "NoValue",
    "NoValueRule",
    "Observation",
    "OptionContractVolume",
    "Orientation",
    "RawValue",
    "breadth",
    "junk_bond",
    "momentum",
    "put_call",
    "safe_haven",
    "strength",
    "strength_over",
    "volatility",
]

#: C1: every stored raw value is quantized to this, ``ROUND_HALF_EVEN``.
RAW_QUANTUM: Final = Decimal("1E-10")

#: The digits every computation here carries before quantizing.
PRECISION: Final = 40

#: C2: momentum's moving average is 125 closes ending *t*.
MOMENTUM_CLOSES: Final = 125

#: C3: strength's "52 weeks" is the 252 NYSE sessions ending *t*.
STRENGTH_SESSIONS: Final = 252

#: C2: volatility's moving average is 50 non-missing VIX observations ending *t*.
VOLATILITY_OBSERVATIONS: Final = 50

#: C2: safe_haven compares 20-session returns.
SAFE_HAVEN_SESSIONS: Final = 20


# --- The table as data (C2) --------------------------------------------------


class ComponentKey(StrEnum):
    """C2's seven keys. The values are what ``composite_component_value`` stores."""

    MOMENTUM = "momentum"
    STRENGTH = "strength"
    BREADTH = "breadth"
    PUT_CALL = "put_call"
    VOLATILITY = "volatility"
    SAFE_HAVEN = "safe_haven"
    JUNK_BOND = "junk_bond"


class Orientation(IntEnum):
    """The sign that makes a higher z mean greed. U2 multiplies z by it."""

    POSITIVE = 1
    NEGATIVE = -1


class Adjustment(StrEnum):
    """Alpaca's ``adjustment=`` for daily bars. The values are the parameter's."""

    SPLIT = "split"
    ALL = "all"


@dataclass(frozen=True)
class ComponentSpec:
    """One row of C2's table, as data rather than prose.

    ``adjustment`` is the daily-bar adjustment the bars job must request for
    this component, or ``None`` for the three that are not built from daily
    stock bars. The functions below refuse a series of any other adjustment,
    so the table and the computation cannot disagree.
    """

    key: ComponentKey
    orientation: Orientation
    adjustment: Adjustment | None
    proxy_of: str


COMPONENT_SPECS: Final[Mapping[ComponentKey, ComponentSpec]] = MappingProxyType(
    {
        spec.key: spec
        for spec in (
            ComponentSpec(
                ComponentKey.MOMENTUM,
                Orientation.POSITIVE,
                Adjustment.SPLIT,
                "S&P 500 vs its 125-day MA",
            ),
            ComponentSpec(
                ComponentKey.STRENGTH,
                Orientation.POSITIVE,
                Adjustment.SPLIT,
                "NYSE 52-week highs vs lows",
            ),
            ComponentSpec(
                ComponentKey.BREADTH,
                Orientation.POSITIVE,
                Adjustment.SPLIT,
                "advance/decline volume",
            ),
            ComponentSpec(
                ComponentKey.PUT_CALL,
                Orientation.NEGATIVE,
                None,
                "CBOE put/call ratio",
            ),
            ComponentSpec(
                ComponentKey.VOLATILITY,
                Orientation.NEGATIVE,
                None,
                "VIX vs its 50-day MA",
            ),
            ComponentSpec(
                ComponentKey.SAFE_HAVEN,
                Orientation.POSITIVE,
                Adjustment.ALL,
                "stock vs Treasury returns",
            ),
            ComponentSpec(
                ComponentKey.JUNK_BOND,
                Orientation.NEGATIVE,
                None,
                "junk vs investment-grade spread",
            ),
        )
    }
)


# --- Results ----------------------------------------------------------------


class NoValueRule(StrEnum):
    """Why a component has no raw value for a session. Logged by the caller."""

    #: The series has no bar dated *t* (momentum, safe_haven).
    NO_BAR_ON_SESSION = "no_bar_on_session"
    #: Fewer bars or non-missing observations ending *t* than the window needs.
    INSUFFICIENT_HISTORY = "insufficient_history"
    #: No universe name meets C3's eligibility on *t* (strength, breadth).
    NO_ELIGIBLE_NAMES = "no_eligible_names"
    #: Breadth's eligible names traded no volume at all on *t*.
    ZERO_TOTAL_VOLUME = "zero_total_volume"
    #: No standard-root contract in the chain has a ``dailyBar`` dated *t*.
    NO_CONTRACTS_ON_SESSION = "no_contracts_on_session"
    #: The counted contracts' call volume is zero; the ratio is undefined.
    ZERO_CALL_VOLUME = "zero_call_volume"
    #: FRED has no observation dated *t* (not yet posted, or a bond holiday).
    NO_OBSERVATION_ON_SESSION = "no_observation_on_session"
    #: FRED published ``"."`` for *t*.
    MISSING_OBSERVATION = "missing_observation"
    #: VIX's 50-observation mean is not positive, so the ratio means nothing.
    NON_POSITIVE_MEAN = "non_positive_mean"


@dataclass(frozen=True)
class NoValue:
    """A stated absence: the rule that produced it and what the inputs were."""

    rule: NoValueRule
    detail: str

    def __post_init__(self) -> None:
        if type(self.rule) is not NoValueRule:
            raise TypeError(f"rule must be a NoValueRule, got {self.rule!r}")
        if not self.detail:
            raise ValueError("a NoValue needs a detail naming its inputs")


@dataclass(frozen=True)
class RawValue:
    """One raw value, quantized to :data:`RAW_QUANTUM`.

    ``members`` is the count behind it -- eligible names for strength, the
    names counted for breadth (owner decision 3a, 2026-11-24), contracts
    counted for put/call, ``None`` for the rest -- which is what
    ``composite_component_value.members`` stores.
    """

    value: Decimal
    members: int | None


# --- Inputs ----------------------------------------------------------------


def _require_session_date(value: date, name: str) -> None:
    # ``datetime`` subclasses ``date``: an instant is not a session date.
    if type(value) is not date:
        raise TypeError(f"{name} must be a date, got {type(value).__name__} {value!r}")


def _require_decimal(value: Decimal, name: str) -> None:
    if type(value) is not Decimal:
        raise TypeError(f"{name} must be a Decimal, got {type(value).__name__} {value!r}")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite, got {value!r}")


def _require_volume(value: int, name: str) -> None:
    # ``type is int`` rather than ``isinstance``: a bool is an int.
    if type(value) is not int:
        raise TypeError(f"{name} must be an int, got {type(value).__name__} {value!r}")
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {value}")


@dataclass(frozen=True)
class DailyBar:
    """One daily bar, as the bars job hands it over. Prices are positive ``Decimal``."""

    session: date
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        _require_session_date(self.session, "session")
        for name in ("high", "low", "close"):
            price: Decimal = getattr(self, name)
            _require_decimal(price, name)
            if price <= 0:
                raise ValueError(f"{name} must be positive, got {price} on {self.session}")
        _require_volume(self.volume, "volume")


def _require_ascending(sessions: Sequence[date], what: str) -> None:
    for earlier, later in zip(sessions, sessions[1:]):
        if not earlier < later:
            raise ValueError(
                f"{what} must be strictly ascending by date, got {earlier} then {later}"
            )


@dataclass(frozen=True)
class BarSeries:
    """One symbol's daily bars, strictly ascending, all of one adjustment."""

    symbol: str
    adjustment: Adjustment
    bars: tuple[DailyBar, ...]

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("a bar series needs a symbol")
        if type(self.adjustment) is not Adjustment:
            raise TypeError(f"adjustment must be an Adjustment, got {self.adjustment!r}")
        if type(self.bars) is not tuple:
            raise TypeError(f"bars must be a tuple, got {type(self.bars).__name__}")
        for bar in self.bars:
            if type(bar) is not DailyBar:
                raise TypeError(f"{self.symbol}: bars must be DailyBar, got {bar!r}")
        _require_ascending([b.session for b in self.bars], f"{self.symbol}'s bars")


@dataclass(frozen=True)
class Observation:
    """One FRED observation. ``value`` is ``None`` where FRED published ``"."``."""

    observed: date
    value: Decimal | None

    def __post_init__(self) -> None:
        _require_session_date(self.observed, "observed")
        if self.value is not None:
            _require_decimal(self.value, "value")


@dataclass(frozen=True)
class OptionContractVolume:
    """One chain contract's put/call side, OCC root, and ``dailyBar`` (C4).

    ``symbol`` is the contract's OCC symbol, carried so :func:`put_call` can
    refuse a contract listed twice. ``root`` and ``is_put`` are parsed from
    it by the caller; ``underlying`` is the chain's (``root_symbol``).
    ``bar_session`` is the session the contract's ``dailyBar`` is dated, or
    ``None`` when the snapshot carries none; ``volume`` is that bar's ``v``.
    """

    symbol: str
    root: str
    underlying: str
    is_put: bool
    bar_session: date | None
    volume: int

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("a contract needs its OCC symbol")
        if not self.root or not self.underlying:
            raise ValueError("a contract needs a root and an underlying")
        if type(self.is_put) is not bool:
            raise TypeError(f"is_put must be a bool, got {self.is_put!r}")
        if self.bar_session is not None:
            _require_session_date(self.bar_session, "bar_session")
        _require_volume(self.volume, "volume")


# --- Shared helpers ----------------------------------------------------------


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(RAW_QUANTUM, rounding=ROUND_HALF_EVEN)


def _require_session(session: date) -> None:
    _require_session_date(session, "session")
    if nyse_sessions_between(session, session) != (session,):
        raise ValueError(f"{session} is not an NYSE session")


def _require_adjustment(series: BarSeries, key: ComponentKey) -> None:
    wanted = COMPONENT_SPECS[key].adjustment
    if series.adjustment is not wanted:
        raise ValueError(
            f"{key.value} needs adjustment={wanted} bars, "
            f"got {series.symbol} with adjustment={series.adjustment.value}"
        )


def _require_universe(universe: Sequence[BarSeries], key: ComponentKey) -> None:
    seen: set[str] = set()
    for series in universe:
        # A lookalike skipped BarSeries' checks, so its bars could carry a
        # float volume into breadth's sums; refuse it before reading anything.
        if type(series) is not BarSeries:
            raise TypeError(f"{key.value}: universe must be BarSeries, got {series!r}")
        _require_adjustment(series, key)
        if series.symbol in seen:
            raise ValueError(f"universe symbols must be unique, {series.symbol} repeats")
        seen.add(series.symbol)


def _bars_through(series: BarSeries, session: date) -> tuple[DailyBar, ...]:
    """The series' bars dated on or before ``session``. Later bars are not read."""
    end = len(series.bars)
    while end and series.bars[end - 1].session > session:
        end -= 1
    return series.bars[:end]


def _sessions_ending(session: date, count: int) -> tuple[date, ...]:
    """The ``count`` NYSE sessions ending ``session``, from the calendar.

    Looks back ``2 * count + 14`` calendar days, which holds far more than
    ``count`` sessions at the exchange's ~250 a year.
    """
    sessions = nyse_sessions_between(session - timedelta(days=2 * count + 14), session)
    return sessions[-count:]


# --- momentum ----------------------------------------------------------------


def momentum(spy: BarSeries, session: date) -> RawValue | NoValue:
    """close(*t*) / mean of the 125 closes ending *t*, minus 1 (C2, ``split`` bars).

    The 125 closes are the last 125 bars on or before *t*: sessions with a
    bar, so a gap is skipped rather than counted.
    """
    _require_session(session)
    _require_adjustment(spy, ComponentKey.MOMENTUM)
    bars = _bars_through(spy, session)
    if not bars or bars[-1].session != session:
        return NoValue(
            NoValueRule.NO_BAR_ON_SESSION, f"{spy.symbol} has no bar dated {session}"
        )
    if len(bars) < MOMENTUM_CLOSES:
        return NoValue(
            NoValueRule.INSUFFICIENT_HISTORY,
            f"{spy.symbol} has {len(bars)} closes through {session}, "
            f"{MOMENTUM_CLOSES} needed",
        )
    window = bars[-MOMENTUM_CLOSES:]
    with localcontext(prec=PRECISION, rounding=ROUND_HALF_EVEN):
        mean = sum((b.close for b in window), Decimal(0)) / Decimal(MOMENTUM_CLOSES)
        return RawValue(_quantize(window[-1].close / mean - 1), None)


# --- strength ----------------------------------------------------------------


def _extremes_over_windows(
    bars: Sequence[DailyBar], windows: Sequence[tuple[date, date]]
) -> dict[date, tuple[Decimal, Decimal]]:
    """The max high and min low over each ``(start, end)`` window, both inclusive.

    The windows' starts and ends must both be non-decreasing, which the
    trailing-252 windows of ascending sessions are. Two monotonic deques of
    bar indices make the whole pass linear in bars plus windows, rather than
    252 comparisons per name per session -- the history load is ~500 names
    over ~250 sessions. Windows holding no bar are absent from the result.
    """
    highs: deque[int] = deque()
    lows: deque[int] = deque()
    result: dict[date, tuple[Decimal, Decimal]] = {}
    next_bar = 0
    for start, end in windows:
        while next_bar < len(bars) and bars[next_bar].session <= end:
            bar = bars[next_bar]
            while highs and bars[highs[-1]].high <= bar.high:
                highs.pop()
            highs.append(next_bar)
            while lows and bars[lows[-1]].low >= bar.low:
                lows.pop()
            lows.append(next_bar)
            next_bar += 1
        while highs and bars[highs[0]].session < start:
            highs.popleft()
        while lows and bars[lows[0]].session < start:
            lows.popleft()
        if highs:
            result[end] = (bars[highs[0]].high, bars[lows[0]].low)
    return result


def strength_over(
    universe: Sequence[BarSeries], sessions: Sequence[date]
) -> dict[date, RawValue | NoValue]:
    """Strength for each of ``sessions`` (strictly ascending), in one pass.

    For session *t* (C3), with the window the 252 NYSE sessions ending *t*:

    * **eligible:** a bar on *t*, and a first bar on or before the window's
      first session. Interior gaps are allowed.
    * **a 52-week high:** ``high(t) >=`` the max high over the window's bars,
      so a tie counts. **A low** is the mirror: ``low(t) <=`` the min low.
    * **raw value:** (highs - lows) / eligible, with ``members`` = eligible.

    **The caller (U5) must fetch with a margin before the earliest window's
    start.** "First bar" is the earliest bar in the series *as given*: this
    function has no listing date, so it reads that bar as one. A request
    starting exactly on the window's first session silently drops every
    long-listed name that happened to have no bar that day (a halt, a vendor
    gap), because its first bar then falls inside the window and it reads as
    recently listed. Fetching from some sessions before the earliest window
    start makes the first bar given precede the window for every name that
    really was listed by then. The rule itself is C3's and is not loosened
    here: a name whose history is cut short is left out, never counted on a
    short year.
    """
    for session in sessions:
        _require_session(session)
    _require_ascending(sessions, "strength sessions")
    _require_universe(universe, ComponentKey.STRENGTH)
    if not sessions:
        return {}
    calendar = nyse_sessions_between(
        sessions[0] - timedelta(days=2 * STRENGTH_SESSIONS + 14), sessions[-1]
    )
    index = {day: i for i, day in enumerate(calendar)}
    windows: list[tuple[date, date]] = []
    for session in sessions:
        position = index[session]
        if position + 1 < STRENGTH_SESSIONS:
            raise ValueError(f"the calendar lookback holds too few sessions for {session}")
        windows.append((calendar[position + 1 - STRENGTH_SESSIONS], session))

    highs = dict.fromkeys(sessions, 0)
    lows = dict.fromkeys(sessions, 0)
    eligible = dict.fromkeys(sessions, 0)
    for series in universe:
        if not series.bars:
            continue
        first = series.bars[0].session
        on_session = {b.session: b for b in series.bars}
        extremes = _extremes_over_windows(series.bars, windows)
        for start, session in windows:
            bar = on_session.get(session)
            if bar is None or first > start:
                continue
            max_high, min_low = extremes[session]
            eligible[session] += 1
            if bar.high >= max_high:
                highs[session] += 1
            if bar.low <= min_low:
                lows[session] += 1

    result: dict[date, RawValue | NoValue] = {}
    for start, session in windows:
        count = eligible[session]
        if count == 0:
            result[session] = NoValue(
                NoValueRule.NO_ELIGIBLE_NAMES,
                f"none of {len(universe)} names has a bar on {session} and a first "
                f"bar on or before {start}",
            )
            continue
        with localcontext(prec=PRECISION, rounding=ROUND_HALF_EVEN):
            value = Decimal(highs[session] - lows[session]) / Decimal(count)
            result[session] = RawValue(_quantize(value), count)
    return result


def strength(universe: Sequence[BarSeries], session: date) -> RawValue | NoValue:
    """Strength for one session. See :func:`strength_over` for the definition.

    The same caller obligation applies: pass each name's bars from some
    sessions *before* the 252-session window's first session, not from that
    session exactly, or a long-listed name with no bar that day is dropped as
    if newly listed.
    """
    return strength_over(universe, [session])[session]


# --- breadth -----------------------------------------------------------------


def breadth(universe: Sequence[BarSeries], session: date) -> RawValue | NoValue:
    """(advancing volume - declining volume) / total volume over the universe (C3).

    A name counts when it has a bar on *t* and on the NYSE session before
    *t* -- the calendar's previous session, not the name's previous bar.
    Advancing is close above the previous close, declining below; unchanged
    adds its volume to the total only. ``members`` is the names counted --
    the names behind the volume totals, not the denominator, which is a
    volume (owner decision 3a, 2026-11-24).

    Split-adjusted volume is rescaled by a later split, so a value recomputed
    inside the recheck window after a split can differ from the stored one;
    accepted and documented, C1's 2026-11-24 amendment.
    """
    _require_session(session)
    _require_universe(universe, ComponentKey.BREADTH)
    previous = _sessions_ending(session, 2)[0]
    advancing = declining = total = members = 0
    for series in universe:
        on_session = {b.session: b for b in series.bars}
        today = on_session.get(session)
        before = on_session.get(previous)
        if today is None or before is None:
            continue
        members += 1
        total += today.volume
        if today.close > before.close:
            advancing += today.volume
        elif today.close < before.close:
            declining += today.volume
    if members == 0:
        return NoValue(
            NoValueRule.NO_ELIGIBLE_NAMES,
            f"none of {len(universe)} names has bars on both {previous} and {session}",
        )
    if total == 0:
        return NoValue(
            NoValueRule.ZERO_TOTAL_VOLUME,
            f"{members} names with bars on {previous} and {session} traded no volume",
        )
    with localcontext(prec=PRECISION, rounding=ROUND_HALF_EVEN):
        value = Decimal(advancing - declining) / Decimal(total)
        return RawValue(_quantize(value), members)


# --- put_call ----------------------------------------------------------------


def put_call(
    contracts: Sequence[OptionContractVolume], session: date
) -> RawValue | NoValue:
    """Sum of put ``dailyBar.v`` / sum of call ``dailyBar.v`` (C4).

    Counts only contracts whose ``dailyBar`` is dated *t*, and only standard
    roots: a contract whose root differs from the chain's underlying is
    adjusted and dropped. ``members`` is the contracts counted, puts and
    calls together, zero-volume ones included.

    Raises on an element that is not an :class:`OptionContractVolume` (it
    skipped the float and bool checks), on a mixed chain, and on an OCC
    symbol listed twice, compared case-insensitively: overlapping snapshot
    pages merged by the caller would otherwise count a contract's volume
    twice. A repeat is refused wherever it sits, including among contracts
    the ratio would exclude.
    """
    _require_session(session)
    seen: set[str] = set()
    for contract in contracts:
        if type(contract) is not OptionContractVolume:
            raise TypeError(
                f"put_call: contracts must be OptionContractVolume, got {contract!r}"
            )
        symbol = contract.symbol.upper()
        if symbol in seen:
            raise ValueError(f"contract symbols must be unique, {contract.symbol} repeats")
        seen.add(symbol)
    underlyings = {c.underlying.upper() for c in contracts}
    if len(underlyings) > 1:
        raise ValueError(f"put_call reads one underlying's chain, got {sorted(underlyings)}")
    puts = calls = members = 0
    for contract in contracts:
        if contract.bar_session != session:
            continue
        if contract.root.upper() != contract.underlying.upper():
            continue
        members += 1
        if contract.is_put:
            puts += contract.volume
        else:
            calls += contract.volume
    if members == 0:
        return NoValue(
            NoValueRule.NO_CONTRACTS_ON_SESSION,
            f"none of {len(contracts)} contracts is a standard root with a dailyBar "
            f"dated {session}",
        )
    if calls == 0:
        return NoValue(
            NoValueRule.ZERO_CALL_VOLUME,
            f"{members} contracts dated {session} carry put volume {puts} "
            "and call volume 0",
        )
    with localcontext(prec=PRECISION, rounding=ROUND_HALF_EVEN):
        return RawValue(_quantize(Decimal(puts) / Decimal(calls)), members)


# --- the two FRED series -----------------------------------------------------


def _observations_through(
    observations: Sequence[Observation], session: date, series: str
) -> Observation | NoValue:
    """The observation dated *t*, or the stated reason there is none to use."""
    _require_session(session)
    for item in observations:
        if type(item) is not Observation:
            raise TypeError(f"{series}: observations must be Observation, got {item!r}")
    _require_ascending([o.observed for o in observations], f"{series} observations")
    on_session = next((o for o in observations if o.observed == session), None)
    if on_session is None:
        return NoValue(
            NoValueRule.NO_OBSERVATION_ON_SESSION,
            f"{series} has no observation dated {session}",
        )
    if on_session.value is None:
        return NoValue(
            NoValueRule.MISSING_OBSERVATION, f'{series} published "." for {session}'
        )
    return on_session


def volatility(vix: Sequence[Observation], session: date) -> RawValue | NoValue:
    """VIX(*t*) / mean of the 50 non-missing VIX observations ending *t*, minus 1.

    ``"."`` rows are skipped, not counted: the window is 50 *values* (C2).
    Observations dated after *t* are not read.
    """
    found = _observations_through(vix, session, "VIXCLS")
    if isinstance(found, NoValue):
        return found
    values = [o.value for o in vix if o.observed <= session and o.value is not None]
    if len(values) < VOLATILITY_OBSERVATIONS:
        return NoValue(
            NoValueRule.INSUFFICIENT_HISTORY,
            f"VIXCLS has {len(values)} observations through {session}, "
            f"{VOLATILITY_OBSERVATIONS} needed",
        )
    window = values[-VOLATILITY_OBSERVATIONS:]
    with localcontext(prec=PRECISION, rounding=ROUND_HALF_EVEN):
        mean = sum(window, Decimal(0)) / Decimal(VOLATILITY_OBSERVATIONS)
        if mean <= 0:
            return NoValue(
                NoValueRule.NON_POSITIVE_MEAN,
                f"VIXCLS's {VOLATILITY_OBSERVATIONS}-observation mean through "
                f"{session} is {mean}",
            )
        return RawValue(_quantize(window[-1] / mean - 1), None)


def junk_bond(oas: Sequence[Observation], session: date) -> RawValue | NoValue:
    """The high-yield OAS dated *t*, in percent, as published (C2).

    A bond-market holiday the NYSE kept has no observation for *t*, and that
    is a :class:`NoValue`, never the previous day's print: the as-of rule
    (C8) is where a lag is handled, visibly.
    """
    found = _observations_through(oas, session, "BAMLH0A0HYM2")
    if isinstance(found, NoValue):
        return found
    assert found.value is not None  # _observations_through returned the "." case
    with localcontext(prec=PRECISION, rounding=ROUND_HALF_EVEN):
        return RawValue(_quantize(found.value), None)


# --- safe_haven --------------------------------------------------------------


def _session_return(series: BarSeries, session: date) -> Decimal | NoValue:
    """close(*t*) / close 20 bars earlier, minus 1, under its own 40-digit context."""
    bars = _bars_through(series, session)
    if not bars or bars[-1].session != session:
        return NoValue(
            NoValueRule.NO_BAR_ON_SESSION, f"{series.symbol} has no bar dated {session}"
        )
    needed = SAFE_HAVEN_SESSIONS + 1
    if len(bars) < needed:
        return NoValue(
            NoValueRule.INSUFFICIENT_HISTORY,
            f"{series.symbol} has {len(bars)} bars through {session}, {needed} needed "
            f"for a {SAFE_HAVEN_SESSIONS}-session return",
        )
    with localcontext(prec=PRECISION, rounding=ROUND_HALF_EVEN):
        return bars[-1].close / bars[-needed].close - 1


def safe_haven(stock: BarSeries, bond: BarSeries, session: date) -> RawValue | NoValue:
    """20-session stock return minus 20-session bond return, ending *t* (C2).

    Both series must be total-return (``adjustment=all``) bars: TLT's coupon
    is most of its return over 20 sessions. Each return is close(*t*) over
    the close 20 bars earlier in its own series.
    """
    _require_session(session)
    _require_adjustment(stock, ComponentKey.SAFE_HAVEN)
    _require_adjustment(bond, ComponentKey.SAFE_HAVEN)
    with localcontext(prec=PRECISION, rounding=ROUND_HALF_EVEN):
        stock_return = _session_return(stock, session)
        if isinstance(stock_return, NoValue):
            return stock_return
        bond_return = _session_return(bond, session)
        if isinstance(bond_return, NoValue):
            return bond_return
        return RawValue(_quantize(stock_return - bond_return), None)
