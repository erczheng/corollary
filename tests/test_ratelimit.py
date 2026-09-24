"""The two-bucket request budget.

The test that matters most here is ``test_the_two_alpaca_hosts_are_independent``:
Alpaca meters ``data.alpaca.markets`` and ``paper-api.alpaca.markets``
separately at 200/min each, and a single shared bucket would silently halve
the budget. Step 3 hits both hosts in one call path — the option *chain*
comes from ``data.``, the option *contracts* that carry ``multiplier`` and
``root_symbol`` come from ``paper-api.`` — so this is not a hypothetical.
"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from corollary.ratelimit import (
    DEFAULT_PER_HOST_BUDGETS,
    FINNHUB_HOST,
    FINNHUB_REQUESTS_PER_MINUTE,
    FRED_HOST,
    MASSIVE_HOST,
    STOCKTWITS_HOST,
    ALPACA_DATA_HOST,
    ALPACA_PAPER_TRADING_HOST,
    DEFAULT_REQUESTS_PER_MINUTE,
    HostBudget,
    HostRateLimiter,
    default_limiter,
    TokenBucket,
)


class FakeClock:
    """A monotonic clock that only moves when a sleep asks it to.

    Real time in a rate-limiter test is how a suite gets slow and flaky. The
    sleeper advances the clock by exactly the interval requested, so a
    ``TokenBucket`` that waits for a refill gets one instantly and the test
    still proves it waited.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def bucket(
    clock: FakeClock, capacity: int = 4, per_seconds: float = 60.0
) -> TokenBucket:
    return TokenBucket(capacity, per_seconds, clock=clock, sleep=clock.sleep)


@pytest.mark.asyncio
async def test_a_full_bucket_grants_immediately(clock: FakeClock) -> None:
    b = bucket(clock, capacity=3)
    for _ in range(3):
        await b.acquire()
    assert clock.slept == []
    assert b.available == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_an_empty_bucket_waits_for_exactly_one_token(clock: FakeClock) -> None:
    # 4 per 60s is one token every 15s.
    b = bucket(clock, capacity=4, per_seconds=60.0)
    for _ in range(4):
        await b.acquire()
    await b.acquire()
    assert clock.slept == [pytest.approx(15.0)]


@pytest.mark.asyncio
async def test_tokens_refill_over_time_and_never_exceed_capacity(
    clock: FakeClock,
) -> None:
    b = bucket(clock, capacity=4, per_seconds=60.0)
    for _ in range(4):
        await b.acquire()
    clock.advance(30.0)
    assert b.available == pytest.approx(2.0)
    clock.advance(10_000.0)
    assert b.available == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_available_does_not_consume(clock: FakeClock) -> None:
    b = bucket(clock, capacity=2)
    assert b.available == pytest.approx(2.0)
    assert b.available == pytest.approx(2.0)
    await b.acquire()
    assert b.available == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_a_request_larger_than_the_bucket_raises_rather_than_hanging(
    clock: FakeClock,
) -> None:
    b = bucket(clock, capacity=2)
    with pytest.raises(ValueError, match="capacity"):
        await b.acquire(3)


def test_a_nonsense_bucket_is_refused(clock: FakeClock) -> None:
    with pytest.raises(ValueError):
        TokenBucket(0, 60.0, clock=clock, sleep=clock.sleep)
    with pytest.raises(ValueError):
        TokenBucket(10, 0.0, clock=clock, sleep=clock.sleep)


@pytest.mark.asyncio
async def test_the_two_alpaca_hosts_are_independent(clock: FakeClock) -> None:
    """Draining the data host must not cost the trading host a single request.

    A shared bucket passes every other test in this file and fails this one.
    """
    limiter = HostRateLimiter(
        requests_per_minute=200, clock=clock, sleep=clock.sleep
    )

    for _ in range(200):
        await limiter.acquire(ALPACA_DATA_HOST)

    assert limiter.bucket_for(ALPACA_DATA_HOST).available == pytest.approx(0.0)
    assert limiter.bucket_for(ALPACA_PAPER_TRADING_HOST).available == pytest.approx(
        200.0
    )
    assert clock.slept == []

    # And the trading host still grants its full budget without waiting.
    for _ in range(200):
        await limiter.acquire(ALPACA_PAPER_TRADING_HOST)
    assert clock.slept == []


