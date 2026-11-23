"""The seven raw components of step 8's composite (C1, C2, C3, C4), pure.

Every expected value below is computed by hand in the comment beside it, not
by running the module -- a fixture checked against its own implementation
proves only that the code agrees with itself.
"""

import ast
import random
import sys
import types
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from corollary.calendars import nyse_sessions_between
from corollary.data.macro import components
from corollary.data.macro.components import (
    COMPONENT_SPECS,
    STRENGTH_SESSIONS,
    Adjustment,
    BarSeries,
    ComponentKey,
    DailyBar,
    NoValue,
    NoValueRule,
    Observation,
    OptionContractVolume,
    Orientation,
    RawValue,
    breadth,
    junk_bond,
    momentum,
    put_call,
    safe_haven,
    strength,
    strength_over,
    volatility,
)

#: A Friday session, and the as-of date every fixture ends on unless it says.
T = date(2026, 11, 20)


def _sessions_ending(end: date, count: int) -> tuple[date, ...]:
    sessions = nyse_sessions_between(end - timedelta(days=2 * count + 14), end)
    assert len(sessions) >= count
    return sessions[-count:]


def _bar(
    session: date,
    close: str | Decimal,
    *,
    high: str | Decimal | None = None,
    low: str | Decimal | None = None,
    volume: int = 1000,
) -> DailyBar:
    c = Decimal(close)
    return DailyBar(
        session=session,
        high=c if high is None else Decimal(high),
        low=c if low is None else Decimal(low),
        close=c,
        volume=volume,
    )


def _series(
    symbol: str, bars: Sequence[DailyBar], adjustment: Adjustment = Adjustment.SPLIT
) -> BarSeries:
    return BarSeries(symbol=symbol, adjustment=adjustment, bars=tuple(bars))


def _value(result: RawValue | NoValue) -> RawValue:
    assert isinstance(result, RawValue), result
    return result


def _no_value(result: RawValue | NoValue) -> NoValue:
    assert isinstance(result, NoValue), result
    return result


# --- The table as data -------------------------------------------------------


def test_the_seven_keys_are_c2_s_and_fit_the_column() -> None:
    assert [k.value for k in ComponentKey] == [
        "momentum",
        "strength",
        "breadth",
        "put_call",
        "volatility",
        "safe_haven",
        "junk_bond",
    ]
    assert all(len(k.value) <= 16 for k in ComponentKey)  # String(16)
    assert set(COMPONENT_SPECS) == set(ComponentKey)


def test_orientation_is_c2_s_table() -> None:
    """Higher z means greed after orientation: three components point down."""
    expected = {
        ComponentKey.MOMENTUM: Orientation.POSITIVE,
        ComponentKey.STRENGTH: Orientation.POSITIVE,
        ComponentKey.BREADTH: Orientation.POSITIVE,
        ComponentKey.PUT_CALL: Orientation.NEGATIVE,
        ComponentKey.VOLATILITY: Orientation.NEGATIVE,
        ComponentKey.SAFE_HAVEN: Orientation.POSITIVE,
        ComponentKey.JUNK_BOND: Orientation.NEGATIVE,
    }
    assert {k: s.orientation for k, s in COMPONENT_SPECS.items()} == expected
    assert int(Orientation.POSITIVE) == 1 and int(Orientation.NEGATIVE) == -1


def test_momentum_s_caller_asks_for_split_and_safe_haven_s_for_all() -> None:
    """C2: total-return bars for safe_haven, price bars for the rest.

    The table is what the bars job (U5) reads to choose ``adjustment=``, and
    the functions refuse a series of the wrong kind, so the two cannot drift.
    """
    assert COMPONENT_SPECS[ComponentKey.MOMENTUM].adjustment is Adjustment.SPLIT
    assert COMPONENT_SPECS[ComponentKey.STRENGTH].adjustment is Adjustment.SPLIT
    assert COMPONENT_SPECS[ComponentKey.BREADTH].adjustment is Adjustment.SPLIT
    assert COMPONENT_SPECS[ComponentKey.SAFE_HAVEN].adjustment is Adjustment.ALL
    for key in (ComponentKey.PUT_CALL, ComponentKey.VOLATILITY, ComponentKey.JUNK_BOND):
        assert COMPONENT_SPECS[key].adjustment is None
    assert Adjustment.SPLIT.value == "split" and Adjustment.ALL.value == "all"


