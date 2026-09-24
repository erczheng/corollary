"""``EngineRuntime``: lifecycle, halt state, and the dead-man's switch.

CLAUDE.md rule 9 is what this module is for, and it is the one item in Phase 2
that cannot be retrofitted:

    If the engine loses its Alpaca connection, or the risk manager stops
    heartbeating for 90 seconds, the engine calls ``halt()`` on itself and
    fires a critical notification. Recovery requires an explicit human resume.

    **Never auto-resume on reconnect.** Reconnecting into an unverified
    position state is how a bot doubles a position it already holds.

So this module **sets** a halt and never ends one. ``POST /api/engine/resume``
is the only thing in this codebase that ends a halt, via the private helper in
``api/routes/engine.py``; ``tests/engine/test_runtime.py`` greps this file to
prove no second path grew here, the same structural standard
``test_no_order_path.py`` holds the order path to. A rule a reviewer has to
notice is a rule that gets lost in a plausible-looking diff.

**Halting stops nothing in Phase 2, because nothing trades.** The state, the
notification and the explicit-resume requirement are real and tested anyway.
By the time there is an order path, every caller that might clear a halt
already exists, and finding them is archaeology.

Rule 7 is not blurred here either: a halt stops new entries and leaves managed
exits running. There is no flatten in this module and there must not be one --
a flatten closes positions, which is a different consequence and stays a
different control.

Where this runs, and why it is not a service
--------------------------------------------

Design spec decision 1 is *one process*, so the runtime is constructed in the
FastAPI lifespan rather than in a daemon of its own. ``api/app.py`` builds it,
calls :meth:`EngineRuntime.start`, hands it to :meth:`EngineRuntime.supervise`,
and closes it on shutdown.

**The halt does not ride the websocket, and that is deliberate.** Engine state
and notifications are polled at 15s while the WS carries quotes and
``trade_updates`` only. Two reasons, both load-bearing. Rule 9 halts *because*
the socket closed, so a halt notification pushed over that socket is a
notification nobody receives at the one moment it matters. And a broken client
socket has to stay distinguishable from a halted engine: collapse the two and
a human presses Resume on an engine that was never halted, which is exactly
the control rule 9 exists to keep explicit and human.

The two conditions, and why neither fires in the shipped app yet
---------------------------------------------------------------

:class:`Watchdog` implements both of rule 9's conditions. **Neither one fires
in the app as shipped, and the two reasons are now different ones.** Saying
which is which here is the point -- a reader who believes the connection
condition is live will not go looking for the missing ``record_*`` calls, and
a reader who believes it has no producer will not look for the missing
composition either.

* **Connection loss** -- ninety seconds with no message and no successful poll,
  or a websocket close. **The producers exist and nothing runs them.**
  ``record_message``, ``record_stream_open`` and ``record_stream_closed`` are
  called from inside the two vendor websocket clients --
  ``AlpacaQuoteStream`` in ``data/providers/alpaca.py`` (the option and stock
  quote sockets) and ``AlpacaTradeUpdateStream`` in
  ``engine/execution/alpaca.py`` (``trade_updates``) -- over the narrow
  ``corollary.sockets.StreamActivityRecorder`` protocol that this class
  satisfies structurally. But nothing under ``corollary/`` constructs one:
  ``option_quote_stream``, ``stock_quote_stream`` and
  ``AlpacaTradeUpdateStream.from_env`` have no caller outside ``tests/``, and
  ``api/app.py``'s lifespan builds no socket. So the switch is **armed in the
  wiring and not yet running**, which is a different state from having no
  wire, and ``api/routes/ws.py`` is not the file that changes it -- that is
  the *browser* socket and it deliberately records nothing.
  ``record_poll`` and ``record_opening_snapshot`` are the REST half and still
  have no caller outside ``tests/``.
* **A stalled risk-manager heartbeat** -- ninety seconds without one. **No
  producer at all, and additionally unarmed by default.** ``RiskManager`` is
  nine lines and has no body, so armed with no producer this condition would
  halt every engine ninety seconds after boot. It ships behind
  ``heartbeat_armed=False`` so that wiring the producer is one argument rather
  than a new condition written under time pressure on the day ``RiskManager``
  grows a body.

The two therefore differ in two ways rather than one: the connection
condition is armed and its producers are written but unconstructed, while the
heartbeat condition is unarmed *and* has nothing that could feed it. Both are
tested, neither is silently missing, and what this module is today is a proven
switch whose wire is attached at one end.

The connection condition is also **unarmed until something first connects**. A
runtime that has never seen a message or a poll has no connection to have
lost, and a cold start is already halted by the ``engine_state`` default --
halting it again for a socket that was never opened would put a fault reason
on an ordinary boot.

What stops a halt being announced twice
---------------------------------------

The watchdog answers *"what is wrong right now"*, freshly, every time it is
asked; it keeps no record of what it has already reported. The gate that stops
a five-second supervisor re-notifying eighteen times a minute lives in
:meth:`EngineRuntime.check_watchdog`, and it reads **``engine_state``** --
the row ``POST /api/engine/resume`` clears, and the only source of truth about
whether this engine is halted. It reads ``halted_reason`` alongside ``halted``,
because a cold start is halted with no reason at all and a fault during one is
still news; :meth:`EngineRuntime._halt_is_recorded` states that in full.

The gate's second term is this process's memory of a halt that ``engine_state``
would **not take** -- a read-only filesystem, a full disk, a locked file. That
halt is real, it was announced, and no row carries it, so a gate reading only
the row re-halts and re-notifies on every tick: one ongoing fault, a critical
alert every five seconds. The memory is set *only* when the write failed, which
is what stops it outliving a resume; :meth:`EngineRuntime.check_watchdog` has
the four states it has to get right, in a table.

**That memory is a state, never a latch.** It means "our record is missing",
and a thing that means that must end when the record is no longer missing --
so every suppressed tick retries the write
(:meth:`EngineRuntime._retry_persist`), and the fault ending clears it
regardless. Without both it was the same defect one level down: a single
refused commit, and the switch was silent for the life of the process, because
nothing a database recovering or a human resuming could do would ever reach it.

That is deliberate, and it replaced an in-memory latch that was wrong in a way
worth recording. A latch inside the watchdog answers *"have I already
announced this?"* out of memory that a resume cannot reach, so an operator who
resumed while the fault was still present got an engine that was running,
un-halted, with the switch permanently silent: the socket latch cleared only
on a reopen and the staleness latch only on a message, neither of which can
arrive from the dead feed that caused the halt in the first place.

Reading the row instead produces one behaviour that is **not** a bug and must
not be suppressed: **a resume into an ongoing fault re-halts within one
watchdog tick, with a fresh notification.** That is a halt, not an
auto-resume, so rule 9 is honoured -- and it is the right answer, because the
alternative leaves a human believing a resume worked while the feed is still
delivering nothing.

One more convention, small and worth stating: in this file the structured-log
key ``rule`` always carries a :class:`HaltRule` value, never a sentence. Prose
goes under ``policy``. See :meth:`EngineRuntime.halt` for the one place that
matters and for the mismatch it leaves behind.

Cold start, and why the runtime does not clear the cold-start halt
------------------------------------------------------------------

The design spec words the cold start as *halted until the opening snapshot
succeeds*. What the runtime does is record the opening snapshot as liveness
and expose :attr:`EngineRuntime.opening_snapshot_ok`; it does **not** clear
the halt, because a second code path that ends a halt is precisely what rule 9
forbids, and one that ends a halt automatically on a successful
reconnect-and-snapshot is the exact wording of what it forbids. The halt ends
where every other halt ends: at an explicit human resume.

``t0`` is written on the first ever start and never rewritten -- the equity
curve's marker for where Corollary started running, which would otherwise walk
forward on every ``uvicorn --reload``. ``corollary/engine/state.py`` owns that
rule in ``mark_started``, along with the create-if-missing rule in
``engine_state``, and this module **calls them rather than restating them**:
two places deciding whether ``t0`` is rewritten is how a marker starts moving,
and two places deciding what a missing row means is how an absent state
becomes evidence of a healthy engine.

Those helpers used to live in ``api/routes/engine.py``, which this module could
only reach through imports deferred into each method -- ``corollary.api``
imports ``app`` eagerly and ``app`` imports this module. They are persistence,
not routing, so they now sit below both layers and are imported at module
level like everything else.
"""

import asyncio
import logging
import os
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Final, Protocol

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from corollary.db.models import NOTIFICATION_CHANNELS, NotificationRoute
from corollary.engine.state import engine_state, mark_started
from corollary.engine.stream import (
    EQUITY_STREAM_SYMBOL_CAP,
    OPTION_STREAM_QUOTE_CAP,
    Stream,
    SubscriptionPlan,
    SubscriptionPriority,
    SubscriptionUnit,
    markets_visible_unit,
    not_streamed_message,
    plan_subscriptions,
    stream_of,
)

__all__ = [
    "ALPACA_DATA_PLAN_ENV",
    "BASIC_PLAN",
    "DISCORD_WEBHOOK_ENV",
    "HALT_EVENT",
    "HALT_SEVERITY",
    "MAX_MARKETS_VISIBLE_SYMBOLS",
    "PAID_OPTION_STREAM_QUOTE_CAP",
    "PAID_PLAN",
    "REPAIR_MAX_ATTEMPTS",
    "UNLIMITED_STREAM_SYMBOL_CAP",
    "WATCHDOG_INTERVAL_SECONDS",
    "WATCHDOG_TIMEOUT_SECONDS",
    "EngineRuntime",
    "HaltDecision",
    "HaltRule",
    "LoggingNotifier",
    "MarketsVisibleOutcome",
    "MarketsVisibleStatus",
    "Notification",
    "Notifier",
    "StreamBudget",
    "StreamPlans",
    "Watchdog",
    "data_plan",
    "stream_budget_for_plan",
]

logger = logging.getLogger(__name__)

#: Rule 9's ninety seconds, written once. Every condition measures against
#: this and no call site carries the literal, so an operational change is one
#: value rather than a grep.
WATCHDOG_TIMEOUT_SECONDS: Final[float] = 90.0

#: How often the supervisor asks the watchdog whether anything is wrong. Far
#: below the timeout on purpose: the interval bounds how *late* a halt can be,
#: and five seconds of lateness on a ninety-second limit is noise.
WATCHDOG_INTERVAL_SECONDS: Final[float] = 5.0

#: The notification event a rule-9 halt is filed under. One of PRD section
#: 10's three critical events and one of the seeded ``notification_route``
#: rows, so the routing decision is already one a human has configured.
HALT_EVENT: Final = "engine_error"

#: The severity every rule-9 halt carries. Not a parameter: a dead-man's
#: switch firing is critical by definition, and a caller that could choose
#: would eventually choose wrong.
HALT_SEVERITY: Final = "critical"

#: Read for *presence* only -- rule 6, the value never reaches a log or a
#: response. A ``discord`` route with no webhook is a critical alert with
#: nowhere to go, which is worth a warning at the moment it happens.
DISCORD_WEBHOOK_ENV: Final = "DISCORD_WEBHOOK_URL"

#: The account's data plan, from the environment. Re-declared here rather than
#: imported from ``api/routes/settings.py`` so that a reader of ``engine/``
#: sees the value this module actually uses; ``test_runtime.py`` pins all
#: three against that module, which is what stops the spellings drifting.
ALPACA_DATA_PLAN_ENV: Final = "ALPACA_DATA_PLAN"
BASIC_PLAN: Final = "basic"
PAID_PLAN: Final = "algo_trader_plus"

#: Algo Trader Plus streams every **equity** symbol -- Alpaca documents no
#: ceiling at all on that stream. A budget still needs a number, so this is one
#: comfortably above anything reachable: the whole US equity universe is
#: roughly 5,000 symbols. It is not infinity; it is a real cap, large enough
#: that ``plan_subscriptions`` never drops a unit under it.
#:
#: **Equities only, and :class:`StreamBudget` enforces that rather than asking
#: for it.** The paid plan does not make options unlimited -- it raises them to
#: :data:`PAID_OPTION_STREAM_QUOTE_CAP`, a ceiling the server enforces -- so
#: putting this number on the option side would subscribe past a real limit and
#: be truncated silently, which is the failure ``engine/stream.py`` exists to
#: prevent, arriving from the opposite direction. Alpaca's own page says
#: *"unlimited symbols"* flatly, which is what makes this the likeliest thing
#: to be wired wrong on upgrade day.
UNLIMITED_STREAM_SYMBOL_CAP: Final[int] = 10_000

#: The **option** stream's cap on Algo Trader Plus: a thousand quotes. Named
#: for what it is rather than treated as an absence of a limit, because it is
#: a limit -- five times Basic's two hundred, and still a ceiling a Phase 4
#: chain view can reach.
PAID_OPTION_STREAM_QUOTE_CAP: Final[int] = 1000

#: How many Markets rows a client may name in one viewport hint. A bound on a
#: value that **arrived from the client** (rule 4), and the reason it exists
#: is allocation rather than policy: an unbounded list is an unbounded
#: allocation, one unit built per entry, driven by whatever a browser sent.
#:
#: Sixty-four rather than the equity cap. The cap is the wrong number twice
#: over -- it is 30 on Basic and the unlimited sentinel on Algo Trader Plus,
#: so binding the hint to it would refuse a legitimate viewport on the plan
#: this account runs today and accept ten thousand entries on the plan it
#: moves to at Phase 4. This is a bound on *the message*, and the budget is
#: enforced separately by the plan, which cuts the tail of this tier first.
#: Sixty-four is generous against what one page of the Markets stock table
#: can report -- ``PAGE_SIZE`` rows, and the observer watches one page -- and
#: small enough that the tail is cheap to drop.
#:
#: **Mirrored on the client** as ``MAX_MARKETS_VISIBLE_SYMBOLS`` in
#: ``web/src/lib/markets.ts``, which truncates the hint to this figure so the
#: browser never knowingly sends a message that will be refused. Raising it
#: here is safe *for the mirror* and asks nothing of that copy; **lowering it
#: requires the mirror to move in the same change.**
#:
#: **What actually triggers the refusal is the hint's size, not the client's
#: cap**, and the distinction decides which lowerings are dangerous. The test
#: below is ``len(asked) > MAX_MARKETS_VISIBLE_SYMBOLS``, so lowering this to
#: any figure still above the number of rows a viewport can report refuses
#: nothing and the mirror does not strictly have to follow. The dangerous
#: regime is a bound *below* what a viewport can report: a hint is applied
#: whole or not at all, so an over-long list is refused entire, and the
#: client re-sends on refusal, walking the next viewport settle into the
#: identical refusal. Move the mirror regardless -- it is the conservative
#: habit, and it is what turns a refusal into an accepted, degraded hint.
#:
#: **Two failure states, and the quieter one is the worse one.** From a cold
#: session where every hint is refused, the ``MARKETS_VISIBLE`` tier holds
#: nothing at all. But a refusal returns before ``self._markets_visible`` is
#: assigned, so **the previous hint is left standing** -- and a bound that
#: bites only on a full page therefore leaves the tier holding a *stale*
#: hint, streaming rows nobody is looking at, which is harder to notice than
#: an empty one. The asymmetry that hides both: this
#: side is loud (:meth:`EngineRuntime._refuse_markets_visible` logs the rule,
#: the inputs and the timestamp on every refusal, rule 8) and the client side
#: is silent -- the Markets page surfaces no refusal and polls on its own
#: cadence regardless, so the only symptom on screen is staleness.
MAX_MARKETS_VISIBLE_SYMBOLS: Final[int] = 64

