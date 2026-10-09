"""Decision 21's tradeability filter -- pure, every boundary its own test.

Every input is synthetic. The bars are real :class:`Bar` instances so the
filter is proven against the type the provider actually returns.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from corollary.calendars import NYSE_TZ, nyse_session_close
from corollary.data.news.tradeability import (
    ADV_LOOKBACK_SESSIONS,
    ADV_SESSIONS,
    MIN_AVG_DAILY_VOLUME,
    MIN_COMPLETED_SESSIONS,
    MIN_LAST_CLOSE,
    STANDARD_CONTRACT_SIZE,
    TradeabilityFailure,
    adv_request_start,
    adv_window,
    adv_window_start,
    assess_tradeability,
    has_standard_contract,
    is_adjusted_root_ticker,
    partial_window_first_session,
)
from corollary.data.providers.interface import Bar, OptionContract, OptionType

SESSION = date(2026, 9, 24)

#: The 20th NYSE session before SESSION, counted by hand: Sep 23 back to
#: Aug 26, skipping weekends and Labor Day (Sep 7). An independent check on
#: the calendar walk, not derived from it.
WINDOW_START = date(2026, 8, 26)

#: The 272nd NYSE session before SESSION -- the window plus the 252-session
#: listing lookback. Too many to count by hand in a comment, so it is counted
#: independently of ``corollary.calendars`` instead: weekdays, minus the NYSE
#: full-day closures listed in :data:`HAND_LISTED_CLOSURES` (see
#: ``test_the_request_start_matches_a_hand_listed_holiday_count``).
REQUEST_START = date(2025, 8, 25)

#: NYSE full-day closures between REQUEST_START and SESSION, typed from the
#: published holiday schedule rather than read from the calendar under test.
HAND_LISTED_CLOSURES = frozenset(
    {
        date(2025, 9, 1),  # Labor Day
        date(2025, 11, 27),  # Thanksgiving
        date(2025, 12, 25),  # Christmas
        date(2026, 1, 1),  # New Year's Day
        date(2026, 1, 19),  # Martin Luther King Jr. Day
        date(2026, 2, 16),  # Washington's Birthday
        date(2026, 4, 3),  # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),  # Independence Day, observed
        date(2026, 9, 7),  # Labor Day
    }
)


def sessions_before(day: date, count: int) -> list[date]:
    """The ``count`` NYSE sessions before ``day``, oldest first, from the calendar."""
    found: list[date] = []
    cursor = day
    while len(found) < count:
        cursor -= timedelta(days=1)
        if nyse_session_close(cursor) is not None:
            found.append(cursor)
    return list(reversed(found))


def bar(day: date, *, volume: int = 2_000_000, close: str = "50", symbol: str = "ACME") -> Bar:
    """A daily bar stamped the way Alpaca stamps one: midnight New York, in UTC."""
    opened = datetime(day.year, day.month, day.day, tzinfo=NYSE_TZ).astimezone(timezone.utc)
    return Bar(
        symbol=symbol,
        at=opened,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=volume,
        trade_count=1000,
        vwap=None,
    )


def history(
    count: int,
    *,
    volume: int = 2_000_000,
    last_close: str = "50",
    volumes: list[int] | None = None,
    before: date = SESSION,
    symbol: str = "ACME",
) -> list[Bar]:
    """One bar on each of the ``count`` NYSE sessions before ``before``, oldest first."""
    days = sessions_before(before, count)
    vols = volumes if volumes is not None else [volume] * count
    assert len(vols) == count
    bars = [bar(d, volume=v, symbol=symbol) for d, v in zip(days, vols, strict=True)]
    if bars:
        bars[-1] = bar(days[-1], volume=vols[-1], close=last_close, symbol=symbol)
    return bars


def assess(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "has_options": True,
        "standard_root": True,
        "daily_bars": history(25),
        "session_date": SESSION,
    }
    kwargs.update(overrides)
    return assess_tradeability("ACME", **kwargs)


def test_the_thresholds_are_the_spec_values() -> None:
    assert MIN_AVG_DAILY_VOLUME == 1_000_000
    assert MIN_LAST_CLOSE == Decimal("5")
    assert isinstance(MIN_LAST_CLOSE, Decimal)
    # Q10: a recent IPO is judged on the sessions it has, down to one.
    assert MIN_COMPLETED_SESSIONS == 1
    assert ADV_SESSIONS == 20
    # Q10 fix round: one year of sessions, so only a name silent for longer
    # than a year reads as a recent listing.
    assert ADV_LOOKBACK_SESSIONS == 252
    assert STANDARD_CONTRACT_SIZE == Decimal(100)


def test_a_liquid_optionable_standard_name_passes_and_carries_every_cache_field() -> None:
    result = assess()
    assert result.passes is True
    assert result.failures == ()
    assert result.first_failure is None
    assert result.ticker == "ACME"
    assert result.session_date == SESSION
    assert result.has_options is True
    assert result.standard_root is True
    assert result.avg_volume_20d == 2_000_000
    assert result.last_close == Decimal("50")
    assert isinstance(result.last_close, Decimal)
    # The divisor the average was taken over: 25 bars, an established name,
    # so all 20 window sessions.
    assert result.sessions_available == 20


# --- never a candidate --------------------------------------------------------


def test_no_has_options_fails() -> None:
    result = assess(has_options=False)
    assert result.passes is False
    assert result.failures == (TradeabilityFailure.NO_OPTIONS,)


def test_an_adjusted_root_ticker_fails() -> None:
    result = assess_tradeability(
        "GME1", has_options=True, standard_root=True, daily_bars=[], session_date=SESSION
    )
    assert result.passes is False
    assert result.first_failure is TradeabilityFailure.ADJUSTED_ROOT


def test_no_standard_root_contract_fails() -> None:
    """A has_options underlying whose only listed chains are adjusted."""
    result = assess(standard_root=False)
    assert result.passes is False
    assert result.failures == (TradeabilityFailure.NO_STANDARD_CONTRACT,)


def test_an_unchecked_standard_root_fails_closed() -> None:
    result = assess(standard_root=None)
    assert result.passes is False
    assert result.failures == (TradeabilityFailure.STANDARD_ROOT_UNCHECKED,)
    assert result.standard_root is None


def test_adv_at_999_999_fails() -> None:
    result = assess(daily_bars=history(20, volume=999_999))
    assert result.avg_volume_20d == 999_999
    assert result.failures == (TradeabilityFailure.LOW_VOLUME,)


def test_adv_is_floored_so_a_mean_just_under_the_line_is_not_rounded_up_to_it() -> None:
    """Sum 19,999,999 over 20 sessions is a mean of 999,999.95: floored, it fails."""
    volumes = [1_000_000] * 19 + [999_999]
    result = assess(daily_bars=history(20, volumes=volumes))
    assert result.avg_volume_20d == 999_999
    assert result.failures == (TradeabilityFailure.LOW_VOLUME,)


def test_a_close_at_4_99_fails() -> None:
    result = assess(daily_bars=history(20, last_close="4.99"))
    assert result.last_close == Decimal("4.99")
    assert result.failures == (TradeabilityFailure.LOW_CLOSE,)


def test_no_completed_session_fails_with_a_named_reason_and_nothing_to_report() -> None:
    """Zero completed sessions: no close, no volume, no divide-by-zero, no pass."""
    result = assess(daily_bars=[])
    assert result.sessions_available == 0
    assert result.avg_volume_20d is None
    assert result.last_close is None
    assert result.failures == (TradeabilityFailure.NO_COMPLETED_SESSION,)


def test_only_a_bar_for_session_date_itself_is_no_completed_session() -> None:
    """Listing day, mid-session: the one bar there is still forming."""
    result = assess(daily_bars=[bar(SESSION, volume=40_000_000)])
    assert result.sessions_available == 0
    assert result.avg_volume_20d is None
    assert result.failures == (TradeabilityFailure.NO_COMPLETED_SESSION,)


# --- recent listings (Q10, settled by the IPO date since Q12) -------------------


def first_session(bars: list[Bar]) -> date:
    """The NY session of the oldest bar -- what a true IPO date would match."""
    return min(item.at.astimezone(NYSE_TZ).date() for item in bars)


def assess_listing(bars: list[Bar], **overrides: Any) -> Any:
    """A partial window whose IPO date is its first bar's session (Q12)."""
    return assess(daily_bars=bars, ipo_date=first_session(bars), **overrides)