def test_each_function_refuses_bars_of_the_wrong_adjustment() -> None:
    """The message names both what was wanted and what arrived, so each check
    matches the whole phrase: matching ``split`` or ``all`` alone would pass
    either way round, since every such message carries both words."""
    sessions = _sessions_ending(T, 130)
    split = _series("SPY", [_bar(s, "100") for s in sessions], Adjustment.SPLIT)
    total = _series("SPY", [_bar(s, "100") for s in sessions], Adjustment.ALL)
    wants_split = "needs adjustment=split bars, got SPY with adjustment=all"
    wants_all = "needs adjustment=all bars, got SPY with adjustment=split"
    with pytest.raises(ValueError, match=f"^momentum {wants_split}$"):
        momentum(total, T)
    with pytest.raises(ValueError, match=f"^safe_haven {wants_all}$"):
        safe_haven(split, total, T)
    with pytest.raises(ValueError, match=f"^safe_haven {wants_all}$"):
        safe_haven(total, split, T)
    with pytest.raises(ValueError, match=f"^strength {wants_split}$"):
        strength([total], T)
    with pytest.raises(ValueError, match=f"^breadth {wants_split}$"):
        breadth([total], T)


# --- momentum ----------------------------------------------------------------


def test_momentum_on_125_closes() -> None:
    """124 closes at 100 and 125 on *t*.

    mean = (124 x 100 + 125) / 125 = 12525 / 125 = 100.2
    raw  = 125 / 100.2 - 1 = 625/501 - 1 = 0.24750499001996...
    """
    sessions = _sessions_ending(T, 125)
    bars = [_bar(s, "100") for s in sessions[:-1]] + [_bar(T, "125")]
    result = _value(momentum(_series("SPY", bars), T))
    assert result.value == Decimal("0.2475049900")
    assert result.members is None


def test_momentum_with_124_closes_has_no_value() -> None:
    sessions = _sessions_ending(T, 124)
    bars = [_bar(s, "100") for s in sessions]
    result = _no_value(momentum(_series("SPY", bars), T))
    assert result.rule is NoValueRule.INSUFFICIENT_HISTORY
    assert "124" in result.detail and "125" in result.detail


def test_momentum_uses_only_the_last_125_closes_and_nothing_after_t() -> None:
    """An older close at 1 and a later close at 1000 change nothing."""
    sessions = _sessions_ending(T, 126)
    later = nyse_sessions_between(T + timedelta(days=1), T + timedelta(days=7))[0]
    bars = (
        [_bar(sessions[0], "1")]
        + [_bar(s, "100") for s in sessions[1:-1]]
        + [_bar(T, "125"), _bar(later, "1000")]
    )
    assert _value(momentum(_series("SPY", bars), T)).value == Decimal("0.2475049900")


def test_momentum_without_a_bar_on_t_has_no_value() -> None:
    sessions = _sessions_ending(T, 200)
    bars = [_bar(s, "100") for s in sessions[:-1]]
    result = _no_value(momentum(_series("SPY", bars), T))
    assert result.rule is NoValueRule.NO_BAR_ON_SESSION
    assert "SPY" in result.detail


def test_momentum_counts_sessions_with_a_bar_not_calendar_sessions() -> None:
    """C2: "125 closes means 125 NYSE sessions with a bar". A gap is skipped.

    126 sessions, one interior one missing: still 125 closes, still a value.
    """
    sessions = _sessions_ending(T, 126)
    kept = sessions[:60] + sessions[61:]
    bars = [_bar(s, "100") for s in kept[:-1]] + [_bar(T, "125")]
    assert _value(momentum(_series("SPY", bars), T)).value == Decimal("0.2475049900")


# --- strength ----------------------------------------------------------------


def _flat(
    sessions: Sequence[date], high: str, low: str, close: str | None = None
) -> list[DailyBar]:
    mid = close if close is not None else low
    return [_bar(s, mid, high=high, low=low) for s in sessions]


def test_strength_a_tie_with_the_window_maximum_counts_as_a_high() -> None:
    """High on *t* equal to the year's high is a 52-week high. 1 of 1."""
    window = _sessions_ending(T, STRENGTH_SESSIONS)
    bars = _flat(window[:-1], "10", "9") + [_bar(T, "9.8", high="10", low="9.5")]
    result = _value(strength([_series("A", bars)], T))
    assert result.value == Decimal("1.0000000000")
    assert result.members == 1


def test_strength_a_tie_with_the_window_minimum_counts_as_a_low() -> None:
    window = _sessions_ending(T, STRENGTH_SESSIONS)
    bars = _flat(window[:-1], "10", "9") + [_bar(T, "9.2", high="9.5", low="9")]
    assert _value(strength([_series("A", bars)], T)).value == Decimal("-1.0000000000")


def test_strength_a_name_whose_first_bar_is_inside_the_window_is_ineligible() -> None:
    """D lists 100 sessions into the window and makes a high on *t*.

    Its "52-week high" covers less than a year, so it is not counted at all:
    not as a high, and not in the denominator. A is eligible and neither.
    """
    window = _sessions_ending(T, STRENGTH_SESSIONS)
    a = _flat(window[:-1], "20", "10") + [_bar(T, "15", high="16", low="14")]
    d = _flat(window[100:-1], "10", "9") + [_bar(T, "11", high="11", low="10")]
    result = _value(strength([_series("A", a), _series("D", d)], T))
    assert result.value == Decimal("0E-10")
    assert result.members == 1