#: An equity ticker, shape only: upper case, dots allowed for a class share
#: (``BRK.B``). Deliberately **not** the same question ``api/routes/ws.py``'s
#: ``_SYMBOL`` asks -- that one asks *"could this be a symbol at all"* of an
#: arbitrary client string and admits OCC contracts, and this one asks *"is
#: this an equity ticker"* of a value that is about to become a subscription
#: unit on the equity socket. The engine validates its own input even behind
#: a transport that validates: rule 4 is that the engine enforces, and a
#: second caller of :meth:`EngineRuntime.set_markets_visible` must not be
#: able to get past it by not being a websocket.
#:
#: **Mirrored on the client** as ``EQUITY_TICKER`` in
#: ``web/src/lib/markets.ts``, which filters a viewport hint through the same
#: shape before sending it. Widening this pattern is safe **for the mirror**
#: and asks nothing of that copy; **narrowing it requires the mirror to
#: narrow in the same change.** A hint is applied whole or not at all, so one
#: symbol the old client still admits refuses the entire message, and the
#: client's re-send-on-refusal sends the next viewport settle into the
#: identical refusal. Where the narrowing bites only sometimes -- a shape
#: that excludes ``BRK.B``, say, refusing only while a class share is on
#: screen -- the tier is left holding the *previous*, stale hint rather than
#: nothing, because a refusal returns before ``self._markets_visible`` is
#: assigned. Loud here (rule 8:
#: :meth:`EngineRuntime._refuse_markets_visible` records the rule, the inputs
#: and the timestamp per refusal) and invisible there, where the page
#: surfaces no refusal and keeps polling, so freshness is the only symptom.
#:
#: **"Safe for the mirror" is the whole of the claim, and it is not a licence
#: to widen.** There is a separate, stronger argument against widening on the
#: length axis, recorded in the client mirror's own docstring and in
#: :meth:`EngineRuntime.markets_visible_units`: ``api/routes/ws.py``'s
#: ``_SYMBOL`` admits 32 characters because ``subscribe`` must also admit an
#: OCC contract, and the sixteen here is what stops a 17-to-32 character
#: client-derived string becoming a subscription unit -- a twelve-character
#: paper account number is a legal ticker shape, and a shape filter is not a
#: redactor. Widening to close that gap is the thing being prevented. The
#: safe direction is safe, and it is also **inert**: the client truncates and
#: filters with its own copies, so nothing here is observable until that copy
#: moves too.
_EQUITY_TICKER: Final = re.compile(r"^[A-Z][A-Z0-9.]{0,15}$")

#: How many healthy watchdog ticks the repair gets before it stops trying and
#: says so out loud. Twelve is a minute at :data:`WATCHDOG_INTERVAL_SECONDS`,
#: and a minute is the right shape of wait: a ``database is locked`` clears in
#: milliseconds to seconds, and a full disk or a read-only mount does not clear
#: at all, so a longer window buys nothing but a longer silence. The bound is
#: the requirement -- the ordinary retry in :meth:`EngineRuntime._retry_persist`
#: is bounded by the fault ending, and a repair runs *after* the fault has
#: ended, so nothing but a count would ever stop it.
#:
#: Giving up is never quiet: the last failed attempt emits a critical
#: notification naming the halt that could not be recorded. At that point the
#: record is wrong and the engine is running, and the only thing left that can
#: fix either is a human being told.
REPAIR_MAX_ATTEMPTS: Final[int] = 12

#: ``engine_state.halted_reason`` is ``String(256)``. A reason is truncated
#: rather than refused: losing the tail of a sentence is survivable, and
#: failing to record *why* the engine halted is not.
_REASON_MAX: Final[int] = 256


# --------------------------------------------------------------------------
# The plan of record, and the budget it implies
# --------------------------------------------------------------------------