def test_a_single_completed_session_passes_when_its_ipo_date_is_recent() -> None:
    """Listed yesterday, and the IPO date says so: one session is enough (Q10, Q12)."""
    result = assess_listing(history(1, volume=1_500_000, last_close="31.25"))
    assert result.passes is True, result.failures
    assert result.sessions_available == 1
    assert result.avg_volume_20d == 1_500_000
    assert result.last_close == Decimal("31.25")


def test_a_single_completed_session_still_meets_every_threshold() -> None:
    thin = assess_listing(history(1, volume=999_999))
    assert thin.failures == (TradeabilityFailure.LOW_VOLUME,)
    cheap = assess_listing(history(1, last_close="4.99"))
    assert cheap.failures == (TradeabilityFailure.LOW_CLOSE,)
    unlisted = assess_listing(history(1), has_options=False)
    assert unlisted.failures == (TradeabilityFailure.NO_OPTIONS,)


def test_19_sessions_the_old_boundary_passes_on_a_19_session_average() -> None:
    result = assess_listing(history(19, volume=1_200_000))
    assert result.passes is True, result.failures
    assert result.sessions_available == 19
    assert result.avg_volume_20d == 1_200_000
    assert result.last_close == Decimal("50")


def test_a_partial_average_is_floored_over_its_own_session_count() -> None:
    """2,999,999 over 3 sessions is 999,999.67: floored, it fails."""
    result = assess_listing(history(3, volumes=[1_000_000, 1_000_000, 999_999]))
    assert result.sessions_available == 3
    assert result.avg_volume_20d == 999_999
    assert result.failures == (TradeabilityFailure.LOW_VOLUME,)


