"""Step 8, U2: windows, z, Phi, scores, the composite and the as-of rule (C6-C10).

Spec: ``docs/superpowers/specs/2026-11-17-phase-3-step-8-composite.md``,
*Testing* -> U2. Every expected number here is either computed by hand in the
test or recomputed from what the result serves, never read back from the code
under test.
"""

import ast
import random
import sys
import types
import typing
from collections.abc import Iterable, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path

import pytest

from corollary.calendars import nyse_session_close, nyse_sessions_between
from corollary.data.macro import composite
from corollary.data.macro.components import ComponentKey
from corollary.data.macro.composite import (
    FORMING_NEEDS,
    WINDOW_SESSIONS,
    AsOfUndetermined,
    ComponentRow,
    CompositeReading,
    ExclusionReason,
    IncompleteLoad,
    UnavailableReason,
    composite_of,
    expected_as_of,
    load_start,
    phi,
    read_composite,
    score,
)

K = ComponentKey
ALPACA = (K.MOMENTUM, K.STRENGTH, K.BREADTH, K.PUT_CALL, K.SAFE_HAVEN)
FRED = (K.VOLATILITY, K.JUNK_BOND)

#: A Monday. 2026-11-13 is the Friday before it.
END = date(2026, 11, 16)
FRIDAY = date(2026, 11, 13)
COMPUTED = datetime(2026, 11, 16, 22, 5, tzinfo=timezone.utc)
HOUR = timedelta(minutes=60)


#: Earlier than any row these tests store, so a load from here is every row
#: there is. The ``loaded_from`` contract itself is pinned in its own section.
EVERYTHING = date(2024, 1, 2)


def read(
    rows: Iterable[ComponentRow], *, now: datetime, loaded_from: date = EVERYTHING
) -> CompositeReading:
    return read_composite(rows, now=now, loaded_from=loaded_from)


def sessions_ending(end: date, count: int) -> tuple[date, ...]:
    return nyse_sessions_between(end - timedelta(days=2 * count + 14), end)[-count:]


def after_close(day: date) -> datetime:
    close = nyse_session_close(day)
    assert close is not None
    return close + HOUR


def row(
    key: ComponentKey,
    session: date,
    value: Decimal,
    computed_at: datetime = COMPUTED,
) -> ComponentRow:
    strength_like = key in (K.STRENGTH, K.BREADTH)
    return ComponentRow(
        component=key,
        session=session,
        raw_value=value,
        source="fred:VIXCLS" if key in FRED else "alpaca_daily:sip",
        members=500 if strength_like else None,
        universe_as_of=date(2026, 9, 30) if strength_like else None,
        computed_at=computed_at,
    )


def wavy(i: int, k: int) -> Decimal:
    """A deterministic, non-constant series per component."""
    return Decimal((i * 7 + k * 3) % 11) + Decimal(k) + Decimal("0.25")


def series(
    key: ComponentKey,
    sessions: Iterable[date],
    values: Sequence[Decimal] | None = None,
) -> list[ComponentRow]:
    days = list(sessions)
    k = list(K).index(key)
    vals = values if values is not None else [wavy(i, k) for i in range(len(days))]
    assert len(vals) == len(days)
    return [row(key, d, v) for d, v in zip(days, vals, strict=True)]


def full_store(
    end: date = END,
    count: int = 100,
    *,
    skip: dict[ComponentKey, set[date]] | None = None,
    stop: dict[ComponentKey, date] | None = None,
) -> list[ComponentRow]:
    """Every component with ``count`` sessions of values ending ``end``.

    ``skip`` removes single dates per component, ``stop`` truncates a
    component after a date.
    """
    days = sessions_ending(end, count)
    rows: list[ComponentRow] = []
    for key in K:
        for r in series(key, days):
            if skip and r.session in skip.get(key, set()):
                continue
            if stop and key in stop and r.session > stop[key]:
                continue
            rows.append(r)
    return rows


def reading_for(result: CompositeReading, key: ComponentKey) -> composite.ComponentReading:
    (found,) = [c for c in result.components if c.key is key]
    return found