def test_strength_a_first_bar_exactly_on_the_window_start_is_eligible() -> None:
    window = _sessions_ending(T, STRENGTH_SESSIONS)
    bars = _flat(window[:-1], "10", "9") + [_bar(T, "11", high="11", low="10")]
    assert bars[0].session == window[0]
    result = _value(strength([_series("A", bars)], T))
    assert result.members == 1
    assert result.value == Decimal("1.0000000000")


def test_strength_interior_gaps_are_allowed() -> None:
    """E has a bar at the window start, a 150-session hole, then bars to *t*.

    Still eligible; its high is taken over the bars that exist.
    """
    window = _sessions_ending(T, STRENGTH_SESSIONS)
    bars = (
        _flat(window[:1], "10", "9")
        + _flat(window[151:-1], "10", "9")
        + [_bar(T, "10.5", high="10.5", low="10")]
    )
    result = _value(strength([_series("E", bars)], T))
    assert result.members == 1
    assert result.value == Decimal("1.0000000000")


def test_strength_a_high_before_the_window_does_not_count() -> None:
    """A 999 high one session before the window opens is outside the year."""
    window = _sessions_ending(T, STRENGTH_SESSIONS + 1)
    bars = (
        [_bar(window[0], "50", high="999", low="9")]
        + _flat(window[1:-1], "10", "9")
        + [_bar(T, "10.5", high="10.5", low="10")]
    )
    assert _value(strength([_series("A", bars)], T)).value == Decimal("1.0000000000")


def test_strength_the_fraction_uses_the_eligible_count() -> None:
    """Six names, four eligible: (2 highs - 1 low) / 4 = 0.25.

    A: tie with the max -> high.   B: below the min -> low.
    C: an interior spike, neither. D: first bar in window -> ineligible.
    E: gaps, new high -> high.     F: no bar on t -> ineligible.
    """
    window = _sessions_ending(T, STRENGTH_SESSIONS)
    a = _flat(window[:-1], "10", "9") + [_bar(T, "9.8", high="10", low="9.5")]
    b = _flat(window[:-1], "20", "18") + [_bar(T, "17.5", high="19", low="17")]
    c = (
        _flat(window[:100], "30", "28")
        + [_bar(window[100], "30", high="35", low="25")]
        + _flat(window[101:-1], "30", "28")
        + [_bar(T, "30", high="31", low="29")]
    )
    d = _flat(window[100:-1], "10", "9") + [_bar(T, "11", high="11", low="10")]
    e = (
        _flat(window[:1], "10", "9")
        + _flat(window[151:-1], "10", "9")
        + [_bar(T, "10.5", high="10.5", low="10")]
    )
    f = _flat(window[:-1], "10", "9")
    universe = [
        _series(n, bars)
        for n, bars in (("A", a), ("B", b), ("C", c), ("D", d), ("E", e), ("F", f))
    ]
    result = _value(strength(universe, T))
    assert result.value == Decimal("0.2500000000")
    assert result.members == 4


def test_strength_with_no_eligible_name_has_no_value() -> None:
    window = _sessions_ending(T, STRENGTH_SESSIONS)
    young = _flat(window[10:], "10", "9")
    result = _no_value(strength([_series("A", young)], T))
    assert result.rule is NoValueRule.NO_ELIGIBLE_NAMES
    assert _no_value(strength([], T)).rule is NoValueRule.NO_ELIGIBLE_NAMES


def _naive_strength(universe: Sequence[BarSeries], session: date) -> RawValue | NoValue:
    """The definition, read straight off C3 with no sliding window."""
    window = _sessions_ending(session, STRENGTH_SESSIONS)
    highs = lows = eligible = 0
    for series in universe:
        by_day = {b.session: b for b in series.bars}
        if session not in by_day or series.bars[0].session > window[0]:
            continue
        eligible += 1
        inside = [b for b in series.bars if window[0] <= b.session <= session]
        if by_day[session].high >= max(b.high for b in inside):
            highs += 1
        if by_day[session].low <= min(b.low for b in inside):
            lows += 1
    if eligible == 0:
        return NoValue(NoValueRule.NO_ELIGIBLE_NAMES, "")
    return RawValue(
        (Decimal(highs - lows) / Decimal(eligible)).quantize(Decimal("1E-10")),
        eligible,
    )


def test_strength_s_sliding_window_matches_the_definition() -> None:
    """Random walks with gaps and late listings, over 120 consecutive *t*."""
    rng = random.Random(8)
    sessions = _sessions_ending(T, STRENGTH_SESSIONS + 140)
    universe = []
    for n in range(25):
        price = rng.randint(2000, 8000)
        first = rng.choice([0, 0, 0, 30, 200, 300])
        bars = []
        for s in sessions[first:]:
            if rng.random() < 0.05:
                continue  # an interior gap
            price = max(100, price + rng.randint(-150, 150))
            spread = rng.randint(0, 80)
            bars.append(
                _bar(
                    s,
                    Decimal(price) / 100,
                    high=Decimal(price + spread) / 100,
                    low=Decimal(price - spread) / 100,
                )
            )
        universe.append(_series(f"N{n}", bars))
    targets = sessions[-120:]
    over = strength_over(universe, targets)
    assert list(over) == list(targets)
    for t in targets:
        expected = _naive_strength(universe, t)
        got = over[t]
        if isinstance(expected, NoValue):
            assert isinstance(got, NoValue) and got.rule is expected.rule
        else:
            assert got == expected, t
        assert strength(universe, t) == got
    assert any(isinstance(v, RawValue) and v.value != 0 for v in over.values())