def test_a_recent_listing_counts_a_gap_after_its_first_bar_as_zero_volume() -> None:
    """Ten sessions since the first bar, one without a bar: 9 x 1,100,000 / 10.

    Averaged over the bars it traded on it would be 1,100,000 and pass.
    """
    bars = history(10, volume=1_100_000)
    gap = sessions_before(SESSION, 5)[0]
    bars = [item for item in bars if item.at.astimezone(NYSE_TZ).date() != gap]
    result = assess_listing(bars)
    assert result.sessions_available == 10
    assert result.avg_volume_20d == 990_000
    assert result.failures == (TradeabilityFailure.LOW_VOLUME,)


def test_a_recent_listing_with_a_stale_tape_still_fails() -> None:
    """Listed five sessions ago, no bar for the previous session: stale, as ever."""
    bars = history(5, volume=9_000_000)[:-1]
    result = assess_listing(bars)
    assert result.sessions_available == 5
    assert result.avg_volume_20d == 7_200_000
    assert result.failures == (TradeabilityFailure.STALE_BARS,)


def test_a_listing_on_the_first_window_session_reads_like_an_established_name() -> None:
    """The two rules meet at 20: first bar on the window's first session.

    That is a full window, not a partial one, so no IPO date is needed.
    """
    listed = assess(daily_bars=history(20, volume=1_000_000))
    established = assess(daily_bars=history(21, volume=1_000_000))
    assert listed.sessions_available == established.sessions_available == 20
    assert listed.avg_volume_20d == established.avg_volume_20d == 1_000_000
    assert listed.passes and established.passes


# --- Q12: the real IPO date settles a partial window -----------------------------


def test_a_partial_window_without_an_ipo_date_fails_closed() -> None:
    """Q12: a partial window is no longer a recent listing by itself.

    With no IPO date nothing is averaged -- neither divisor can be defended --
    so the average is ``None`` and the divisor 0, and the other checks still
    report.
    """
    result = assess(daily_bars=history(5, volume=2_000_000, last_close="4"))
    assert result.avg_volume_20d is None
    assert result.sessions_available == 0
    assert result.last_close == Decimal("4")
    assert result.failures == (
        TradeabilityFailure.IPO_DATE_UNAVAILABLE,
        TradeabilityFailure.LOW_CLOSE,
    )


def test_an_old_ipo_date_with_a_partial_window_fails_as_missing_bars() -> None:
    """Q12: listed long ago, bars only on five window sessions -- missing bars.

    Judged as the established name it is: 20 sessions, the fifteen with no bar
    counting zero -- 10,000,000 / 20 = 500,000, which fails. This is the case
    Q10 left open: a name silent for more than the 252-session lookback, then
    resumed, which bars alone read as a five-session IPO.
    """
    resumed = history(5, volume=2_000_000)
    assert first_session(resumed) > REQUEST_START
    result = assess(daily_bars=resumed, ipo_date=date(1999, 3, 10))
    assert result.sessions_available == 20
    assert result.avg_volume_20d == 500_000
    assert result.failures == (TradeabilityFailure.LOW_VOLUME,)


def test_an_old_ipo_date_with_a_stale_partial_window_fails_stale_and_on_volume() -> None:
    resumed = history(5, volume=2_000_000)[:-1]
    result = assess(daily_bars=resumed, ipo_date=date(2010, 6, 29))
    assert result.sessions_available == 20
    assert result.failures == (
        TradeabilityFailure.STALE_BARS,
        TradeabilityFailure.LOW_VOLUME,
    )


def test_an_ipo_the_session_before_the_window_is_established() -> None:
    """The boundary that rejects: listed one session before the window starts."""
    day_before = sessions_before(WINDOW_START, 1)[0]
    result = assess(daily_bars=history(10, volume=1_500_000), ipo_date=day_before)
    assert result.sessions_available == 20
    assert result.avg_volume_20d == 750_000
    assert result.failures == (TradeabilityFailure.LOW_VOLUME,)