def sqrt40(value: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 40
        return value.sqrt()


# --- The window (C6) -------------------------------------------------------------


def test_the_window_constants_are_the_specs() -> None:
    assert WINDOW_SESSIONS == 252
    assert FORMING_NEEDS == 60


def test_59_values_is_forming_and_60_is_not() -> None:
    rows = [r for r in full_store() if r.component is not K.PUT_CALL]
    days = sessions_ending(END, 60)
    rows += series(K.PUT_CALL, days[1:])  # 59 values ending END
    result = read(rows, now=after_close(END))
    pc = reading_for(result, K.PUT_CALL)
    assert pc.observations == 59
    assert pc.forming is True
    assert pc.excluded_reason is ExclusionReason.FORMING
    assert pc.score is None and pc.z is None
    assert result.components_averaged == 6

    rows += series(K.PUT_CALL, days[:1])  # the 60th
    result = read(rows, now=after_close(END))
    pc = reading_for(result, K.PUT_CALL)
    assert pc.observations == 60
    assert pc.forming is False
    assert pc.excluded_reason is None
    assert pc.score is not None
    assert result.components_averaged == 7


def test_a_gap_shortens_the_window_rather_than_stretching_it() -> None:
    days = sessions_ending(END, 300)
    window = days[-252:]
    gaps = set(window[10:20])
    rows = [r for r in full_store(count=300) if r.component is not K.MOMENTUM]
    rows += [r for r in series(K.MOMENTUM, days) if r.session not in gaps]
    result = read(rows, now=after_close(END))
    momentum = reading_for(result, K.MOMENTUM)
    # 252 sessions minus the ten missing, and nothing borrowed from before
    # the window's first session to make up the count.
    assert momentum.observations == 242
    assert momentum.window_start == window[0]


def test_the_window_is_252_sessions_from_the_calendar_not_observations() -> None:
    rows = full_store(count=400)
    result = read(rows, now=after_close(END))
    for c in result.components:
        assert c.observations == 252
        assert c.window_start == sessions_ending(END, 252)[0]


# --- z (C6) ----------------------------------------------------------------------


def _hand_window_store() -> list[ComponentRow]:
    days = sessions_ending(END, 60)
    rows = [r for r in full_store() if r.component is not K.MOMENTUM]
    rows += series(K.MOMENTUM, days, [Decimal(i) for i in range(1, 61)])
    return rows


def test_z_on_a_hand_computed_window() -> None:
    # Values 1..60, x_D = 60. mean = 30.5; sample variance of 1..n is
    # n(n+1)/12 = 305; so z = 29.5 / sqrt(305).
    result = read(_hand_window_store(), now=after_close(END))
    momentum = reading_for(result, K.MOMENTUM)
    with localcontext() as ctx:
        ctx.prec = 40
        expected = Decimal("29.5") / sqrt40(Decimal(305))
    assert momentum.z == expected
    assert momentum.raw_value == Decimal(60)
    assert momentum.observations == 60


def test_z_includes_x_d() -> None:
    # Without x_D the window is 1..59: mean 30, variance 295, z = 30/sqrt(295).
    result = read(_hand_window_store(), now=after_close(END))
    momentum = reading_for(result, K.MOMENTUM)
    with localcontext() as ctx:
        ctx.prec = 40
        excluding = Decimal(30) / sqrt40(Decimal(295))
    assert momentum.z is not None
    assert momentum.z != excluding
    assert momentum.z < excluding


def test_zero_variance_is_no_variance_not_fifty() -> None:
    rows = [r for r in full_store() if r.component is not K.SAFE_HAVEN]
    rows += series(K.SAFE_HAVEN, sessions_ending(END, 80), [Decimal("0.5")] * 80)
    result = read(rows, now=after_close(END))
    haven = reading_for(result, K.SAFE_HAVEN)
    assert haven.excluded_reason is ExclusionReason.NO_VARIANCE
    assert haven.z is None and haven.score is None
    assert haven.excluded_detail
    assert result.components_averaged == 6


# --- Phi (C7) --------------------------------------------------------------------


def test_phi_of_zero_is_one_half_exactly() -> None:
    assert phi(Decimal(0)) == Decimal("0.5")


def _significant(value: Decimal, digits: int) -> Decimal:
    """``value`` correctly rounded (half-even) to ``digits`` significant digits."""
    exponent = Decimal(1).scaleb(value.adjusted() - digits + 1)
    return value.quantize(exponent, rounding=ROUND_HALF_EVEN)


@pytest.mark.parametrize(
    ("z", "published"),
    [
        # Correctly rounded to 15 significant digits. Recomputed to 28+ digits
        # before writing, both with phi's own erf series and independently
        # with Phi(z) = 1/2 + pdf(z) * sum z^(2n+1) / (1*3*...*(2n+1)) at 80
        # digits (pi from Machin, exp from Decimal); the two agree past 1E-45:
        #   Phi(1)    = 0.8413447460685429485852325456...
        #   Phi(1.96) = 0.9750021048517795658634157309...
        #   Phi(-2.5) = 0.0062096653257761351669781045...
        (Decimal(1), Decimal("0.841344746068543")),
        (Decimal("1.96"), Decimal("0.975002104851780")),
        (Decimal("-2.5"), Decimal("0.00620966532577614")),
    ],
)
def test_phi_matches_published_values_to_15_digits(z: Decimal, published: Decimal) -> None:
    value = phi(z)
    assert type(value) is Decimal
    assert _significant(value, 15) == published
    # Equal as numbers and digit for digit: the trailing zero of Phi(1.96) counts.
    assert str(_significant(value, 15)) == str(published)


def test_the_pi_constant_agrees_with_an_independent_machin_series() -> None:
    def arctan_inverse(n: int) -> Decimal:
        x = Decimal(1) / n
        x2 = x * x
        total, term, k = Decimal(0), x, 1
        while abs(term) > Decimal("1E-70"):
            total += term / k
            term *= -x2
            k += 2
        return total

    with localcontext() as ctx:
        ctx.prec = 70
        machin = 16 * arctan_inverse(5) - 4 * arctan_inverse(239)
        assert abs(composite.PI - machin) < Decimal("1E-58")


def test_the_score_either_side_of_7() -> None:
    assert phi(Decimal("6.99")) < 1  # computed, not short-circuited
    assert score(Decimal("6.99")) == 100
    assert score(Decimal("-6.99")) == 0
    assert score(Decimal(7)) == 100
    assert score(Decimal(-7)) == 0
    assert score(Decimal("123.4")) == 100
    assert score(Decimal("-123.4")) == 0


def test_z_is_never_clipped() -> None:
    days = sessions_ending(END, 100)
    values = [Decimal(1), Decimal(2)] * 49 + [Decimal(1), Decimal(1000)]
    rows = [r for r in full_store() if r.component is not K.MOMENTUM]
    rows += series(K.MOMENTUM, days, values)
    momentum = reading_for(read(rows, now=after_close(END)), K.MOMENTUM)
    assert momentum.z is not None and momentum.z > 7
    assert momentum.score == 100


def _boundary(target: Decimal) -> Decimal:
    """The z at which 100 * phi(z) crosses ``target``, by bisection."""
    lo, hi = Decimal(-7), Decimal(7)
    with localcontext() as ctx:
        ctx.prec = 50
        for _ in range(200):
            mid = (lo + hi) / 2
            if 100 * phi(mid) < target:
                lo = mid
            else:
                hi = mid
    return lo


@pytest.mark.parametrize("half", [Decimal("50.5"), Decimal("12.5"), Decimal("97.5")])
def test_integer_rounding_just_either_side_of_a_half(half: Decimal) -> None:
    z = _boundary(half)
    with localcontext() as ctx:
        # The default 28 digits would round both the offset and the product
        # straight back onto the boundary.
        ctx.prec = 60
        below, above = z - Decimal("1E-30"), z + Decimal("1E-30")
        assert below < z < above
        assert 100 * phi(below) < half < 100 * phi(above)
    assert score(below) == int(half - Decimal("0.5"))
    assert score(above) == int(half + Decimal("0.5"))


def test_the_composite_rounds_a_half_up_not_to_even() -> None:
    assert composite_of([50, 51]) == 51  # 50.5; HALF_EVEN would print 50
    assert composite_of([52, 51]) == 52  # 51.5
    assert composite_of([50, 50, 51]) == 50  # 50.33
    assert composite_of([0]) == 0
    assert composite_of([100, 100, 99]) == 100  # 99.67
    with pytest.raises(ValueError):
        composite_of([])


# --- Orientation ---------------------------------------------------------------------


def _spike(key: ComponentKey) -> CompositeReading:
    days = sessions_ending(END, 100)
    values = [Decimal(10) + Decimal(i % 3) for i in range(99)] + [Decimal(20)]
    rows = [r for r in full_store() if r.component is not key]
    rows += series(key, days, values)
    return read(rows, now=after_close(END))


def test_a_higher_momentum_raises_the_score() -> None:
    m = reading_for(_spike(K.MOMENTUM), K.MOMENTUM)
    assert m.z is not None and m.z > 0 and m.oriented_z == m.z
    assert m.score is not None and m.score > 50


@pytest.mark.parametrize("key", [K.VOLATILITY, K.PUT_CALL, K.JUNK_BOND])
def test_vix_above_its_ma_a_higher_put_call_and_a_wider_spread_lower_the_score(
    key: ComponentKey,
) -> None:
    c = reading_for(_spike(key), key)
    assert c.z is not None and c.z > 0
    assert c.oriented_z == c.z.copy_negate()
    assert c.score is not None and c.score < 50


# --- The composite (C7) --------------------------------------------------------------


@pytest.mark.parametrize("seed", range(25))
def test_the_composite_is_the_rounded_mean_of_the_served_scores(seed: int) -> None:
    rng = random.Random(seed)
    days = sessions_ending(END, 120)
    rows: list[ComponentRow] = []
    for key in K:
        count = rng.choice([30, 59, 60, 90, 120])
        values = [Decimal(rng.randint(-5000, 5000)) / 1000 for _ in range(count)]
        rows += series(key, days[-count:], values)
    result = read(rows, now=after_close(END))
    served = [c.score for c in result.components if c.score is not None]
    assert result.components_averaged == len(served)
    if not served:
        assert result.composite is None
        return
    with localcontext() as ctx:
        ctx.prec = 40
        mean = Decimal(sum(served)) / len(served)
    expected = int(mean.quantize(Decimal(1), rounding="ROUND_HALF_UP"))
    assert result.composite == expected


def test_forming_and_excluded_components_are_not_averaged_but_are_counted() -> None:
    days = sessions_ending(END, 100)
    rows = [
        r
        for r in full_store()
        if r.component not in (K.PUT_CALL, K.JUNK_BOND, K.VOLATILITY)
    ]
    rows += series(K.PUT_CALL, days[-30:])  # forming
    rows += series(K.JUNK_BOND, days, [Decimal("3.1")] * 100)  # no variance
    rows += series(K.VOLATILITY, days[:-2])  # two sessions old: stale
    result = read(rows, now=after_close(END))

    assert len(result.components) == 7
    assert [c.key for c in result.components] == list(K)
    reasons = {c.key: c.excluded_reason for c in result.components}
    assert reasons[K.PUT_CALL] is ExclusionReason.FORMING
    assert reasons[K.JUNK_BOND] is ExclusionReason.NO_VARIANCE
    assert reasons[K.VOLATILITY] is ExclusionReason.STALE
    for c in result.components:
        if c.excluded_reason is not None:
            assert c.score is None
            assert c.excluded_detail  # rule 8: the reason is stated
    included = [c.score for c in result.components if c.score is not None]
    assert len(included) == 4
    assert result.components_averaged == 4
    assert result.components_total == 7
    assert result.composite == composite_of(included)


def test_zero_usable_components_gives_no_composite_and_a_reason() -> None:
    days = sessions_ending(END, 30)
    rows = [r for key in K for r in series(key, days)]  # every component forming
    result = read(rows, now=after_close(END))
    assert result.as_of == END
    assert result.composite is None
    assert result.components_averaged == 0
    assert result.unavailable_reason is UnavailableReason.NO_USABLE_COMPONENTS
    assert result.unavailable_detail and "forming" in result.unavailable_detail
    assert len(result.components) == 7
    assert all(c.excluded_reason is ExclusionReason.FORMING for c in result.components)


def test_an_empty_store_gives_no_composite_and_a_reason() -> None:
    result = read([], now=after_close(END))
    assert result.as_of is None
    assert result.expected_as_of == END
    assert result.composite is None
    assert result.unavailable_reason is UnavailableReason.NO_ROWS
    assert result.unavailable_detail
    assert result.stale is False
    assert result.previous is None and result.change is None
    assert result.components == ()
    assert result.computed_at is None


def test_only_fred_rows_gives_no_composite_and_a_reason() -> None:
    rows = [r for r in full_store() if r.component in FRED]
    result = read(rows, now=after_close(END))
    assert result.as_of is None
    assert result.unavailable_reason is UnavailableReason.NO_ROWS


# --- The as-of rule (C8, Q28 option b) -------------------------------------------------


def test_every_component_dated_d_has_lag_zero_and_is_not_provisional() -> None:
    result = read(full_store(), now=after_close(END))
    assert result.as_of == END
    assert result.expected_as_of == END
    assert result.stale is False
    assert result.provisional is False
    assert result.provisional_note is None
    assert result.lagged == ()
    for c in result.components:
        assert c.value_date == END
        assert c.lag_sessions == 0
    assert result.components_averaged == 7


def test_a_one_session_lag_is_included_and_provisional_naming_each_and_its_date() -> None:
    rows = full_store(stop={K.VOLATILITY: FRIDAY, K.JUNK_BOND: FRIDAY})
    result = read(rows, now=after_close(END))
    assert result.as_of == END  # the five Alpaca components set D
    for key in FRED:
        c = reading_for(result, key)
        assert c.value_date == FRIDAY
        assert c.lag_sessions == 1
        assert c.excluded_reason is None
        assert c.score is not None
    assert result.components_averaged == 7
    assert result.provisional is True
    assert result.lagged == ((K.VOLATILITY, FRIDAY), (K.JUNK_BOND, FRIDAY))
    # C8: "naming each lagged component and its date". C2 gives no display
    # name per component, so the note uses the key; ISO dates, C8's example
    # being "VIX: Nov 13 close". Two components, so the plural verb.
    assert result.provisional_note == (
        "Provisional: as of the 2026-11-16 close, volatility and junk_bond "
        "use the 2026-11-13 close"
    )


def test_a_lagged_component_is_scored_over_the_window_ending_d() -> None:
    rows = full_store(count=300, stop={K.VOLATILITY: FRIDAY})
    result = read(rows, now=after_close(END))
    vol = reading_for(result, K.VOLATILITY)
    assert vol.window_start == sessions_ending(END, 252)[0]
    assert vol.observations == 251  # 252 sessions ending D, one of them unfilled


def test_a_two_session_lag_is_stale_and_drops_the_count() -> None:
    thursday = date(2026, 11, 12)
    rows = full_store(stop={K.VOLATILITY: thursday})
    result = read(rows, now=after_close(END))
    vol = reading_for(result, K.VOLATILITY)
    assert vol.excluded_reason is ExclusionReason.STALE
    assert vol.value_date == thursday
    assert vol.lag_sessions == 2
    assert vol.score is None
    assert vol.excluded_detail and "2026-11-12" in vol.excluded_detail
    assert result.components_averaged == 6
    # A stale component is not included, so it does not make D provisional.
    assert result.provisional is False


def test_no_row_on_or_before_d_is_no_value() -> None:
    rows = [r for r in full_store() if r.component is not K.JUNK_BOND]
    rows += [row(K.JUNK_BOND, date(2026, 11, 17), Decimal(1))]  # after D: unseen
    result = read(rows, now=after_close(END))
    junk = reading_for(result, K.JUNK_BOND)
    assert junk.excluded_reason is ExclusionReason.NO_VALUE
    assert junk.value_date is None and junk.lag_sessions is None
    # A statement about the rows loaded, so it says which rows those were.
    assert junk.excluded_detail and "loaded from 2024-01-02" in junk.excluded_detail
    assert result.components_averaged == 6


def test_a_bond_holiday_gap_lags_junk_by_one_session() -> None:
    veterans = date(2026, 11, 11)  # NYSE open, bond market closed
    assert nyse_session_close(veterans) is not None
    rows = full_store(end=date(2026, 11, 12), skip={K.JUNK_BOND: {veterans}})

    on_holiday = read(rows, now=after_close(veterans))
    assert on_holiday.as_of == veterans
    junk = reading_for(on_holiday, K.JUNK_BOND)
    assert junk.value_date == date(2026, 11, 10)
    assert junk.lag_sessions == 1
    assert junk.excluded_reason is None
    assert on_holiday.provisional is True
    assert on_holiday.lagged == ((K.JUNK_BOND, date(2026, 11, 10)),)
    # One component, so the singular verb.
    assert on_holiday.provisional_note == (
        "Provisional: as of the 2026-11-11 close, junk_bond uses the 2026-11-10 close"
    )

    next_day = read(rows, now=after_close(date(2026, 11, 12)))
    assert next_day.as_of == date(2026, 11, 12)
    junk = reading_for(next_day, K.JUNK_BOND)
    assert junk.lag_sessions == 0
    assert junk.observations == 99  # the holiday is a gap in its window
    assert next_day.provisional is False


def test_the_close_plus_60_minutes_boundary_for_d() -> None:
    rows = full_store()
    boundary = after_close(END)
    assert boundary == datetime(2026, 11, 16, 22, 0, tzinfo=timezone.utc)

    before = read(rows, now=boundary - timedelta(microseconds=1))
    assert before.as_of == FRIDAY
    assert before.expected_as_of == FRIDAY
    assert before.stale is False
    assert all(c.value_date == FRIDAY for c in before.components)

    at = read(rows, now=boundary)
    assert at.as_of == END
    assert at.expected_as_of == END


def test_the_boundary_follows_a_half_day_close_from_the_calendar() -> None:
    half_day = date(2026, 11, 27)  # the day after Thanksgiving: 13:00 ET
    close = nyse_session_close(half_day)
    assert close == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    rows = full_store(end=half_day)
    boundary = close + HOUR

    before = read(rows, now=boundary - timedelta(seconds=1))
    assert before.as_of == date(2026, 11, 25)  # Thanksgiving is no session

    at = read(rows, now=boundary)
    assert at.as_of == half_day


def test_a_now_in_any_timezone_is_the_same_instant() -> None:
    rows = full_store()
    utc = read(rows, now=after_close(END))
    eastern = read(
        rows, now=after_close(END).astimezone(timezone(timedelta(hours=-5)))
    )
    assert utc == eastern


def test_d_older_than_the_latest_completed_session_is_stale() -> None:
    rows = full_store(end=FRIDAY)
    result = read(rows, now=after_close(END))
    assert result.as_of == FRIDAY
    assert result.expected_as_of == END
    assert result.stale is True
    assert result.composite is not None


def test_d_is_the_latest_session_all_five_alpaca_components_have() -> None:
    # Put/call missed END (a gap); the other six have it. C8: D is the latest
    # session for which the five Alpaca components have values.
    rows = full_store(skip={K.PUT_CALL: {END}})
    result = read(rows, now=after_close(END))
    assert result.as_of == FRIDAY
    assert result.stale is True
    for c in result.components:
        assert c.value_date == FRIDAY
        assert c.lag_sessions == 0


def test_an_alpaca_component_with_no_row_at_all_is_refused_as_undetermined() -> None:
    # U5: "No seed, no row" -- strength and breadth may have no rows at all,
    # while C8 makes D the latest session all five Alpaca components have.
    # The spec does not decide this case; U2 refuses it rather than guess.
    rows = [r for r in full_store() if r.component not in (K.STRENGTH, K.BREADTH)]
    with pytest.raises(AsOfUndetermined) as raised:
        read(rows, now=after_close(END))
    assert raised.value.missing == (K.STRENGTH, K.BREADTH)
    assert isinstance(raised.value, NotImplementedError)


def test_alpaca_rows_that_stopped_give_a_stale_d_at_the_last_common_session() -> None:
    """C8 read literally picks *D*, and C14 marks it stale. Pending the owner.

    The open owner question (see composite.py's docstring) covers this shape
    together with never-had-rows; this test pins the current literal-C8
    behaviour, not a settled one. C14: "The composite is **stale** when its
    *D* is older than the latest
    NYSE session whose close + 60 minutes has passed ... the panel reads
    'stale: as of Nov 13; Nov 14's close not computed'". Strength and
    breadth have history to 2026-11-10 and then stop (a lapsed seed); the
    other five components run to 2026-11-16.
    """
    stopped = date(2026, 11, 10)
    rows = full_store(stop={K.STRENGTH: stopped, K.BREADTH: stopped})
    result = read(rows, now=after_close(END))
    assert result.as_of == stopped
    assert result.expected_as_of == END
    assert result.stale is True
    assert result.unavailable_reason is None
    assert result.composite is not None
    assert result.components_averaged == 7
    # Every component reads its value at D, not its newer rows (C8).
    for c in result.components:
        assert c.value_date == stopped
        assert c.lag_sessions == 0
    assert result.previous is not None
    assert result.previous.as_of == date(2026, 11, 9)


def test_alpaca_rows_with_no_common_session_give_no_composite_and_a_reason() -> None:
    """Decided by C8 read literally, not open.

    C8: "*D* is the latest session for which the five Alpaca components have
    values." All five have rows here, but momentum has only even-numbered
    sessions and put/call only odd-numbered ones, so no session has all
    five, there is no *D*, and the result is ``NO_COMMON_SESSION``.
    """
    days = sessions_ending(END, 100)
    rows = [r for r in full_store() if r.component not in (K.MOMENTUM, K.PUT_CALL)]
    rows += series(K.MOMENTUM, days[0::2])
    rows += series(K.PUT_CALL, days[1::2])
    result = read(rows, now=after_close(END))
    assert result.as_of is None
    assert result.expected_as_of == END
    assert result.unavailable_reason is UnavailableReason.NO_COMMON_SESSION
    assert result.unavailable_detail and "all five" in result.unavailable_detail
    assert result.composite is None
    assert result.components == ()
    assert result.previous is None
    assert result.change is None and result.change_note is None


# --- previous and the change (C9) -------------------------------------------------------


def test_previous_is_the_same_derivation_over_windows_ending_the_session_before() -> None:
    rows = full_store()
    today = read(rows, now=after_close(END))
    yesterday = read(rows, now=after_close(FRIDAY))
    assert yesterday.as_of == FRIDAY
    assert today.previous is not None
    assert today.previous.as_of == FRIDAY
    assert today.previous.composite == yesterday.composite
    assert today.previous.components_averaged == yesterday.components_averaged
    assert today.composite is not None and yesterday.composite is not None
    assert today.change == today.composite - yesterday.composite
    assert today.change_note is None


def test_a_different_component_set_gives_no_change_and_the_note() -> None:
    # Put/call has 59 values at FRIDAY and 60 at END: it stops forming today.
    days = sessions_ending(END, 60)
    rows = [r for r in full_store() if r.component is not K.PUT_CALL]
    rows += series(K.PUT_CALL, days)
    result = read(rows, now=after_close(END))
    assert result.components_averaged == 7
    assert result.previous is not None
    assert result.previous.components_averaged == 6
    assert result.change is None
    assert result.change_note == "component set changed"


def test_lagging_alone_never_trips_component_set_changed() -> None:
    rows = full_store(stop={K.VOLATILITY: FRIDAY, K.JUNK_BOND: FRIDAY})
    result = read(rows, now=after_close(END))
    assert result.provisional is True
    assert result.previous is not None
    assert result.previous.components_averaged == result.components_averaged == 7
    assert result.change is not None
    assert result.change_note is None


def test_an_alpaca_component_lagging_inside_previous_is_included_at_lag_one() -> None:
    # Put/call missed FRIDAY, the session before D, but has END, so D is END.
    # previous is scored over windows ending FRIDAY (C9), where put/call's
    # newest value is Thursday's: a lag of one, included (C8) rather than
    # stale. previous averages all seven, and lagging alone is not a set
    # change, so the change is printed.
    rows = full_store(skip={K.PUT_CALL: {FRIDAY}})
    result = read(rows, now=after_close(END))
    assert result.as_of == END
    assert result.provisional is False
    assert result.components_averaged == 7
    assert result.previous is not None
    assert result.previous.as_of == FRIDAY
    assert result.previous.components_averaged == 7
    assert result.previous.composite is not None and result.composite is not None
    assert result.change == result.composite - result.previous.composite
    assert result.change_note is None


# --- How much the caller must load (F3) -------------------------------------------------


@pytest.mark.parametrize(
    ("as_of", "before"),
    [
        (date(2026, 11, 27), date(2026, 11, 25)),  # Thanksgiving, 11-26, between
        (date(2026, 1, 20), date(2026, 1, 16)),  # MLK Day, 01-19, between
    ],
)
def test_load_start_is_the_first_of_the_252_sessions_ending_the_session_before(
    as_of: date, before: date
) -> None:
    assert list(nyse_sessions_between(before, as_of)) == [before, as_of]
    start = load_start(as_of)
    window = nyse_sessions_between(start, before)
    assert len(window) == WINDOW_SESSIONS
    assert window[0] == start
    assert nyse_sessions_between(EVERYTHING, before)[-WINDOW_SESSIONS] == start


def test_load_start_refuses_a_day_that_is_not_a_session() -> None:
    with pytest.raises(ValueError, match="not an NYSE session"):
        load_start(date(2026, 11, 26))
    with pytest.raises(TypeError):
        load_start(after_close(END))  # a datetime is a date to mypy; refused at runtime


def test_an_exact_load_reads_the_same_as_a_load_of_everything() -> None:
    rows = full_store(count=400)
    start = load_start(END)
    loaded = [r for r in rows if r.session >= start]
    exact = read(loaded, now=after_close(END), loaded_from=start)
    assert exact == read(rows, now=after_close(END))
    assert all(c.observations == WINDOW_SESSIONS for c in exact.components)


def test_a_load_one_session_short_of_previous_window_is_refused() -> None:
    # A load from the first session of today's window covers today's 252
    # but only 251 of previous's: computed anyway, yesterday's z (and so the
    # change) would quietly move.
    rows = full_store(count=400)
    needed = load_start(END)
    short = sessions_ending(END, WINDOW_SESSIONS)[0]
    assert len(nyse_sessions_between(needed, short)) == 2  # one session short
    loaded = [r for r in rows if r.session >= short]
    with pytest.raises(IncompleteLoad) as raised:
        read(loaded, now=after_close(END), loaded_from=short)
    assert raised.value.needed_from == needed
    assert raised.value.loaded_from == short
    assert raised.value.as_of == END


def test_a_stale_d_needs_an_earlier_load_and_the_reload_completes() -> None:
    # D is three sessions behind expected_as_of, so its windows start three
    # sessions earlier than a load sized from expected_as_of covers.
    d = date(2026, 11, 11)
    rows = full_store(end=d, count=400)
    now = after_close(END)
    first_from = load_start(expected_as_of(now))
    first = [r for r in rows if r.session >= first_from]
    with pytest.raises(IncompleteLoad) as raised:
        read(first, now=now, loaded_from=first_from)
    needed = raised.value.needed_from
    assert needed == load_start(d)
    assert len(nyse_sessions_between(needed, first_from)) == 4
    assert raised.value.as_of == d

    # U7's one reload, from needed_from, completes and matches a full load.
    second = [r for r in rows if r.session >= needed]
    result = read(second, now=now, loaded_from=needed)
    assert result.as_of == d and result.stale is True
    assert all(c.observations == WINDOW_SESSIONS for c in result.components)
    assert result == read(rows, now=now)


def test_loaded_from_must_be_a_date() -> None:
    with pytest.raises(TypeError, match="loaded_from"):
        read(full_store(), now=after_close(END), loaded_from=after_close(END))


# --- computed_at -------------------------------------------------------------------------


def test_computed_at_is_the_newest_computed_at_among_the_rows_used() -> None:
    rows = full_store(count=300)
    newest = datetime(2026, 11, 16, 22, 7, tzinfo=timezone.utc)
    outside = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)
    oldest_session = sessions_ending(END, 300)[0]  # outside every window
    patched: list[ComponentRow] = []
    for r in rows:
        if r.component is K.BREADTH and r.session == date(2026, 11, 10):
            r = row(r.component, r.session, r.raw_value, computed_at=newest)
        if r.component is K.MOMENTUM and r.session == oldest_session:
            r = row(r.component, r.session, r.raw_value, computed_at=outside)
        patched.append(r)
    result = read(patched, now=after_close(END))
    assert result.computed_at == newest