def test_strength_over_refuses_unordered_sessions() -> None:
    sessions = _sessions_ending(T, 3)
    with pytest.raises(ValueError, match="ascending"):
        strength_over([], [sessions[1], sessions[0]])
    with pytest.raises(ValueError, match="ascending"):
        strength_over([], [sessions[0], sessions[0]])


# --- breadth -----------------------------------------------------------------


def test_breadth_unchanged_closes_count_only_toward_the_total() -> None:
    """A up on 1000, B down on 300, C flat on 200.

    (1000 - 300) / (1000 + 300 + 200) = 700 / 1500 = 0.46666...
    """
    p, t = _sessions_ending(T, 2)
    universe = [
        _series("A", [_bar(p, "10"), _bar(t, "11", volume=1000)]),
        _series("B", [_bar(p, "20"), _bar(t, "19", volume=300)]),
        _series("C", [_bar(p, "5"), _bar(t, "5", volume=200)]),
    ]
    result = _value(breadth(universe, T))
    assert result.value == Decimal("0.4666666667")
    assert result.members == 3


def test_breadth_a_name_missing_the_previous_session_is_excluded() -> None:
    """D has no bar on the session before *t* -- only one two sessions back.

    "The session before" is the calendar's, not D's previous bar. E has no
    bar on *t*. Both are out of numerator and denominator alike.
    """
    pp, p, t = _sessions_ending(T, 3)
    universe = [
        _series("A", [_bar(p, "10"), _bar(t, "11", volume=1000)]),
        _series("B", [_bar(p, "20"), _bar(t, "19", volume=300)]),
        _series("C", [_bar(p, "5"), _bar(t, "5", volume=200)]),
        _series("D", [_bar(pp, "1"), _bar(t, "50", volume=5000)]),
        _series("E", [_bar(pp, "1"), _bar(p, "50", volume=5000)]),
    ]
    result = _value(breadth(universe, T))
    assert result.value == Decimal("0.4666666667")
    assert result.members == 3


def test_breadth_the_session_before_a_half_day_skips_the_holiday() -> None:
    """2025-11-28 (half-day) follows 2025-11-26: Thanksgiving is no session."""
    t = date(2025, 11, 28)
    p = date(2025, 11, 26)
    universe = [
        _series("A", [_bar(p, "10"), _bar(t, "9", volume=400)]),
        _series("B", [_bar(p, "10"), _bar(t, "11", volume=100)]),
    ]
    result = _value(breadth(universe, t))
    assert result.value == Decimal("-0.6000000000")  # (100 - 400) / 500
    assert result.members == 2


def test_breadth_zero_total_volume_gives_no_value() -> None:
    p, t = _sessions_ending(T, 2)
    universe = [_series("A", [_bar(p, "10"), _bar(t, "11", volume=0)])]
    result = _no_value(breadth(universe, T))
    assert result.rule is NoValueRule.ZERO_TOTAL_VOLUME


def test_breadth_with_no_eligible_name_has_no_value() -> None:
    p, t = _sessions_ending(T, 2)
    universe = [_series("A", [_bar(t, "11")])]
    assert _no_value(breadth(universe, T)).rule is NoValueRule.NO_ELIGIBLE_NAMES


# --- put_call ----------------------------------------------------------------


def _contract(
    is_put: bool,
    volume: int,
    strike: int,
    bar_session: date | None = T,
    root: str = "SPY",
) -> OptionContractVolume:
    """A contract whose OCC symbol is built from its root, side and strike.

    Every contract in a fixture has a distinct ``(root, side, strike)``, so a
    distinct symbol: :func:`put_call` refuses a repeated one.
    """
    side = "P" if is_put else "C"
    return OptionContractVolume(
        symbol=f"{root}261218{side}{strike * 1000:08d}",
        root=root,
        underlying="SPY",
        is_put=is_put,
        bar_session=bar_session,
        volume=volume,
    )


def test_put_call_counts_only_contracts_whose_daily_bar_is_dated_t() -> None:
    """Puts 500 + 100 over calls 400 = 1.5, from three contracts.

    Excluded: a put last traded the session before (9999), a call with no
    daily bar at all, and an adjusted-root put (7777).
    """
    previous = _sessions_ending(T, 2)[0]
    contracts = [
        _contract(True, 500, 600),
        _contract(True, 100, 610),
        _contract(False, 400, 600),
        _contract(True, 9999, 620, bar_session=previous),
        _contract(False, 8888, 610, bar_session=None),
        _contract(True, 7777, 600, root="SPY1"),
    ]
    result = _value(put_call(contracts, T))
    assert result.value == Decimal("1.5000000000")
    assert result.members == 3