def test_an_ipo_on_the_window_start_is_a_recent_listing() -> None:
    """The boundary that permits: "on or after the start of the ADV window"."""
    bars = history(20, volume=1_000_000)[1:]  # first bar on the second window session
    second = sessions_before(SESSION, 19)[0]
    assert first_session(bars) == second
    on_start = assess(daily_bars=bars, ipo_date=WINDOW_START)
    # Listed on the window's first session with no trade that day: that
    # session counts zero, exactly as it would for any listed name.
    assert on_start.sessions_available == 20
    assert on_start.avg_volume_20d == 950_000
    assert on_start.failures == (TradeabilityFailure.LOW_VOLUME,)

    on_first_bar = assess(daily_bars=bars, ipo_date=second)
    assert on_first_bar.sessions_available == 19
    assert on_first_bar.avg_volume_20d == 1_000_000
    assert on_first_bar.passes is True


def test_sessions_between_the_ipo_and_the_first_bar_count_zero() -> None:
    """Listed eight sessions ago, first traded five sessions ago: divisor 8."""
    listed = sessions_before(SESSION, 8)[0]
    result = assess(daily_bars=history(5, volume=1_600_000), ipo_date=listed)
    assert result.sessions_available == 8
    assert result.avg_volume_20d == 1_000_000
    assert result.passes is True


def test_an_ipo_date_after_the_first_bar_contradicts_the_tape_and_fails_closed() -> None:
    """Bars before the listing date: the vendor's date is not evidence here."""
    bars = history(5, volume=2_000_000)
    result = assess(daily_bars=bars, ipo_date=first_session(bars) + timedelta(days=1))
    assert result.avg_volume_20d is None
    assert result.sessions_available == 0
    assert result.failures == (TradeabilityFailure.IPO_DATE_UNAVAILABLE,)


def test_an_ipo_date_is_never_consulted_for_a_full_window() -> None:
    """An established name, or one first trading on the window start, ignores it."""
    for bars in (history(25), history(20)):
        without = assess(daily_bars=bars)
        with_nonsense = assess(daily_bars=bars, ipo_date=date(2030, 1, 1))
        assert without == with_nonsense
        assert without.passes is True


def test_no_completed_session_needs_no_ipo_date() -> None:
    result = assess(daily_bars=[], ipo_date=date(2026, 9, 1))
    assert result.failures == (TradeabilityFailure.NO_COMPLETED_SESSION,)


def test_partial_window_first_session_names_only_the_case_an_ipo_date_settles() -> None:
    assert partial_window_first_session("ACME", history(25), SESSION) is None
    assert partial_window_first_session("ACME", history(20), SESSION) is None
    assert partial_window_first_session("ACME", [], SESSION) is None
    assert partial_window_first_session("ACME", [bar(SESSION)], SESSION) is None
    five = history(5)
    assert partial_window_first_session("acme", five, SESSION) == first_session(five)
    with pytest.raises(ValueError):
        partial_window_first_session("ACME", history(5, symbol="OTHER"), SESSION)


# --- a partial window is not missing bars ---------------------------------------


def test_an_established_ticker_with_missing_recent_bars_still_fails() -> None:
    """Long-listed, last three sessions absent: stale, judged over all 20."""
    bars = history(60, volume=5_000_000)[:-3]
    result = assess(daily_bars=bars)
    assert result.sessions_available == 20
    assert result.avg_volume_20d == 4_250_000
    assert result.failures == (TradeabilityFailure.STALE_BARS,)


def test_an_established_name_missing_the_front_of_its_window_is_not_a_recent_listing() -> None:
    """Bars only on the last 10 window sessions -- but one in the lookback too.

    That lookback bar proves the name was listed before the window, so the
    ten missing sessions are zero-volume sessions of an established name:
    15,000,000 / 20 = 750,000, which fails -- and no IPO date is asked for.
    Without it the same ten bars are a partial window: failing closed with no
    IPO date, and a ten-session listing averaging 1,500,000 only when the IPO
    date says it listed then.
    """
    recent = history(10, volume=1_500_000)
    lookback_bar = bar(sessions_before(WINDOW_START, 1)[0], volume=1_500_000)

    established = assess(daily_bars=[lookback_bar, *recent])
    assert established.sessions_available == 20
    assert established.avg_volume_20d == 750_000
    assert established.failures == (TradeabilityFailure.LOW_VOLUME,)

    unknown = assess(daily_bars=recent)
    assert unknown.failures == (TradeabilityFailure.IPO_DATE_UNAVAILABLE,)

    listing = assess_listing(recent)
    assert listing.sessions_available == 10
    assert listing.avg_volume_20d == 1_500_000
    assert listing.passes is True