def test_the_same_host_always_gets_the_same_bucket(clock: FakeClock) -> None:
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    assert limiter.bucket_for(ALPACA_DATA_HOST) is limiter.bucket_for(
        ALPACA_DATA_HOST
    )
    assert limiter.bucket_for(ALPACA_DATA_HOST) is not limiter.bucket_for(
        ALPACA_PAPER_TRADING_HOST
    )


def test_host_matching_ignores_case(clock: FakeClock) -> None:
    """DNS is case-insensitive; two spellings of one host are one budget."""
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    assert limiter.bucket_for("Data.Alpaca.Markets") is limiter.bucket_for(
        ALPACA_DATA_HOST
    )


def test_the_default_budget_is_alpacas_documented_ceiling(clock: FakeClock) -> None:
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    assert DEFAULT_REQUESTS_PER_MINUTE == 200
    assert limiter.bucket_for(ALPACA_DATA_HOST).available == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_concurrent_acquires_do_not_oversubscribe() -> None:
    """Two coroutines racing on one bucket must not both spend the last token.

    Uses the real event loop with a tiny period so the wait is measured in
    milliseconds; the assertion is on the token count, not on the clock.
    """
    b = TokenBucket(2, 0.05)
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep
    assert sleeper is asyncio.sleep  # the default, spelled out

    await asyncio.gather(*(b.acquire() for _ in range(4)))
    assert b.available <= 2.0


def test_the_default_limiter_is_one_budget_for_the_whole_process() -> None:
    """Two limiters against one server-side ceiling is over-spending by two.

    The mirror image of the bug this module's docstring is written against.
    One shared bucket for two hosts under-spends by half and looks slow; two
    limiters for one host over-spends by double and looks fine until the 429s
    arrive, at which point the local budget says there is nothing wrong.
    """
    assert default_limiter() is default_limiter()
    assert default_limiter().bucket_for(ALPACA_DATA_HOST) is (
        default_limiter().bucket_for(ALPACA_DATA_HOST)
    )


def test_the_default_limiter_carries_the_documented_ceiling() -> None:
    assert default_limiter().bucket_for(
        ALPACA_PAPER_TRADING_HOST
    ).capacity == float(DEFAULT_REQUESTS_PER_MINUTE)


def test_a_second_vendors_host_carries_its_own_smaller_ceiling(
    clock: FakeClock,
) -> None:
    """Finnhub's free tier is 60/min, not Alpaca's 200.

    One limiter per process is the rule (see above), so the second vendor
    cannot be given its own limiter without re-creating the over-spending bug
    the shared one exists to prevent. The per-host override is how both facts
    hold at once.
    """
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    assert FINNHUB_REQUESTS_PER_MINUTE == 60
    assert limiter.bucket_for(FINNHUB_HOST).capacity == 60.0
    assert limiter.bucket_for(ALPACA_DATA_HOST).capacity == 200.0


def test_the_default_limiter_meters_finnhub_at_sixty() -> None:
    assert default_limiter().bucket_for(FINNHUB_HOST).capacity == float(
        FINNHUB_REQUESTS_PER_MINUTE
    )


def test_an_explicit_per_host_budget_overrides_the_default(clock: FakeClock) -> None:
    limiter = HostRateLimiter(
        requests_per_minute=10_000,
        per_host={"example.test": 3},  # replaces the table, does not extend it
        clock=clock,
        sleep=clock.sleep,
    )
    assert limiter.bucket_for("EXAMPLE.TEST").capacity == 3.0
    assert limiter.bucket_for("other.test").capacity == 10_000.0
    assert limiter.bucket_for(FINNHUB_HOST).capacity == 10_000.0


# --------------------------------------------------------------------------
# Phase 3 decision 15: three new hosts, one of them metered per hour
# --------------------------------------------------------------------------