def test_put_call_a_different_session_s_daily_bar_is_excluded() -> None:
    previous = _sessions_ending(T, 2)[0]
    contracts = [
        _contract(True, 300, 600),
        _contract(False, 200, 600),
        _contract(False, 100_000, 610, bar_session=previous),
    ]
    assert _value(put_call(contracts, T)).value == Decimal("1.5000000000")


def test_put_call_an_adjusted_root_contract_is_excluded() -> None:
    contracts = [
        _contract(True, 300, 600),
        _contract(False, 200, 600),
        _contract(False, 100_000, 600, root="SPY1"),
        _contract(False, 100_000, 610, root="spy1"),
    ]
    result = _value(put_call(contracts, T))
    assert result.value == Decimal("1.5000000000")
    assert result.members == 2


def test_put_call_a_case_difference_alone_is_not_an_adjusted_root() -> None:
    """``instruments.is_adjusted_root`` normalises case; so does this."""
    contracts = [_contract(True, 300, 600, root="spy"), _contract(False, 200, 600)]
    assert _value(put_call(contracts, T)).members == 2


def test_put_call_zero_call_volume_gives_no_value() -> None:
    contracts = [_contract(True, 300, 600), _contract(False, 0, 600)]
    result = _no_value(put_call(contracts, T))
    assert result.rule is NoValueRule.ZERO_CALL_VOLUME


def test_put_call_with_no_contract_dated_t_has_no_value() -> None:
    previous = _sessions_ending(T, 2)[0]
    result = _no_value(put_call([_contract(True, 300, 600, bar_session=previous)], T))
    assert result.rule is NoValueRule.NO_CONTRACTS_ON_SESSION
    assert _no_value(put_call([], T)).rule is NoValueRule.NO_CONTRACTS_ON_SESSION


def test_put_call_refuses_a_mixed_chain() -> None:
    other = OptionContractVolume(
        symbol="QQQ261218P00500000",
        root="QQQ",
        underlying="QQQ",
        is_put=True,
        bar_session=T,
        volume=1,
    )
    with pytest.raises(ValueError, match="one underlying"):
        put_call([_contract(True, 1, 600), other], T)


def test_put_call_refuses_a_contract_listed_twice() -> None:
    """Overlapping snapshot pages merged by the caller would double-count a
    contract's volume. A repeat is a caller bug, so it raises, as a repeated
    universe symbol does -- it is not a missing value. The repeat is refused
    wherever it sits, including among contracts the ratio would exclude, and
    whatever its case."""
    put = _contract(True, 300, 600)
    call = _contract(False, 200, 600)
    with pytest.raises(ValueError, match=r"unique, SPY261218P00600000 repeats"):
        put_call([put, call, put], T)
    stale = _contract(True, 9, 610, bar_session=_sessions_ending(T, 2)[0])
    with pytest.raises(ValueError, match="repeats"):
        put_call([put, call, stale, stale], T)
    lower = OptionContractVolume(
        symbol=put.symbol.lower(),
        root="SPY",
        underlying="SPY",
        is_put=True,
        bar_session=T,
        volume=300,
    )
    with pytest.raises(ValueError, match="repeats"):
        put_call([put, call, lower], T)


def test_a_contract_needs_a_symbol() -> None:
    with pytest.raises(ValueError, match="symbol"):
        OptionContractVolume(
            symbol="", root="SPY", underlying="SPY", is_put=True, bar_session=T, volume=1
        )


@dataclass(frozen=True)
class _ContractRow:
    """A lookalike of :class:`OptionContractVolume` that skipped its checks:
    a raw row whose volume is a float, as a row from a JSON payload would be."""

    symbol: str
    root: str
    underlying: str
    is_put: bool
    bar_session: date | None
    volume: float


def test_put_call_refuses_an_element_that_is_not_an_option_contract_volume() -> None:
    """Without this a float volume reaches ``puts +=`` and ``Decimal(puts)``
    accepts it, which is the float the module promises never to take."""
    row = _ContractRow("SPY261218P00600000", "SPY", "SPY", True, T, 1234.0)
    with pytest.raises(TypeError, match="OptionContractVolume"):
        put_call([_contract(False, 200, 600), row], T)  # type: ignore[list-item]


# --- volatility --------------------------------------------------------------