def test_any_older_bar_marks_a_name_established_however_far_back() -> None:
    """The caller may pass more than the lookback; an older bar still counts."""
    recent = history(5, volume=3_000_000)
    ancient = bar(date(2025, 1, 2), volume=3_000_000)
    result = assess(daily_bars=[ancient, *recent])
    assert result.sessions_available == 20
    assert result.avg_volume_20d == 750_000
    assert result.failures == (TradeabilityFailure.LOW_VOLUME,)


def test_a_name_silent_for_longer_than_the_lookback_is_settled_by_its_ipo_date() -> None:
    """The residual ambiguity Q10 left open, closed by Q12.

    A long-listed name with no bar anywhere in the 252-session lookback --
    suspended for more than a year, then resumed five sessions ago -- is
    indistinguishable by bars from a five-session IPO. Before Q12 it was
    judged as a recent listing and passed. Now its IPO date decides: none at
    all fails closed, an old one fails as missing bars, and only a date inside
    the window makes it the recent listing the bars suggest.
    """
    resumed = history(5, volume=2_000_000)
    assert first_session(resumed) > REQUEST_START
    assert assess(daily_bars=resumed).failures == (TradeabilityFailure.IPO_DATE_UNAVAILABLE,)
    assert assess(daily_bars=resumed, ipo_date=date(2004, 8, 19)).failures == (
        TradeabilityFailure.LOW_VOLUME,
    )
    assert assess_listing(resumed).passes is True


def test_a_name_suspended_for_months_then_resumed_is_judged_as_established() -> None:
    """The audit's case: a long-listed name halted for well over 40 sessions.

    Its last pre-suspension bar is 120 sessions back -- outside the old
    40-session lookback, inside the 252-session one. It resumed five sessions
    ago at 2,000,000 shares a day. Under the old lookback it read as a
    five-session listing and passed on a 2,000,000 average; now the pre-window
    bar marks it established, the average runs over all 20 window sessions
    with the silent ones as zeros, and it fails on volume.
    """
    halted_at = sessions_before(SESSION, 120)[0]
    # The fix is in what gets fetched: the request must reach the halted bar,
    # or the filter never sees it and the name reads as a listing again.
    assert adv_request_start(SESSION) <= halted_at < WINDOW_START
    before_halt = bar(halted_at, volume=2_000_000)
    resumed = history(5, volume=2_000_000, last_close="8")
    result = assess(daily_bars=[before_halt, *resumed])
    assert result.sessions_available == 20
    assert result.avg_volume_20d == 500_000
    assert result.failures == (TradeabilityFailure.LOW_VOLUME,)


# --- the boundaries pass ------------------------------------------------------


def test_adv_at_exactly_1_000_000_passes() -> None:
    result = assess(daily_bars=history(20, volume=1_000_000))
    assert result.avg_volume_20d == 1_000_000
    assert result.passes is True


def test_a_close_at_exactly_5_passes() -> None:
    result = assess(daily_bars=history(20, last_close="5.00"))
    assert result.last_close == Decimal("5")
    assert result.passes is True


def test_exactly_20_sessions_passes() -> None:
    result = assess(daily_bars=history(20))
    assert result.sessions_available == 20
    assert result.passes is True


# --- which bars count ---------------------------------------------------------


def test_only_the_trailing_20_sessions_enter_the_average() -> None:
    """Five heavy old sessions are outside the window and must not lift it."""
    volumes = [50_000_000] * 5 + [900_000] * 20
    result = assess(daily_bars=history(25, volumes=volumes))
    assert result.sessions_available == 20
    assert result.avg_volume_20d == 900_000
    assert result.failures == (TradeabilityFailure.LOW_VOLUME,)


def test_the_bar_for_session_date_itself_is_never_counted() -> None:
    """The session being keyed is not complete, whenever in the day this runs.

    The caller may pass every bar it has; the filter always uses the sessions
    strictly before ``session_date``, so a result cached at 09:00 and one
    computed at 17:00 agree.
    """
    bars = history(20, volume=900_000) + [bar(SESSION, volume=500_000_000, close="4.00")]
    result = assess(daily_bars=bars)
    assert result.sessions_available == 20
    assert result.avg_volume_20d == 900_000
    assert result.last_close == Decimal("50")
    assert result.failures == (TradeabilityFailure.LOW_VOLUME,)


def test_a_bar_after_session_date_is_never_counted() -> None:
    bars = history(20) + [bar(SESSION + timedelta(days=1), close="1.00")]
    result = assess(daily_bars=bars)
    assert result.sessions_available == 20
    assert result.last_close == Decimal("50")
    assert result.passes is True