def data_plan(env: Mapping[str, str]) -> str:
    """Which Alpaca data plan this account is on, from the environment.

    An unrecognised value reads as :data:`BASIC_PLAN` with a warning rather
    than raising, matching ``api/routes/settings.py``. Basic is the
    restrictive direction: the failure mode of a typo is a stream capped
    lower than it needed to be, which is visible and harmless, rather than an
    engine subscribing to 200 symbols on a plan that allows thirty.
    """
    raw = (env.get(ALPACA_DATA_PLAN_ENV) or "").strip().lower()
    if not raw:
        return BASIC_PLAN
    if raw not in (BASIC_PLAN, PAID_PLAN):
        logger.warning(
            "%s=%r is not a plan this app knows; reading it as %s",
            ALPACA_DATA_PLAN_ENV,
            raw,
            BASIC_PLAN,
            extra={
                "event": "engine_unknown_plan",
                "policy": (
                    "an unknown plan reads as basic -- the restrictive "
                    "direction, so a typo cannot lift a cap nobody paid for"
                ),
                "accepted": [BASIC_PLAN, PAID_PLAN],
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return BASIC_PLAN
    return raw


@dataclass(frozen=True, slots=True)
class StreamBudget:
    """Both websocket caps, together, because neither is meaningful alone.

    Alpaca meters the equity stream and the option stream **separately**, and
    one number cannot stand for both: this engine spent a year's worth of
    design on a single cap of thirty, which made an ordinary eight-position
    book -- 32 contracts and 8 underlyings -- look ten symbols over budget.
    Ten over cost *twelve* unstreamed, because a unit is refused whole and
    spending stops at the first refusal, and four of the twelve were held
    contracts with 170 unused option quotes sitting beside them.

    A pair rather than two lookups, so that no call site ever picks *a* number
    -- it reads the field named for the socket it is about. Reaching
    :attr:`option` requires having typed the word, which is the point.

    Constructed by :func:`stream_budget_for_plan`. Building one by hand is
    allowed and validated: :attr:`option` above
    :data:`PAID_OPTION_STREAM_QUOTE_CAP` is **refused**, which is what makes
    the "unlimited applies to options too" mistake impossible rather than
    merely documented. The symptom it would otherwise produce is the worst
    kind -- a subscribe the server silently truncates, and marks that stop
    updating with nothing on screen to say so.
    """

    #: Equity symbols. May be :data:`UNLIMITED_STREAM_SYMBOL_CAP`, because on
    #: the paid plan Alpaca really does document no ceiling here.
    equity: int
    #: Option quotes. Never the unlimited sentinel: the highest this may be is
    #: :data:`PAID_OPTION_STREAM_QUOTE_CAP`, enforced below.
    option: int

    def __post_init__(self) -> None:
        if self.equity < 0 or self.option < 0:
            raise ValueError(
                f"a stream budget cannot be negative; got equity={self.equity} "
                f"option={self.option}. A cap is a count of slots"
            )
        if self.option > PAID_OPTION_STREAM_QUOTE_CAP:
            raise ValueError(
                f"an option stream cap of {self.option} is above the highest "
                f"Alpaca allows on any plan ({PAID_OPTION_STREAM_QUOTE_CAP} "
                "quotes on Algo Trader Plus). The option stream is never "
                "unlimited -- UNLIMITED_STREAM_SYMBOL_CAP is the equity "
                "sentinel and belongs only on that side. Subscribing past a "
                "server-enforced limit is truncated silently, which leaves "
                "contracts marking at a last known price with nothing to say so"
            )


#: Every plan with a budget written for it, keyed the way ``ALPACA_DATA_PLAN``
#: spells it. A mapping rather than a chain of ``if``s so that *"which plans
#: are handled?"* is a question with an answer: a third tier added to
#: ``api/routes/settings.py``'s ``_PLAN_LABELS`` and not to this would fall
#: through to Basic's caps and be capped at 30 and 200 on an account that paid
#: for more -- silently, because a symbol dropped for budget looks exactly like
#: a symbol nobody subscribed.
#: ``test_every_plan_the_settings_page_offers_has_a_budget_written_for_it``
#: compares the two lists, which a walk over this function's *output* cannot
#: do: the fall-through answers, so every invariant asserted of an unhandled
#: plan holds trivially.
_PLAN_BUDGETS: Final[Mapping[str, StreamBudget]] = {
    BASIC_PLAN: StreamBudget(
        equity=EQUITY_STREAM_SYMBOL_CAP,
        option=OPTION_STREAM_QUOTE_CAP,
    ),
    PAID_PLAN: StreamBudget(
        equity=UNLIMITED_STREAM_SYMBOL_CAP,
        option=PAID_OPTION_STREAM_QUOTE_CAP,
    ),
}


def stream_budget_for_plan(plan: str) -> StreamBudget:
    """Both websocket budgets this plan allows. The only place they are chosen.

    ``engine/stream.py`` is deliberately ignorant of the environment and takes
    each cap as an argument; this is the caller side it named. An unrecognised
    plan reads as Basic -- the restrictive direction, the same answer
    :func:`data_plan` gives, and the right one for a typo in the environment.
    It is the *wrong* one for a tier somebody actually pays for, which is what
    :data:`_PLAN_BUDGETS` exists to make checkable rather than trusted.

    On Algo Trader Plus the equity limit does not exist -- a stream still
    cutting at thirty while running full SIP reports *"N symbols not streamed"*
    for a limit that was lifted, and a banner that is wrong is worse than no
    banner because the next real one gets ignored. The option stream is the
    other way about: it rises to a thousand and **stays a ceiling**, so the
    unlimited sentinel is not the answer there and :class:`StreamBudget`
    refuses to hold it.
    """
    return _PLAN_BUDGETS.get(plan, _PLAN_BUDGETS[BASIC_PLAN])


@dataclass(frozen=True, slots=True)
class StreamPlans:
    """One decision, two sockets. What to subscribe where, and what will not be.

    Both plans come from one call, share one clock and one correlation id, and
    are subscribed to their own stream by the caller.

    **With one exception, which is a pair rebuilt mid-session.** The Markets
    viewport hint feeds the equity list and nothing else, so
    ``SocketSupervisor`` re-plans that half alone and carries the option plan
    across by reference -- the option socket is not touched, not on the wire
    and not in what is reported about it. Such a pair holds two correlation
    ids on purpose: they are two decisions taken at two times, and one id
    over both would say the option stream resubscribed when it did not.

    :attr:`not_streamed` and :attr:`message` sum across the two, because the
    reader's question is *"is anything I hold unmarked?"* and not *"which
    socket ran out?"* -- that is a detail for the log record, which names the
    ``stream`` on every drop and carries its ``cap`` beside it. The sum never
    double-counts: a symbol is an option symbol or an equity symbol and is
    therefore planned on exactly one of these, which ``stream.py`` enforces
    per unit against the stream it was filed under.

    Neither figure counts a trimmed viewport row, for the same reason the
    banner is phrased the way it is: the client tier is expected to lose the
    tail of its list, every Markets row is polled regardless, and a count
    that treats designed churn as a fault is a false alarm. That is
    :attr:`client_not_streamed`, reported separately.
    """

    option: SubscriptionPlan
    equity: SubscriptionPlan

    @property
    def not_streamed(self) -> int:
        """The N the UI renders, across both streams."""
        return self.option.not_streamed + self.equity.not_streamed

    @property
    def client_not_streamed(self) -> int:
        """Client-supplied rows the cut trimmed, across both sockets.

        Never the banner and never added to it. Carried so that a viewport
        which lost half its list is a number somebody can read, rather than
        an absence -- and so that the thing being excluded from
        :attr:`not_streamed` is excluded *into* somewhere.
        """
        return self.option.client_not_streamed + self.equity.client_not_streamed

    @property
    def message(self) -> str | None:
        """One banner for both sockets, or ``None`` when everything is streaming."""
        return not_streamed_message(self.not_streamed)


# --------------------------------------------------------------------------
# What a viewport hint did
# --------------------------------------------------------------------------


class MarketsVisibleStatus(StrEnum):
    """What :meth:`EngineRuntime.set_markets_visible` did with a hint.

    **Three, because a ``bool`` was two of them.** The method used to return
    ``False`` for *"the set did not differ"* and for *"the whole message was
    refused"*, and ``api/routes/ws.py`` read it as the first: a refusal was
    logged at INFO as a hint successfully applied, and the client was sent no
    error frame at all. The browser then believed it was watching rows the
    engine had refused -- a silently ignored subscription, which is exactly
    what ``ws.py``'s own ``_refuse`` docstring says must never happen,
    because it looks like a feed with nothing to say.
    """

    #: The held set changed. The caller may re-plan.
    APPLIED = "applied"
    #: Valid, and identical to what was already held. Nothing to re-plan --
    #: every resubscribe is a gap in the marks.
    UNCHANGED = "unchanged"
    #: Refused whole, with the previous hint left standing. Logged under rule
    #: 8 by the engine, and stated to the client by the transport.
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class MarketsVisibleOutcome:
    """The answer to a viewport hint: what happened, and why if it was refused."""

    status: MarketsVisibleStatus
    #: The rule that refused, a **server-side constant** -- safe to log and
    #: safe to send to a client, because nothing client-derived is in it.
    #: ``None`` for every status but :attr:`MarketsVisibleStatus.REFUSED`,
    #: and never ``None`` for that one, so a caller may branch on
    #: ``rule is not None`` and get a ``str`` the type checker believes in.
    rule: str | None = None

    def __post_init__(self) -> None:
        refused = self.status is MarketsVisibleStatus.REFUSED
        if refused and not self.rule:
            raise ValueError(
                "a refusal states the rule it was refused by; rule 8 wants "
                "the rule, the inputs and the timestamp, and the client is "
                "told the first of those"
            )
        if self.rule is not None and not refused:
            raise ValueError(
                f"a {self.status.value} outcome carries no rule; a rule on "
                "one would make 'was this refused?' two questions"
            )

    @property
    def changed(self) -> bool:
        """Did the held set move? The only reason to re-plan the stream."""
        return self.status is MarketsVisibleStatus.APPLIED

    def __bool__(self) -> bool:
        """Refused, deliberately. Ask :attr:`changed` or read :attr:`status`.

        This type exists because one boolean answered two questions, and
        ``if runtime.set_markets_visible(...)`` at a call site would restore
        that silently while still type-checking -- every outcome is truthy,
        so a refusal would read as a change. Raising here is the one thing
        that makes the old shape fail loudly rather than quietly.
        """
        raise TypeError(
            "a viewport-hint outcome is three-valued and has no truth value; "
            "read .changed to decide whether to re-plan, or .status to tell "
            "a refusal from an unchanged set"
        )


# --------------------------------------------------------------------------
# What a halt is
# --------------------------------------------------------------------------


class HaltRule(StrEnum):
    """Why the engine halted itself. One value per condition, never a message.

    A rule is a thing you can count, filter and alert on; a sentence is not --
    the same reasoning as ``ledger.RejectionRule``, ``grouping.DeclineRule``
    and ``stream.DropRule``.
    """

    #: The websocket closed. Immediate: there is nothing to wait for.
    STREAM_CLOSED = "stream_closed"
    #: Ninety seconds with no message and no successful poll. The connection
    #: may still look open; nothing has come through it.
    CONNECTION_STALE = "connection_stale"
    #: Ninety seconds without a risk-manager heartbeat. **Inert** -- nothing
    #: produces one until ``RiskManager`` grows a body. See the module
    #: docstring for why it ships implemented and unarmed rather than absent.
    HEARTBEAT_STALE = "heartbeat_stale"
    #: The socket is open and has never confirmed its subscription. Distinct
    #: from :attr:`CONNECTION_STALE`, which is a feed that *was* flowing and
    #: stopped: this one never started, so a record that called it staleness
    #: would send a reader looking for the message that was never there.
    STREAM_UNCONFIRMED = "stream_unconfirmed"


@dataclass(frozen=True, slots=True)
class HaltDecision:
    """One halt, with everything rule 8 asks a rejection to record.

    The **rule** (:attr:`rule`), the **inputs** (:attr:`inputs`) and the
    **timestamp** (:attr:`at`), plus the sentence that goes in
    ``engine_state.halted_reason`` and in front of a human.
    """

    rule: HaltRule
    #: Persisted verbatim, bounded to 256 characters. Written for the person
    #: who finds the engine doing nothing and wants to know why in one line.
    reason: str
    #: Everything the condition measured, for the structured log. Not persisted
    #: -- the column is one sentence wide, and these are for the record that
    #: gets grepped rather than the one that gets rendered.
    inputs: Mapping[str, Any]
    at: datetime


@dataclass(frozen=True, slots=True)
class _UnrecordedHalt:
    """A halt this process announced, the record refused, and the fault outlived.

    Handed to :meth:`EngineRuntime._repair_unrecorded_halt` by the healthy
    branch of :meth:`EngineRuntime.check_watchdog`. Deliberately a separate
    field from ``_announced`` rather than an extension of it: ``_announced``
    is bounded to one fault episode and **must** be cleared when the episode
    ends, or one refused write suppresses every announcement afterwards. This
    outlives the episode on purpose, because the missing record does too.

    Carries the whole decision, not just the rule: a record written half an
    hour late still has to name the condition, its inputs and its timestamp,
    and by then the watchdog has nothing left to say about a fault that ended.
    """

    decision: HaltDecision
    #: The announcing halt's id -- the one alert the operator actually
    #: received for this episode. ``None`` only if the announcement somehow
    #: carried none, which :meth:`EngineRuntime.halt` does not do.
    correlation_id: str | None
    announced_at: datetime | None


@dataclass(frozen=True, slots=True)
class Notification:
    """A critical alert, addressed to the channels a human left switched on.

    Carried rather than sent: :attr:`channels` is the routing *decision*, read
    from ``notification_route``, and delivering to each channel is a
    :class:`Notifier`. The split is what lets the halt path stay synchronous
    and testable while a Discord webhook is an HTTP call that can hang.
    """

    event: str
    severity: str
    title: str
    body: str
    at: datetime
    correlation_id: str
    #: The enabled channels, in ``NOTIFICATION_CHANNELS`` order. Empty means a
    #: human switched every channel off for this event, which the PRD permits
    #: and which is logged rather than overridden.
    channels: tuple[str, ...]
    #: The book the event happened in, or ``None`` for one that belongs to no
    #: book -- every engine event, this halt included. ``None`` shows in both
    #: books' bells, which is what stops an engine fault being hidden by
    #: whichever account happens to be selected.
    account: str | None = None
    #: Generated when the notification is raised, so every sink records
    #: against the same id without waiting on another: the Discord sink's
    #: delivery rows name the ``notification`` row the database sink writes,
    #: and neither has to run first. A uuid4 hex; ``notification.id``.
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


class Notifier(Protocol):
    """Delivery. One method, so a Discord sender is a new class not an edit."""

    def emit(self, notification: Notification) -> None: ...


class LoggingNotifier:
    """The default sink: a structured log line per notification.

    The real sinks -- the ``notification`` table and the Discord webhook --
    live in ``engine/notify.py`` and are wired by the API's lifespan, which
    hands :class:`EngineRuntime` a ``FanoutNotifier`` holding this one plus
    both. This stays the *default* so a runtime built with no notifier (every
    test that is not about delivery) still leaves a record of each alert.

    Logging rather than raising on an unconfigured channel is deliberate. A
    dead-man's switch whose alerting raises would turn one fault into two, and
    the halt itself is already persisted by the time this runs.
    """

    def emit(self, notification: Notification) -> None:
        logger.critical(
            "%s: %s",
            notification.title,
            notification.body,
            extra={
                "event": "engine_notification",
                "notification_event": notification.event,
                "notification_id": notification.id,
                "severity": notification.severity,
                "account": notification.account,
                "channels": list(notification.channels),
                "title": notification.title,
                "body": notification.body,
                "at": notification.at.isoformat(),
                "correlation_id": notification.correlation_id,
            },
        )


def _utc(moment: datetime) -> datetime:
    """UTC, refusing a naive datetime rather than guessing what it meant.

    ``astimezone`` on a naive datetime silently reads it as *this machine's*
    local time, which on a halt record reconciled against a fill is a
    four-or-five-hour error with no symptom. The same refusal
    ``stream._utc``, ``db.types.UtcDateTime`` and ``wire.require_aware`` each
    make at their own boundary.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(
            "an engine runtime timestamp must be timezone-aware; a naive "
            f"datetime would be read as this machine's local time. Got {moment!r}"
        )
    return moment.astimezone(timezone.utc)


def _bounded(reason: str) -> str:
    """Fit a reason into ``engine_state.halted_reason``, truncating visibly."""
    if len(reason) <= _REASON_MAX:
        return reason
    return reason[: _REASON_MAX - 1] + "…"


def _compact_utc(moment: datetime) -> str:
    """``2026-09-14T13:05:00Z`` -- an instant a person can read, in 20 characters.

    ``halted_reason`` is 256 characters wide and a repaired halt has to spend
    two timestamps inside them; a full ``isoformat`` is twelve characters that
    sentence cannot afford, twice over. Structured logs keep ``isoformat``,
    which is the machine-readable side. This is for the one column a human
    reads.
    """
    return _utc(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def _late_reason(decision: HaltDecision, *, recorded_at: datetime) -> str:
    """What a repaired halt writes into ``halted_reason``.

    The sentence has one job beyond rule 8's record: it lands **after
    everything already looks fine**, so a reader three minutes later has to be
    able to tell it from a fault happening now. "Engine halted" on a visibly
    healthy feed reads as a glitch, and the realistic response to a glitch is
    a reflexive resume without reading -- which would end exactly where the
    unrepaired defect ended.

    So the rule and the moment of the fault come **first**, before the
    original sentence: this column truncates from the tail, and a long
    ``stream_closed`` detail must cost the boilerplate rather than the *what*
    and the *when*.
    """
    return _bounded(
        f"Recorded late at {_compact_utc(recorded_at)}: the engine halted for "
        f"{decision.rule.value} at {_compact_utc(decision.at)}. {decision.reason}"
    )


def _late_body(decision: HaltDecision, *, recorded_at: datetime) -> str:
    """The notification for a repaired halt. Unbounded, so it can say it all.

    Same job as :func:`_late_reason` with room to finish the thought: what
    broke, when, why the record is late, and that the halt stands. The last
    sentence is the one that matters, because the feed being healthy again is
    the thing that makes this alert look spurious.
    """
    return (
        f"The engine halted for {decision.rule.value} at "
        f"{_compact_utc(decision.at)} and the database refused the record at "
        f"the time. The record has just been written, at "
        f"{_compact_utc(recorded_at)}. The fault has since cleared, so this is "
        "the record of a real outage rather than one happening now -- and the "
        f"halt stands until an explicit resume. {decision.reason}"
    )


def _abandoned_body(decision: HaltDecision, *, attempts: int) -> str:
    """The notification for a halt this process could not record at all.

    Sent once, after :data:`REPAIR_MAX_ATTEMPTS`. It is the only remaining way
    the fault reaches anybody: ``engine_state`` says the engine is running,
    ``GET /api/engine/state`` will agree with it, and nothing in this process
    can change that while the database refuses writes.
    """
    return (
        f"The engine halted for {decision.rule.value} at "
        f"{_compact_utc(decision.at)} and the database refused the record "
        f"{attempts} times running. **engine_state does not carry this halt**, "
        "so the engine reads as running and nothing here can correct it. Check "
        "the database, then halt or resume deliberately. "
        f"{decision.reason}"
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_correlation_id() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------
# The watchdog
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _SocketLiveness:
    """One socket's own evidence of life, and its own unreported close.

    Three sockets, three of these. One shared set of these fields was fix B's
    defect in both halves: ``record_message`` from the stock stream refreshed
    the liveness clock the ``trade_updates`` socket was judged by, so
    ``CONNECTION_STALE`` asked *"is any socket alive"*; and one shared close
    slot let a reopen on one socket mark another's close *"has since
    reconnected"* in a critical alert about a feed that was still down.
    """

    last_activity_at: datetime | None = None
    last_activity_source: str = "none"
    closed_at: datetime | None = None
    closed_detail: str = ""
    #: Whether an ``evaluate`` has seen the close in :attr:`closed_at`. A
    #: close nothing has evaluated outlives a reopen -- see
    #: :meth:`Watchdog.record_stream_open`.
    close_observed: bool = False
    #: Whether **this** socket came back while that close was still pending.
    close_reopened: bool = False

    def clear_close(self) -> None:
        """Forget the close. Called on observation, never on a reopen alone."""
        self.closed_at = None
        self.closed_detail = ""
        self.close_observed = False
        self.close_reopened = False


@dataclass(frozen=True, slots=True)
class _FeedExpectation:
    """Whether silence on one feed is evidence, and from when.

    Staleness is the claim *"something should have arrived by now"*, and that
    claim needs somebody to have expected something. Between 16:00 and 09:30
    the option socket is silent because there is nothing to say: held to the
    in-session standard it halts the engine every evening, and an engine that
    halts itself every evening is one whose halts stop being read.

    So the owner of the sockets -- ``corollary/engine/sockets.py``, the only
    thing that knows which ones it is holding open -- says which feeds are
    expected. :attr:`since` is when the expectation *began*, and staleness is
    measured from the later of it and the last message: a socket reopened at
    09:30 whose last quote was yesterday afternoon gets a fresh ninety
    seconds, instead of being seventeen hours stale at the bell.
    """

    expected: bool
    #: When the expectation began. ``None`` on the default expectation, which
    #: no caller set and which therefore floors nothing -- a runtime with no
    #: composition root around it behaves exactly as it did before the gate.
    since: datetime | None


#: What a feed nobody has spoken about is worth: expected, floored at nothing.
#: The strict reading, deliberately -- a missing expectation halts on silence
#: rather than waving it through, so the failure mode of forgetting to wire
#: the gate is a visible halt and not a switch that quietly does nothing.
_EXPECTED_BY_DEFAULT: Final = _FeedExpectation(expected=True, since=None)


class Watchdog:
    """Rule 9's two conditions, over an injected clock. Opens no socket.

    The caller records what happened -- a message, a successful poll, a
    heartbeat, a close, a reopen -- and asks :meth:`evaluate` whether any of
    that adds up to a halt. Nothing here reads a clock or touches a database,
    which is what makes ninety seconds testable in microseconds.

    **Nothing here latches.** :meth:`evaluate` reports the fault that is
    present at the moment it is asked, every time it is asked, for as long as
    the fault lasts. A fault ends when the thing that caused it ends: a
    message or a poll ends :attr:`HaltRule.CONNECTION_STALE`, a reopen ends
    :attr:`HaltRule.STREAM_CLOSED`, a heartbeat ends
    :attr:`HaltRule.HEARTBEAT_STALE`. A poll arriving while the socket is
    still shut ends nothing, because the socket is still shut.

    **One exception, and it is the opposite of a latch: a close is not ended
    by a reopen that no ``evaluate`` has seen.** The supervisor asks every
    five seconds and a reconnect takes one, so a close cleared by its own
    reopen was a lost connection nothing ever evaluated -- rule 9's switch
    silently not firing for the *common* case rather than a rare one. The
    close is therefore held until the first :meth:`evaluate` reports it and
    cleared **on that observation**, which is "the supervisor sees every close
    at least once" and not "the close is remembered until a human acts".
    Memory a resume cannot reach is the defect the module docstring records;
    this memory lasts one tick, ends without anybody doing anything, and
    cannot outlive the observation that ends it.

    Not re-announcing an ongoing fault is
    :meth:`EngineRuntime.check_watchdog`'s job, and it does it by reading
    ``engine_state.halted`` rather than by remembering. See the module
    docstring: a latch in here is memory a human's resume cannot reach, and it
    silenced the switch for exactly the operator who most needed it. Keeping
    this class memoryless is what makes its view and the database's impossible
    to diverge.

    **Liveness is per socket; the halt is not.** One of these serves all three
    Alpaca sockets, and it used to serve them out of one last-activity clock
    and one close slot -- so a chatty stock stream refreshed the clock the
    ``trade_updates`` socket was judged by, and ``CONNECTION_STALE`` answered
    *"is any socket alive"*. The consequence is not symmetric between the
    three: the order socket can die while quotes keep flowing, and then the
    book stops receiving fills with rule 9 believing the feed healthy, which
    is the *"unverified position state"* rule 9 names with no reconnect even
    attempted. So the **tracking** splits into one :class:`_SocketLiveness`
    per socket and the **decision** does not: any socket ninety seconds silent
    halts the engine, and the halt says which one. Rule 9's halt is
    engine-wide and there is still exactly one path to it.

    A caller that names no socket is filed under ``None`` and is deliberately
    *not* held to the per-socket standard: there is no socket to put in a halt
    reason, and the REST poll is the case that matters -- a successful poll
    says the data host answers, which is no claim about any socket at all. It
    still counts for the engine-wide condition, because the spec says
    *message or poll* and a process with no stream wired yet has nothing else.
    Every real socket names itself; ``corollary.sockets.VendorStream``
    requires the name in its constructor so that it cannot be forgotten at one
    call site out of five.
    """

    __slots__ = (
        "_expectations",
        "_handshakes",
        "_heartbeat_armed",
        "_last_activity_at",
        "_last_activity_source",
        "_last_heartbeat_at",
        "_sockets",
        "_started_at",
        "_timeout_seconds",
    )

    def __init__(
        self,
        *,
        started_at: datetime,
        timeout_seconds: float = WATCHDOG_TIMEOUT_SECONDS,
        heartbeat_armed: bool = False,
    ) -> None:
        self._started_at = _utc(started_at)
        self._timeout_seconds = timeout_seconds
        #: False until ``RiskManager`` has a body that heartbeats. See the
        #: module docstring: armed with no producer, this halts every engine
        #: ninety seconds after boot.
        self._heartbeat_armed = heartbeat_armed
        #: The engine-wide clock: the most recent evidence of life from
        #: **anywhere**, socket or poll. Kept alongside the per-socket clocks
        #: rather than replaced by them, because it is what answers for a
        #: process whose only producer is the poll loop -- the opening
        #: snapshot and a REST poll are all a run has before a stream is
        #: wired, and *"nothing has come through at all"* is a condition in
        #: its own right.
        self._last_activity_at: datetime | None = None
        self._last_activity_source = "none"
        self._last_heartbeat_at: datetime | None = None
        #: One record per socket, keyed by the name the socket reports itself
        #: under. ``None`` is the unnamed source -- the poll loop, and a
        #: hand-recorded close in a test -- which is tracked for its close and
        #: never for its staleness. See the class docstring.
        self._sockets: dict[str | None, _SocketLiveness] = {}
        #: Which feeds are expected, keyed the same way :attr:`_sockets` is --
        #: a socket name, or ``None`` for the engine as a whole. Empty by
        #: default, and an absent key reads as
        #: :data:`_EXPECTED_BY_DEFAULT`, so the gate changes nothing for a
        #: caller that never mentions it. See :class:`_FeedExpectation`.
        self._expectations: dict[str | None, _FeedExpectation] = {}
        #: Which sockets are expected to have completed a **handshake**, keyed
        #: by socket name only -- there is no engine-wide handshake. Empty by
        #: default, and an absent key reads as *not expected*, which is the
        #: opposite of :attr:`_expectations`' default and deliberately so: a
        #: feed's silence is measurable against its last message, whereas a
        #: handshake has nothing to measure at all until somebody says when
        #: the socket started being held open. See :meth:`expect_handshake`.
        self._handshakes: dict[str, _FeedExpectation] = {}

    # -- what the caller records ------------------------------------------

    def record_message(self, at: datetime, *, socket: str | None = None) -> None:
        """A quote or a ``trade_updates`` message arrived on ``socket``.

        ``socket`` is the name the client reports itself under, and it is what
        a halt reason gets to say. Omitted, this counts only for the
        engine-wide clock -- see the class docstring.
        """
        self._record_activity(at, "message", socket)

    def record_poll(self, at: datetime) -> None:
        """A poll succeeded. Liveness too -- the spec says *message or poll*.

        **Never attributed to a socket, and there is no argument for it.** A
        successful REST call says the data host answers; it is not evidence
        about any websocket, and treating it as such is how a dead
        ``trade_updates`` socket stayed invisible while the poll loop ran.
        """
        self._record_activity(at, "poll", None)

    def record_stream_open(
        self, at: datetime, *, socket: str | None = None
    ) -> None:
        """The socket is up again.

        Ends the *close* this class reports -- **once something has evaluated
        it** -- and nothing else. The halt in ``engine_state`` is untouched:
        rule 9's whole point is that the socket coming back is not evidence
        that the position state is verified, and this class could not end a
        halt if it wanted to -- it has no database and no opinion about one.

        **A close no tick has seen yet outlives the reopen.** Clearing it
        here unconditionally was rule 9's connection condition failing to
        fire for the ordinary case: ``reconnect_delay(1)`` is one second and
        the open is recorded the moment the new session authenticates, so a
        1006 closes and reopens inside about 1.0-1.5s, entirely between two
        ticks of the 5.0s supervisor -- and the condition was gone before
        anything asked. The connection was lost all the same, neither socket
        replays, and on ``trade_updates`` that is precisely the unverified
        position state rule 9 names.

        So the close is held until :meth:`evaluate` reports it, and cleared
        **on that observation** rather than on this call. Not a latch: one
        close, one halt, and :meth:`evaluate` is where the clearing happens.

        **It ends only this socket's close.** One shared slot meant a reopen
        here cleared -- or worse, marked *"has since reconnected"* -- a close
        recorded by a different socket, so a critical alert could say the feed
        was back about one that was still down.
        """
        moment = _utc(at)
        state = self._state(socket)
        if state.closed_at is not None and not state.close_observed:
            state.close_reopened = True
        else:
            state.clear_close()
        self._record_activity(moment, "stream_open", socket)

    def record_stream_closed(
        self, at: datetime, *, socket: str | None = None, detail: str = ""
    ) -> None:
        """The websocket closed, for the stated reason if the transport gave one.

        A second close on **the same socket** inside one unobserved window
        replaces the first: the condition is *that socket dropped*, the latest
        drop is the one to report, and it is pending again whatever the
        previous one's state was. A close on a *different* socket is a
        different close and is kept beside it, so two feeds dropping at once
        are two halts rather than one report attributed to whichever arrived
        last.
        """
        state = self._state(socket)
        state.closed_at = _utc(at)
        state.closed_detail = detail or "no reason given"
        state.close_observed = False
        state.close_reopened = False

    def _state(self, socket: str | None) -> _SocketLiveness:
        """This socket's record, created on first sight.

        Tracking starts when a socket first reports something, which is what
        keeps an un-wired producer from being judged: a socket that has never
        spoken has no liveness clock to fail, and a cold start is already
        halted for its own reasons.
        """
        state = self._sockets.get(socket)
        if state is None:
            state = _SocketLiveness()
            self._sockets[socket] = state
        return state

    def record_heartbeat(self, at: datetime) -> None:
        """The risk manager is alive.

        **Nothing calls this yet.** ``RiskManager`` has no body, so the
        condition it feeds is unarmed by default; see the module docstring.
        """
        self._last_heartbeat_at = _utc(at)

    # -- which feeds are expected -----------------------------------------

    def expect_feed(self, at: datetime, *, socket: str | None = None) -> None:
        """This feed is held open from ``at``, so silence on it is evidence.

        Called by whoever owns the socket, at the moment it starts holding it
        open -- which in this process is ``engine/sockets.py`` at the market
        open. ``socket=None`` expects the engine-wide condition, the one a
        REST poll also answers for.

        **Calling it again while the feed is already expected is a no-op, and
        that is a guard rather than an optimisation.** The floor is an
        expectation's *start*; refreshed on every tick of a five-second loop
        it would be a ninety-second condition that can never reach ninety
        seconds. Rule 9 off, with nothing anywhere to say so. So a caller may
        re-assert an expectation as often as it likes and the floor stays
        where the expectation began.
        """
        current = self._expectations.get(socket)
        if current is not None and current.expected:
            return
        self._expectations[socket] = _FeedExpectation(expected=True, since=_utc(at))

    def stop_expecting_feed(self, *, socket: str | None = None) -> None:
        """This feed is not held open, so its silence proves nothing.

        The other half of the market session: out of session no socket is
        held open, so no socket can be stale. **It disarms staleness and
        nothing else** -- a close recorded on this socket is still reported,
        because a drop at 15:59:50 is a lost connection whether or not the
        tick that sees it lands after the bell, and a close we asked for
        ourselves records nothing in the first place
        (:meth:`corollary.sockets.VendorStream.begin_close`).

        Takes no timestamp: there is nothing to measure from once nothing is
        expected, and a stored moment nothing reads is a field that goes
        stale without anybody noticing.
        """
        self._expectations[socket] = _FeedExpectation(expected=False, since=None)

    def feed_expected(self, socket: str | None = None) -> bool:
        """Is silence on this feed evidence right now? Reported, never inferred."""
        return self._expectation(socket).expected

    def _expectation(self, socket: str | None) -> _FeedExpectation:
        return self._expectations.get(socket, _EXPECTED_BY_DEFAULT)

    # -- which sockets owe a handshake -------------------------------------

    def expect_handshake(self, at: datetime, *, socket: str) -> None:
        """This socket has been held open since ``at`` and owes a handshake.

        The order socket's condition. ``trade_updates`` carries fills, so on a
        day with no fills it carries nothing and its *silence* proves nothing
        -- which left the socket that connects and never completes its
        handshake reporting nothing at all: no message, no close, no
        staleness, no timer, with fills reaching the account and never
        reaching this process. So what is expected is the handshake, and
        unlike an absent fill an absent handshake is never legitimate.

        Withdrawn by :meth:`stop_expecting_handshake` the moment the socket
        confirms, and whenever it is given up. **Re-asserting is a no-op**,
        for :meth:`expect_feed`'s reason: the supervisor re-asserts on a
        five-second tick, and a refreshed floor is a ninety-second condition
        that can never reach ninety seconds.
        """
        current = self._handshakes.get(socket)
        if current is not None and current.expected:
            return
        self._handshakes[socket] = _FeedExpectation(expected=True, since=_utc(at))

    def stop_expecting_handshake(self, *, socket: str) -> None:
        """This socket has confirmed, or it is no longer held open.

        Both cases, one method: what the condition asks is *"is a socket being
        held open without having confirmed"*, and a socket nobody is holding
        open is not.
        """
        self._handshakes[socket] = _FeedExpectation(expected=False, since=None)

    def handshake_expected(self, socket: str) -> bool:
        """Does this socket still owe a handshake? Reported, never inferred."""
        expectation = self._handshakes.get(socket)
        return expectation is not None and expectation.expected

    def _stale_since(self, socket: str | None, last_activity: datetime) -> datetime:
        """When this feed's silence started being measurable.

        The later of the last message and the moment the feed became
        expected. See :class:`_FeedExpectation` for the morning case the
        second half exists for.
        """
        since = self._expectation(socket).since
        if since is None or since <= last_activity:
            return last_activity
        return since

    def _record_activity(
        self, at: datetime, source: str, socket: str | None
    ) -> None:
        """Refresh the engine-wide clock, and this source's own.

        Both, always. The engine-wide one is monotonic -- an out-of-order
        stamp never moves it backwards -- and so is each socket's, for the
        same reason: a late frame is not evidence that the feed went quiet.
        """
        moment = _utc(at)
        if self._last_activity_at is None or moment > self._last_activity_at:
            self._last_activity_at = moment
            self._last_activity_source = source
        state = self._state(socket)
        if state.last_activity_at is None or moment > state.last_activity_at:
            state.last_activity_at = moment
            state.last_activity_source = source

    # -- the question -----------------------------------------------------

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def heartbeat_armed(self) -> bool:
        return self._heartbeat_armed

    @property
    def last_activity_at(self) -> datetime | None:
        return self._last_activity_at

    def evaluate(self, now: datetime) -> HaltDecision | None:
        """Has anything gone wrong? ``None`` when the answer is no.

        Conditions are checked in the order they become knowable: a close is a
        fact the moment it arrives, staleness takes ninety seconds to become
        one. The boundary is ``>=``, so eighty-nine seconds is healthy and
        ninety is not.

        Asked twice during one ongoing fault it answers twice, with equal
        decisions. That is not a bug to fix in here: the caller is what
        decides whether an answer is *news*, and it decides that against
        ``engine_state.halted``.
        """
        moment = _utc(now)
        pending = self._pending_close()
        if pending is not None:
            name, state = pending
            closed_at = state.closed_at
            assert closed_at is not None  # `_pending_close` selected on it
            reconnected = state.close_reopened
            label = f"The {name} stream" if name else "The market data stream"
            decision = self._decide(
                HaltRule.STREAM_CLOSED,
                _bounded(
                    f"{label} closed ({state.closed_detail}) and "
                    "has since reconnected. The engine halted itself; the socket "
                    "is back but the halt does not clear without an explicit "
                    "resume."
                    if reconnected
                    else f"{label} closed ({state.closed_detail}). "
                    "The engine halted itself; the socket may reconnect but the "
                    "halt does not clear without an explicit resume."
                ),
                {
                    # Which feed. Losing quotes and losing fills have very
                    # different consequences, and a record that does not say
                    # which one cannot be read after the fact.
                    "socket": name,
                    "closed_at": closed_at.isoformat(),
                    "detail": state.closed_detail,
                    "elapsed_seconds": (moment - closed_at).total_seconds(),
                    "timeout_seconds": self._timeout_seconds,
                    # Whether the socket is already back. The sentence above
                    # says so too, because a critical alert on a visibly
                    # healthy feed reads as a glitch otherwise -- the same
                    # reason `_late_reason` names the original condition.
                    "reconnected": reconnected,
                },
                moment,
            )
            # Observed. That is what ends a close the socket has recovered
            # from -- see `record_stream_open`. A socket still shut keeps
            # being reported, because nothing here latches and the fault is
            # still present.
            state.close_observed = True
            if reconnected:
                state.clear_close()
            return decision

        # After the close and before staleness. A socket that dropped mid
        # handshake is both, and the close is the cause; a socket that never
        # handshaked at all is reported under its own condition rather than as
        # silence, because "no message" sends a reader looking for a message
        # that was never coming.
        unconfirmed = self._unconfirmed_handshake(moment)
        if unconfirmed is not None:
            name, elapsed = unconfirmed
            since = self._handshakes[name].since
            assert since is not None  # `_unconfirmed_handshake` selected on it
            return self._decide(
                HaltRule.STREAM_UNCONFIRMED,
                _bounded(
                    f"The {name} socket has been open {elapsed:.0f}s without "
                    f"confirming its subscription, against a "
                    f"{self._timeout_seconds:.0f}s limit. Nothing it carries "
                    "can reach this process. The engine halted itself; "
                    "recovery requires an explicit resume."
                ),
                {
                    "socket": name,
                    "elapsed_seconds": elapsed,
                    "timeout_seconds": self._timeout_seconds,
                    # When the socket started being held open, which is the
                    # only clock this condition has: there is no message to
                    # measure from, and that is the fault.
                    "expected_since": since.isoformat(),
                    "last_activity_at": self._socket_activity_at(name),
                },
                moment,
            )

        silent = self._silent_socket(moment)
        if silent is not None:
            name, elapsed = silent
            return self._decide(
                HaltRule.CONNECTION_STALE,
                _bounded(
                    f"No message on the {name} socket for {elapsed:.0f}s, "
                    f"against a {self._timeout_seconds:.0f}s limit. Another feed "
                    "may still be live; this one is not. The engine halted "
                    "itself; recovery requires an explicit resume."
                ),
                {
                    "socket": name,
                    "elapsed_seconds": elapsed,
                    "timeout_seconds": self._timeout_seconds,
                    "last_activity_at": self._socket_activity_at(name),
                    "last_activity_source": self._socket_activity_source(name),
                },
                moment,
            )

        # Gated on the engine-wide expectation as well as the per-socket one.
        # Every quote refreshes this clock too, so gating only the named
        # sockets left the nightly halt exactly where it was -- at 16:01:30,
        # under this rule, with ``socket: None`` in the record.
        if self._last_activity_at is not None and self._expectation(None).expected:
            elapsed = (
                moment - self._stale_since(None, self._last_activity_at)
            ).total_seconds()
            if elapsed >= self._timeout_seconds:
                return self._decide(
                    HaltRule.CONNECTION_STALE,
                    _bounded(
                        f"No market data message and no successful poll for "
                        f"{elapsed:.0f}s, against a {self._timeout_seconds:.0f}s "
                        "limit. The engine halted itself; recovery requires an "
                        "explicit resume."
                    ),
                    {
                        # No socket: this is the engine-wide condition, and
                        # the last thing heard from may have been a poll.
                        # Stated rather than omitted so one query reads both.
                        "socket": None,
                        "elapsed_seconds": elapsed,
                        "timeout_seconds": self._timeout_seconds,
                        "last_activity_at": self._last_activity_at.isoformat(),
                        "last_activity_source": self._last_activity_source,
                    },
                    moment,
                )

        if self._heartbeat_armed:
            since = self._last_heartbeat_at or self._started_at
            elapsed = (moment - since).total_seconds()
            if elapsed >= self._timeout_seconds:
                return self._decide(
                    HaltRule.HEARTBEAT_STALE,
                    _bounded(
                        f"The risk manager has not heartbeat for {elapsed:.0f}s, "
                        f"against a {self._timeout_seconds:.0f}s limit. The engine "
                        "halted itself; recovery requires an explicit resume."
                    ),
                    {
                        "elapsed_seconds": elapsed,
                        "timeout_seconds": self._timeout_seconds,
                        "last_heartbeat_at": (
                            self._last_heartbeat_at.isoformat()
                            if self._last_heartbeat_at is not None
                            else None
                        ),
                        "started_at": self._started_at.isoformat(),
                    },
                    moment,
                )

        return None

    def _pending_close(self) -> tuple[str | None, _SocketLiveness] | None:
        """The oldest close no ``evaluate`` has retired yet, if there is one.

        Oldest first, and the socket name breaks a tie, so two feeds dropping
        in the same instant are reported in a fixed order rather than in
        whatever order the dict happens to hold. One decision per tick: the
        next tick reports the next close, and no close is cleared without
        having been reported at least once.
        """
        pending: list[tuple[datetime, str, str | None, _SocketLiveness]] = []
        for name, state in self._sockets.items():
            closed_at = state.closed_at
            if closed_at is not None:
                pending.append((closed_at, name or "", name, state))
        if not pending:
            return None
        chosen = min(pending, key=lambda item: (item[0], item[1]))
        return chosen[2], chosen[3]

    def _judged_names(self) -> list[str]:
        """Every named socket staleness could be asked about, in a fixed order.

        The **union** of the sockets that have reported something and the
        sockets somebody is expecting, because those two sets are not the same
        and the difference is a fault. A socket gets a liveness entry only
        when a message, an open or a close arrives on it, so a socket that
        fails *before its first frame* -- the connection accepted and the auth
        frame raising -- has no entry at all. Iterating the entries alone
        skipped exactly that socket: the one case where the feed is never
        coming back on its own.

        Unnamed sources are excluded: the poll loop is the one that matters,
        and a poll is no evidence about any socket.
        """
        names = {name for name in self._sockets if name is not None}
        names.update(
            name
            for name, expectation in self._expectations.items()
            if name is not None and expectation.expected
        )
        return sorted(names)

    def _silent_socket(self, moment: datetime) -> tuple[str, float] | None:
        """The **named** socket that has been silent longest, past the limit.

        A socket **nobody is expecting** is skipped, because its silence is
        not evidence of anything: see :class:`_FeedExpectation`.

        A socket with no message at all is measured from the moment it became
        expected -- the clock its expectation carries, for exactly this case.
        With neither a message nor a stated expectation there is no clock in
        existence and the socket is skipped: the default expectation floors
        nothing on purpose, so a runtime with no composition root around it
        cannot halt on a feed nobody ever opened.
        """
        worst: tuple[str, float] | None = None
        for name in self._judged_names():
            expectation = self._expectation(name)
            if not expectation.expected:
                continue
            state = self._sockets.get(name)
            last_activity = state.last_activity_at if state is not None else None
            if last_activity is None:
                since = expectation.since
                if since is None:
                    continue
            else:
                since = self._stale_since(name, last_activity)
            elapsed = (moment - since).total_seconds()
            if elapsed < self._timeout_seconds:
                continue
            # Longest silence first, and the *lowest* name on a tie -- the
            # same order `_pending_close` uses, so one reader does not have to
            # hold two tie-breaks in their head.
            if worst is None or elapsed > worst[1] or (
                elapsed == worst[1] and name < worst[0]
            ):
                worst = (name, elapsed)
        return worst

    def _socket_activity_at(self, name: str) -> str | None:
        """When this socket was last heard from, or ``None`` if it never was.

        ``.get``, not ``[]``: a socket judged from its expectation alone has
        no liveness entry, and a ``KeyError`` raised while building the halt
        record would turn a detected fault into an unhandled exception in the
        watchdog loop.
        """
        state = self._sockets.get(name)
        last = state.last_activity_at if state is not None else None
        return last.isoformat() if last is not None else None

    def _socket_activity_source(self, name: str) -> str:
        state = self._sockets.get(name)
        return state.last_activity_source if state is not None else "none"

    def _unconfirmed_handshake(self, moment: datetime) -> tuple[str, float] | None:
        """The socket that has owed a handshake longest, past the limit.

        The same tie-breaks as :meth:`_silent_socket` and
        :meth:`_pending_close`: longest first, lowest name on a tie, so one
        reader does not have to hold three orderings in their head.
        """
        worst: tuple[str, float] | None = None
        for name in sorted(self._handshakes):
            expectation = self._handshakes[name]
            if not expectation.expected or expectation.since is None:
                continue
            elapsed = (moment - expectation.since).total_seconds()
            if elapsed < self._timeout_seconds:
                continue
            if worst is None or elapsed > worst[1]:
                worst = (name, elapsed)
        return worst

    def _decide(
        self,
        rule: HaltRule,
        reason: str,
        inputs: Mapping[str, Any],
        at: datetime,
    ) -> HaltDecision:
        """Build the decision. Remembers nothing -- see the class docstring."""
        return HaltDecision(rule=rule, reason=reason, inputs=inputs, at=at)


# --------------------------------------------------------------------------
# The runtime
# --------------------------------------------------------------------------


class EngineRuntime:
    """The engine's lifecycle and its halt state. One per process.

    Constructed in the FastAPI lifespan (design spec decision 1: one process).
    Everything it depends on is injected, so a test drives ninety seconds of
    silence in microseconds and no test needs a socket or a credential:

    * ``session_factory`` -- opens a :class:`~sqlalchemy.orm.Session`.
    * ``now`` -- the clock. Every timestamp this class writes comes from it.
    * ``notifier`` -- delivery, defaulting to :class:`LoggingNotifier`.
    * ``env`` -- the process environment, defaulting to ``os.environ``.
      Nothing under ``corollary/`` reads ``.env``; the environment is the
      engine's input and the launcher is what loads the file.
    * ``correlation_ids`` -- one id per decision, so a halt's persistence, its
      log line and its notification read as the one event they are.

    **Nothing on this class ends a halt.** It is the only rule the type
    enforces structurally, and ``test_runtime.py`` greps the file to keep it
    that way.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        now: Callable[[], datetime] = _utc_now,
        notifier: Notifier | None = None,
        env: Mapping[str, str] | None = None,
        correlation_ids: Callable[[], str] = _new_correlation_id,
        timeout_seconds: float = WATCHDOG_TIMEOUT_SECONDS,
        heartbeat_armed: bool = False,
        watchdog_interval_seconds: float = WATCHDOG_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._now = now
        self._notifier: Notifier = LoggingNotifier() if notifier is None else notifier
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._correlation_ids = correlation_ids
        self._interval = watchdog_interval_seconds
        self._watchdog = Watchdog(
            started_at=_utc(now()),
            timeout_seconds=timeout_seconds,
            heartbeat_armed=heartbeat_armed,
        )
        self._task: asyncio.Task[None] | None = None
        self._opening_snapshot_ok = False
        #: The Markets rows a client last said were on screen, in the order
        #: it named them, deduped. The **only** client-supplied input this
        #: class holds, and it feeds exactly one thing: the lowest
        #: subscription tier. See :meth:`set_markets_visible`.
        self._markets_visible: tuple[str, ...] = ()
        #: Who sent the hint above -- a connection id, or ``None`` for a
        #: caller that did not say. Held so that a closing connection can
        #: drop *its own* hint and leave a later one alone; see
        #: :meth:`clear_markets_visible`. Never an identity and never
        #: authorisation: the tier is the lowest there is, so the worst a
        #: wrong answer here can do is spend a spare slot on a stale row.
        self._markets_visible_owner: str | None = None
        #: The halt this process announced that the record does **not**
        #: carry: ``_persist`` refused, or the row could not be read to check.
        #: Consulted by :meth:`check_watchdog`'s gate in every branch, which
        #: is what keeps a readable-but-unwritable database from re-notifying
        #: on every tick. It is not the halt state and must never be used as
        #: one -- ``engine_state.halted`` is, and a resume writes that row.
        #:
        #: **Set only when the write failed, and that is the whole trick.** A
        #: successful read saying "no explained halt is in force" cannot on
        #: its own separate *a human resumed* from *our write never landed*:
        #: during a live fault both read as an un-halted row. ``_persist``'s
        #: return value is the only thing that can, so a halt the row took
        #: clears this immediately and is policed by the row alone, where a
        #: resume is visible. Per-process, and dies with the process.
        #:
        #: **It is bounded, and the bounds are the point.** This flag says
        #: "our record is missing", which is a *state* and not an event, so it
        #: has to end when the state does. Three things end it: the row coming
        #: to carry an explained halt (:meth:`_halt_is_recorded`), the retry in
        #: :meth:`_retry_persist` landing once the database is writable again,
        #: and the fault itself ending (:meth:`check_watchdog`'s healthy
        #: branch). Without the last two it was a one-way latch on a single
        #: refused commit: :meth:`halt` cannot clear it, because the gate that
        #: consults it is what stops ``halt`` running again, and a resume
        #: clears the row -- not halted, no stated reason -- which is not an
        #: explained halt either. One ``database is locked`` then suppressed
        #: every announcement for the life of the process -- rule 9 off, with
        #: no sign of it anywhere.
        self._announced: HaltRule | None = None
        #: The announcing halt's correlation id, and the moment it was
        #: announced. Carried for exactly as long as :attr:`_announced` is --
        #: set, cleared and forgotten in the same places, on adjacent lines,
        #: so the three can never come to describe different halts.
        #:
        #: Read in **one** place, :meth:`_retry_persist`'s log line, and never
        #: by the gate -- the same standing the two fields below have. Without
        #: them a halt written half an hour late is a record with no key
        #: joining it to the alert it belongs to: three artifacts, two
        #: timestamps, one event, and a post-incident reader reconciling the
        #: gap out of prose. CLAUDE.md asks for one correlation id per
        #: decision, and every other record on this file's halt path has one.
        self._announced_correlation_id: str | None = None
        self._announced_at: datetime | None = None
        #: The announcing halt's whole decision, carried for the same span and
        #: cleared on the same lines as the three fields above. The rule and
        #: the timestamp are already in two of them; what this adds is the
        #: **reason and the inputs**, which a record written after the fault
        #: has ended cannot get from anywhere else -- the watchdog reports
        #: what is wrong *now*, and by then nothing is.
        self._announced_decision: HaltDecision | None = None
        #: A halt whose fault has **ended** with the record still missing.
        #: Moved here out of :attr:`_announced` by the healthy branch of
        #: :meth:`check_watchdog`, which is what lets that flag be cleared on
        #: schedule -- see :class:`_UnrecordedHalt` for why the two cannot be
        #: one field. Repaired on healthy ticks, at the watchdog's cadence,
        #: for at most :data:`REPAIR_MAX_ATTEMPTS` of them.
        #:
        #: **Dropped the instant any explained halt reaches the row**, which
        #: is the whole of this engine's answer to "repair or human resume,
        #: which wins". From that moment the row is the record and a resume is
        #: an observation the gate can act on, so there is nothing left to
        #: write; what stays repairable is only ever a halt no row ever
        #: carried, and therefore one no human can knowingly have cleared.
        self._pending_repair: _UnrecordedHalt | None = None
        #: How many healthy ticks have tried to write :attr:`_pending_repair`.
        #: Reset with each new pending repair; bounded by
        #: :data:`REPAIR_MAX_ATTEMPTS`.
        self._repair_attempts = 0
        #: The rule whose halt this process wrote into ``engine_state``, for
        #: as long as a read still shows *that* halt in force -- cleared the
        #: moment a read says otherwise, because then the row is somebody
        #: else's or nobody's. Read in one place: :meth:`_log_ongoing`, to
        #: tell an ongoing fault the record already describes from one it does
        #: not. Never a halt state either.
        #:
        #: Set by :meth:`halt`, and **only on a write that landed**: a halt
        #: the database refused is not in the record, so there is nothing for
        #: a later fault to agree with.
        self._recorded: HaltRule | None = None
        #: The exact ``halted_reason`` text written beside :attr:`_recorded`,
        #: which is how that field is checked against the row rather than
        #: merely remembered. ``halted_reason`` is prose, not a
        #: :class:`HaltRule`, so the row cannot say which rule explains it;
        #: what it can say is whether it still carries the sentence this
        #: process wrote. Already truncated through :func:`_bounded`, so it
        #: compares equal to what a read brings back.
        self._recorded_reason: str | None = None

    # -- state a caller can read ------------------------------------------

    @property
    def watchdog(self) -> Watchdog:
        return self._watchdog

    @property
    def opening_snapshot_ok(self) -> bool:
        """Has the opening snapshot succeeded since this process started?

        Readiness, **not** permission. The frontend's ``lastTickAt === null``
        skeleton is what this answers; whether the engine may act is
        ``engine_state.halted``, and only an explicit resume changes that.
        """
        return self._opening_snapshot_ok

    @property
    def stream_budget(self) -> StreamBudget:
        """Both websocket budgets, derived from the plan of record.

        ``engine/stream.py`` names this as the caller's responsibility. This
        is that caller. A pair rather than a single cap, because the equity
        and option streams are metered separately -- see :class:`StreamBudget`.
        """
        return stream_budget_for_plan(data_plan(self._env))

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Write ``t0`` if it has never been written, and say what state we are in.

        Does **not** touch ``halted``, in either direction. A cold start is
        halted because ``engine_state.halted`` defaults to true; a restart
        after an explicit resume stays running, and the watchdog will halt it
        within ninety seconds if the connection is not really there. A restart
        is not a resume, and it is not a halt either -- inventing a third halt
        condition here would be a fault reason on an ordinary reload.

        A database failure is logged and swallowed, the same call the lifespan
        makes for the same reason: an unmigrated database must degrade the
        routes that need a table, not stop the process booting.
        """
        at = _utc(self._now())
        try:
            with self._session_factory() as session:
                state = mark_started(session, at=at)
                # Named ``is_halted`` rather than ``halted`` on purpose.
                # A local called ``halted`` sits one careless edit away from
                # the exact literal that
                # ``test_the_runtime_has_no_code_that_clears_a_halt`` greps
                # this file for, and a guard that cries wolf is a guard
                # somebody eventually deletes.
                is_halted = state.halted
                reason = state.halted_reason
        except SQLAlchemyError:
            logger.exception(
                "could not read or write engine_state on start; run "
                "`uv run alembic upgrade head`",
                extra={
                    "event": "engine_start_degraded",
                    "policy": (
                        "a missing migration degrades the engine state, it does "
                        "not stop the process"
                    ),
                    "at": at.isoformat(),
                },
            )
            return
        logger.info(
            "engine runtime started (halted=%s)",
            is_halted,
            extra={
                "event": "engine_runtime_started",
                "policy": (
                    "a cold start comes up halted and a restart is not a resume "
                    "(CLAUDE.md rule 9)"
                ),
                "halted": is_halted,
                "halted_reason": reason,
                "equity_stream_symbol_cap": self.stream_budget.equity,
                "option_stream_quote_cap": self.stream_budget.option,
                "watchdog_timeout_seconds": self._watchdog.timeout_seconds,
                "heartbeat_armed": self._watchdog.heartbeat_armed,
                "at": at.isoformat(),
            },
        )

    def supervise(self) -> None:
        """Run the watchdog on its own asyncio task. Idempotent.

        Separate from :meth:`start` because starting needs no event loop and
        this does. The loop never dies of one bad tick: a watchdog that stops
        checking is indistinguishable from a healthy engine, which is the
        failure this whole module exists to make impossible.
        """
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                try:
                    self.check_watchdog()
                except Exception:  # pragma: no cover - defensive
                    logger.exception(
                        "the watchdog raised; it keeps running",
                        extra={"event": "engine_watchdog_error"},
                    )
        except asyncio.CancelledError:
            raise

    async def aclose(self) -> None:
        """Stop supervising. Safe on a runtime that never supervised."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        logger.info(
            "engine runtime stopped",
            extra={
                "event": "engine_runtime_stopped",
                "at": _utc(self._now()).isoformat(),
            },
        )

    # -- what the sockets and the poll loop report -------------------------

    def record_message(
        self, at: datetime | None = None, *, socket: str | None = None
    ) -> None:
        """A quote or a ``trade_updates`` message arrived on ``socket``.

        ``socket`` is what :class:`Watchdog` judges that feed by and what the
        halt reason names. Every real client passes it --
        ``corollary.sockets.VendorStream`` requires the name -- and an omitted
        one counts for the engine-wide condition only.
        """
        self._watchdog.record_message(self._moment(at), socket=socket)

    def record_poll(self, at: datetime | None = None) -> None:
        """A poll succeeded."""
        self._watchdog.record_poll(self._moment(at))

    def record_heartbeat(self, at: datetime | None = None) -> None:
        """The risk manager is alive. **No producer yet** -- see the module docstring."""
        self._watchdog.record_heartbeat(self._moment(at))

    def record_stream_open(
        self, at: datetime | None = None, *, socket: str | None = None
    ) -> None:
        """The socket came back. **This does not resume anything.**

        Rule 9, verbatim: never auto-resume on reconnect. Reconnecting into an
        unverified position state is how a bot doubles a position it already
        holds. The watchdog stops complaining about the socket -- *after* a
        tick has seen the close, never before one has, per
        :meth:`Watchdog.record_stream_open` -- and the halt in
        ``engine_state`` stays exactly where it was.

        It ends **this** socket's close and no other's. A reopen credited to
        the wrong socket is an alert saying *"has since reconnected"* about a
        feed that is still down.
        """
        self._watchdog.record_stream_open(self._moment(at), socket=socket)

    def record_stream_closed(
        self,
        at: datetime | None = None,
        *,
        socket: str | None = None,
        detail: str = "",
    ) -> None:
        """The socket closed. The next watchdog check halts on it, by name.

        *The next one*, whenever it comes: a reconnect in between does not
        take the condition away. See :meth:`Watchdog.record_stream_open`.
        """
        self._watchdog.record_stream_closed(
            self._moment(at), socket=socket, detail=detail
        )

    def expect_feed(
        self, at: datetime | None = None, *, socket: str | None = None
    ) -> None:
        """A socket is now held open. Its silence is evidence from here on.

        The market session's opening half, called by
        :class:`corollary.engine.sockets.SocketSupervisor` -- the only thing
        in this process that knows which sockets it is holding. ``at``
        defaults to now, and re-expecting an already-expected feed is a
        no-op: see :meth:`Watchdog.expect_feed` for why that guard is what
        stops a five-second loop disarming a ninety-second condition.

        **This is not a resume.** It arms a fault detector; it cannot clear
        one, and nothing on this class can.
        """
        self._watchdog.expect_feed(self._moment(at), socket=socket)

    def stop_expecting_feed(self, *, socket: str | None = None) -> None:
        """A socket is no longer held open, so its silence proves nothing.

        The closing half. Called at the session close and whenever the
        supervisor gives a socket up -- an empty option plan, for instance,
        subscribes to nothing and can never tick.

        It disarms **staleness only**: a close already recorded still halts
        the engine on the next tick, by name. A close of *our own* records
        nothing at all, which is what keeps an orderly shutdown out of the
        halt log entirely.
        """
        self._watchdog.stop_expecting_feed(socket=socket)

    def expect_handshake(self, at: datetime | None = None, *, socket: str) -> None:
        """A socket is held open and owes a handshake from ``at``.

        The order socket's condition, and the one thing rule 9 can say about a
        feed whose silence is legitimate. See
        :meth:`Watchdog.expect_handshake`; re-asserting is a no-op there for
        the reason it is for a feed.

        **This is not a resume.** It arms a fault detector; it cannot clear
        one, and nothing on this class can.
        """
        self._watchdog.expect_handshake(self._moment(at), socket=socket)

    def stop_expecting_handshake(self, *, socket: str) -> None:
        """The socket confirmed its handshake, or is no longer held open."""
        self._watchdog.stop_expecting_handshake(socket=socket)

    def record_opening_snapshot(self, at: datetime | None = None) -> None:
        """The opening snapshot succeeded.

        Counts as liveness and sets :attr:`opening_snapshot_ok`. It does
        **not** clear the halt -- see the module docstring on why the
        cold-start halt still ends at an explicit resume like every other one.
        """
        moment = self._moment(at)
        self._watchdog.record_poll(moment)
        first = not self._opening_snapshot_ok
        self._opening_snapshot_ok = True
        if first:
            logger.info(
                "opening snapshot succeeded",
                extra={
                    "event": "engine_opening_snapshot",
                    "policy": (
                        "the snapshot is readiness, not permission; the halt "
                        "ends only at POST /api/engine/resume"
                    ),
                    "at": moment.isoformat(),
                },
            )

    def _moment(self, at: datetime | None) -> datetime:
        return _utc(self._now() if at is None else at)

    # -- the budget --------------------------------------------------------

    @property
    def markets_visible(self) -> tuple[str, ...]:
        """The Markets rows a client last reported on screen. Never a ranking.

        Order is the client's, preserved and not sorted, because order is the
        tie-break for which rows keep their slots when the tail of this tier
        is cut. It is not a claim about importance that anything above this
        tier reads.
        """
        return self._markets_visible

    def set_markets_visible(
        self,
        symbols: Iterable[str],
        *,
        owner: str | None = None,
        correlation_id: str | None = None,
    ) -> MarketsVisibleOutcome:
        """Record a viewport hint. What happened, and why if it was refused.

        Decision 18's hint, and the whole of what a client may influence
        about the stream: a debounced list of the Markets rows on screen,
        arriving on ``/api/ws``, which becomes
        :attr:`~corollary.engine.stream.SubscriptionPriority.MARKETS_VISIBLE`
        units and **nothing else**. There is no argument for a priority
        because there is no other priority it could take: a client that could
        outrank a held contract could make a position mark stale by scrolling,
        which is rule 4's principle -- the engine enforces, the UI displays --
        applied to a stream budget rather than to a risk limit.

        **Applied whole or refused whole**, with the previous hint left
        standing, for the reason a subscription is: a half-applied viewport is
        a client believing it watches a list it does not watch. Three refusals,
        each logged with the rule, the inputs and the timestamp (rule 8):

        * more entries than :data:`MAX_MARKETS_VISIBLE_SYMBOLS` -- an
          unbounded client list is an unbounded allocation;
        * an OCC symbol -- the hint feeds the *equity* stream, and pointing
          this tier at option contracts is Phase 4 work (U7) with its own
          producer, not something a client can ask for;
        * anything that is not an equity ticker by shape.

        **Nothing client-derived reaches a refusal record's fields**: counts
        and server-side constants only, the same rule ``api/routes/ws.py``'s
        ``_refuse`` states at length, and :meth:`_refuse_markets_visible`
        types it. That is a claim about *this* path and not about the hint as
        a whole -- an **accepted** hint's symbols become
        :func:`~corollary.engine.stream.markets_visible_unit` keys, so they
        can still reach ``engine/stream.py``'s records. Which ones, and what
        bounds them, is written out on :meth:`markets_visible_units`, because
        a docstring claiming a property the code does not have is worse than
        no docstring: the next reader stops checking.

        Nothing here halts, resumes or touches the switch -- a re-plan is not
        a resume. Nor can it *cause* a halt: a hint alone opens no socket and
        arms no watchdog condition, which ``engine/sockets.py`` enforces by
        reading
        :attr:`~corollary.engine.stream.SubscriptionPlan.engine_subscribed`.

        Returns a :class:`MarketsVisibleOutcome` rather than a ``bool``.
        Applied, unchanged and refused are three answers and the caller needs
        all three: only *applied* is worth a re-plan, since every resubscribe
        is a gap in the marks, and only *refused* is worth an error frame.
        Conflated, a refusal read as "unchanged" is a silently ignored
        subscription.
        """
        asked = tuple(symbols)
        at = _utc(self._now())
        handle = correlation_id or self._correlation_ids()

        if len(asked) > MAX_MARKETS_VISIBLE_SYMBOLS:
            return self._refuse_markets_visible(
                rule=(
                    "a client-supplied viewport hint is bounded server-side; "
                    "an unbounded list is an unbounded allocation"
                ),
                at=at,
                correlation_id=handle,
                inputs={
                    "asked": len(asked),
                    "bound": MAX_MARKETS_VISIBLE_SYMBOLS,
                    "refused": len(asked),
                },
            )

        contracts = sum(1 for symbol in asked if stream_of(symbol) is Stream.OPTION)
        if contracts:
            return self._refuse_markets_visible(
                rule=(
                    "the viewport hint names equity tickers; an option "
                    "contract on it is a caller bug, and re-pointing this "
                    "tier at contracts is Phase 4 work"
                ),
                at=at,
                correlation_id=handle,
                inputs={"asked": len(asked), "refused": contracts},
            )

        malformed = sum(1 for symbol in asked if not _EQUITY_TICKER.match(symbol))
        if malformed:
            return self._refuse_markets_visible(
                rule=(
                    "a viewport hint is applied whole or refused whole; a "
                    "symbol that is not an equity ticker refuses the message"
                ),
                at=at,
                correlation_id=handle,
                inputs={"asked": len(asked), "refused": malformed},
            )

        # ``dict`` rather than a set: dedup that keeps the client's order,
        # because the order is what decides which rows survive the cut.
        held = tuple(dict.fromkeys(asked))
        # The owner is taken even when the set is identical: the tab that
        # spoke last is the one whose closing should drop it, and two tabs
        # showing the same rows would otherwise leave the hint owned by
        # whichever one happened to connect first.
        self._markets_visible_owner = owner
        if held == self._markets_visible:
            return MarketsVisibleOutcome(MarketsVisibleStatus.UNCHANGED)

        previous = len(self._markets_visible)
        self._markets_visible = held
        logger.info(
            "the Markets viewport hint now names %d rows",
            len(held),
            extra={
                "event": "markets_visible_set",
                "correlation_id": handle,
                "symbols": len(held),
                "previous": previous,
                "priority": SubscriptionPriority.MARKETS_VISIBLE.label,
                "at": at.isoformat(),
            },
        )
        return MarketsVisibleOutcome(MarketsVisibleStatus.APPLIED)

    def clear_markets_visible(self, *, owner: str) -> bool:
        """Drop the viewport hint if ``owner`` is the one who set it.

        A closed browser tab is nobody looking at anything, and a hint that
        outlived its client would hold the lowest tier's slots on rows that
        are on no screen. Scoped to the owner so that a second connection
        which has since sent its own hint keeps it -- the hint is one per
        engine and belongs to whoever spoke last.

        Returns whether anything changed, so the caller can re-plan only when
        there is something to re-plan.
        """
        if owner != self._markets_visible_owner:
            return False
        self._markets_visible_owner = None
        if not self._markets_visible:
            return False
        logger.info(
            "dropped the Markets viewport hint with its client",
            extra={
                "event": "markets_visible_cleared",
                "symbols": len(self._markets_visible),
                "priority": SubscriptionPriority.MARKETS_VISIBLE.label,
                "at": _utc(self._now()).isoformat(),
            },
        )
        self._markets_visible = ()
        return True

    def _refuse_markets_visible(
        self,
        *,
        rule: str,
        at: datetime,
        correlation_id: str,
        inputs: Mapping[str, int],
    ) -> MarketsVisibleOutcome:
        """Rule 8 on a refused hint: the rule, the inputs, the timestamp.

        ``inputs`` is ``Mapping[str, int]`` for the reason ``ws.py``'s is:
        the log record is the durable, searchable surface, and a
        client-derived string routed through it arrives unscrubbed. Counts
        only, and mypy refuses a string at the call site.

        Returns the refusal so that the record and the caller's answer are
        built from **one** ``rule`` string. Two would be a log line and a
        client frame free to disagree about why a message was refused, and
        the one a human reads afterwards is the log.
        """
        logger.warning(
            "refused a Markets viewport hint: %s",
            rule,
            extra={
                "event": "markets_visible_refused",
                "rule": rule,
                "correlation_id": correlation_id,
                "at": at.isoformat(),
                **dict(inputs),
            },
        )
        return MarketsVisibleOutcome(MarketsVisibleStatus.REFUSED, rule=rule)

    def markets_visible_units(self) -> tuple[SubscriptionUnit, ...]:
        """The held hint as subscription units. One row, one unit.

        Built here rather than by the caller, and that is the rule-4
        enforcement: the priority is stamped by the engine, so there is no
        call site at which a client symbol could be filed as a position
        underlying, and no argument by which a client could ask to be one.
        Rows are independent of each other -- a row is marked or it is not --
        so one symbol per unit, and the tail is trimmed rather than the tier
        blanked.

        **Where a client string goes from here, stated rather than assumed.**
        The symbol becomes the unit's ``key`` and its only entry in
        ``symbols``, so it is on the wire to the vendor -- which is the whole
        point -- and it is reachable by ``engine/stream.py``'s records. Two
        things bound that. The tier's routine drops are logged by **count**
        (``stream_client_tier_trimmed``), so the sixty-four per-symbol
        warnings a trimmed viewport used to emit no longer exist, and the
        budget summary's ``dropped_symbols`` excludes this tier. What remains
        is ``stream_subscription_unacknowledged``, whose ``absent`` list is
        the symbols the vendor did not confirm: a genuine anomaly, rare, and
        not a per-plan volume.

        That is a smaller surface than it was and it is not zero, so nothing
        here claims it is. ``engine/stream.py`` may not redact -- decision 17
        requires it to have no vendor import, no clock read and no
        environment read, so that identical inputs give an identical plan --
        and the shape filters are what stand between a browser and those
        records: :data:`_EQUITY_TICKER` here and ``_SYMBOL`` in
        ``api/routes/ws.py``. A shape filter is not a redactor: a
        twelve-character paper account number is a legal ticker shape. The
        answer is that this tier's *volume* paths carry counts only, which is
        checkable and is checked in ``test_stream.py``.
        """
        return tuple(markets_visible_unit(symbol) for symbol in self._markets_visible)

    def plan_stream_subscriptions(
        self,
        *,
        option_units: Iterable[SubscriptionUnit],
        equity_units: Iterable[SubscriptionUnit],
        correlation_id: str | None = None,
        equity_cap: int | None = None,
    ) -> StreamPlans:
        """Fit the desired symbols into this account's two budgets.

        **One call, two plans, two sockets.** The allocation is the same
        proven function run twice -- option units against the option quote
        cap, equity units against the equity symbol cap -- because every
        property it has (all-or-nothing units, free dedup, a strict prefix
        cut, ``dropped``/``not_streamed``/``spare_capacity``) is correct at
        any cap, and what was ever wrong was the number handed in. Teaching it
        to hold two budgets at once would put symbol classification inside
        allocation and turn one prefix cut into two interleaved ones.

        Which units go where is the caller's to say and **not** the caller's
        to be trusted on. These are two lists of the same type, so a book
        filed under the wrong one type-checks: five four-leg spreads passed as
        ``equity_units`` fit inside thirty slots and were admitted, handing
        twenty OCC symbols to the stock socket with ``not_streamed == 0`` and
        no banner. The equity stream never quotes them, so every leg marks at
        a last known price while the plan states that nothing is missing --
        worse than the straddling unit ``stream.py`` already refused, which at
        least emitted drop records. So each list is checked against the stream
        it was filed under, inside ``plan_subscriptions``, where the symbols
        are already being validated and where allocation never sees the
        answer.

        Neither list has a default, for the reason ``cap`` has none: an
        omitted list is a silent choice. It is planned against nothing,
        dropped by nothing, and counted by nothing, and the one banner then
        says everything is streaming while eight underlyings go unmarked. An
        empty book is an ordinary state and spells itself ``option_units=[],
        equity_units=[]``, which reads differently from a forgotten argument
        and is the distinction worth keeping.

        Both plans share one clock reading and one correlation id, because
        they are one decision; two ids would make the log read as two. The
        clock and the id come from here rather than from ``stream.py``, which
        reads neither by design -- identical inputs have to give an identical
        subscription list, and an id minted inside that module would tie the
        plan to nothing upstream of it.

        **``equity_cap`` narrows and can never widen.** The account's budget
        is what a plan is normally built at, but a 405 is the *server's* own
        figure for how many symbols one connection may carry and it is lower.
        The supervisor passes the cap in force when it re-folds the viewport
        hint, so a correction survives a scroll rather than being re-planned
        back up to the budget, refused again, and ratcheted down by halving --
        each round a whole-list resubscribe and a gap in every position
        underlying's mark. It is clamped here rather than trusted: the plan is
        built at ``min(equity_cap, budget.equity)``, so this is not a door a
        caller can widen a budget through either.
        """
        budget = self.stream_budget
        if equity_cap is not None and equity_cap < 0:
            raise ValueError(
                f"an equity cap is a count of symbols; got {equity_cap}. "
                "Zero is a legitimate answer -- it means the socket streams "
                "nothing and every symbol counts into the banner -- and "
                "below zero is a caller bug"
            )
        equity_slots = (
            budget.equity if equity_cap is None else min(equity_cap, budget.equity)
        )
        at = _utc(self._now())
        handle = correlation_id or self._correlation_ids()
        return StreamPlans(
            option=plan_subscriptions(
                option_units,
                at=at,
                correlation_id=handle,
                cap=budget.option,
                stream=Stream.OPTION,
            ),
            equity=plan_subscriptions(
                # The viewport hint is folded in here rather than passed by
                # the caller, and only into the equity list. A caller cannot
                # forget it, cannot file it under the option socket, and
                # cannot give it a priority -- the units carry
                # ``MARKETS_VISIBLE`` because :meth:`markets_visible_units`
                # stamps it. Order within the list does not matter: the
                # planner sorts by priority, so this tier is last however it
                # arrives, and the prefix cut reaches it before anything a
                # position needs. An empty hint adds nothing at all.
                [*equity_units, *self.markets_visible_units()],
                at=at,
                correlation_id=handle,
                cap=equity_slots,
                stream=Stream.EQUITY,
            ),
        )

    # -- the switch --------------------------------------------------------

    def check_watchdog(self) -> HaltDecision | None:
        """Ask the watchdog, and halt if it answers. ``None`` when all is well.

        Called by the supervisor task every :data:`WATCHDOG_INTERVAL_SECONDS`,
        and callable directly by a test or a poll loop that has just noticed
        something.

        Returns a decision **only when this call halted the engine**. An
        ongoing fault on an engine already halted *with a stated reason*
        returns ``None``: it is not news, and the alert channel would
        otherwise carry the same line every five seconds until a human read it
        once. A halt with no stated reason is a cold start rather than a
        recorded fault, and a fault during one is announced --
        :meth:`_halt_is_recorded` is where that distinction lives.

        The gate has **two terms**, because neither alone is right in all
        five states it has to cover:

        =================================  =============================  ==========
        State                              What the gate has              Outcome
        =================================  =============================  ==========
        Cold start, first fault            row halted, no reason;         announces
                                           nothing announced
        Persisted halt, ongoing fault      row halted with a reason       suppressed
        Readable but unwritable database   row does not carry the halt;   suppressed
                                           this process announced one     (and the
                                           the row refused                write is
                                                                          retried)
        Observed resume, fault present     row carries no explained       announces
                                           halt, and the halt announced
                                           *was* recorded -- so a human
                                           cleared it
        New episode, halt never recorded   row carries no explained       announces
                                           halt, and the announcement
                                           ended with the episode it
                                           belonged to
        =================================  =============================  ==========

        **The last two rows read identically to the gate and are not the same
        thing**, which is why both are listed rather than one standing in for
        the other. ``_announced is None`` means *the halt we announced was
        recorded, so the empty row is a human's doing* in the fourth, and *the
        episode that announcement belonged to ended* in the fifth -- where
        nobody cleared anything, the write was refused, the feed came back and
        then died again. Both announce, which is what a returning fault needs
        either way, but only one of them involved a human. A reader who takes
        the fourth row as the only reading of an empty :attr:`_announced` goes
        looking for a resume that never happened, and
        ``test_a_fault_that_ends_clears_an_announcement_the_row_never_took``
        is what pins the fifth.

        **A third history reaches the fourth row's shape, and it is not a
        resume either.** If the write is refused and a human then halts
        deliberately -- ``POST /api/engine/halt`` succeeding where ours did
        not -- :meth:`_halt_is_recorded` sees an explained halt in force and
        clears :attr:`_announced` against *their* record rather than ours.
        Our halt is then permanently unrecorded, because :meth:`_retry_persist`
        does nothing once :attr:`_announced` is empty. When that human resumes
        into a feed that is still dead, the gate announces, correctly -- but
        no ``engine_halt_persisted_late`` was ever written and the row's reason
        was never ours, so the only surviving record of that first fault is the
        ``engine_halt_ongoing`` line. Do not read the fourth row as promising
        that our halt reached the row; read it as the gate having nothing left
        in memory, which three different histories can produce.

        Row first: ``engine_state`` is the only source of truth about whether
        this engine is halted, and it is the row ``POST /api/engine/resume``
        clears. Memory second, and only for a halt that never reached that row
        -- see :attr:`_announced`, which is set only when the write failed for
        exactly this reason. A database that goes unreadable *after* a halt it
        did record costs one further alert and then goes quiet: the process
        can no longer see the record and has just failed to write one, and one
        alert in that state is the right price.

        **Third row, second half: the write is retried, every suppressed tick
        the row does not carry our halt.** Memory suppressing an announcement
        is a stopgap for a missing record, not a substitute for one, and it
        has to be able to end. :meth:`_retry_persist` is how it ends when the
        database comes back -- the halt finally lands, the row polices it from
        then on, and a resume is visible to this gate again. Until it lands
        nothing changes and nothing is announced, so the storm case is
        untouched. The healthy branch above is how it ends when the *fault*
        goes instead: an announcement belongs to one fault episode, and the
        episode being over is the end of it.

        **A sixth state, which the table cannot show because the gate never
        sees it: the fault ends before the write lands.** The feed dies for
        ninety seconds, the halt fires and announces, SQLite answers
        ``database is locked``, and the feed is back inside one interval. The
        next tick is *this* branch rather than the suppressed one, so
        :meth:`_retry_persist` -- which needs a decision and therefore a live
        fault -- is unreachable, and the episode used to end with the row
        saying **not halted**, with no ``halted_reason`` at all, one critical
        alert delivered, and no human resume ever asked for. That is rule 9 announced and not enforced: the
        engine trading on through a connection loss that must end in a halt
        only a human clears.

        So the healthy branch **repairs** rather than merely forgetting.
        :meth:`_repair_unrecorded_halt` writes the halt that was announced,
        and the engine ends up genuinely halted, awaiting the explicit resume
        rule 9 requires. Repairing is the opposite of resuming: the only value
        it ever writes to ``halted`` is ``True``, and there is no ordering in
        which it clears one.

        Its cost is real and is designed against rather than denied: the halt
        lands **after everything already looks fine**, which is why
        :func:`_late_reason` and :func:`_late_body` name the original
        condition and its timestamp instead of saying "engine halted". A
        critical alert on a visibly healthy feed reads as a glitch, and the
        realistic response to a glitch is a reflexive resume without reading.

        The consequence is worth stating twice because it looks like a bug and
        is not: a human who resumes while the fault is still present is
        re-halted on the next tick, with a fresh notification. That is the
        switch working, and suppressing it would leave the operator believing
        the resume took.
        """
        decision = self._watchdog.evaluate(self._now())
        if decision is None:
            # The fault ended, which ends the episode any announcement
            # belonged to. Clearing here is what bounds `_announced` to one
            # episode: without it, a single refused write silences not only
            # the rest of *this* fault -- which is correct -- but every fault
            # afterwards for the life of the process, which is the switch
            # disarmed. `_recorded` is deliberately untouched: it describes
            # the row, not an episode, and `_halt_is_recorded` maintains it.
            #
            # What the announcement *records*, though, outlives the episode,
            # so it is handed to the repair path on the way past rather than
            # dropped with the rest. See this method's docstring on the sixth
            # state: an announced halt whose write was refused and whose fault
            # then ended used to reach no path that could ever write it.
            if self._announced is not None and self._announced_decision is not None:
                self._pending_repair = _UnrecordedHalt(
                    decision=self._announced_decision,
                    correlation_id=self._announced_correlation_id,
                    announced_at=self._announced_at,
                )
                self._repair_attempts = 0
            self._announced = None
            self._announced_correlation_id = None
            self._announced_at = None
            self._announced_decision = None
            if self._pending_repair is not None:
                self._repair_unrecorded_halt(self._pending_repair)
            return None
        recorded = self._halt_is_recorded()
        if recorded or self._announced is not None:
            if not recorded and self._retry_persist(decision):
                recorded = True
            self._log_ongoing(decision, recorded=recorded)
            return None
        self.halt(decision)
        return decision

    def _log_ongoing(self, decision: HaltDecision, *, recorded: bool) -> None:
        """One line for a fault that is real but not news. The level says why.

        DEBUG while the record already describes *this rule*: an engine halted
        for a silent feed, with the feed still silent, is the expected steady
        state, and repeating that every five seconds at a visible level is how
        a log stops being read.

        **WARNING when it does not**, which is rule 8's point. A human halt
        reading ``adjusting limits`` masks a feed that died underneath it. No
        entry can be opened either way, and the next tick after a resume halts
        for the real fault and notifies, so nobody can resume into a dead feed
        unnoticed -- but for the duration of that manual halt ``engine_state``
        states a reason that is no longer the whole truth, and at DEBUG a
        post-incident reader asking *"when did the feed die?"* has nothing at
        all. The same line covers a halt no row took: an announcement with no
        record is a gap in the record by definition.

        The comparison is against :attr:`_recorded`, which
        :meth:`_halt_is_recorded` holds only while the row still carries the
        halt *this* process wrote -- matched by its text, never by parsing
        prose into a rule. So an unknown rule reads as masked, which is the
        right way round: a record this process cannot vouch for is a record
        that may not describe the fault.
        """
        masked = self._recorded is not decision.rule
        logger.log(
            logging.WARNING if masked else logging.DEBUG,
            (
                "the watchdog reports %s, and the halt in force does not "
                "describe it"
                if masked
                else "the watchdog still reports %s and a halt is already "
                "recorded"
            ),
            decision.rule.value,
            extra={
                "event": "engine_halt_ongoing",
                "rule": decision.rule.value,
                "reason": decision.reason,
                # What the record says the halt is, as far as this process can
                # tell: the rule it wrote and the row still carries, or None
                # for a halt somebody else explained or nobody recorded.
                "recorded_rule": (
                    self._recorded.value if self._recorded is not None else None
                ),
                "halt_is_recorded": recorded,
                "masked": masked,
                "policy": (
                    "an ongoing fault is not announced again while a halt is "
                    "in force; a record that does not describe the fault is "
                    "logged at WARNING, because the record is what rule 8 is "
                    "about"
                ),
                "at": decision.at.isoformat(),
            },
        )

    def _halt_is_recorded(self) -> bool:
        """Is a halt **with a stated reason** already in force? One source of truth.

        Not a cache and not a latch: read on every tick that has something to
        report, because that row is what ``POST /api/engine/resume`` writes,
        and a resume this process never hears about is exactly the case that
        used to disarm the switch for good.

        **``halted`` alone is the wrong question, and getting it wrong is
        silent.** A cold start is halted by the ``engine_state`` default with
        no reason and no timestamp -- rule 5's Paper-and-halted boot, not a
        fault -- so a gate reading ``halted`` by itself swallows the *first*
        fault after every boot, never records why, and never alerts. The
        question is whether a halt that somebody already explained is in
        force: this switch writes ``halted_reason`` when it fires, ``POST
        /api/engine/halt`` writes it when a human halts, and
        ``POST /api/engine/resume`` clears it. An explained halt is one nobody
        needs told about twice; an unexplained one is a boot, and a fault
        during it is news.

        **This answers about the row and nothing else**, including when the
        row cannot be read at all: an unreadable database records nothing this
        process can see, so the answer is ``False``. It does not follow that
        the fault is then announced -- :meth:`check_watchdog`'s gate consults
        :attr:`_announced` in every branch, and a halt that failed to persist
        left its mark there. That keeps the old, worse answer exactly where it
        is the right one: with no readable row there is nothing to diverge
        from, the halt could not have been persisted either, and repeating a
        critical alert into Discord every five seconds for a fault the human
        already has is its own failure.

        It also maintains the two memory fields, because this is the only
        place a read happens and they are only meaningful against one. A row
        carrying an explained halt makes :attr:`_announced` redundant -- the
        row is what a resume will change, so clear it, or one failed write
        would suppress every announcement for the life of the process. A row
        carrying none makes :attr:`_recorded` false: whatever this process
        wrote is gone, or never landed.

        **How a text reason is matched to a rule: it is not.** ``halted_reason``
        is prose -- this switch writes a sentence, a human writes whatever they
        like -- so nothing here parses it into a :class:`HaltRule`, and nothing
        should. What is compared is the row's reason against
        :attr:`_recorded_reason`, the exact text this process wrote when its
        own halt landed. Equal means the row is still the halt this process
        recorded, and :attr:`_recorded` names its rule. Anything else -- a
        human's halt, a newer reason written over ours by ``POST
        /api/engine/halt``, a row somebody restored -- clears
        :attr:`_recorded`, and :meth:`_log_ongoing` then reports the rule as
        unknown rather than inventing one from the prose.
        """
        try:
            with self._session_factory() as session:
                state = engine_state(session)
                recorded_reason = state.halted_reason
                in_force = bool(state.halted) and recorded_reason is not None
        except Exception:
            logger.exception(
                "could not read engine_state to see whether the engine is "
                "already halted; the gate falls back to this process's own "
                "record",
                extra={
                    "event": "engine_halt_state_unreadable",
                    "policy": (
                        "an unreadable halt state records nothing; the gate "
                        "falls back to what this process announced and could "
                        "not record, which dies with the process"
                    ),
                    "announced": (
                        self._announced.value if self._announced is not None else None
                    ),
                },
            )
            return False
        if in_force:
            self._announced = None
            self._announced_correlation_id = None
            self._announced_at = None
            self._announced_decision = None
            # The earliest point at which "an explained halt is in the row"
            # becomes observable, and therefore the earliest at which a
            # pending repair is superseded. `_repair_unrecorded_halt` drops it
            # again on its own branch, for the reader who arrives there first.
            self._pending_repair = None
            if recorded_reason != self._recorded_reason:
                self._recorded = None
                self._recorded_reason = None
        else:
            self._recorded = None
            self._recorded_reason = None
        return in_force

    def halt(self, decision: HaltDecision) -> None:
        """Persist the halt, record it, and raise the alarm.

        Rule 7: this stops new entries. Nothing is closed -- a flatten is a
        different consequence and stays a different control. Rule 8: the rule,
        the inputs and the timestamp all go into the record.

        The three steps are deliberately independent. A database that cannot
        take the write must not swallow the alert about the fault it is part
        of, so the notification is emitted whether or not the persistence
        succeeded, and the log line says which happened.
        """
        correlation_id = self._correlation_ids()
        channels = self._channels_for(HALT_EVENT)
        persisted = self._persist(decision)

        logger.warning(
            "engine halted (%s): %s",
            decision.rule.value,
            decision.reason,
            extra={
                "event": "engine_halted",
                # ``rule`` carries the enum, here and everywhere in this file:
                # a value you can count, filter and alert on. Prose lives
                # under ``policy``.
                #
                # **Mismatch worth someone's attention.**
                # ``api/routes/engine.py`` emits this same ``engine_halted``
                # event with a *sentence* in ``rule``, so a query filtering on
                # ``event=engine_halted`` and grouping by ``rule`` gets enums
                # for automatic halts and prose for manual ones. That file is
                # not this step's to edit; whoever owns it should move its
                # sentence under ``policy`` too.
                "rule": decision.rule.value,
                "reason": decision.reason,
                "inputs": dict(decision.inputs),
                "persisted": persisted,
                "channels": list(channels),
                "at": decision.at.isoformat(),
                "correlation_id": correlation_id,
            },
        )
        self._emit(
            title="Engine halted",
            body=decision.reason,
            at=decision.at,
            channels=channels,
            correlation_id=correlation_id,
        )
        # Consulted by `check_watchdog`'s gate, and never as the halt state:
        # it says the record is missing, not that the engine is running.
        #
        # This is the only place it is *set*, and the only place that can be.
        # Clearing it is somebody else's job in every case -- the row taking a
        # halt, `_retry_persist` landing this one, or the fault ending -- for
        # the plain reason that the gate stops `halt` being reached again
        # while it is set, so a line here could never run to undo it.
        #
        # **Set only when the write failed**, which is what makes the fallback
        # safe to consult. Armed after a *successful* halt it would outlive the
        # halt itself: `POST /api/engine/resume` clears the row, the next read
        # correctly reports no halt in force, and this flag would then suppress
        # the re-halt -- leaving the operator believing the resume took while
        # the fault ran on. That is the blind spot this whole gate exists to
        # close, so it must not be re-opened by the gate's own fallback. When
        # the write succeeded the row is the record, and memory is redundant.
        self._announced = None if persisted else decision.rule
        # Its provenance, for the record `_retry_persist` may have to write on
        # its behalf later: that record describes *this* halt, from a
        # timestamp that can be half an hour away, so it carries this id.
        self._announced_correlation_id = None if persisted else correlation_id
        self._announced_at = None if persisted else decision.at
        # And the decision whole, because a repair written after the fault has
        # ended has no other source for the reason and the inputs: the
        # watchdog reports what is wrong *now*, and by then nothing is.
        self._announced_decision = None if persisted else decision
        if persisted:
            # An explained halt is in the row, so any older halt still waiting
            # to be repaired is superseded -- and, more to the point, a resume
            # from here on is a resume of a record a human could actually see,
            # which the repair must never write back over.
            self._pending_repair = None
        # What the record now says, for `_log_ongoing` to compare the next
        # tick's fault against: the rule, and the exact sentence written
        # beside it. Both cleared when the write was refused -- there is no
        # record then, and an ongoing fault with no record of it is a gap in
        # the record whatever rule caused it.
        self._recorded = decision.rule if persisted else None
        self._recorded_reason = _bounded(decision.reason) if persisted else None

    def _retry_persist(self, decision: HaltDecision) -> bool:
        """Write a halt this process announced and the database refused. Again.

        Returns ``True`` when the row now carries it, which is also the tick
        on which :attr:`_announced` is handed back to the record.

        Called from the suppressed branch of :meth:`check_watchdog`, and only
        when the row does **not** carry our halt -- so the state on entry is
        always the same one: this process announced a halt, ``_persist``
        refused it, and the fault is still here. Two things follow from that,
        and both matter.

        **The record is missing, and a missing record is what gets written.**
        Rule 8 says a halt records the rule, the inputs and the timestamp;
        right now ``engine_state`` says nothing of the sort, and nothing else
        in this class will ever fix that -- :meth:`halt` cannot run again,
        because the gate that sent us here is what stops it. Left alone the
        row stays permanently wrong about a halt that really was announced,
        and ``GET /api/engine/state`` reports an engine that is running.

        **It is also what re-arms the switch.** :attr:`_announced` has no
        other way back: :meth:`halt` cannot clear it, and
        :meth:`_halt_is_recorded` clears it only on a row carrying an
        explained halt, which is exactly what the refused write prevented. So
        a single ``database is locked`` used to suppress every announcement
        for the life of the process -- including the next genuinely new fault,
        after the database recovered and after a human resumed. Once this
        write lands the row is the record again, a resume is visible to the
        gate the ordinary way, and the switch works.

        **A retry that fails changes nothing and announces nothing.** That is
        the storm case and it stays exactly as it was: still suppressed, still
        one alert for the episode. The cost is that ``_persist`` logs its
        failure once per tick rather than once per halt, which is a log line
        and not an alert -- the thing that must not repeat is the
        notification, and it does not.

        This is deliberately not an announcement either way. A row carrying no
        explained halt cannot distinguish *a human resumed* from *our write
        never landed* -- that ambiguity is the whole reason
        :attr:`_announced` exists -- so on this tick the honest reading is the
        one we have evidence for: our record is missing. The operator already
        has the alert for this fault. The *next* resume is a real observation
        and announces the ordinary way.
        """
        if self._announced is None:
            return False
        if not self._persist(decision):
            return False
        # Read out before they are cleared: the record below is the
        # announcement's, not this tick's, and its id is the only thing
        # joining the two.
        announced_correlation_id = self._announced_correlation_id
        announced_at = self._announced_at
        # Exactly what a successful `halt` sets, and for the same reasons:
        # the row is the record now, so memory of an unrecorded announcement
        # is wrong to keep, and `_log_ongoing` has a rule and a sentence to
        # compare the next tick's fault against.
        self._announced = None
        self._announced_correlation_id = None
        self._announced_at = None
        self._announced_decision = None
        # Same reason as `halt`: the row now carries an explained halt, so an
        # older unwritten one is superseded and a resume from here is visible
        # to the gate the ordinary way.
        self._pending_repair = None
        self._recorded = decision.rule
        self._recorded_reason = _bounded(decision.reason)
        logger.warning(
            "the halt for %s is now recorded; the database had refused it",
            decision.rule.value,
            extra={
                "event": "engine_halt_persisted_late",
                "rule": decision.rule.value,
                "reason": decision.reason,
                "at": decision.at.isoformat(),
                # The announcing halt's id, so this record, its `engine_halted`
                # line and its critical notification join up -- and the moment
                # it was announced, so the gap to `at` above is arithmetic
                # rather than prose.
                #
                # Not necessarily the same *rule*, and deliberately so. The
                # watchdog latches nothing and checks its conditions in order,
                # so a silent feed can be announced and the socket can then
                # formally close before the write lands; the row and this line
                # take the rule evaluated now, while the id and `announced_at`
                # come from the announcement. That is the right way round: the
                # announcing id is the only alert the operator actually
                # received for the episode, since everything after it was
                # suppressed, so joining to it beats joining to nothing. It
                # does mean `at` minus `announced_at` can measure the earlier
                # rule's outage rather than this one's. `at` and
                # the row's `halted_at` both stay the moment the write landed:
                # they are written from one decision alongside a reason
                # measured at that moment, and a timestamp that disagrees with
                # the sentence beside it is worse than one that is late.
                "correlation_id": announced_correlation_id,
                "announced_at": (
                    announced_at.isoformat() if announced_at is not None else None
                ),
                "policy": (
                    "a halt this process announced but could not write is "
                    "retried on every suppressed tick; until it lands the "
                    "record does not describe the halt, and the switch "
                    "cannot see a resume"
                ),
            },
        )
        return True

    def _repair_unrecorded_halt(self, pending: _UnrecordedHalt) -> None:
        """Write a halt whose fault has ended and whose record never landed.

        The sixth state in :meth:`check_watchdog`'s docstring, and the only
        path that can reach it: :meth:`_retry_persist` needs a live fault to
        be suppressed by, and there is none left here.

        **Repair or human resume -- the repair wins here, and it can only ever
        have been offered a halt no human could knowingly have cleared.** The
        two are in contention only while some resume might have been a
        response to this halt, and the moment any explained halt reaches the
        row (:meth:`halt` landing it, :meth:`_retry_persist` landing it, or
        :meth:`_halt_is_recorded` seeing one in force) :attr:`_pending_repair`
        is dropped -- from then on the row is the record, and a resume is an
        observation the gate acts on the ordinary way. So what survives to be
        repaired is a halt ``engine_state`` never carried: the operator got
        the alert, but ``GET /api/engine/state`` said *running* throughout,
        and a resume pressed in that window cleared nothing that existed.

        That leaves one genuinely ambiguous ordering -- a resume between the
        announcement and the repair, which reads from the row exactly like our
        write never landing, because it is the same empty row either way. It
        is resolved the same way
        ``test_a_resume_the_gate_could_not_observe_ends_with_the_engine_halted``
        resolves it in the suppressed branch: write the missing record. The
        two readings are indistinguishable and their costs are not. A halt
        written over a resume costs one more resume, by a human who is being
        told in the same breath what the halt was and when. A resume honoured
        over a halt costs an engine trading through an unrecorded connection
        loss, which is the defect this method exists to close.

        **It is not, and cannot become, a resume.** The only value anything on
        this path writes to ``halted`` is ``True``; ``_persist`` is the single
        writer and has no branch that sets it otherwise.

        **A repair that also fails is retried at the watchdog's cadence and
        then given up on, loudly** -- see :data:`REPAIR_MAX_ATTEMPTS`. The
        ordinary retry is bounded by the fault ending; this one runs *after*
        the fault has ended, so a count is the only thing that could ever stop
        it. Giving up silently would be the original defect with machinery on
        top, so the last attempt's failure is a critical notification saying
        the record is wrong and the engine reads as running.
        """
        if self._halt_is_recorded():
            # Superseded: somebody's explained halt is in force -- a manual
            # one, or a later automatic one that landed. The engine is halted
            # and awaiting a human either way, and writing our older sentence
            # over a newer record would misreport which fault is in force.
            self._pending_repair = None
            logger.info(
                "a halt is already recorded; the unwritten one for %s is dropped",
                pending.decision.rule.value,
                extra={
                    "event": "engine_halt_repair_superseded",
                    "rule": pending.decision.rule.value,
                    "reason": pending.decision.reason,
                    "inputs": dict(pending.decision.inputs),
                    "policy": (
                        "a repair is dropped the moment the row carries an "
                        "explained halt: from then on the row is the record, "
                        "and an older sentence written over it would name the "
                        "wrong fault"
                    ),
                    "at": pending.decision.at.isoformat(),
                    "correlation_id": pending.correlation_id,
                },
            )
            return

        at = _utc(self._now())
        self._repair_attempts += 1
        reason = _late_reason(pending.decision, recorded_at=at)
        # A fresh decision rather than the announced one, because the sentence
        # is different: it has to say what broke, when, and that this record
        # is late. `at` stays the moment of the *fault*, so ``halted_at`` in
        # the row is when the engine stopped trusting the feed rather than
        # when the database finally took the write.
        repaired = HaltDecision(
            rule=pending.decision.rule,
            reason=reason,
            inputs={
                **pending.decision.inputs,
                "announced_at": (
                    pending.announced_at.isoformat()
                    if pending.announced_at is not None
                    else None
                ),
                "recorded_at": at.isoformat(),
                "repair_attempt": self._repair_attempts,
            },
            at=pending.decision.at,
        )
        # The announcing halt's id, so the late record, the `engine_halted`
        # line and the critical notification all read as the one event. A new
        # id here would leave the operator's only alert joined to nothing.
        correlation_id = pending.correlation_id or self._correlation_ids()

        if self._persist(repaired):
            # The row is the record again: nothing left to repair, and
            # `_log_ongoing` has a rule and a sentence to compare against.
            self._pending_repair = None
            self._recorded = repaired.rule
            self._recorded_reason = _bounded(reason)
            logger.warning(
                "the halt for %s is recorded after the fault ended; the "
                "database had refused it",
                repaired.rule.value,
                extra={
                    "event": "engine_halt_repaired",
                    "rule": repaired.rule.value,
                    "reason": reason,
                    "inputs": dict(repaired.inputs),
                    "policy": (
                        "a halt announced and refused is written once the "
                        "database takes it, even after the fault clears -- "
                        "rule 9 ends at an explicit human resume, and an "
                        "announced halt that never became state ends nowhere"
                    ),
                    "at": repaired.at.isoformat(),
                    "correlation_id": correlation_id,
                },
            )
            self._emit(
                title="Engine halted (recorded late)",
                body=_late_body(pending.decision, recorded_at=at),
                at=at,
                channels=self._channels_for(HALT_EVENT),
                correlation_id=correlation_id,
            )
            return

        if self._repair_attempts >= REPAIR_MAX_ATTEMPTS:
            self._pending_repair = None
            logger.error(
                "the halt for %s could not be recorded in %d attempts; "
                "engine_state does not carry it",
                pending.decision.rule.value,
                self._repair_attempts,
                extra={
                    "event": "engine_halt_repair_abandoned",
                    "rule": pending.decision.rule.value,
                    "reason": pending.decision.reason,
                    "inputs": dict(repaired.inputs),
                    "policy": (
                        "the repair is bounded; when it cannot be done the "
                        "operator is told, because the record is wrong and "
                        "nothing in this process can correct it"
                    ),
                    "attempts": self._repair_attempts,
                    "at": pending.decision.at.isoformat(),
                    "correlation_id": correlation_id,
                },
            )
            self._emit(
                title="Engine halt could not be recorded",
                body=_abandoned_body(pending.decision, attempts=self._repair_attempts),
                at=at,
                channels=self._channels_for(HALT_EVENT),
                correlation_id=correlation_id,
            )
            return

        logger.warning(
            "the halt for %s is still unrecorded; retrying on the next "
            "healthy tick (%d of %d)",
            pending.decision.rule.value,
            self._repair_attempts,
            REPAIR_MAX_ATTEMPTS,
            extra={
                "event": "engine_halt_repair_deferred",
                "rule": pending.decision.rule.value,
                "reason": pending.decision.reason,
                "attempts": self._repair_attempts,
                "max_attempts": REPAIR_MAX_ATTEMPTS,
                "policy": (
                    "a failed repair announces nothing -- the operator already "
                    "has the alert for this fault, and the record is what is "
                    "missing"
                ),
                "at": pending.decision.at.isoformat(),
                "correlation_id": correlation_id,
            },
        )

    def _persist(self, decision: HaltDecision) -> bool:
        """Write the halt to ``engine_state``. False when the database refused.

        Sets ``halted`` true and nothing else touches it. There is no branch
        in this method that could set it the other way, which is the point.

        **Catches every exception, not only the database's**, and the breadth
        is the requirement rather than laziness. :meth:`halt` promises that
        persisting, logging and notifying are independent; anything escaping
        this method breaks that promise at the worst possible moment, by
        swallowing the alert about the fault it is part of. An unexpected
        error from the session factory or the ORM is no more entitled to
        silence the switch than a locked SQLite file is. ``KeyboardInterrupt`` and ``SystemExit`` derive
        from ``BaseException`` and still propagate, which is right: those are
        the process being told to stop, not the halt path failing.
        """
        try:
            with self._session_factory() as session:
                state = engine_state(session)
                state.halted = True
                state.halted_reason = _bounded(decision.reason)
                state.halted_at = decision.at
                session.commit()
            return True
        except Exception:
            logger.exception(
                "could not persist the halt; the notification is sent anyway",
                extra={
                    "event": "engine_halt_not_persisted",
                    "policy": (
                        "persisting, logging and notifying are independent; "
                        "nothing failing here may swallow the alert"
                    ),
                    "rule": decision.rule.value,
                    "at": decision.at.isoformat(),
                },
            )
            return False

    def _channels_for(self, event: str) -> tuple[str, ...]:
        """Which channels this event is routed to, in ``NOTIFICATION_CHANNELS`` order.

        Read from ``notification_route``, which a human configures in Settings.
        **A database failure falls back to every channel**, not to none: the
        failure mode of an extra alert is an annoyed reader, and the failure
        mode of a missing one is a halted engine nobody hears about.

        Catches every exception for the same reason :meth:`_persist` does: it
        runs inside :meth:`halt`, and anything that escapes it takes the
        notification with it.
        """
        try:
            with self._session_factory() as session:
                enabled = set(
                    session.scalars(
                        select(NotificationRoute.channel).where(
                            NotificationRoute.event == event,
                            NotificationRoute.enabled.is_(True),
                        )
                    )
                )
        except Exception:
            logger.exception(
                "could not read notification routing; alerting every channel",
                extra={
                    "event": "engine_notification_routing_unavailable",
                    "policy": (
                        "an unreadable route falls back to every channel -- a "
                        "missing critical alert is the worse failure"
                    ),
                    "notification_event": event,
                },
            )
            return tuple(NOTIFICATION_CHANNELS)
        return tuple(channel for channel in NOTIFICATION_CHANNELS if channel in enabled)

    def _emit(
        self,
        *,
        title: str,
        body: str,
        at: datetime,
        channels: tuple[str, ...],
        correlation_id: str,
    ) -> None:
        """Deliver one critical notification, and log what would swallow it.

        Takes the words rather than the :class:`HaltDecision`, because the two
        callers have different ones to say. A halt announced as it happens is
        "Engine halted" and the watchdog's own sentence; a halt recorded after
        its fault cleared has to say so in the title and the body both, or it
        reads as a fault happening now on a feed that is visibly fine.
        """
        notification = Notification(
            event=HALT_EVENT,
            severity=HALT_SEVERITY,
            title=title,
            body=body,
            at=at,
            correlation_id=correlation_id,
            channels=channels,
        )
        if not channels:
            logger.warning(
                "the engine halted and every notification channel is switched off",
                extra={
                    "event": "engine_halt_unrouted",
                    "policy": (
                        "silencing a critical event is permitted and is logged, "
                        "so a halt cannot be both silent and unrecorded"
                    ),
                    "notification_event": HALT_EVENT,
                    "at": at.isoformat(),
                    "correlation_id": correlation_id,
                },
            )
        elif "discord" in channels and not (
            self._env.get(DISCORD_WEBHOOK_ENV) or ""
        ).strip():
            logger.warning(
                "a critical halt is routed to Discord and %s is not set",
                DISCORD_WEBHOOK_ENV,
                extra={
                    "event": "engine_halt_discord_unconfigured",
                    "policy": (
                        "presence only -- rule 6, the webhook value never "
                        "reaches a log or a response"
                    ),
                    "notification_event": HALT_EVENT,
                    "at": at.isoformat(),
                    "correlation_id": correlation_id,
                },
            )
        # Rule 9's alert must never become rule 9's failure. The halt is
        # already persisted by the time this runs, but ``halt`` still has its
        # bookkeeping to do after this call -- ``_announced`` and friends,
        # which the watchdog's gate reads -- so a sink that raised here would
        # leave the gate believing a halt it never finished recording. The
        # shipped ``FanoutNotifier`` isolates its own sinks; this catches the
        # notifier that does not. Logged by class name only: a sink's
        # exception text is not ours to vouch for, and the Discord sink's
        # would carry its webhook URL (rule 6).
        try:
            self._notifier.emit(notification)
        except Exception as exc:
            logger.error(
                "the notifier raised delivering a halt alert; the halt stands",
                extra={
                    "event": "engine_notification_failed",
                    "policy": (
                        "a notifier failure is logged and never raised into "
                        "the halt path"
                    ),
                    "error_type": type(exc).__name__,
                    "notification_event": HALT_EVENT,
                    "notification_id": notification.id,
                    "at": at.isoformat(),
                    "correlation_id": correlation_id,
                },
            )