def test_volatility_skips_fred_gaps_when_counting_50_observations() -> None:
    """49 prints at 20 and 25 on *t*, five FRED "." rows among them, 1000s before.

    mean = (49 x 20 + 25) / 50 = 1005 / 50 = 20.1
    raw  = 25 / 20.1 - 1 = 4.9 / 20.1 = 0.243781094527...

    Counting the dots as observations would stop at 45 values (mean
    (44 x 20 + 25) / 45); reaching past 50 prints would pull in a 1000.
    """
    twenty, gap = Decimal("20"), None
    values: list[Decimal | None] = (
        [Decimal("1000")] * 20
        + [twenty] * 10 + [gap]
        + [twenty] * 10 + [gap]
        + [twenty] * 10 + [gap]
        + [twenty] * 10 + [gap]
        + [twenty] * 9 + [gap]
        + [Decimal("25")]
    )
    assert values.count(twenty) == 49 and values.count(gap) == 5
    n = len(values)
    observations = [
        Observation(T - timedelta(days=n - 1 - i), v) for i, v in enumerate(values)
    ]
    result = _value(volatility(observations, T))
    assert result.value == Decimal("0.2437810945")
    assert result.members is None


def test_volatility_with_49_observations_has_no_value() -> None:
    observations = [Observation(T - timedelta(days=i), Decimal("20")) for i in range(48, -1, -1)]
    result = _no_value(volatility(observations, T))
    assert result.rule is NoValueRule.INSUFFICIENT_HISTORY


def test_volatility_a_dot_on_t_has_no_value() -> None:
    observations = [Observation(T - timedelta(days=i), Decimal("20")) for i in range(99, 0, -1)]
    observations.append(Observation(T, None))
    result = _no_value(volatility(observations, T))
    assert result.rule is NoValueRule.MISSING_OBSERVATION


def test_volatility_with_no_observation_dated_t_has_no_value() -> None:
    observations = [Observation(T - timedelta(days=i), Decimal("20")) for i in range(99, 0, -1)]
    result = _no_value(volatility(observations, T))
    assert result.rule is NoValueRule.NO_OBSERVATION_ON_SESSION


def test_volatility_ignores_observations_after_t() -> None:
    observations = [Observation(T - timedelta(days=i), Decimal("20")) for i in range(49, 0, -1)]
    observations += [Observation(T, Decimal("25")), Observation(T + timedelta(days=1), Decimal("80"))]
    # 49 + 1 = 50 values ending t: mean 20.1, as above.
    assert _value(volatility(observations, T)).value == Decimal("0.2437810945")


# --- junk_bond ---------------------------------------------------------------


def test_junk_bond_is_the_oas_as_published() -> None:
    observations = [
        Observation(T - timedelta(days=1), Decimal("3.05")),
        Observation(T, Decimal("3.12")),
    ]
    result = _value(junk_bond(observations, T))
    assert result.value == Decimal("3.1200000000")
    assert result.members is None


def test_junk_bond_a_dot_or_an_absence_on_t_has_no_value() -> None:
    """A bond-market holiday the NYSE kept: no row for *t*, not yesterday's."""
    dot = [Observation(T - timedelta(days=1), Decimal("3.05")), Observation(T, None)]
    assert _no_value(junk_bond(dot, T)).rule is NoValueRule.MISSING_OBSERVATION
    absent = [Observation(T - timedelta(days=1), Decimal("3.05"))]
    assert _no_value(junk_bond(absent, T)).rule is NoValueRule.NO_OBSERVATION_ON_SESSION


# --- safe_haven --------------------------------------------------------------


def test_safe_haven_is_computed_on_its_own_total_return_bars() -> None:
    """SPY 100 -> 110 (+10%), TLT 80 -> 84 (+5%), over 20 sessions: 0.05.

    An extra, older bar on each (at 1) proves the base is 20 bars back.
    """
    sessions = _sessions_ending(T, 22)
    spy = (
        [_bar(sessions[0], "1"), _bar(sessions[1], "100")]
        + [_bar(s, "105") for s in sessions[2:-1]]
        + [_bar(T, "110")]
    )
    tlt = (
        [_bar(sessions[0], "1"), _bar(sessions[1], "80")]
        + [_bar(s, "90") for s in sessions[2:-1]]
        + [_bar(T, "84")]
    )
    result = _value(
        safe_haven(
            _series("SPY", spy, Adjustment.ALL), _series("TLT", tlt, Adjustment.ALL), T
        )
    )
    assert result.value == Decimal("0.0500000000")
    assert result.members is None


def test_safe_haven_with_20_bars_has_no_value() -> None:
    sessions = _sessions_ending(T, 20)
    spy = _series("SPY", [_bar(s, "100") for s in sessions], Adjustment.ALL)
    tlt = _series("TLT", [_bar(s, "80") for s in _sessions_ending(T, 30)], Adjustment.ALL)
    result = _no_value(safe_haven(spy, tlt, T))
    assert result.rule is NoValueRule.INSUFFICIENT_HISTORY
    assert "SPY" in result.detail