def test_the_bar_session_is_read_in_new_york_not_utc() -> None:
    """A bar opened 23:30 ET on the 23rd is 03:30 UTC on the 24th -- still the 23rd."""
    late = datetime(2026, 9, 23, 23, 30, tzinfo=NYSE_TZ).astimezone(timezone.utc)
    assert late.date() == SESSION
    base = history(20, volume=1_000_000)[:-1]
    odd = Bar(
        symbol="ACME", at=late, open=Decimal(9), high=Decimal(9), low=Decimal(9),
        close=Decimal(9), volume=1_000_000, trade_count=1, vwap=None,
    )
    result = assess(daily_bars=[*base, odd])
    assert result.sessions_available == 20
    assert result.last_close == Decimal(9)


def test_bars_out_of_order_are_ordered_before_the_last_close_is_read() -> None:
    bars = history(20, last_close="7")
    result = assess(daily_bars=list(reversed(bars)))
    assert result.last_close == Decimal("7")


def test_two_bars_for_one_session_are_refused() -> None:
    bars = history(20)
    with pytest.raises(ValueError, match="two daily bars"):
        assess(daily_bars=[*bars, bars[-1]])


def test_a_bar_for_another_symbol_is_refused() -> None:
    bars = history(19) + [bar(SESSION - timedelta(days=30), symbol="OTHER")]
    with pytest.raises(ValueError, match="OTHER"):
        assess(daily_bars=bars)


def test_a_naive_bar_timestamp_is_refused() -> None:
    naive = Bar(
        symbol="ACME", at=datetime(2026, 9, 1), open=Decimal(1), high=Decimal(1),
        low=Decimal(1), close=Decimal(1), volume=1, trade_count=1, vwap=None,
    )
    with pytest.raises(ValueError, match="timezone"):
        assess(daily_bars=[naive])


def test_a_float_close_is_refused() -> None:
    bad = Bar(
        symbol="ACME", at=datetime(2026, 9, 1, 4, tzinfo=timezone.utc), open=Decimal(1),
        high=Decimal(1), low=Decimal(1), close=5.0,  # type: ignore[arg-type]
        volume=1, trade_count=1, vwap=None,
    )
    with pytest.raises(TypeError, match="Decimal"):
        assess(daily_bars=[bad])


def test_a_negative_volume_is_refused() -> None:
    bars = history(20)
    bars[0] = bar(sessions_before(SESSION, 20)[0], volume=-1)
    with pytest.raises(ValueError, match="volume"):
        assess(daily_bars=bars)


# --- the window is calendar sessions, not bars ---------------------------------


def test_the_window_is_the_20_nyse_sessions_before_session_date() -> None:
    window = adv_window(SESSION)
    assert len(window) == ADV_SESSIONS
    assert window[0] == WINDOW_START
    assert window[-1] == date(2026, 9, 23)
    assert date(2026, 9, 7) not in window  # Labor Day
    assert SESSION not in window
    assert adv_window_start(SESSION) == WINDOW_START


def test_the_bar_request_reaches_back_252_sessions_before_the_window() -> None:
    assert adv_request_start(SESSION) == REQUEST_START
    assert sessions_before(SESSION, ADV_LOOKBACK_SESSIONS + ADV_SESSIONS)[0] == REQUEST_START
    assert sessions_before(WINDOW_START, ADV_LOOKBACK_SESSIONS)[0] == REQUEST_START


def test_the_request_start_matches_a_hand_listed_holiday_count() -> None:
    """An independent check on the 272-session walk: weekdays minus closures."""
    counted = 0
    cursor = SESSION
    while counted < ADV_LOOKBACK_SESSIONS + ADV_SESSIONS:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5 and cursor not in HAND_LISTED_CLOSURES:
            counted += 1
    assert cursor == REQUEST_START


def test_the_lookback_walk_reaches_a_full_year_but_the_window_walk_stays_short() -> None:
    """Two bounds, not one: the 272-session walk needs ~395 calendar days, and
    widening the 20-session window's bound to match would let a date far
    outside the published schedule find a window instead of raising."""
    weekdays = lambda day: day.weekday() < 5  # noqa: E731
    # 272 weekday sessions span ~380 days: inside the lookback bound.
    assert adv_request_start(SESSION, is_session=weekdays) < WINDOW_START
    # A calendar with no sessions in the last 200 days: the window walk (bound
    # 180) must raise; it would not with the lookback's bound.
    gap_start = SESSION - timedelta(days=200)
    with pytest.raises(ValueError, match="sessions"):
        adv_window(SESSION, is_session=lambda day: weekdays(day) and day < gap_start)