# --- Inputs, purity, and no float --------------------------------------------------------


def test_a_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        read(full_store(), now=datetime(2026, 11, 16, 22, 0))


def test_a_float_raw_value_is_refused() -> None:
    with pytest.raises(TypeError):
        row(K.MOMENTUM, END, 1.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        score(0.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        phi(0.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        composite_of([50.0])  # type: ignore[list-item]


def test_malformed_rows_are_refused() -> None:
    with pytest.raises(TypeError, match="date"):
        row(K.MOMENTUM, datetime(2026, 11, 16, 22, 0, tzinfo=timezone.utc), Decimal(1))
    with pytest.raises(ValueError, match="finite"):
        row(K.MOMENTUM, END, Decimal("NaN"))
    with pytest.raises(ValueError, match="timezone-aware"):
        row(K.MOMENTUM, END, Decimal(1), computed_at=datetime(2026, 11, 16, 22, 0))
    with pytest.raises(TypeError):
        ComponentRow("momentum", END, Decimal(1), "s", None, None, COMPUTED)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="source"):
        ComponentRow(K.MOMENTUM, END, Decimal(1), "", None, None, COMPUTED)


def test_duplicate_and_non_session_rows_are_refused() -> None:
    rows = full_store()
    with pytest.raises(ValueError, match="duplicate"):
        read([*rows, rows[0]], now=after_close(END))
    saturday = row(K.MOMENTUM, date(2026, 11, 14), Decimal(1))
    with pytest.raises(ValueError, match="not an NYSE session"):
        read([*rows, saturday], now=after_close(END))


def _mentions_float(annotation: object) -> bool:
    if annotation is float:
        return True
    return any(_mentions_float(arg) for arg in typing.get_args(annotation))


def test_no_float_appears_in_any_public_signature_or_field() -> None:
    checked = 0
    for name in composite.__all__:
        obj = getattr(composite, name)
        if is_dataclass(obj) and isinstance(obj, type):
            hints = typing.get_type_hints(obj)
            for f in fields(obj):
                assert not _mentions_float(hints[f.name]), (obj, f.name)
                checked += 1
        elif isinstance(obj, types.FunctionType):
            for arg, hint in typing.get_type_hints(obj).items():
                assert not _mentions_float(hint), (obj, arg)
                checked += 1
    assert checked > 30


def test_the_module_imports_only_calendars_components_and_the_standard_library() -> None:
    source = Path(composite.__file__).read_text(encoding="utf-8")
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
        and name not in ("corollary.calendars", "corollary.data.macro.components")
    }
    assert outside == set()
    # No float special functions either (C7).
    assert "erf" not in {n.attr for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute)}
    assert "statistics" not in imported and "math" not in imported