def test_safe_haven_without_a_tlt_bar_on_t_has_no_value() -> None:
    sessions = _sessions_ending(T, 30)
    spy = _series("SPY", [_bar(s, "100") for s in sessions], Adjustment.ALL)
    tlt = _series("TLT", [_bar(s, "80") for s in sessions[:-1]], Adjustment.ALL)
    result = _no_value(safe_haven(spy, tlt, T))
    assert result.rule is NoValueRule.NO_BAR_ON_SESSION
    assert "TLT" in result.detail


# --- C1: ratios survive a re-adjustment ---------------------------------------


def _rescaled(series: BarSeries, before: date, factor: Decimal) -> BarSeries:
    return BarSeries(
        symbol=series.symbol,
        adjustment=series.adjustment,
        bars=tuple(
            DailyBar(
                session=b.session,
                high=b.high * factor if b.session < before else b.high,
                low=b.low * factor if b.session < before else b.low,
                close=b.close * factor if b.session < before else b.close,
                volume=b.volume,
            )
            for b in series.bars
        ),
    )


def _walk(rng: random.Random, symbol: str, sessions: Sequence[date], adj: Adjustment) -> BarSeries:
    price = rng.randint(5000, 15000)
    bars = []
    for s in sessions:
        price = max(500, price + rng.randint(-300, 300))
        spread = rng.randint(0, 120)
        bars.append(
            _bar(
                s,
                Decimal(price) / 100,
                high=Decimal(price + spread) / 100,
                low=Decimal(price - spread) / 100,
                volume=rng.randint(0, 10_000_000),
            )
        )
    return _series(symbol, bars, adj)


@pytest.mark.parametrize("factor", [Decimal("0.5"), Decimal(3), Decimal("0.25")])
def test_rescaling_every_price_before_a_date_leaves_every_raw_value_unchanged(
    factor: Decimal,
) -> None:
    """C1's reason for storing ratios, pinned.

    A split or dividend re-adjustment at ``cut`` rescales every earlier price
    by one factor. Every raw value for a session before ``cut`` reads only
    prices before ``cut``, so it must come out identical. Each series gets
    its own factor too (a split touches one name), and that must not matter
    either.
    """
    rng = random.Random(1)
    sessions = _sessions_ending(T, 300)
    cut = sessions[-10]
    checked = sessions[-25:-10]
    spy_split = _walk(rng, "SPY", sessions, Adjustment.SPLIT)
    spy_all = _walk(rng, "SPY", sessions, Adjustment.ALL)
    tlt_all = _walk(rng, "TLT", sessions, Adjustment.ALL)
    universe = [_walk(rng, f"N{n}", sessions, Adjustment.SPLIT) for n in range(12)]

    def compute(
        spy_s: BarSeries, spy_a: BarSeries, tlt_a: BarSeries, names: Sequence[BarSeries]
    ) -> list[RawValue | NoValue]:
        out: list[RawValue | NoValue] = []
        for t in checked:
            out += [
                momentum(spy_s, t),
                strength(names, t),
                breadth(names, t),
                safe_haven(spy_a, tlt_a, t),
            ]
        return out

    before = compute(spy_split, spy_all, tlt_all, universe)
    assert all(isinstance(v, RawValue) for v in before)
    after = compute(
        _rescaled(spy_split, cut, factor),
        _rescaled(spy_all, cut, factor),
        _rescaled(tlt_all, cut, factor * 2),
        [_rescaled(s, cut, factor + n) for n, s in enumerate(universe)],
    )
    assert after == before


# --- No float, anywhere --------------------------------------------------------


def _mentions_float(annotation: object) -> bool:
    if annotation is float:
        return True
    return any(_mentions_float(arg) for arg in typing.get_args(annotation))


def test_no_float_appears_in_any_public_signature_or_input_field() -> None:
    public = [getattr(components, name) for name in components.__all__]
    checked = 0
    for obj in public:
        if is_dataclass(obj) and isinstance(obj, type):
            hints = typing.get_type_hints(obj)
            for f in fields(obj):
                assert not _mentions_float(hints[f.name]), (obj, f.name)
                checked += 1
        elif isinstance(obj, types.FunctionType):
            for name, hint in typing.get_type_hints(obj).items():
                assert not _mentions_float(hint), (obj, name)
                checked += 1
    assert checked > 20


@pytest.mark.parametrize(
    "build",
    [
        lambda: DailyBar(T, Decimal("1"), Decimal("1"), 1.5, 1),  # type: ignore[arg-type]
        lambda: DailyBar(T, 1.5, Decimal("1"), Decimal("1"), 1),  # type: ignore[arg-type]
        lambda: DailyBar(T, Decimal("1"), 1.5, Decimal("1"), 1),  # type: ignore[arg-type]
        lambda: DailyBar(T, Decimal("1"), Decimal("1"), Decimal("1"), 10.0),  # type: ignore[arg-type]
        lambda: DailyBar(T, Decimal("1"), Decimal("1"), Decimal("1"), True),
        lambda: DailyBar(T, Decimal("1"), Decimal("1"), 1, 1),  # type: ignore[arg-type]
        lambda: Observation(T, 20.5),  # type: ignore[arg-type]
        lambda: OptionContractVolume(
            "SPY261218P00600000", "SPY", "SPY", True, T, 1.0  # type: ignore[arg-type]
        ),
        lambda: OptionContractVolume("SPY261218P00600000", "SPY", "SPY", True, T, False),
    ],
)
def test_a_float_or_bool_input_is_refused_at_construction(
    build: Callable[[], object],
) -> None:
    with pytest.raises(TypeError):
        build()


