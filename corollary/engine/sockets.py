"""Which vendor sockets this engine holds open, and when. Rule 9's producers.

Three sockets exist -- ``option_quotes`` and ``equity_quotes`` in
``data/providers/alpaca.py``, ``trade_updates`` in
``engine/execution/alpaca.py`` -- and until this module they had no caller
outside the tests. Every one of them records a message, a stream open and a
stream close into the watchdog's per-socket liveness, so rule 9's connection
condition was fully implemented and never fired in the running process. This
is what starts them.

Not to be confused with ``corollary/sockets.py``, which is **one** socket's
lifecycle: connect, read, attribute the close, reconnect with backoff. This
module is the layer above -- which sockets are held open at all, against
which subscription plans, over which part of the day, and how they stop.

Why it lives under ``engine/`` and takes its sinks as arguments
---------------------------------------------------------------

The sinks are built by ``api/fanout.py``, which imports ``api/schemas.py``:
the browser's wire shapes. An engine module that imported them would invert
the layering -- the engine would depend on the API instead of the other way
about -- so this takes two plain callables and never learns what a frame is.
``api/app.py`` is the composition root that supplies them, along with the
Paper broker (rule 5) and the one :class:`EngineRuntime`.

``engine/scheduler.py`` is *not* the home for this. That module is PRD §6.5's
pre-market build, fifteen-minute refresh and end-of-day roll -- Phase 4 -- and
re-planning a subscription when the book changes belongs there, beside the
position poll that would notice.

The session gate, which is the load-bearing part
------------------------------------------------

Per-socket staleness halts a socket that has gone ninety seconds without a
message. That is exactly right while the socket is held open during a session
and exactly wrong for the other seventeen and a half hours: the option socket
says nothing overnight because there is nothing to say. Under the in-session
standard the engine would halt itself every evening, fire a critical
notification every evening, and require a human resume every morning -- and a
halt log full of nightly false positives is one nobody reads on the morning
that matters.

So a socket is held open only in session, and the watchdog is *told* which
feeds are expected (:meth:`EngineRuntime.expect_feed`). The boundaries come
from the market calendar, never from 09:30--16:00: the Friday after
Thanksgiving closes at 13:00, and a hardcoded afternoon would hold the
sockets open into three hours of guaranteed silence.

The order socket is the exception, stated rather than implied
------------------------------------------------------------

``trade_updates`` carries fills. On a day with no fills it carries nothing,
which is most days and every day of this phase -- nothing in this process
places an order yet. So its staleness is **never** expected, at any hour. It
is not exempt from rule 9: losing it is detected by its *close*, which the
transport reports whether or not anything was flowing, and which halts the
engine by name on the next tick. What it is exempt from is inferring a fault
from silence that was never evidence of one.

That left one hole, and it was not a quiet day: a close covers a socket that
**drops**, not one that **never comes up**. A connection accepted whose auth
frame is never answered records nothing, raises nothing and drops nothing --
no close, no staleness, no timer -- so fills would reach the account and never
reach this process for a whole session with the switch reading healthy. So
what is expected of this socket is its **handshake**
(:meth:`EngineRuntime.expect_handshake`), consulted here from the client's own
``listening`` flag on every tick and withdrawn the moment the server
acknowledges the ``trade_updates`` stream by name. Unlike an absent fill, an
absent handshake is never legitimate.

What this module does not do
----------------------------

**It never resumes anything.** A socket reconnects freely; the halt in
``engine_state`` is a human's to clear through ``POST /api/engine/resume``.
Reconnecting into an unverified position state is how a bot doubles a
position it already holds.

**It does not re-plan when the book changes.** The plan is built once per
session open. Nothing here places an order, so the book cannot change under
it except by a human trading in Alpaca's own UI; re-planning on a five-second
tick would spend the 200/min budget re-asking a question whose answer cannot
have moved, and a re-subscribe protocol (unsubscribe, replace the plan,
reconcile the acknowledgement) is a decision that belongs with the
fifteen-minute refresh. Deferred deliberately, not overlooked.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import date, datetime
from typing import Any, Final

from corollary.calendars import NYSE_TZ, nyse_session_close, nyse_session_open
from corollary.data.providers.alpaca import option_quote_stream, stock_quote_stream
from corollary.data.providers.interface import Quote
from corollary.engine.execution.alpaca import (
    TRADE_UPDATES_STREAM,
    AlpacaTradeUpdateStream,
)
from corollary.engine.execution.interface import (
    BrokerAccount,
    OrderQueryStatus,
    TradeUpdate,
)
from corollary.engine.grouping import GroupingResult, group_positions
from corollary.engine.runtime import EngineRuntime, StreamPlans
from corollary.engine.stream import (
    Stream,
    SubscriptionPlan,
    SubscriptionUnit,
    contract_unit,
    underlying_unit,
)
from corollary.sockets import SocketConnect, VendorStream, sleep_for, utcnow

__all__ = [
    "EQUITY_SOCKET",
    "OPTION_SOCKET",
    "SESSION_CHECK_INTERVAL_SECONDS",
    "STALENESS_SOCKETS",
    "TRADE_SOCKET",
    "SocketSupervisor",
    "subscription_units",
]

logger = logging.getLogger(__name__)

#: The three socket names, derived rather than typed out: each client builds
#: its own name the same way, so a rename moves both at once. These are what
#: the watchdog keys liveness on and what a halt reason says, so a second
#: spelling anywhere would be a socket judged under a name nothing reports.
OPTION_SOCKET: Final = f"{Stream.OPTION.label}_quotes"
EQUITY_SOCKET: Final = f"{Stream.EQUITY.label}_quotes"
TRADE_SOCKET: Final = TRADE_UPDATES_STREAM

#: The order sockets are opened, released and reported in. The order socket
#: first because it is the one that matters when something has to give: losing
#: quotes costs marks, losing fills costs knowing what is held.
_SOCKET_ORDER: Final[tuple[str, ...]] = (TRADE_SOCKET, OPTION_SOCKET, EQUITY_SOCKET)

#: The sockets whose **silence** is evidence, when they are held open and
#: carrying a subscription. ``trade_updates`` is deliberately absent; see the
#: module docstring.
STALENESS_SOCKETS: Final[tuple[str, ...]] = (OPTION_SOCKET, EQUITY_SOCKET)

#: How often the session boundary is re-read. A calendar lookup is a dict hit
#: on a cached build, so the cost is the coroutine, and five seconds matches
#: the watchdog's own cadence -- the two loops answer neighbouring questions
#: and a reader should not have to hold two periods in their head.
SESSION_CHECK_INTERVAL_SECONDS: Final[float] = 5.0

SessionResolver = Callable[[date], datetime | None]
Sleep = Callable[[float], Awaitable[None]]


def subscription_units(
    result: GroupingResult,
) -> tuple[tuple[SubscriptionUnit, ...], tuple[SubscriptionUnit, ...]]:
    """``(option units, equity units)`` for one book. Pure, and separately tested.

    One unit per *logical* position, carrying every leg together, because the
    budget says yes or no to a unit whole: a four-leg condor half-subscribed
    is a position marking off two legs and a payoff curve drawn from a price
    that does not exist. One unit per distinct underlying beside it, at its
    own lower priority, so a tight budget drops the underlyings before it
    drops anything a position is made of.

    The two lists go to two sockets and are **not** interchangeable.
    ``plan_stream_subscriptions`` checks each against the stream it was filed
    under, because a list of OCC symbols handed to the equity socket fits
    inside thirty slots, is admitted, and is never quoted -- with
    ``not_streamed == 0`` and no banner to say so.
    """
    options: list[SubscriptionUnit] = []
    equities: list[SubscriptionUnit] = []
    seen: set[str] = set()
    for position in result.positions:
        options.append(
            contract_unit(position.id, [leg.symbol for leg in position.legs])
        )
        if position.underlying not in seen:
            seen.add(position.underlying)
            equities.append(underlying_unit(position.underlying))
    return tuple(options), tuple(equities)


class SocketSupervisor:
    """Holds the three vendor sockets open through a session, and stops them.

    One per process, constructed in the FastAPI lifespan. Everything it
    depends on is injected, so the tests script three connections, wind a
    clock by hand and never bind a port or read a credential:

    * ``runtime`` -- the one :class:`EngineRuntime`. Its watchdog is what
      these sockets report to, and the *only* thing this class asks of it is
      to start and stop expecting a feed. It never halts and never resumes.
    * ``broker`` -- called to get the Paper book. A callable rather than an
      account, so a missing credential surfaces when the book is read and not
      when the app is built.
    * ``on_quote`` / ``on_update`` -- the fan-out's sinks. Synchronous by
      contract: a slow browser tab must not be able to stall a vendor socket.
    * ``env`` -- the process environment. The feed names and the paper
      credentials are read out of it *inside* the provider, which is the only
      place either may appear.
    * ``session_open`` / ``session_close`` -- the market calendar.
    """

    def __init__(
        self,
        *,
        runtime: EngineRuntime,
        broker: Callable[[], BrokerAccount],
        on_quote: Callable[[Quote], None],
        on_update: Callable[[TradeUpdate], None],
        env: Mapping[str, str] | None = None,
        now: Callable[[], datetime] = utcnow,
        session_open: SessionResolver = nyse_session_open,
        session_close: SessionResolver = nyse_session_close,
        interval_seconds: float = SESSION_CHECK_INTERVAL_SECONDS,
        sleep: Sleep | None = None,
        connect: SocketConnect | None = None,
        stream_sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self._runtime = runtime
        self._broker = broker
        self._on_quote = on_quote
        self._on_update = on_update
        self._env = env
        self._now = now
        self._session_open = session_open
        self._session_close = session_close
        self._interval = interval_seconds
        self._sleep: Sleep = sleep or sleep_for
        self._connect = connect
        self._stream_sleep = stream_sleep
        self._streams: dict[str, VendorStream] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        #: The session's plan, built once at the open and dropped at the
        #: close. ``None`` means *not planned yet* -- which is also what it
        #: means after a broker failure, so the next tick tries again.
        self._plans: StreamPlans | None = None
        #: Sockets whose *construction* failed, so the failure is stated once
        #: a session rather than once every five seconds. A missing feed
        #: variable is a standing condition, and a warning repeated 720 times
        #: an hour buries the one that is news. The build is still retried on
        #: every tick -- a variable added to a reloaded environment should be
        #: picked up -- and the set is dropped with the session.
        self._unbuildable: set[str] = set()
        #: Whether this session's broker failure has been stated. Same
        #: argument as :attr:`_unbuildable` and the opposite treatment until
        #: now: the likeliest trigger is the documented mistake of launching
        #: the API without ``--env-file .env``, which is a standing condition,
        #: and a warning every five seconds for a whole session buries the
        #: record that is news. The plan is still retried on every tick.
        self._plan_failure_logged = False
        #: Whether *"the exchange is closed"* has been said since the last
        #: time anything was held open. Without it a correctly idle engine and
        #: one that will never open a socket again look identical to an
        #: operator: both are a log with nothing in it.
        self._idle_logged = False
        self._task: asyncio.Task[None] | None = None

    # -- what a caller can read -------------------------------------------

    @property
    def held(self) -> tuple[str, ...]:
        """The sockets currently held open, in a stable order."""
        return tuple(name for name in _SOCKET_ORDER if name in self._streams)

    @property
    def plans(self) -> StreamPlans | None:
        """This session's subscription plans. ``None`` outside a session.

        Carries ``not_streamed`` and the *"N symbols not streamed"* banner for
        whoever surfaces it. The cap is never enforced silently: every drop is
        already a log record naming the rule, the unit, the stream and the
        cap.
        """
        return self._plans

    def in_session(self, at: datetime) -> bool:
        """Is the exchange open at ``at``? The calendar answers, never a literal.

        The session is chosen by the **Eastern** date, because that is the
        date a session is named by: 01:00 UTC on a Tuesday is Monday evening
        in New York, and asking Tuesday's calendar about it would compare an
        instant against the wrong day's boundaries.

        Regular hours only. Equities stream pre-market and options do not, so
        holding the option socket open from 04:00 would be four hours of
        guaranteed silence on a socket whose silence halts the engine.
        """
        day = at.astimezone(NYSE_TZ).date()
        opens = self._session_open(day)
        closes = self._session_close(day)
        if opens is None or closes is None:
            return False
        return opens <= at < closes

    # -- the loop ----------------------------------------------------------

    def start(self) -> None:
        """Run the session loop on its own asyncio task. Idempotent.

        Separate from construction because it needs a running loop, and
        called by the lifespan straight after ``runtime.supervise()`` so that
        the watchdog is already running when the first socket opens.
        """
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(
            self._run(), name="corollary-socket-supervisor"
        )

    async def _run(self) -> None:
        """Check the session, act, repeat. Never dies of one bad tick.

        **Sleeps first.** Startup must not block on the network, and a
        lifespan that opened three websockets before answering its first
        request would be a slow boot on a good day and a failed one on a bad
        one. It also keeps a short-lived test out of the vendor's way
        entirely.
        """
        await self._warm_calendar()
        try:
            while True:
                await self._sleep(self._interval)
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover - defensive
                    logger.exception(
                        "the socket supervisor raised; it keeps running",
                        extra={"event": "socket_supervisor_error"},
                    )
        except asyncio.CancelledError:
            raise

    async def _warm_calendar(self) -> None:
        """Build the market calendar on a worker thread, once, before the loop.

        ``exchange_calendars`` is imported and the schedule built on the first
        call, which costs about half a second. On the event loop that is half
        a second in which nothing is served and no socket is read; off it, it
        is nothing. Failures are swallowed deliberately -- this is a cache
        warm, and the real lookup in :meth:`in_session` is where a broken
        calendar has to surface.
        """
        with suppress(Exception):
            await asyncio.to_thread(self.in_session, self._now())

    async def tick(self) -> None:
        """One decision: in session, hold the sockets; out of it, hold none.

        Public because it is the whole behaviour of this class, and a test
        that drives it directly is a test with no timers in it.
        """
        at = self._now()
        if self.in_session(at):
            await self._open(at)
        else:
            await self._stop(reason="out of session")

    # -- opening -----------------------------------------------------------

    async def _open(self, at: datetime) -> None:
        """Hold what should be held. Idempotent: an open socket is left alone.

        **A socket is opened for what the engine asked for, never for what a
        client asked for.** The gate reads
        :attr:`~corollary.engine.stream.SubscriptionPlan.engine_subscribed`
        rather than ``subscribed``, and that difference is rule 9: a socket
        launched here is a socket :meth:`_settle_expectations` then arms the
        ninety-second silence condition on, so a viewport hint that could
        launch one would let a browser scrolling on a flat book halt the
        engine on symbols nobody validated. The hint still *rides* a socket
        the book opens -- it is in the subscribe either way -- which is
        exactly what decision 18 says it is: spare slots, never a reason to
        spend one.
        """
        self._launch(TRADE_SOCKET, self._build_trade_stream)
        if self._plans is None:
            self._plans = await self._plan()
        plans = self._plans
        if plans is not None:
            if plans.option.engine_subscribed:
                self._launch(
                    OPTION_SOCKET, lambda: self._build_quote_stream(plans.option)
                )
            if plans.equity.engine_subscribed:
                self._launch(
                    EQUITY_SOCKET, lambda: self._build_quote_stream(plans.equity)
                )
        self._settle_expectations(at)

    async def _plan(self) -> StreamPlans | None:
        """Read the book and fit it into both budgets. ``None`` if it cannot be read.

        Two broker calls, once a session: the positions, and the **nested**
        order history that is the only evidence of which contract rows are one
        structure. A failure here is logged and swallowed -- the order socket
        is already up, the next tick asks again, and a vendor outage at 09:30
        must not be a process that needs restarting at 09:31.
        """
        try:
            broker = self._broker()
            rows = await broker.positions()
            orders = await broker.orders(status=OrderQueryStatus.ALL)
            result: GroupingResult = group_positions(rows, orders)
        except Exception as error:
            if not self._plan_failure_logged:
                self._plan_failure_logged = True
                logger.warning(
                    "could not read the book; the quote sockets stay shut: %s",
                    type(error).__name__,
                    extra={
                        "event": "socket_plan_failed",
                        "rule": (
                            "a book we cannot read is a subscription we "
                            "cannot make; the order socket stays up and the "
                            "next tick tries again"
                        ),
                        "error": type(error).__name__,
                        "said_once": True,
                        "at": self._now().isoformat(),
                    },
                )
            return None
        self._plan_failure_logged = False
        options, equities = subscription_units(result)
        plans = self._runtime.plan_stream_subscriptions(
            option_units=options, equity_units=equities
        )
        logger.info(
            "planned %d option and %d equity symbols",
            len(plans.option.subscribed),
            len(plans.equity.subscribed),
            extra={
                "event": "socket_plan",
                "correlation_id": plans.option.correlation_id,
                "option_symbols": len(plans.option.subscribed),
                "equity_symbols": len(plans.equity.subscribed),
                # What the engine itself asked for, which is what decides
                # whether each socket opens at all and whether the watchdog
                # judges it. Beside the totals rather than instead of them:
                # a plan whose equity content is entirely a viewport hint
                # reads ``equity_symbols: 3, equity_engine_symbols: 0``, and
                # the socket that never opened is then explicable from the
                # log rather than only from this code.
                "option_engine_symbols": len(plans.option.engine_subscribed),
                "equity_engine_symbols": len(plans.equity.engine_subscribed),
                "not_streamed": plans.not_streamed,
                "viewport_not_streamed": plans.client_not_streamed,
                # The banner, verbatim, so the log and the UI cannot disagree
                # about how many symbols went unsubscribed.
                "message": plans.message,
                "at": self._now().isoformat(),
            },
        )
        return plans

    def _launch(self, name: str, build: Callable[[], VendorStream]) -> None:
        """Build one socket and put its read loop on a task. Never raises.

        A socket that cannot be *constructed* -- a missing credential, most
        likely -- is absent and said so. A socket that cannot *connect* is the
        stream's own business: it retries with backoff, and while it retries
        the expectation stands, so a feed that never comes back is ninety
        seconds of silence and a halt. Loud, which is the point.
        """
        if name in self._streams:
            return
        try:
            stream = build()
        except Exception as error:
            if name not in self._unbuildable:
                self._unbuildable.add(name)
                logger.warning(
                    "the %s socket was not started: %s",
                    name,
                    type(error).__name__,
                    extra={
                        "event": "socket_not_started",
                        "rule": (
                            "a socket that cannot be built leaves the app "
                            "serving; the API must never stop answering "
                            "/api/health"
                        ),
                        "socket": name,
                        "error": type(error).__name__,
                        "at": self._now().isoformat(),
                    },
                )
            return
        self._unbuildable.discard(name)
        self._idle_logged = False
        self._streams[name] = stream
        self._tasks[name] = asyncio.get_running_loop().create_task(
            self._guard(stream), name=f"corollary-{name}"
        )
        logger.info(
            "holding the %s socket open",
            name,
            extra={
                "event": "socket_opened",
                "socket": name,
                "at": self._now().isoformat(),
            },
        )

    async def _guard(self, stream: VendorStream) -> None:
        """Run one socket's loop, and let nothing out of it but cancellation.

        ``VendorStream.run`` deliberately does not catch anything but a
        close: a refusal that recurs identically on every reconnect must
        surface rather than loop forever. Surfacing it means *here* -- one
        log record, and a task that ends. It is **not** relaunched, and the
        expectation is **not** withdrawn, so in session the watchdog halts
        the engine on it within ninety seconds. An unretrieved task exception
        would be a warning on stderr and a silently dead feed.
        """
        try:
            await stream.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "the %s socket stopped and will not be reopened this session",
                stream.name,
                extra={
                    "event": "socket_failed",
                    "rule": (
                        "a socket that stops is left stopped and still "
                        "expected, so rule 9 halts on its silence"
                    ),
                    "socket": stream.name,
                    "at": self._now().isoformat(),
                },
            )

    def _settle_expectations(self, at: datetime) -> None:
        """Tell the watchdog which silences are evidence right now.

        Safe to call on every tick: re-expecting an already-expected feed is a
        no-op in :meth:`Watchdog.expect_feed`, which is what stops a
        five-second loop refreshing a ninety-second condition into one that
        can never fire.

        A quote socket is expected only while it is held **and** carries a
        subscription the **engine** asked for -- an empty plan subscribes
        nothing and can never tick, so judging it would halt the engine for
        holding no positions, and a plan whose only content is a client's
        viewport hint is the same thing with a browser's name on it. Reading
        ``subscribed`` here would arm rule 9's condition on symbols that
        arrived over ``/api/ws``, so it reads
        :attr:`~corollary.engine.stream.SubscriptionPlan.engine_subscribed`.
        ``trade_updates``\'s *silence* is never expected at all; its
        **handshake** is, for as long as it is held open without having been
        acknowledged. See the module docstring for why those are different
        questions.
        """
        plans = self._plans
        expected: set[str] = set()
        if plans is not None:
            for name, plan in (
                (OPTION_SOCKET, plans.option),
                (EQUITY_SOCKET, plans.equity),
            ):
                if name in self._streams and plan.engine_subscribed:
                    expected.add(name)
        for name in _SOCKET_ORDER:
            if name in expected:
                self._runtime.expect_feed(at, socket=name)
            else:
                self._runtime.stop_expecting_feed(socket=name)
        # The engine-wide condition, which every message also refreshes. Left
        # armed with nothing held open it re-creates the nightly halt under a
        # different rule, with ``socket: None`` in the record.
        if expected:
            self._runtime.expect_feed(at)
        else:
            self._runtime.stop_expecting_feed()
        self._settle_handshake(at)

    def _settle_handshake(self, at: datetime) -> None:
        """Does the order socket still owe us a handshake? Asked every tick.

        The client\'s ``listening`` flag is the answer and this is the only
        thing that reads it -- the evidence existed and nothing consulted it.
        Re-asserting while still unconfirmed is a no-op in
        :meth:`Watchdog.expect_handshake`, so the floor stays at the tick that
        first found the socket held, and the withdrawal lands on the first
        tick after the acknowledgement: a fifth of the way through a
        ninety-second condition, on a five-second loop.

        A socket the supervisor is no longer holding owes nothing, which is
        the same shape as the staleness gate and the same reason -- nothing
        held open can be judged for how it is behaving.
        """
        stream = self._streams.get(TRADE_SOCKET)
        confirmed = (
            stream.listening
            if isinstance(stream, AlpacaTradeUpdateStream)
            else stream is None
        )
        if confirmed:
            self._runtime.stop_expecting_handshake(socket=TRADE_SOCKET)
        else:
            self._runtime.expect_handshake(at, socket=TRADE_SOCKET)

    # -- stopping ----------------------------------------------------------

    async def _stop(self, *, reason: str) -> None:
        """Give up every socket. Idempotent, and prompt on a socket that hangs.

        Order matters in three places.

        **The expectations go first.** From this moment nothing is held open,
        so nothing may be judged for silence -- including during the few
        milliseconds this teardown takes.

        **Then every ``begin_close()``, before any socket is touched.** The
        flag is the only evidence that exists *before* a close, and it is what
        separates our shutdown from a fault. Setting all three up front means
        a vendor close that lands while the second socket is closing cannot be
        recorded as a fault on the third.

        **Then ``aclose()``, then cancel.** ``aclose()`` closes an open socket
        and abandons a pending backoff, which covers the two ordinary states.
        It cannot reach the third: a socket that has stopped answering leaves
        the read blocked in ``recv()`` forever, and a connect that never
        returns holds no socket for ``aclose()`` to find. Cancelling the task
        is the only bound on that wait, so the owner cancels rather than
        trusting either method.

        **The withdrawals happen before the early return**, unconditionally. A
        supervisor with nothing held has nothing to release, but an absent
        expectation is not a withdrawn one: the watchdog reads a missing key
        as *expected with no floor*, so a process booted after the bell used
        to take the early return on its first tick and never say the
        engine-wide expectation was off. Latent only while nothing calls
        ``record_poll`` -- the moment one does, that is the nightly
        ``socket: null`` halt arriving by the one path that skips the code
        written to close it.
        """
        for name in _SOCKET_ORDER:
            self._runtime.stop_expecting_feed(socket=name)
        self._runtime.stop_expecting_feed()
        self._runtime.stop_expecting_handshake(socket=TRADE_SOCKET)
        if not self._streams and self._plans is None and not self._unbuildable:
            self._say_idle()
            return

        streams = tuple(self._streams.values())
        tasks = tuple(self._tasks.values())
        names = self.held
        self._streams.clear()
        self._tasks.clear()
        self._plans = None
        self._unbuildable.clear()
        self._plan_failure_logged = False

        for stream in streams:
            stream.begin_close()
        for stream in streams:
            with suppress(Exception):
                await stream.aclose()
        for task in tasks:
            task.cancel()
        if tasks:
            # ``return_exceptions`` so one socket's failure cannot leave the
            # other two un-awaited -- an un-awaited cancelled task is a
            # "Task was destroyed but it is pending" on the way out of a
            # lifespan, which reads as a crash.
            await asyncio.gather(*tasks, return_exceptions=True)
        if names:
            logger.info(
                "released %d socket(s): %s",
                len(names),
                reason,
                extra={
                    "event": "sockets_released",
                    "rule": (
                        "no socket is held outside a session, so no socket "
                        "can be judged stale outside one"
                    ),
                    "sockets": list(names),
                    "reason": reason,
                    "at": self._now().isoformat(),
                },
            )
        self._say_idle()

    def _say_idle(self) -> None:
        """State that nothing is held, once, so *idle* and *broken* differ.

        An engine correctly holding no sockets at 03:00 and an engine that
        will never open one again produce the same log: nothing. One record
        per idle stretch is the difference, and the flag is what stops it
        being 17,000 of them overnight. Cleared when a socket is next held.
        """
        if self._idle_logged:
            return
        self._idle_logged = True
        logger.info(
            "no socket is held; the exchange is closed",
            extra={
                "event": "sockets_idle",
                "rule": (
                    "out of session nothing is held open, so nothing can be "
                    "judged for silence; said once so that a correctly idle "
                    "engine does not read like a stopped one"
                ),
                "at": self._now().isoformat(),
            },
        )

    async def aclose(self) -> None:
        """Stop the loop, then give up every socket. Safe if never started."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._stop(reason="shutdown")

    # -- the three sockets -------------------------------------------------

    def _build_quote_stream(self, plan: SubscriptionPlan) -> VendorStream:
        """One market-data socket, chosen by the plan's own stream.

        The plan carries which socket it is for, so this cannot be inferred
        from a cap -- and a plan handed to the wrong socket is refused by the
        client's constructor rather than subscribed and never quoted.
        """
        build = (
            option_quote_stream if plan.stream is Stream.OPTION else stock_quote_stream
        )
        return build(
            plan=plan,
            activity=self._runtime,
            on_quote=self._on_quote,
            env=self._env,
            connect=self._connect,
            sleep=self._stream_sleep,
            now=self._now,
        )

    def _build_trade_stream(self) -> VendorStream:
        """The order socket. Paper, by rule 5, and by default rather than by choice."""
        return AlpacaTradeUpdateStream.from_env(
            activity=self._runtime,
            on_update=self._on_update,
            env=self._env,
            paper=True,
            connect=self._connect,
            sleep=self._stream_sleep,
            now=self._now,
        )
