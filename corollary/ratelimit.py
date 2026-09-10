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

The budget from the Phase 2 design: a 2s market poll is 30/min, the account
trio at 15s is 12/min, chains on demand. Comfortable headroom on both hosts,
and no headroom at all if the two are added together against one ceiling.

The bucket is a plain token bucket with continuous refill — no windowing, no
jitter, no 429 backoff. 429 handling belongs to the caller that can see the
``X-RateLimit-Reset`` header; this type's job is to stay under the ceiling in
the first place. The clock and the sleeper are injected so tests measure
tokens rather than wall time.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

__all__ = [
    "ALPACA_DATA_HOST",
    "ALPACA_LIVE_TRADING_HOST",
    "ALPACA_PAPER_TRADING_HOST",
    "DEFAULT_REQUESTS_PER_MINUTE",
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

#: Alpaca's documented Basic-plan ceiling, per host. Algo Trader Plus raises
#: this to 10,000/min, which is a constructor argument and not a code change.
DEFAULT_REQUESTS_PER_MINUTE = 200

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
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._requests_per_minute = requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}

    def bucket_for(self, host: str) -> TokenBucket:
        key = host.lower()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(
                self._requests_per_minute,
                60.0,
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
