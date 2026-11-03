"""The two-bucket request budget.

The test that matters most here is ``test_the_two_alpaca_hosts_are_independent``:
Alpaca meters ``data.alpaca.markets`` and ``paper-api.alpaca.markets``
separately at 200/min each, and a single shared bucket would silently halve
the budget. Step 3 hits both hosts in one call path — the option *chain*
comes from ``data.``, the option *contracts* that carry ``multiplier`` and
``root_symbol`` come from ``paper-api.`` — so this is not a hypothetical.
"""

import asyncio
import bisect
from collections.abc import Awaitable, Callable

import pytest

from corollary.ratelimit import (
    DEFAULT_PER_HOST_BUDGETS,
    FINNHUB_HOST,
    FINNHUB_REQUESTS_PER_MINUTE,
    FRED_HOST,
    MASSIVE_HOST,
    SEC_BUCKET,
    SEC_DATA_HOST,
    SEC_REQUESTS_PER_SECOND,
    SEC_WWW_HOST,
    STOCKTWITS_HOST,
    ALPACA_DATA_HOST,
    ALPACA_PAPER_TRADING_HOST,
    DEFAULT_REQUESTS_PER_MINUTE,
    HostBudget,
    HostRateLimiter,
    default_limiter,
    TokenBucket,
    ceiling_budget,
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
    data = limiter.bucket_for(ALPACA_DATA_HOST)
    trading = limiter.bucket_for(ALPACA_PAPER_TRADING_HOST)

    for _ in range(int(data.capacity)):
        await limiter.acquire(ALPACA_DATA_HOST)

    assert data.available == pytest.approx(0.0)
    assert trading.available == pytest.approx(trading.capacity)
    assert clock.slept == []

    # And the trading host still grants its full burst without waiting.
    for _ in range(int(trading.capacity)):
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
    """200/min is the *server's* ceiling; the bucket is sized to stay under it.

    A bucket of depth 200 refilling at 200/min admits 400 in a rolling minute
    (C + r*T). Depth 10 refilling at 180/min admits at most 190.
    """
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    assert DEFAULT_REQUESTS_PER_MINUTE == 200
    data = limiter.bucket_for(ALPACA_DATA_HOST)
    assert data.capacity == 10.0
    assert data.refill_per_window == 180.0
    assert data.worst_case_per_window == 190.0
    assert data.available == pytest.approx(10.0)


@pytest.mark.parametrize("host", [ALPACA_DATA_HOST, ALPACA_PAPER_TRADING_HOST])
@pytest.mark.asyncio
async def test_no_rolling_minute_ever_sees_more_than_the_alpaca_ceiling(
    clock: FakeClock, host: str
) -> None:
    """The 2026-11-02 SPDR catch-up 429 (Fair Isaac, CUSIP lookup #233).

    The old bucket started full at 200 and refilled at 200/min, so a
    sequential burst from a cold start was admitted ~233 requests in well
    under a minute against Alpaca's 200/min -- the "2 x capacity" hole the
    module docstring names. Measured here from a full bucket (the worst
    start), every window closed at both ends.
    """
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    stamps: list[float] = []
    for _ in range(600):
        await limiter.acquire(host)
        stamps.append(clock.now)
    worst = max(
        bisect.bisect_right(stamps, start + 60.0 + 1e-9) - i
        for i, start in enumerate(stamps)
    )
    assert worst <= 190
    assert worst < DEFAULT_REQUESTS_PER_MINUTE
    # Not starved either: after the burst of 10, the steady rate is 3/s.
    assert clock.now == pytest.approx((600 - 10) / 3.0)


@pytest.mark.parametrize("ceiling", [5, 200, 10_000])
@pytest.mark.asyncio
async def test_a_configured_ceiling_is_never_exceeded_in_any_rolling_minute(
    ceiling: int,
) -> None:
    """``requests_per_minute`` is the server's number, Basic or Algo Trader Plus."""
    clock = FakeClock()
    limiter = HostRateLimiter(ceiling, per_host={}, clock=clock, sleep=clock.sleep)
    bucket_ = limiter.bucket_for(ALPACA_PAPER_TRADING_HOST)
    assert bucket_.worst_case_per_window < ceiling
    stamps: list[float] = []
    for _ in range(ceiling * 3):
        await limiter.acquire(ALPACA_PAPER_TRADING_HOST)
        stamps.append(clock.now)
    worst = max(
        bisect.bisect_right(stamps, start + 60.0 + 1e-9) - i
        for i, start in enumerate(stamps)
    )
    assert worst < ceiling


def test_a_ceiling_too_small_to_keep_under_is_refused() -> None:
    with pytest.raises(ValueError, match="ceiling"):
        ceiling_budget(2)


def test_a_burst_separate_from_the_rate_bounds_the_worst_window(clock: FakeClock) -> None:
    b = TokenBucket(5, 60.0, refill=55, clock=clock, sleep=clock.sleep)
    assert b.capacity == 5.0
    assert b.refill_per_window == 55.0
    assert b.worst_case_per_window == 60.0
    clock.advance(1.0)
    assert b.available == pytest.approx(5.0)  # never above the depth


@pytest.mark.parametrize("burst", [0, -1])
def test_a_nonsense_burst_is_refused(burst: int) -> None:
    with pytest.raises(ValueError, match="burst"):
        HostBudget(10, 60.0, burst=burst)


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
    trading = default_limiter().bucket_for(ALPACA_PAPER_TRADING_HOST)
    assert trading.worst_case_per_window < float(DEFAULT_REQUESTS_PER_MINUTE)
    assert trading.refill_per_window == 180.0


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
    assert limiter.bucket_for(ALPACA_DATA_HOST).worst_case_per_window == 190.0


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
    # Hosts outside the table take the ceiling-derived budget: depth 500,
    # refill 9,000/min, worst rolling minute 9,500 against 10,000.
    assert limiter.bucket_for("other.test").capacity == 500.0
    assert limiter.bucket_for("other.test").refill_per_window == 9_000.0
    assert limiter.bucket_for(FINNHUB_HOST).capacity == 500.0


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
    # Alpaca's are depth 10 refilling 180/min (ceiling_budget(200)); Finnhub's
    # table entry keeps its old depth-equals-refill shape.
    for host, capacity, refill in (
        (ALPACA_DATA_HOST, 10.0, 180.0),
        (ALPACA_PAPER_TRADING_HOST, 10.0, 180.0),
        (FINNHUB_HOST, 60.0, 60.0),
    ):
        assert limiter.bucket_for(host).capacity == capacity
        assert limiter.bucket_for(host).refill_per_window == refill
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
async def test_the_data_bucket_refills_a_token_every_third_of_a_second_past_its_burst(
    clock: FakeClock,
) -> None:
    """180/min steady is a third of a second a token, once the burst of 10 is spent."""
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    for _ in range(10):
        await limiter.acquire(ALPACA_DATA_HOST)
    assert clock.slept == []
    await limiter.acquire(ALPACA_DATA_HOST)
    assert clock.slept == [pytest.approx(1 / 3)]


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


# --------------------------------------------------------------------------
# SEC EDGAR (Phase 3 step 4): two hosts, one fair-access ceiling
# --------------------------------------------------------------------------


def test_the_two_sec_hosts_share_one_bucket(clock: FakeClock) -> None:
    """SEC's 10/s fair-access ceiling is per *requester*, across its hosts.

    ``data.sec.gov`` (submissions) and ``www.sec.gov`` (the ticker map and
    the archive) are one budget. Two buckets would admit twice the rate the
    policy allows -- the over-spending mirror of Alpaca's two-bucket split.
    """
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    assert (SEC_DATA_HOST, SEC_WWW_HOST) == ("data.sec.gov", "www.sec.gov")
    shared = limiter.bucket_for(SEC_DATA_HOST)
    assert limiter.bucket_for(SEC_WWW_HOST) is shared
    assert limiter.bucket_for("WWW.SEC.GOV") is shared
    assert limiter.bucket_for(SEC_BUCKET) is shared
    assert limiter.bucket_for(ALPACA_DATA_HOST) is not shared


def test_the_sec_bucket_is_four_per_second() -> None:
    assert SEC_REQUESTS_PER_SECOND == 4
    budget = DEFAULT_PER_HOST_BUDGETS[SEC_BUCKET]
    assert (budget.requests, budget.window_seconds) == (4, 1.0)
    bucket = default_limiter().bucket_for(SEC_WWW_HOST)
    assert (bucket.capacity, bucket.window_seconds) == (4.0, 1.0)


@pytest.mark.asyncio
async def test_sec_requests_on_either_host_draw_down_the_same_tokens(
    clock: FakeClock,
) -> None:
    """Four immediately, split across the hosts; the fifth waits a quarter second."""
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    for host in (SEC_DATA_HOST, SEC_WWW_HOST, SEC_DATA_HOST, SEC_WWW_HOST):
        await limiter.acquire(host)
    assert clock.slept == []
    await limiter.acquire(SEC_WWW_HOST)
    assert clock.slept == [pytest.approx(0.25)]


@pytest.mark.asyncio
async def test_no_one_second_window_ever_sees_more_than_eight_sec_requests(
    clock: FakeClock,
) -> None:
    """The ceiling SEC states is 10/s; a token bucket's worst case is C + r*T.

    With C=4 and r=4/s, any window of T=1s admits at most 8 -- a full bucket
    spent at once plus a second's refill. Measured here over a burst from a
    full bucket (the worst start), alternating hosts, with every window
    closed at both ends so a request exactly one second later is counted.
    """
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    stamps: list[float] = []
    for i in range(60):
        await limiter.acquire(SEC_DATA_HOST if i % 2 else SEC_WWW_HOST)
        stamps.append(clock.now)
    worst = max(
        sum(1 for s in stamps if start <= s <= start + 1.0 + 1e-9) for start in stamps
    )
    assert worst == 8
    assert worst <= 10
    # Steady state is the stated rate: 60 requests take (60 - 4) / 4 seconds.
    assert clock.now == pytest.approx(14.0)


def test_a_per_host_budget_for_one_shared_sec_host_is_refused(clock: FakeClock) -> None:
    """A budget for ``www.sec.gov`` alone would be silently ignored -- the
    bucket is keyed on the shared name -- so it is refused, not dropped."""
    with pytest.raises(ValueError, match="www.sec.gov"):
        HostRateLimiter(
            per_host={SEC_WWW_HOST: HostBudget(100, 1.0)}, clock=clock, sleep=clock.sleep
        )
    limiter = HostRateLimiter(
        per_host={SEC_BUCKET: HostBudget(2, 1.0)}, clock=clock, sleep=clock.sleep
    )
    assert limiter.bucket_for(SEC_DATA_HOST).capacity == 2.0


@pytest.mark.parametrize("ceiling", [-1, 0, 1, 2])
def test_a_limiter_with_an_unkeepable_ceiling_is_refused_at_construction(ceiling: int) -> None:
    """Refused where it is configured, not at the first request.

    Raised lazily from ``bucket_for``, the ``ValueError`` surfaced deep inside
    a provider's or broker's ``_get`` -- not a ``ProviderError`` or a
    ``BrokerError``, so callers that handle a vendor failure did not handle it.
    """
    with pytest.raises(ValueError, match="ceiling"):
        HostRateLimiter(ceiling, per_host={})


def test_the_smallest_keepable_ceiling_is_accepted(clock: FakeClock) -> None:
    limiter = HostRateLimiter(3, per_host={}, clock=clock, sleep=clock.sleep)
    b = limiter.bucket_for(ALPACA_PAPER_TRADING_HOST)
    assert (b.capacity, b.refill_per_window) == (1.0, 1.0)


class YieldingClock(FakeClock):
    """A fake clock whose sleep also yields to the event loop, as a real sleep does.

    ``FakeClock.sleep`` never suspends, so under it a caller holding the
    bucket's lock never lets another task run at all -- that would test the
    fake, not the bucket.
    """

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_late_account_read_is_admitted_within_a_few_tokens_of_a_bulk_run() -> None:
    """The starvation argument for one shared trading bucket, pinned.

    The SPDR build's CUSIP lookups and the engine's account reads share the
    trading host's bucket. ``TokenBucket.acquire`` holds an ``asyncio.Lock``
    across its refill sleep, and that lock queues FIFO, so a sequential bulk
    caller and a late reader take turns: the reader waits for at most the
    bulk caller's current token and its own, not for the bulk run to finish.
    """
    clock = YieldingClock()
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    b = limiter.bucket_for(ALPACA_PAPER_TRADING_HOST)
    interval = b.window_seconds / b.refill_per_window  # one token: 1/3 s at 180/min
    bulk_total, arrive_after = 200, 50
    bulk_admitted: list[float] = []
    late: dict[str, float] = {}
    late_task: asyncio.Task[None] | None = None

    async def account_read() -> None:
        late["arrived"] = clock()
        late["bulk_done_at_arrival"] = len(bulk_admitted)
        await limiter.acquire(ALPACA_PAPER_TRADING_HOST)
        late["admitted"] = clock()
        late["bulk_done_at_admission"] = len(bulk_admitted)

    async def bulk() -> None:
        nonlocal late_task
        for i in range(bulk_total):
            await limiter.acquire(ALPACA_PAPER_TRADING_HOST)
            bulk_admitted.append(clock())
            if i + 1 == arrive_after:
                late_task = asyncio.create_task(account_read())

    await bulk()
    assert late_task is not None
    await late_task

    # Midway: the bucket's depth of 10 is long spent, every request waits.
    assert late["bulk_done_at_arrival"] >= arrive_after
    assert late["admitted"] - late["arrived"] <= 3 * interval + 1e-9
    assert late["bulk_done_at_admission"] <= late["bulk_done_at_arrival"] + 2
    assert late["bulk_done_at_admission"] < bulk_total