def test_a_calendar_that_cannot_reach_the_lookback_raises() -> None:
    with pytest.raises(ValueError, match="sessions"):
        adv_request_start(SESSION, is_session=lambda day: False)


def test_a_half_day_is_a_session_and_a_holiday_is_not() -> None:
    """Friday 28 Nov 2025 closed at 13:00 and is a session; Thanksgiving is not.

    The half-day's bar carries 20 extra shares. ADV 1,000,001 proves it was
    counted and the holiday did not take a zero-volume slot in the divisor.
    """
    session = date(2025, 12, 5)
    window = adv_window(session)
    assert date(2025, 11, 28) in window
    assert date(2025, 11, 27) not in window
    volumes = [1_000_020 if day == date(2025, 11, 28) else 1_000_000 for day in window]
    bars = history(20, volumes=volumes, before=session)
    result = assess(daily_bars=bars, session_date=session)
    assert result.sessions_available == 20
    assert result.avg_volume_20d == 1_000_001
    assert result.passes is True


def test_a_halted_name_is_not_judged_on_its_pre_halt_tape() -> None:
    """Six weeks without a bar, still listed with has_options: 20+ bars, all old."""
    halted_from = sessions_before(SESSION, 30)[0]
    bars = history(40, volume=5_000_000, last_close="42", before=halted_from)
    result = assess(daily_bars=bars)
    # Established (its bars predate the window), so the divisor is all 20.
    assert result.sessions_available == 20
    # Every window session is a no-trade session: zero volume, not the old average.
    assert result.avg_volume_20d == 0
    # The close is still the newest bar's, so the row shows what it was judged on.
    assert result.last_close == Decimal("42")
    assert result.failures == (TradeabilityFailure.STALE_BARS, TradeabilityFailure.LOW_VOLUME)


def test_missing_only_the_previous_session_is_stale() -> None:
    bars = history(25)[:-1]
    result = assess(daily_bars=bars)
    assert result.sessions_available == 20
    assert result.failures == (TradeabilityFailure.STALE_BARS,)


def test_a_newest_bar_on_a_non_session_day_is_stale_not_trusted() -> None:
    """A Saturday bar is not the previous session: the calendar and the tape disagree."""
    saturday = date(2026, 9, 19)
    assert nyse_session_close(saturday) is None
    bars = history(25, before=date(2026, 9, 21))[1:] + [bar(saturday)]
    result = assess(daily_bars=bars, session_date=date(2026, 9, 21))
    assert TradeabilityFailure.STALE_BARS in result.failures


def test_a_session_with_no_bar_counts_as_zero_volume() -> None:
    """A thin name is averaged over all 20 sessions, not only the days it traded.

    Nineteen bars of 1,050,000 in the window: 19,950,000 / 20 = 997,500, which
    fails. Averaged over the bars it would be 1,050,000 and pass.
    """
    bars = history(25, volume=1_050_000)
    gap = sessions_before(SESSION, 10)[0]
    bars = [item for item in bars if item.at.astimezone(NYSE_TZ).date() != gap]
    result = assess(daily_bars=bars)
    assert result.sessions_available == 20
    assert result.avg_volume_20d == 997_500
    assert result.failures == (TradeabilityFailure.LOW_VOLUME,)


def test_no_bar_on_the_first_window_session_but_an_older_one_is_established() -> None:
    bars = history(25, volume=2_000_000)
    bars = [item for item in bars if item.at.astimezone(NYSE_TZ).date() != WINDOW_START]
    result = assess(daily_bars=bars)
    assert result.sessions_available == 20
    assert result.avg_volume_20d == 1_900_000
    assert result.passes is True


def test_the_calendar_is_a_parameter() -> None:
    """A weekday-only fake that knows no holidays counts Labor Day as a session."""
    result = assess(daily_bars=history(25), is_session=lambda day: day.weekday() < 5)
    assert adv_window_start(SESSION, is_session=lambda day: day.weekday() < 5) == date(
        2026, 8, 27
    )
    # Labor Day has no bar, so under the fake it is a zero-volume window session.
    assert result.sessions_available == 20
    assert result.avg_volume_20d == 1_900_000


def test_a_calendar_that_cannot_answer_raises_rather_than_shortening_the_window() -> None:
    with pytest.raises(ValueError, match="sessions"):
        adv_window(SESSION, is_session=lambda day: False)


# --- every failure is reported, in a stable order -----------------------------


