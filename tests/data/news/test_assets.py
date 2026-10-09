"""The in-memory asset directory holder (decision 21's daily ``has_options`` list).

What the ingest's tag filter, the watch routes' validation and the
tradeability refresh read. The contract under test: nothing before the first
successful fetch, and a failed refresh never replaces a good directory with
nothing.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from corollary.data.news.assets import MAX_DIRECTORY_AGE, AssetDirectoryHolder
from corollary.data.providers.interface import AssetDirectory, EquityAsset, ProviderError

pytestmark = pytest.mark.asyncio

T0 = datetime(2026, 9, 24, 11, 30, tzinfo=timezone.utc)


def _asset(symbol: str, *, has_options: bool = True) -> EquityAsset:
    return EquityAsset(
        symbol=symbol, name=f"{symbol} Inc.", tradable=True, has_options=has_options,
        exchange="NASDAQ",
    )


class FakeSource:
    """Answers each ``active_equities`` call from a queue of results or errors."""

    def __init__(self, *answers: AssetDirectory | BaseException) -> None:
        self._answers = list(answers)
        self.calls = 0

    async def active_equities(self) -> AssetDirectory:
        self.calls += 1
        answer = self._answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


async def test_nothing_before_the_first_successful_fetch():
    holder = AssetDirectoryHolder(now=Clock(T0))
    assert holder.current() is None
    assert holder.fetched_at is None
    assert holder.last_failure is None


async def test_the_first_fetch_is_held_with_its_time():
    directory = AssetDirectory(assets=(_asset("AAPL"), _asset("AAA", has_options=False)))
    holder = AssetDirectoryHolder(now=Clock(T0))

    returned = await holder.refresh(FakeSource(directory))

    assert returned is directory
    assert holder.current() is directory
    assert holder.fetched_at == T0
    assert holder.last_failure is None


async def test_a_failed_refresh_keeps_the_previous_directory_and_records_why(caplog):
    good = AssetDirectory(assets=(_asset("AAPL"),))
    clock = Clock(T0)
    holder = AssetDirectoryHolder(now=clock)
    source = FakeSource(good, ProviderError("GET /v2/assets failed: 503"))
    await holder.refresh(source)

    clock.now = T0 + timedelta(days=1)
    with caplog.at_level(logging.WARNING), pytest.raises(ProviderError):
        await holder.refresh(source)

    # The good directory survives, still stamped with when it was fetched, so
    # a caller can say "asset list stale since 07:30".
    assert holder.current() is good
    assert holder.fetched_at == T0
    failure = holder.last_failure
    assert failure is not None
    assert failure.at == T0 + timedelta(days=1)
    assert "503" in failure.error
    assert any(
        getattr(r, "event", None) == "asset_directory_refresh_failed" for r in caplog.records
    )


async def test_a_failed_first_refresh_leaves_nothing_rather_than_something_empty():
    holder = AssetDirectoryHolder(now=Clock(T0))
    with pytest.raises(ProviderError):
        await holder.refresh(FakeSource(ProviderError("down")))
    assert holder.current() is None
    assert holder.fetched_at is None
    assert holder.last_failure is not None


async def test_an_empty_asset_list_is_refused_and_the_previous_one_kept():
    # An empty list would empty the optionable set and fail every tag in
    # silence; the vendor never lists zero active equities.
    good = AssetDirectory(assets=(_asset("AAPL"),))
    clock = Clock(T0)
    holder = AssetDirectoryHolder(now=clock)
    await holder.refresh(FakeSource(good))

    clock.now = T0 + timedelta(hours=1)
    with pytest.raises(ProviderError, match="empty"):
        await holder.refresh(FakeSource(AssetDirectory(assets=())))

    assert holder.current() is good
    assert holder.fetched_at == T0
    assert holder.last_failure is not None


async def test_a_directory_with_no_optionable_name_is_refused_and_the_previous_one_kept(caplog):
    # A vendor schema change dropping ``attributes`` reads every asset as
    # has_options=False: a non-empty list whose optionable set is empty. Held,
    # it would cache every candidate as "no options" for the session.
    good = AssetDirectory(assets=(_asset("AAPL"),))
    clock = Clock(T0)
    holder = AssetDirectoryHolder(now=clock)
    await holder.refresh(FakeSource(good))

    optionless = AssetDirectory(
        assets=(_asset("AAPL", has_options=False), _asset("MSFT", has_options=False)),
        missing_attributes=2,
    )
    clock.now = T0 + timedelta(hours=1)
    with caplog.at_level(logging.WARNING), pytest.raises(ProviderError, match="options"):
        await holder.refresh(FakeSource(optionless))

    assert holder.current() is good
    assert holder.fetched_at == T0
    failure = holder.last_failure
    assert failure is not None
    assert failure.at == T0 + timedelta(hours=1)
    assert "options" in failure.error
    assert any(
        getattr(r, "event", None) == "asset_directory_refresh_failed" for r in caplog.records
    )


async def test_a_first_fetch_with_no_optionable_name_leaves_nothing():
    holder = AssetDirectoryHolder(now=Clock(T0))
    with pytest.raises(ProviderError):
        await holder.refresh(
            FakeSource(AssetDirectory(assets=(_asset("AAPL", has_options=False),)))
        )
    assert holder.current() is None
    assert holder.fetched_at is None
    assert holder.last_failure is not None


async def test_a_later_success_replaces_the_directory_and_clears_the_failure():
    first = AssetDirectory(assets=(_asset("AAPL"),))
    second = AssetDirectory(assets=(_asset("AAPL"), _asset("MSFT")))
    clock = Clock(T0)
    holder = AssetDirectoryHolder(now=clock)
    source = FakeSource(first, ProviderError("down"), second)
    await holder.refresh(source)
    clock.now = T0 + timedelta(hours=1)
    with pytest.raises(ProviderError):
        await holder.refresh(source)
    clock.now = T0 + timedelta(hours=2)
    await holder.refresh(source)

    assert holder.current() is second
    assert holder.fetched_at == T0 + timedelta(hours=2)
    assert holder.last_failure is None


async def test_a_naive_clock_is_refused():
    holder = AssetDirectoryHolder(now=lambda: datetime(2026, 9, 24, 11, 30))
    with pytest.raises(ValueError):
        await holder.refresh(FakeSource(AssetDirectory(assets=(_asset("AAPL"),))))
    assert holder.current() is None


async def test_staleness_before_the_first_fetch_is_stale_with_no_age():
    holder = AssetDirectoryHolder(now=Clock(T0))
    assert holder.age() is None
    assert holder.is_stale() is True


async def test_staleness_is_measured_from_the_last_good_fetch_inclusive_at_the_boundary():
    clock = Clock(T0)
    holder = AssetDirectoryHolder(now=clock)
    await holder.refresh(FakeSource(AssetDirectory(assets=(_asset("AAPL"),))))

    clock.now = T0 + MAX_DIRECTORY_AGE
    assert holder.age() == MAX_DIRECTORY_AGE
    assert holder.is_stale() is False

    clock.now = T0 + MAX_DIRECTORY_AGE + timedelta(seconds=1)
    assert holder.is_stale() is True


async def test_a_failed_refresh_does_not_freshen_the_directory():
    clock = Clock(T0)
    holder = AssetDirectoryHolder(now=clock)
    source = FakeSource(AssetDirectory(assets=(_asset("AAPL"),)), ProviderError("down"))
    await holder.refresh(source)
    clock.now = T0 + MAX_DIRECTORY_AGE + timedelta(hours=1)
    with pytest.raises(ProviderError):
        await holder.refresh(source)
    assert holder.is_stale() is True


async def test_the_default_staleness_ceiling_is_one_day_plus_slack():
    # Refreshed daily at 07:30 ET: a directory is due a replacement every 24h,
    # so 26h is "the last refresh ran and a little slack", not "one was missed".
    assert MAX_DIRECTORY_AGE == timedelta(hours=26)
