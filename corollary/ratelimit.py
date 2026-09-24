"""Per-host request budgets.

**Two buckets, not one.** Alpaca meters ``data.alpaca.markets`` and
``paper-api.alpaca.markets`` separately — 200 requests per minute each on the
Basic plan. A single shared bucket would throttle the process to half its real
budget for no reason, and it would do so invisibly: nothing errors, the poll
just runs at half cadence and the Markets page looks slow.

That split is load-bearing in step 3 rather than theoretical. One logical
operation — "show me the chain for NVDA" — crosses both hosts:

* ``GET /v1beta1/options/snapshots/{underlying}`` is market data
  (``data.alpaca.markets``) and carries quotes, trades and bars.
* ``GET /v2/options/contracts`` is the *trading* API
  (``paper-api.alpaca.markets``) and is the only source of ``multiplier``,
  ``root_symbol`` and ``open_interest``.

The budget from the Phase 2 design: the market poll runs at 400ms in the
foreground and 5s in the background, so roughly 150 requests a minute at its
hottest; the account trio at 15s is 12/min; chains on demand. Comfortable headroom on both hosts,
and no headroom at all if the two are added together against one ceiling.

The bucket is a plain token bucket with continuous refill — no windowing, no
jitter, no 429 backoff. 429 handling belongs to the caller that can see the
``X-RateLimit-Reset`` header; this type's job is to stay under the ceiling in
the first place. The clock and the sleeper are injected so tests measure
tokens rather than wall time.

**Windows other than a minute** (Phase 3 decision 15). StockTwits meters per
*hour*, 200 of them, so a bucket carries its window length rather than
StockTwits getting a private limiter -- the per-host table is the one place a
ceiling lives, whatever its window. Read as per-minute, StockTwits' 200 would
refill a token every 0.3 seconds instead of every 18, a sixty-fold over-spend.

**What a token bucket does and does not promise.** It holds the *steady*
rate to ``capacity / window``, but a full bucket spent at once and then
refilled over the next window admits up to ``2 × capacity`` in any one
rolling window. That is true of the per-minute buckets today and is
unchanged; for StockTwits it means a caller's own cadence -- 180/hour by the
spec's budget, leaving 20 for retries -- is what keeps a *fixed-window*
server counter happy after an idle spell, and this bucket is the backstop.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

__all__ = [
    "ALPACA_DATA_HOST",
    "DEFAULT_PER_HOST_BUDGETS",
    "FINNHUB_HOST",
    "FINNHUB_REQUESTS_PER_MINUTE",
    "FRED_HOST",
    "FRED_REQUESTS_PER_MINUTE",
    "MASSIVE_HOST",
    "MASSIVE_REQUESTS_PER_MINUTE",
    "STOCKTWITS_HOST",
    "STOCKTWITS_REQUESTS_PER_HOUR",
    "ALPACA_LIVE_TRADING_HOST",
    "ALPACA_PAPER_TRADING_HOST",
    "DEFAULT_REQUESTS_PER_MINUTE",
    "HostBudget",
    "HostRateLimiter",
    "TokenBucket",
    "default_limiter",
]

#: Market data. Quotes, snapshots, bars, the option chain.
ALPACA_DATA_HOST = "data.alpaca.markets"

#: The paper trading API. Account, positions, orders, activities — and
#: ``/v2/options/contracts``, which is why the market-data path touches it.
ALPACA_PAPER_TRADING_HOST = "paper-api.alpaca.markets"

#: The live trading API. Named here so the third bucket exists the day Phase 7
#: needs it; nothing in Phase 2 reaches it.
ALPACA_LIVE_TRADING_HOST = "api.alpaca.markets"

#: Company reference data -- market cap today, and PRD section 7's news,
#: calendar and analyst consensus later. A second vendor rather than a second
#: Alpaca host, which is the whole reason the ceiling below is per host.
FINNHUB_HOST = "finnhub.io"

#: Alpaca's documented Basic-plan ceiling, per host. Algo Trader Plus raises
#: this to 10,000/min, which is a constructor argument and not a code change.
DEFAULT_REQUESTS_PER_MINUTE = 200

#: Finnhub's free-tier ceiling. A third of Alpaca's, on a host that is not
#: Alpaca's, which is why :class:`HostRateLimiter` meters per host rather than
#: applying one number everywhere.
FINNHUB_REQUESTS_PER_MINUTE = 60

#: Massive (formerly Polygon) -- vendor-scored news. The free tier's
#: ceiling is 5/min; one untickered call every 15 minutes spends ~1% of it.
MASSIVE_HOST = "api.massive.com"
MASSIVE_REQUESTS_PER_MINUTE = 5

#: FRED -- VIX, credit spreads, the 3-month bill, release dates.
FRED_HOST = "api.stlouisfed.org"
FRED_REQUESTS_PER_MINUTE = 120

#: StockTwits -- keyless symbol streams, metered **per hour**, not per minute.
STOCKTWITS_HOST = "api.stocktwits.com"
STOCKTWITS_REQUESTS_PER_HOUR = 200


@dataclass(frozen=True, slots=True)
class HostBudget:
    """A ceiling of ``requests`` per ``window_seconds`` on one host.

    Every entry in the per-host table is one of these, so a ceiling and its
    window travel together: a number without its window is exactly how a
    per-hour vendor ends up metered per minute.
    """

    requests: int
    window_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.requests <= 0:
            raise ValueError(f"requests must be positive, got {self.requests!r}")
        if self.window_seconds <= 0:
            raise ValueError(
                f"window_seconds must be positive, got {self.window_seconds!r}"
            )


#: The hosts whose ceiling is not :data:`DEFAULT_REQUESTS_PER_MINUTE`.
#:
#: A table rather than a second limiter, and that is the load-bearing part.
#: :func:`default_limiter`'s docstring explains why a component must not
#: construct its own budget: two limiters against one server-side ceiling
#: over-spend by double and look fine locally until the 429s arrive. A second
#: *vendor* is exactly the case that makes a private limiter tempting -- a
#: different key, a different host, a different number -- so the number moves
#: into the shared limiter instead of the limiter multiplying.
#: Read-only, and that is the point of a module-level default: a limiter
#: built with ``per_host=None`` reads this table, so anything that could
#: mutate it would silently re-budget every vendor in the process. A
#: ``MappingProxyType`` makes the declared ``Mapping`` true rather than
#: aspirational -- ``HostRateLimiter`` copies it on the way in anyway, so
#: nothing here loses a capability.
DEFAULT_PER_HOST_BUDGETS: Mapping[str, HostBudget] = MappingProxyType(
    {
        FINNHUB_HOST: HostBudget(FINNHUB_REQUESTS_PER_MINUTE),
        MASSIVE_HOST: HostBudget(MASSIVE_REQUESTS_PER_MINUTE),
        FRED_HOST: HostBudget(FRED_REQUESTS_PER_MINUTE),
        STOCKTWITS_HOST: HostBudget(STOCKTWITS_REQUESTS_PER_HOUR, 3600.0),
    }
)

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class TokenBucket:
    """A budget of ``capacity`` requests per ``per_seconds``, refilled smoothly.

    Not thread-safe and deliberately not trying to be: the design spec chose
    one process with an asyncio event loop, so the only concurrency here is
    cooperative. The ``asyncio.Lock`` exists for that — two coroutines racing
    on the last token — not for threads.
    """

    def __init__(
        self,
        capacity: int,
        per_seconds: float = 60.0,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity!r}")
        if per_seconds <= 0:
            raise ValueError(f"per_seconds must be positive, got {per_seconds!r}")
        self._capacity = float(capacity)
        self._per_seconds = float(per_seconds)
        #: Tokens per second. The refill is continuous rather than a step at
        #: the top of each minute, because a step lets a burst of 200 land in
        #: one second and trip the server's own window.
        self._rate = self._capacity / self._per_seconds
        self._clock = clock
        self._sleep = sleep
        self._tokens = self._capacity
        self._updated = clock()
        self._lock = asyncio.Lock()

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def window_seconds(self) -> float:
        """The window ``capacity`` is spent over: 60 for most hosts, 3600 for StockTwits."""
        return self._per_seconds

    @property
    def available(self) -> float:
        """Tokens on hand right now. A pure read — it consumes nothing.

        ``max(0.0, ...)`` on the elapsed term guards a clock that appears to
        go backwards. ``time.monotonic`` will not, but an injected test clock
        might, and a negative elapsed would *remove* tokens.
        """
        elapsed = max(0.0, self._clock() - self._updated)
        return min(self._capacity, self._tokens + elapsed * self._rate)

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

    async def acquire(self, tokens: int = 1) -> None:
        """Wait until ``tokens`` are available, then spend them.

        Raises rather than deadlocking when the request cannot ever be
        satisfied. A coroutine that hangs forever waiting for a 201st token in
        a 200-token bucket looks exactly like a dropped connection, and the
        watchdog in rule 9 would eventually halt the engine over an arithmetic
        mistake.
        """
        if tokens <= 0:
            raise ValueError(f"tokens must be positive, got {tokens!r}")
        if tokens > self._capacity:
            raise ValueError(
                f"cannot acquire {tokens} tokens from a bucket whose capacity "
                f"is {self._capacity:g}; this would wait forever"
            )
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                await self._sleep(deficit / self._rate)


class HostRateLimiter:
    """One :class:`TokenBucket` per host, created on first use.

    Keyed on the hostname alone, lower-cased. DNS is case-insensitive, so
    ``Data.Alpaca.Markets`` and ``data.alpaca.markets`` are one budget; two
    entries would be two budgets against one server-side ceiling, which is the
    over-spending mirror of the under-spending bug a single shared bucket
    causes.
    """

    def __init__(
        self,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
        *,
        per_host: Mapping[str, int | HostBudget] | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._requests_per_minute = requests_per_minute
        #: A bare ``int`` in ``per_host`` is requests **per minute**, which is
        #: what every caller before Phase 3 passed; a :class:`HostBudget`
        #: names its own window. Normalised to budgets here so ``bucket_for``
        #: has one shape to read.
        #:
        #: ``None`` means the documented table, **not** "no overrides": a
        #: limiter that has to be told Finnhub is 60/min is a limiter that
        #: will one day not be told. Pass ``per_host={}`` to opt out
        #: explicitly, which is what a test wanting one budget everywhere
        #: does.
        #:
        #: Lower-cased on the way in, because ``bucket_for`` lower-cases its
        #: lookup key and a mixed-case entry here would silently never match
        #: -- handing a 60/min vendor Alpaca's 200/min budget, which is the
        #: one failure this table exists to prevent.
        source: Mapping[str, int | HostBudget] = (
            DEFAULT_PER_HOST_BUDGETS if per_host is None else per_host
        )
        self._per_host: dict[str, HostBudget] = {
            host.lower(): (
                limit if isinstance(limit, HostBudget) else HostBudget(limit, 60.0)
            )
            for host, limit in source.items()
        }
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}

    def bucket_for(self, host: str) -> TokenBucket:
        key = host.lower()
        bucket = self._buckets.get(key)
        if bucket is None:
            budget = self._per_host.get(key)
            if budget is None:
                budget = HostBudget(self._requests_per_minute, 60.0)
            bucket = TokenBucket(
                budget.requests,
                budget.window_seconds,
                clock=self._clock,
                sleep=self._sleep,
            )
            self._buckets[key] = bucket
        return bucket

    async def acquire(self, host: str, tokens: int = 1) -> None:
        await self.bucket_for(host).acquire(tokens)


#: The process-wide budget. See :func:`default_limiter`.
_DEFAULT_LIMITER: HostRateLimiter | None = None


def default_limiter() -> HostRateLimiter:
    """The one :class:`HostRateLimiter` shared by everything in this process.

    Alpaca's ceiling is per host *per key*, not per object. A component that
    quietly constructs its own limiter believes it holds 200/min for itself,
    and two such components in one process believe they hold 400/min between
    them against a server-side 200 — the over-spending mirror of the
    two-buckets-for-one-host bug this module's docstring is written against,
    and just as invisible until the 429s start.

    So the default is shared and the seam stays open: anything that wants its
    own budget passes one in, which is what the tests do. Constructed lazily
    rather than at import so a process that never talks to Alpaca never builds
    one.
    """
    global _DEFAULT_LIMITER
    if _DEFAULT_LIMITER is None:
        _DEFAULT_LIMITER = HostRateLimiter()
    return _DEFAULT_LIMITER