def test_a_datetime_is_not_a_session_date() -> None:
    instant = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)
    with pytest.raises(TypeError, match="date"):
        DailyBar(instant, Decimal("1"), Decimal("1"), Decimal("1"), 1)
    with pytest.raises(TypeError, match="date"):
        Observation(instant, Decimal("1"))
    with pytest.raises(TypeError, match="date"):
        junk_bond([], instant)


def test_non_finite_and_non_positive_prices_are_refused() -> None:
    with pytest.raises(ValueError, match="finite"):
        DailyBar(T, Decimal("NaN"), Decimal("1"), Decimal("1"), 1)
    with pytest.raises(ValueError, match="finite"):
        Observation(T, Decimal("Infinity"))
    with pytest.raises(ValueError, match="positive"):
        DailyBar(T, Decimal("1"), Decimal("1"), Decimal("0"), 1)
    with pytest.raises(ValueError, match="negative"):
        DailyBar(T, Decimal("1"), Decimal("1"), Decimal("1"), -1)


def test_a_series_must_be_strictly_ascending() -> None:
    a, b = _sessions_ending(T, 2)
    with pytest.raises(ValueError, match="ascending"):
        _series("A", [_bar(b, "1"), _bar(a, "1")])
    with pytest.raises(ValueError, match="ascending"):
        _series("A", [_bar(a, "1"), _bar(a, "2")])
    with pytest.raises(ValueError, match="ascending"):
        volatility([Observation(b, Decimal(1)), Observation(a, Decimal(1))], T)
    with pytest.raises(ValueError, match="unique"):
        strength([_series("A", [_bar(b, "1")]), _series("A", [_bar(b, "1")])], T)


@dataclass(frozen=True)
class _SeriesRow:
    """A lookalike of :class:`BarSeries` that skipped its checks: its bars
    are never validated, so one could carry a float volume."""

    symbol: str
    adjustment: Adjustment
    bars: tuple[DailyBar, ...]


def test_strength_and_breadth_refuse_a_universe_element_that_is_not_a_bar_series() -> None:
    p, t = _sessions_ending(T, 2)
    real = _series("A", [_bar(p, "3"), _bar(t, "4")])
    row = _SeriesRow("B", Adjustment.SPLIT, (_bar(p, "3"), _bar(t, "4")))
    with pytest.raises(TypeError, match="BarSeries"):
        breadth([real, row], T)  # type: ignore[list-item]
    with pytest.raises(TypeError, match="BarSeries"):
        strength([real, row], T)  # type: ignore[list-item]
    with pytest.raises(TypeError, match="BarSeries"):
        strength_over([row], [T])  # type: ignore[list-item]


def test_t_must_be_an_nyse_session() -> None:
    thanksgiving = date(2026, 11, 26)
    with pytest.raises(ValueError, match="session"):
        momentum(_series("SPY", []), thanksgiving)
    with pytest.raises(ValueError, match="session"):
        junk_bond([], thanksgiving)


def test_every_value_is_a_decimal_quantized_to_the_raw_quantum() -> None:
    p, t = _sessions_ending(T, 2)
    universe = [
        _series("A", [_bar(p, "3"), _bar(t, "4", volume=1)]),
        _series("B", [_bar(p, "3"), _bar(t, "2", volume=2)]),
    ]
    result = _value(breadth(universe, T))  # (1 - 2) / 3 = -0.333...
    assert type(result.value) is Decimal
    assert result.value == Decimal("-0.3333333333")
    assert result.value.as_tuple().exponent == -10
    assert components.RAW_QUANTUM == Decimal("1E-10")


def test_no_value_carries_a_stated_rule_never_none() -> None:
    assert {r.value for r in NoValueRule} >= {
        "no_bar_on_session",
        "insufficient_history",
        "no_eligible_names",
        "zero_total_volume",
        "no_contracts_on_session",
        "zero_call_volume",
        "no_observation_on_session",
        "missing_observation",
    }
    with pytest.raises(TypeError):
        NoValue("zero_call_volume", "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="detail"):
        NoValue(NoValueRule.ZERO_CALL_VOLUME, "")


# --- Rule 9: pure ---------------------------------------------------------------


def test_the_module_imports_only_the_calendar_and_the_standard_library() -> None:
    source = Path(components.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module is not None
            imported.add(node.module)
    outside = {
        name
        for name in imported
        if name.split(".")[0] not in sys.stdlib_module_names
        and name != "corollary.calendars"
    }
    assert outside == set()
