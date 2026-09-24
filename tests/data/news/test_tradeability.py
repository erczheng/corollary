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
    ADV_SESSIONS,
    MIN_AVG_DAILY_VOLUME,
    MIN_LAST_CLOSE,
    MIN_SESSIONS_OF_HISTORY,
    STANDARD_CONTRACT_SIZE,
    TradeabilityFailure,
    adv_window,
    adv_window_start,
    assess_tradeability,
    has_standard_contract,
    is_adjusted_root_ticker,
)
from corollary.data.providers.interface import Bar, OptionContract, OptionType

SESSION = date(2026, 9, 24)

#: The 20th NYSE session before SESSION, counted by hand: Sep 23 back to
#: Aug 26, skipping weekends and Labor Day (Sep 7). An independent check on
#: the calendar walk, not derived from it.
WINDOW_START = date(2026, 8, 26)


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
    assert MIN_SESSIONS_OF_HISTORY == 20
    assert ADV_SESSIONS == 20
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
    # Window coverage, not history length: 25 bars, 20 of them in the window.
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


def test_19_sessions_of_history_fails_without_a_partial_average() -> None:
    result = assess(daily_bars=history(19, volume=5_000_000))
    assert result.sessions_available == 19
    assert result.failures == (TradeabilityFailure.INSUFFICIENT_HISTORY,)
    # A recent IPO is not judged on a partial average, and none is recorded.
    assert result.avg_volume_20d is None
    # The close is a fact about the last bar and is still reported.
    assert result.last_close == Decimal("50")


def test_no_history_at_all_fails_with_nothing_to_report() -> None:
    result = assess(daily_bars=[])
    assert result.sessions_available == 0
    assert result.avg_volume_20d is None
    assert result.last_close is None
    assert result.failures == (TradeabilityFailure.INSUFFICIENT_HISTORY,)


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
    assert result.sessions_available == 0
    # Every window session is a no-trade session: zero volume, not the old average.
    assert result.avg_volume_20d == 0
    # The close is still the newest bar's, so the row shows what it was judged on.
    assert result.last_close == Decimal("42")
    assert result.failures == (TradeabilityFailure.STALE_BARS, TradeabilityFailure.LOW_VOLUME)


def test_missing_only_the_previous_session_is_stale() -> None:
    bars = history(25)[:-1]
    result = assess(daily_bars=bars)
    assert result.sessions_available == 19
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
    assert result.sessions_available == 19
    assert result.avg_volume_20d == 997_500
    assert result.failures == (TradeabilityFailure.LOW_VOLUME,)


def test_no_bar_on_the_first_window_session_but_an_older_one_still_has_history() -> None:
    bars = history(25, volume=2_000_000)
    bars = [item for item in bars if item.at.astimezone(NYSE_TZ).date() != WINDOW_START]
    result = assess(daily_bars=bars)
    assert result.sessions_available == 19
    assert result.avg_volume_20d == 1_900_000
    assert result.passes is True


def test_the_calendar_is_a_parameter() -> None:
    """A weekday-only fake that knows no holidays counts Labor Day as a session."""
    result = assess(daily_bars=history(25), is_session=lambda day: day.weekday() < 5)
    assert adv_window_start(SESSION, is_session=lambda day: day.weekday() < 5) == date(
        2026, 8, 27
    )
    # Labor Day has no bar, so under the fake it is a zero-volume window session.
    assert result.sessions_available == 19
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