def test_every_failing_check_is_reported_first_one_first() -> None:
    result = assess(
        has_options=False,
        standard_root=False,
        daily_bars=history(20, volume=10, last_close="1"),
    )
    assert result.failures == (
        TradeabilityFailure.NO_OPTIONS,
        TradeabilityFailure.NO_STANDARD_CONTRACT,
        TradeabilityFailure.LOW_VOLUME,
        TradeabilityFailure.LOW_CLOSE,
    )
    assert result.first_failure is TradeabilityFailure.NO_OPTIONS


def test_the_ticker_is_normalised() -> None:
    result = assess_tradeability(
        " brk/b ",
        has_options=True,
        standard_root=True,
        daily_bars=history(20, symbol="BRK.B"),
        session_date=SESSION,
    )
    assert result.ticker == "BRK.B"
    assert result.passes is True


def test_a_malformed_ticker_fails_closed_rather_than_raising() -> None:
    result = assess_tradeability(
        "NOT A TICKER", has_options=True, standard_root=True, daily_bars=[], session_date=SESSION
    )
    assert result.passes is False
    assert result.first_failure is TradeabilityFailure.MALFORMED_TICKER


@pytest.mark.parametrize("ticker", ["", "   ", "123", "7"])
def test_an_empty_or_all_digit_ticker_is_malformed_not_an_adjusted_root(ticker: str) -> None:
    """Neither has a root for it to be an adjustment of."""
    result = assess_tradeability(
        ticker, has_options=True, standard_root=True, daily_bars=[], session_date=SESSION
    )
    assert result.first_failure is TradeabilityFailure.MALFORMED_TICKER
    assert TradeabilityFailure.ADJUSTED_ROOT not in result.failures
    assert is_adjusted_root_ticker(ticker) is False


def test_the_result_is_deterministic() -> None:
    bars = history(22, volumes=[1_000_000 + i for i in range(22)])
    assert assess(daily_bars=bars) == assess(daily_bars=list(bars))


# --- the ticker's adjusted-root detection -------------------------------------


@pytest.mark.parametrize("ticker", ["AAPL1", "GME1", "XRX1", "ABC12"])
def test_a_numeric_suffix_is_an_adjusted_root(ticker: str) -> None:
    assert is_adjusted_root_ticker(ticker) is True


@pytest.mark.parametrize("ticker", ["AAPL", "BRK.B", "A", "GOOGL"])
def test_a_plain_equity_symbol_is_not_an_adjusted_root(ticker: str) -> None:
    assert is_adjusted_root_ticker(ticker) is False


# --- the standard-contract check ----------------------------------------------


def contract(root: str, *, underlying: str = "ACME", size: str = "100") -> OptionContract:
    return OptionContract(
        symbol=f"{root}261016C00050000",
        underlying_symbol=underlying,
        root_symbol=root,
        expiration=date(2026, 10, 16),
        option_type=OptionType.CALL,
        strike=Decimal(50),
        style="american",
        multiplier=Decimal(100),
        size=Decimal(size),
        open_interest=None,
        open_interest_date=None,
        close_price=None,
        close_price_date=None,
        tradable=True,
        status="active",
        name="ACME call",
    )


def test_a_contract_on_the_ticker_root_with_size_100_is_standard() -> None:
    assert has_standard_contract("ACME", [contract("ACME1"), contract("ACME")]) is True


def test_only_adjusted_contracts_is_not_standard() -> None:
    assert has_standard_contract("ACME", [contract("ACME1"), contract("ACME2")]) is False


def test_a_root_match_with_a_size_other_than_100_is_not_standard() -> None:
    assert has_standard_contract("ACME", [contract("ACME", size="10")]) is False


def test_a_contract_on_another_underlying_is_not_evidence() -> None:
    assert has_standard_contract("ACME", [contract("ACME", underlying="OTHER")]) is False


def test_a_class_share_never_has_a_standard_contract() -> None:
    """OCC writes BRK.B's root as BRKB: a known, documented exclusion, failing closed."""
    assert has_standard_contract("BRK.B", [contract("BRKB", underlying="BRK.B")]) is False


def test_no_contracts_is_not_standard() -> None:
    assert has_standard_contract("ACME", []) is False


def test_the_contract_check_ignores_case() -> None:
    assert has_standard_contract("acme", [contract("ACME")]) is True


@pytest.mark.parametrize(
    ("session_date", "previous_session"),
    [
        (date(2026, 9, 26), date(2026, 9, 25)),  # a Saturday
        (date(2026, 9, 7), date(2026, 9, 4)),  # Labor Day
    ],
)
def test_a_non_session_date_windows_back_to_the_last_session(
    session_date: date, previous_session: date
) -> None:
    """The window ends on the last session before a weekend or holiday date,
    exactly as it would for the next session day."""
    window = adv_window(session_date)
    assert window[-1] == previous_session
    assert len(window) == 20