def test_the_phase_three_hosts_are_registered_at_their_documented_rates(
    clock: FakeClock,
) -> None:
    """Massive 5/min, FRED 120/min, StockTwits 200/hour -- in the shared table.

    In the shared limiter rather than in each client: two limiters against
    one server-side ceiling over-spend by double and look fine locally.
    """
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    massive = limiter.bucket_for(MASSIVE_HOST)
    fred = limiter.bucket_for(FRED_HOST)
    stocktwits = limiter.bucket_for(STOCKTWITS_HOST)

    assert (MASSIVE_HOST, FRED_HOST, STOCKTWITS_HOST) == (
        "api.massive.com",
        "api.stlouisfed.org",
        "api.stocktwits.com",
    )
    assert (massive.capacity, massive.window_seconds) == (5.0, 60.0)
    assert (fred.capacity, fred.window_seconds) == (120.0, 60.0)
    assert (stocktwits.capacity, stocktwits.window_seconds) == (200.0, 3600.0)


def test_the_default_limiter_carries_the_phase_three_hosts() -> None:
    assert default_limiter().bucket_for(STOCKTWITS_HOST).window_seconds == 3600.0
    assert default_limiter().bucket_for(MASSIVE_HOST).capacity == 5.0


def test_the_existing_hosts_keep_their_per_minute_windows(clock: FakeClock) -> None:
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    for host, capacity in (
        (ALPACA_DATA_HOST, 200.0),
        (ALPACA_PAPER_TRADING_HOST, 200.0),
        (FINNHUB_HOST, 60.0),
    ):
        assert limiter.bucket_for(host).capacity == capacity
        assert limiter.bucket_for(host).window_seconds == 60.0


@pytest.mark.asyncio
async def test_an_hourly_bucket_waits_past_its_ceiling_and_refills_over_the_hour(
    clock: FakeClock,
) -> None:
    """The 201st StockTwits request in an hour waits for one refill.

    200 per 3600s refills a token every 18 seconds -- not every 0.3 seconds,
    which is what the same 200 read as per-minute would do, and which is the
    sixty-fold over-spend a per-minute-only bucket would commit against
    StockTwits.
    """
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    for _ in range(200):
        await limiter.acquire(STOCKTWITS_HOST)
    assert clock.slept == []

    await limiter.acquire(STOCKTWITS_HOST)
    assert clock.slept == [pytest.approx(18.0)]

    bucket = limiter.bucket_for(STOCKTWITS_HOST)
    clock.advance(1800.0)
    assert bucket.available == pytest.approx(100.0)
    clock.advance(3600.0)
    assert bucket.available == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_a_per_minute_bucket_still_refills_at_its_old_rate(
    clock: FakeClock,
) -> None:
    """The data bucket's arithmetic is unchanged: 200/min is 0.3s a token."""
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    for _ in range(200):
        await limiter.acquire(ALPACA_DATA_HOST)
    await limiter.acquire(ALPACA_DATA_HOST)
    assert clock.slept == [pytest.approx(0.3)]


def test_a_per_host_budget_may_name_its_window(clock: FakeClock) -> None:
    """A bare int in ``per_host`` still means per minute; a budget names its own."""
    limiter = HostRateLimiter(
        per_host={"minute.test": 3, "hour.test": HostBudget(7, 3600.0)},
        clock=clock,
        sleep=clock.sleep,
    )
    assert limiter.bucket_for("minute.test").window_seconds == 60.0
    hourly = limiter.bucket_for("HOUR.test")
    assert (hourly.capacity, hourly.window_seconds) == (7.0, 3600.0)


@pytest.mark.parametrize(
    ("requests", "window"), [(0, 60.0), (-1, 60.0), (5, 0.0), (5, -60.0)]
)
def test_a_nonsense_host_budget_is_refused(requests: int, window: float) -> None:
    with pytest.raises(ValueError):
        HostBudget(requests, window)


def test_the_default_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        DEFAULT_PER_HOST_BUDGETS[STOCKTWITS_HOST] = HostBudget(10_000)  # type: ignore[index]
