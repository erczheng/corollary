"""The sentiment self-audit's runner (Phase 3 step 6, unit 6.4a).

:mod:`corollary.data.news.audit` is the pure half -- grading, accuracy and
decision 13's transitions. This module drives it end to end against the
database, the market-data provider and a clock, and is the **only writer** of
``audit_run``, ``label_outcome``, ``sentiment_accuracy``,
``sentiment_source_status`` and the ``sentiment`` category of ``audit_log``.

One run, in order (:func:`run_sentiment_audit`):

0. **Universe.** Under :data:`_RUN_LOCK`, read the watch universe *before*
   any row exists: a :class:`WatchUniverseUnavailable` (a degraded
   universe) ends the attempt with nothing written.
a. **Open.** Close any ``running`` row older than
   :data:`ABANDONED_RUN_AFTER` as ``failed`` (``detail.abandoned``) -- a
   younger one raises :class:`AuditAlreadyRunning` -- then insert this run's
   ``running`` row, both on the one start reading -- so the next run's
   ``started_at`` is never before the closed row's ``finished_at``
   (``models.AuditRun``). A cancellation that arrives while this commits
   closes the row it committed ``failed`` (``detail.stage = "open"``)
   before re-raising, so on a single cancellation it does not block the
   next run for :data:`ABANDONED_RUN_AFTER`; a second cancellation during
   that wait leaves the row to that bound (:func:`_open`).
b. **Plan.** The labels with an unwritten window, the keys already written,
   the duplicate groups already claimed, step 0's watch universe, and each
   off-watch ticker's cached tradeability verdict history
   (:func:`~corollary.data.news.signals.verdict_histories`, through the same
   fail-closed row read Movers uses) go to :func:`~audit.plan_audit`. Each
   label's ``group_pick`` is :func:`~corollary.data.news.signals.group_labels`'
   own pick (``audit.py``'s decision 24), and the history reaches back to the
   newest verdict on or before the earliest label's ET date -- never later
   than its reference session, so each label's own verdict is loaded
   (decision 25).
c. **Fetch.** One ``stock_bars`` call per session per
   :data:`BAR_BATCH_MAX_SYMBOLS` symbols (decision 6), through
   :class:`MinuteBarSource` -- the provider interface, whose implementation
   meters the shared ``data.`` bucket and **waits** on it rather than
   refusing. A failed batch is retried one symbol at a time, and a raise
   there becomes a fault for that (symbol, session); one failed request
   never stops the run, though an outage stops the *stage* (decision 6).
d. **Grade** and write the ``label_outcome`` rows, committed on their own.
e. **Accuracy.** Each source's graded outcomes referenced after
   :func:`~audit.earliest_reference_needed` go to :func:`~audit.audit_sources`.
f. **Close**, in **one** transaction: the ``sentiment_accuracy`` rows, each
   transition's ``audit_log`` row and ``sentiment_source_status`` update, and
   the run set ``succeeded`` with ``finished_at`` and ``detail``.
g. **Notify**, after the commit: each transition's notice is handed to the
   caller's ``emit``, which in the engine puts it on the context outbox and
   returns -- never awaiting Discord (decision 14).

Any other exception marks the run ``failed`` with the exception's *type*
only, logs it, and re-raises -- the scheduler's job contract. Every status
write is ``UPDATE ... WHERE id = ? AND status = 'running'`` with its rowcount
checked, so no two writers can turn one terminal state into the other; and a
cancelled run waits for its in-flight worker thread (:func:`_on_worker`)
before the lock is released. Nothing here
reads or writes ``engine_state``: a context job is never a rule-9 producer
(decision 1).

Decisions this module takes (flagged in the unit's report)
----------------------------------------------------------

1. **One run per process, by a lock; across processes, by the row's age.**
   While :data:`_RUN_LOCK` is held no other run is live *in this process*,
   and a second concurrent call raises :class:`AuditAlreadyRunning`. But
   ``dev_app`` starts the context scheduler too (``api/app.py``), so a
   ``running`` row may be another process's live run: it is treated as
   abandoned (a process that died mid-run) only when its ``started_at`` is
   more than :data:`ABANDONED_RUN_AFTER` before this run's start; a younger
   one raises :class:`AuditAlreadyRunning` and nothing is written.
2. **Accuracy rows are written in the closing transaction**, with the
   transitions, not in a transaction of their own: a failed run then leaves
   no ``sentiment_accuracy`` rows naming it, so "the latest run's figures"
   can never be a failed run's. The outcomes *are* committed first (step d):
   they are facts about labels, keyed so no later run re-writes them.
3. **A transient fetch failure never writes ``price_unavailable``.** The pure
   grader writes ``price_unavailable`` for a window with a missing price
   once it is more than a week old. A window whose missing bars are
   explained by a request that *raised* this run -- a timeout, a 5xx, a
   rate-limit refusal -- would be recorded permanently ungradable over a
   blip, so its record is held back (it stays pending and the next run
   retries) -- **but only while** ``run_at - outcome_at <=``
   :data:`~audit.TRAILING_WINDOW`. Past that, a window one of whose
   (symbol, session) pairs was **actually requested** and failed is written
   ``price_unavailable`` whatever the fault (``detail.released_past_hold_bound``):
   a fault that fails identically every week -- a 422, a bar missing a field
   -- would otherwise be re-requested forever, and the provider raises those
   as a bare ``ProviderError``, the same type as a timeout or a 5xx, so the
   type alone cannot tell them apart (only the message carries the status,
   and parsing it is not a contract). The other exception is a **vendor-data
   fault**: a :class:`~corollary.wire.WireFormatError` (bare, or as the
   provider error's cause) or a bare ``KeyError`` (a bar without its ``t``
   stamp): the vendor's row itself is bad, retrying gets the same row, and
   decision 16's quarantine applies at once as the audit module's note for
   this unit says. ``FeedAccessError`` (a 403) *is* distinguishable and is
   deliberately left transient-and-bounded: it is an entitlement or a
   misconfigured feed, which a person fixes, not a fact about the bar --
   classed as data it would write every week-old window ``price_unavailable``
   permanently on a single misconfigured Saturday, and would never count
   toward decision 6's abort. Both kinds are in ``detail.bar_faults``.
4. **``finished_at`` is strictly after ``started_at``.** The closing reading
   is the clock's, bumped to one microsecond past ``started_at`` only when
   the clock has not moved (a test's frozen clock) -- otherwise the
   half-open span would be empty and the history join would find no run. A
   clock that went *backwards* raises instead of being masked.
5. **The verdict a discovery label is graded on is the ticker's newest
   cached verdict on or before the run's Eastern date** -- the same one
   Movers would show that day -- not the verdict on the label's own day.
   (Superseded by fix A: each label reads the verdict of its own session;
   see ``audit.py``'s decision 25 and :func:`_load_verdicts`.)
6. **One request per session over at most** :data:`BAR_BATCH_MAX_SYMBOLS`
   **symbols** -- the spec's page budget (<= 190 pages a week) assumes
   multi-symbol requests; one per (symbol, session) is roughly ten times it.
   A batch starts at the session's open and ends at the latest end any of
   its symbols needs; extra bars are harmless to ``grade()``. A batch that
   raises is retried one symbol at a time, so faults stay per (symbol,
   session). After :data:`FETCH_ABORT_AFTER` consecutive transient failures
   (batches and retries alike; a success or a data fault resets the count)
   the stage stops: everything not yet requested is held back as transient
   -- and, never having been tried, is **not** subject to decision 3's
   bound: a window only an unrequested pair explains is held at any age
   (follow-up F2), since releasing it would write ``price_unavailable``
   without one attempt. The pairs of a batch that failed just before the
   stop count as unrequested too: no per-symbol retry ever attributed a
   fault to them. ``detail.fetch_aborted``
   records the count, the last error's type and how many (symbol, session)
   pairs went unrequested.
"""

import asyncio
import json
import logging
import threading
import uuid
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final, Literal, ParamSpec, Protocol, TypeVar, cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from corollary.calendars import NYSE_TZ, nyse_session_containing
from corollary.data.news.audit import (
    BENCHMARK_SYMBOL,
    TRAILING_WINDOW,
    WINDOW_SESSION_MINUTES,
    AuditLabel,
    AuditPlan,
    BarFault,
    BarRequest,
    CurrentStatus,
    GradedOutcome,
    GradeResult,
    GradingWindow,
    LabelOutcomeRecord,
    SkipReason,
    SourceAudit,
    SourceStatus,
    Transition,
    TransitionNotice,
    audit_sources,
    earliest_reference_needed,
    grade,
    graded_outcome,
    plan_audit,
    reference_instant,
)
from corollary.data.news.labels import Direction
from corollary.data.news.signals import group_labels, verdict_histories
from corollary.data.news.watchlist import MARKET_TICKER
from corollary.data.providers.interface import Bar, BarTimeframe
from corollary.db.models import (
    AuditLog,
    AuditRun,
    LabelOutcome,
    NewsArticle,
    SentimentAccuracy,
    SentimentLabelRow,
    SentimentSourceStatus,
)
from corollary.wire import WireFormatError

__all__ = [
    "ABANDONED_RUN_AFTER",
    "BAR_BATCH_MAX_SYMBOLS",
    "FETCH_ABORT_AFTER",
    "AuditAlreadyRunning",
    "AuditRunResult",
    "AuditTrigger",
    "MinuteBarSource",
    "NoticeSink",
    "SentimentAuditDeps",
    "WatchUniverseSource",
    "WatchUniverseUnavailable",
    "run_sentiment_audit",
]

logger = logging.getLogger(__name__)

AuditTrigger = Literal["scheduled", "manual"]

#: Label ids per ``IN (...)`` when reading the written keys.
_KEY_CHUNK: Final = 500

#: The bump that keeps a run's span non-empty under a clock that did not move.
_TICK: Final = timedelta(microseconds=1)

#: One run per process (decision 1 above). A ``threading.Lock`` rather than
#: an ``asyncio.Lock``: it is taken without blocking, so it never stalls the
#: event loop, and it is bound to no loop.
_RUN_LOCK: Final = threading.Lock()

#: Symbols per minute-bar request: one request per session over at most this
#: many symbols (decision 6 below). The house ceiling for a multi-symbol bars
#: request, ``ADV_MAX_SYMBOLS`` in the vendor file -- restated, because this
#: module must not import the vendor surface.
BAR_BATCH_MAX_SYMBOLS: Final = 200

#: After this many **consecutive** requests fail transiently, the fetch stage
#: stops and every (symbol, session) not yet requested is held back as
#: transient (decision 6 below). Bounds what an outage costs a run.
FETCH_ABORT_AFTER: Final = 5

#: A ``running`` row whose ``started_at`` is more than this before the new
#: run's start is abandoned; a younger one is another process's live run
#: (decision 1 below). Generous against a run's real length -- minutes even
#: at the spec's request budget -- so a slow run is never closed under itself.
ABANDONED_RUN_AFTER: Final = timedelta(hours=2)

_P = ParamSpec("_P")
_T = TypeVar("_T")


class MinuteBarSource(Protocol):
    """The one provider call the audit makes: ``MarketDataProvider.stock_bars``.

    Bars come from the provider's **historical** feed -- SIP, from
    ``ALPACA_STOCK_FEED_HISTORICAL`` inside the provider; no feed name is
    spelled here -- and the request waits on the shared ``data.`` bucket.
    """

    async def stock_bars(
        self,
        symbols: Sequence[str],
        *,
        timeframe: BarTimeframe = BarTimeframe.DAY,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> Mapping[str, Sequence[Bar]]: ...


#: The watch universe's members, ``MARKET`` included or not (the plan adds it).
#: Raises :class:`WatchUniverseUnavailable` when the universe is degraded.
WatchUniverseSource = Callable[[], Awaitable[Collection[str]]]

#: Where a transition's notice goes: ``(notice, at, correlation_id)``. Must
#: return without waiting on delivery; a raise is logged and swallowed.
NoticeSink = Callable[[TransitionNotice, datetime, str], None]


class AuditAlreadyRunning(RuntimeError):
    """Another audit run is live -- in this process, or a ``running`` row
    younger than :data:`ABANDONED_RUN_AFTER` -- so this one did not start."""


class WatchUniverseUnavailable(RuntimeError):
    """The watch universe could not be read whole, so no run starts.

    Raised by a :data:`WatchUniverseSource`. The runner reads the universe
    **before** it inserts its ``running`` row, so this leaves no row at all:
    a label classified against a universe missing its sector leaders or its
    position underlyings is written ``off_watch_untradeable`` or
    ``discovery`` permanently, so a degraded universe is a reason not to
    run, never a run. The message is a fixed reason, never vendor text.
    """


@dataclass(frozen=True, slots=True)
class SentimentAuditDeps:
    """Everything a run reads from or writes to. No runtime, no broker."""

    session_factory: Callable[[], Session]
    bars: MinuteBarSource
    watch_universe: WatchUniverseSource
    emit: NoticeSink
    clock: Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class AuditRunResult:
    """A succeeded run: its id, its two instants, its transitions and its detail."""

    run_id: int
    started_at: datetime
    finished_at: datetime
    transitions: tuple[Transition, ...]
    detail: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _FetchFault:
    fault: BarFault
    #: A vendor-data fault -- a ``WireFormatError`` (bare, or as the provider
    #: error's cause) or a bare ``KeyError`` (:func:`_is_data_fault`) --
    #: rather than a transient one.
    data_fault: bool


# --------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------- #


def _aware_utc(moment: datetime, what: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware, got {moment!r}")
    return moment.astimezone(timezone.utc)


def _is_data_fault(exc: BaseException) -> bool:
    """A malformed vendor row: retrying gets the same row (decision 3 below).

    A :class:`~corollary.wire.WireFormatError`, bare or as the provider
    error's cause, or a bare ``KeyError`` -- the provider decodes a bar's
    ``payload["t"]`` by subscript, so a row without its stamp raises exactly
    that. Everything else is transient here, bounded by the hold-back's age
    limit.
    """
    return isinstance(exc, (WireFormatError, KeyError)) or isinstance(
        exc.__cause__, WireFormatError
    )


async def _on_worker(fn: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """``asyncio.to_thread``, except a cancellation first waits for the thread.

    A cancelled ``await asyncio.to_thread(...)`` stops waiting but the thread
    runs on, so its commit could land after the run released the lock, or
    after ``_fail_run`` closed the row. Here the **first** cancellation waits
    (shielded) for the thread to finish and then re-raises; a second
    cancellation during that wait is honoured at once, so shutdown is never
    held hostage by a stuck thread.
    """
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "a sentiment audit worker thread failed after its run was cancelled",
                extra={
                    "event": "sentiment_audit_worker_failed_after_cancel",
                    "step": getattr(fn, "__name__", "unknown"),
                    "error_type": type(task.exception()).__name__,
                },
            )
        raise


def _session_of(instant: datetime) -> date:
    """The Eastern date of a session-open instant (a ``BarRequest.start``)."""
    return instant.astimezone(NYSE_TZ).date()


def _needed_sessions(item: GradingWindow) -> set[tuple[str, date]]:
    """Every (symbol, session) a window's prices are read from -- SPY included."""
    reference_day = nyse_session_containing(item.reference_at)
    outcome_day = item.outcome_at.astimezone(NYSE_TZ).date()
    symbols = {BENCHMARK_SYMBOL} if item.benchmark_only else {item.symbol, BENCHMARK_SYMBOL}
    days = {outcome_day} if reference_day is None else {reference_day, outcome_day}
    return {(symbol, day) for symbol in symbols for day in days}


def _fault_json(fault: BarFault) -> dict[str, str]:
    return {
        "symbol": fault.symbol,
        "session_date": fault.session_date.isoformat(),
        "reason": fault.reason,
    }


# --------------------------------------------------------------------- #
# Database steps (each runs on a worker thread)
# --------------------------------------------------------------------- #


def _open_run(
    session_factory: Callable[[], Session],
    started_at: datetime,
    trigger: AuditTrigger,
    correlation_id: str,
) -> int:
    """Close any abandoned ``running`` row, then insert this run's. One commit.

    Raises :class:`AuditAlreadyRunning`, writing nothing, when a ``running``
    row started no more than :data:`ABANDONED_RUN_AFTER` ago (decision 1).
    """
    with session_factory() as session:
        latest_finished = session.scalar(select(func.max(AuditRun.finished_at)))
        latest_started = session.scalar(select(func.max(AuditRun.started_at)))
        for previous in (latest_finished, latest_started):
            if previous is not None and _aware_utc(previous, "a stored run instant") > started_at:
                raise RuntimeError(
                    "the clock reads earlier than a stored audit run; refusing to "
                    "start a run whose span would overlap it"
                )
        running = [
            (row.id, _aware_utc(row.started_at, "started_at"))
            for row in session.execute(
                select(AuditRun.id, AuditRun.started_at).where(AuditRun.status == "running")
            )
        ]
        for run_id, run_started in running:
            if started_at - run_started <= ABANDONED_RUN_AFTER:
                raise AuditAlreadyRunning(
                    f"audit run {run_id} has been running since {run_started.isoformat()}, "
                    f"no more than {ABANDONED_RUN_AFTER} ago: another process's live run"
                )
        for run_id, run_started in running:
            closed = _set_terminal(
                session,
                run_id,
                status="failed",
                finished_at=started_at,
                detail={
                    "abandoned": True,
                    "closed_by_correlation_id": correlation_id,
                    "closed_at": started_at.isoformat(),
                },
            )
            if not closed:
                continue  # its own process closed it since the read: nothing to abandon
            logger.warning(
                "an abandoned sentiment audit run was closed as failed",
                extra={
                    "event": "sentiment_audit_abandoned_run_closed",
                    "audit_run_id": run_id,
                    "started_at": run_started.isoformat(),
                    "closed_at": started_at.isoformat(),
                    "correlation_id": correlation_id,
                },
            )
        run = AuditRun(started_at=started_at, finished_at=None, trigger=trigger, status="running")
        session.add(run)
        session.flush()
        run_id = run.id
        session.commit()
    return run_id


def _set_terminal(
    session: Session,
    run_id: int,
    *,
    status: Literal["succeeded", "failed"],
    finished_at: datetime,
    detail: Mapping[str, object],
) -> bool:
    """``UPDATE audit_run ... WHERE id = ? AND status = 'running'``; whether it matched.

    Every status write goes through here, so two writers -- a cancelled run's
    orphaned close and its ``_fail_run``, or two processes -- can never turn
    one terminal state into the other: the second matches no row. It is also
    the write that takes SQLite's lock, so a caller doing it first holds the
    row for the rest of its transaction.
    """
    # ``cast``: a DML statement's result is a ``CursorResult``, which carries
    # ``rowcount``; ``Session.execute`` is typed as the general ``Result``.
    result = cast(
        CursorResult[Any],
        session.execute(
            update(AuditRun)
            .where(AuditRun.id == run_id)
            .where(AuditRun.status == "running")
            .values(
                status=status,
                finished_at=finished_at,
                detail=json.dumps(detail, sort_keys=True),
            )
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount == 1


def _load_labels(
    session_factory: Callable[[], Session],
) -> tuple[list[AuditLabel], set[tuple[int, str]], set[tuple[int, str, str]]]:
    """Every label with at least one unwritten window, the keys already written,
    and the claimed duplicate groups.

    Each label carries its group and ``group_labels``' pick, computed over
    **every** label (the CTE's window runs before this query's filter). A
    group is claimed when some label in it holds an outcome row other than
    ``duplicate_copy`` -- usually a fully written one, which is not loaded
    (``audit.py``'s decision 24).
    """
    windows = len(WINDOW_SESSION_MINUTES)
    picked = group_labels()
    written_count = (
        select(func.count())
        .select_from(LabelOutcome)
        .where(LabelOutcome.label_id == SentimentLabelRow.id)
        .scalar_subquery()
    )
    stmt = (
        select(
            SentimentLabelRow.id,
            SentimentLabelRow.ticker,
            SentimentLabelRow.source,
            SentimentLabelRow.direction,
            SentimentLabelRow.labeled_at,
            NewsArticle.published_at,
            picked.c.gid,
            picked.c.pick,
        )
        .join(NewsArticle, NewsArticle.id == SentimentLabelRow.article_id)
        .join(picked, picked.c.label_id == SentimentLabelRow.id)
        .where(written_count < windows)
        .order_by(SentimentLabelRow.id)
    )
    with session_factory() as session:
        labels = [
            AuditLabel(
                label_id=row.id,
                ticker=row.ticker,
                source=row.source,
                direction=Direction(row.direction),
                published_at=row.published_at,
                labeled_at=row.labeled_at,
                group_id=row.gid,
                group_pick=row.pick == 1,
            )
            for row in session.execute(stmt)
        ]
        ids = [label.label_id for label in labels]
        written: set[tuple[int, str]] = set()
        for start in range(0, len(ids), _KEY_CHUNK):
            chunk = ids[start : start + _KEY_CHUNK]
            written.update(
                (row.label_id, row.window)
                for row in session.execute(
                    select(LabelOutcome.label_id, LabelOutcome.window).where(
                        LabelOutcome.label_id.in_(chunk)
                    )
                )
            )
        gids = sorted({label.group_id for label in labels})
        claimed: set[tuple[int, str, str]] = set()
        for start in range(0, len(gids), _KEY_CHUNK):
            chunk_gids = gids[start : start + _KEY_CHUNK]
            claimed.update(
                (row.gid, row.ticker, row.source)
                for row in session.execute(
                    select(picked.c.gid, picked.c.ticker, picked.c.source)
                    .join(LabelOutcome, LabelOutcome.label_id == picked.c.label_id)
                    .where(picked.c.gid.in_(chunk_gids))
                    .where(
                        or_(
                            LabelOutcome.skip_reason.is_(None),
                            LabelOutcome.skip_reason != SkipReason.DUPLICATE_COPY.value,
                        )
                    )
                    .distinct()
                )
            )
    return labels, written, claimed


def _load_verdicts(
    session_factory: Callable[[], Session],
    labels: Sequence[AuditLabel],
    off_watch: Collection[str],
    today: date,
    correlation_id: str,
) -> dict[str, list[tuple[date, bool]]]:
    """Each off-watch ticker's verdict history, for each label's own session.

    The lower bound is the earliest off-watch label's reference-instant ET
    date: a reference session is that date or later, so the newest verdict on
    or before every label's session is loaded.
    """
    days = [
        reference_instant(label).astimezone(NYSE_TZ).date()
        for label in labels
        if label.ticker in off_watch
    ]
    if not days:
        return {}
    with session_factory() as session:
        found = verdict_histories(session, off_watch, min(days), today, correlation_id)
    return {
        ticker: [(verdict.session_date, verdict.passes) for verdict in history]
        for ticker, history in found.items()
    }


def _write_outcomes(
    session_factory: Callable[[], Session],
    run_id: int,
    records: Sequence[LabelOutcomeRecord],
) -> None:
    with session_factory() as session:
        session.add_all(
            LabelOutcome(
                label_id=r.label_id,
                window=r.window,
                reference_at=r.reference_at,
                reference_price=r.reference_price,
                outcome_at=r.outcome_at,
                outcome_price=r.outcome_price,
                benchmark_return=r.benchmark_return,
                excess_return=r.excess_return,
                correct=r.correct,
                skip_reason=None if r.skip_reason is None else r.skip_reason.value,
                scope=None if r.scope is None else r.scope.value,
                audit_run_id=run_id,
            )
            for r in records
        )
        session.commit()


def _current_status(row: SentimentSourceStatus) -> CurrentStatus:
    return CurrentStatus(
        source=row.source,
        status=SourceStatus(row.status),
        changed_at=row.changed_at,
    )


def _load_accuracy_inputs(
    session_factory: Callable[[], Session], run_at: datetime
) -> tuple[list[CurrentStatus], list[GradedOutcome]]:
    """Each source's status row, and its graded outcomes any basis can count."""
    with session_factory() as session:
        statuses = [
            _current_status(row)
            for row in session.scalars(
                select(SentimentSourceStatus).order_by(SentimentSourceStatus.source)
            )
        ]
        outcomes: list[GradedOutcome] = []
        for status in statuses:
            earliest = earliest_reference_needed(status, run_at)
            stmt = (
                select(
                    LabelOutcome.window,
                    LabelOutcome.scope,
                    LabelOutcome.reference_at,
                    LabelOutcome.correct,
                    LabelOutcome.skip_reason,
                    SentimentLabelRow.direction,
                )
                .join(SentimentLabelRow, SentimentLabelRow.id == LabelOutcome.label_id)
                .where(SentimentLabelRow.source == status.source)
                .where(LabelOutcome.skip_reason.is_(None))
                # Bounded below only: a row referenced after the run must still
                # arrive, so decision 21 can report it.
                .where(LabelOutcome.reference_at > earliest)
            )
            for row in session.execute(stmt):
                outcome = graded_outcome(
                    source=status.source,
                    window=row.window,
                    scope=row.scope,
                    reference_at=row.reference_at,
                    correct=row.correct,
                    skip_reason=row.skip_reason,
                    direction=Direction(row.direction),
                )
                if outcome is not None:
                    outcomes.append(outcome)
    return statuses, outcomes


def _close_run(
    session_factory: Callable[[], Session],
    run_id: int,
    audits: Sequence[SourceAudit],
    statuses: Sequence[CurrentStatus],
    finished_at: datetime,
    detail: Mapping[str, object],
) -> None:
    """Step f: accuracy, transitions and the run's close -- one transaction."""
    seen = {status.source: status for status in statuses}
    with session_factory() as session:
        # First, and first on purpose: the conditional write takes the lock,
        # and a row some other writer closed matches nothing -- the whole
        # transaction is then abandoned, accuracy and transitions with it.
        if not _set_terminal(
            session, run_id, status="succeeded", finished_at=finished_at, detail=detail
        ):
            raise RuntimeError(f"audit run {run_id} is no longer running; not closing it")
        for audit in audits:
            session.add_all(
                SentimentAccuracy(
                    audit_run_id=run_id,
                    source=row.source,
                    window=row.window,
                    basis=row.basis.value,
                    scope=row.scope.value,
                    graded=row.graded,
                    correct=row.correct,
                    neutral=row.neutral,
                )
                for row in audit.accuracy
            )
            transition = audit.transition
            if transition is None:
                continue
            status_row = session.get(SentimentSourceStatus, transition.source)
            if status_row is None or _current_status(status_row) != seen[transition.source]:
                raise RuntimeError(
                    f"{transition.source}'s status changed during the run; not applying "
                    "a transition decided on the state it replaced"
                )
            fields = transition.audit_log
            session.add(
                AuditLog(
                    # Inside the run's half-open span (started_at, finished_at]:
                    # the finishing reading, never ``transition.changed_at``.
                    at=finished_at,
                    category=fields.category,
                    field=fields.field,
                    previous_value=fields.previous_value,
                    new_value=fields.new_value,
                )
            )
            status_row.status = transition.new.value
            status_row.changed_at = transition.changed_at
            status_row.reason = transition.reason
            status_row.audit_run_id = run_id
        session.commit()


def _fail_run(
    session_factory: Callable[[], Session],
    run_id: int,
    finished_at: datetime,
    detail: Mapping[str, object],
) -> bool:
    """Close the run ``failed``; ``False`` when it was no longer ``running``."""
    with session_factory() as session:
        closed = _set_terminal(
            session, run_id, status="failed", finished_at=finished_at, detail=detail
        )
        session.commit()
    return closed


# --------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------- #


def _finishing_reading(clock: Callable[[], datetime], started_at: datetime) -> datetime:
    """Decision 4 above: strictly after ``started_at``, never masking a backwards clock."""
    reading = _aware_utc(clock(), "the clock's reading")
    if reading < started_at:
        raise RuntimeError("the clock went backwards during the sentiment audit run")
    finished_at = reading if reading > started_at else started_at + _TICK
    assert finished_at > started_at
    return finished_at


@dataclass(frozen=True, slots=True)
class _Fetched:
    """Step c's result: the bars, the per-(symbol, session) faults, and an abort."""

    bars: list[Bar]
    faults: list[_FetchFault]
    #: Provider calls made, batches and one-symbol retries alike.
    requests: int
    #: (symbol, session) pairs never requested because the stage stopped early.
    unrequested: frozenset[tuple[str, date]]
    #: The last transient failure's type name when the stage stopped early.
    abort_error_type: str | None

    def attempted_transient(self) -> frozenset[tuple[str, date]]:
        """Every (symbol, session) actually requested whose request raised transiently.

        :attr:`unrequested` is deliberately not here: those were never tried,
        and :func:`_to_write` holds them at any age (decision 3).
        """
        return frozenset(
            (f.fault.symbol, f.fault.session_date) for f in self.faults if not f.data_fault
        )


def _batches(requests: Sequence[BarRequest]) -> list[list[BarRequest]]:
    """Decision 6: per session, at most :data:`BAR_BATCH_MAX_SYMBOLS` requests a batch.

    In session order, then symbol order. Every request in one session starts
    at that session's open (``BarRequest``'s contract), which is what lets a
    batch share one ``start``; that is checked here, outside any ``try``, so
    a broken plan raises rather than reading as a fetch failure.
    """
    by_session: dict[date, list[BarRequest]] = {}
    for request in requests:
        by_session.setdefault(_session_of(request.start), []).append(request)
    size = BAR_BATCH_MAX_SYMBOLS
    batches: list[list[BarRequest]] = []
    for day in sorted(by_session):
        ordered = sorted(by_session[day], key=lambda r: r.symbol)
        if len({r.start for r in ordered}) != 1:
            raise ValueError(f"bar requests for session {day.isoformat()} do not share its open")
        batches.extend(ordered[i : i + size] for i in range(0, len(ordered), size))
    return batches


async def _fetch(bars: MinuteBarSource, plan: AuditPlan, correlation_id: str) -> _Fetched:
    """Step c (decision 6): one request per batch; a failed batch is retried by symbol.

    A batch runs from its session's open to the latest end any of its
    symbols needs; the extra bars a shorter range did not ask for are
    harmless to :func:`~audit.grade`. A batch that raises is re-queued one
    symbol at a time, so a fault -- and decision 16's quarantine -- stays
    per (symbol, session). After :data:`FETCH_ABORT_AFTER` consecutive
    transient failures the stage stops; whatever is left is ``unrequested``.
    """
    queue: deque[list[BarRequest]] = deque(_batches(plan.bar_requests))
    fetched: list[Bar] = []
    faults: list[_FetchFault] = []
    requests = 0
    streak = 0
    last_error: str | None = None
    while queue and streak < FETCH_ABORT_AFTER:
        batch = queue.popleft()
        start = batch[0].start
        end = max(r.end for r in batch)
        day = _session_of(start)
        requests += 1
        try:
            answer = await bars.stock_bars(
                [r.symbol for r in batch],
                timeframe=BarTimeframe.MINUTE,
                start=start,
                end=end,
            )
        except Exception as exc:  # per batch, by design: see step c
            data_fault = _is_data_fault(exc)
            if data_fault:
                streak = 0  # the vendor answered; its row was bad
            else:
                streak += 1
                last_error = type(exc).__name__
            if len(batch) > 1:
                logger.warning(
                    "a sentiment audit bar batch failed; retrying it one symbol at a time",
                    extra={
                        "event": "sentiment_audit_bar_batch_failed",
                        "symbols": len(batch),
                        "session_date": day.isoformat(),
                        "error_type": type(exc).__name__,
                        "data_fault": data_fault,
                        "consecutive_transient_failures": streak,
                        "correlation_id": correlation_id,
                    },
                )
                queue.extendleft(reversed([[request] for request in batch]))
                continue
            (request,) = batch
            kind = "vendor_data" if data_fault else "fetch_failed"
            faults.append(
                _FetchFault(BarFault(request.symbol, day, f"{kind}:{type(exc).__name__}"), data_fault)
            )
            logger.warning(
                "a sentiment audit bar request failed; that symbol and session are "
                "graded without bars",
                extra={
                    "event": "sentiment_audit_bar_request_failed",
                    "rule": "decision 16: one (symbol, session) is quarantined, never the run",
                    "symbol": request.symbol,
                    "session_date": day.isoformat(),
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "error_type": type(exc).__name__,
                    "data_fault": data_fault,
                    "consecutive_transient_failures": streak,
                    "correlation_id": correlation_id,
                },
            )
            continue
        streak = 0
        for request in batch:
            fetched.extend(answer.get(request.symbol, ()))
    unrequested = frozenset(
        (request.symbol, _session_of(request.start)) for batch in queue for request in batch
    )
    if unrequested:
        logger.error(
            "the sentiment audit's fetch stage stopped after consecutive failures; "
            "the rest is held back and retried next run",
            extra={
                "event": "sentiment_audit_fetch_aborted",
                "rule": "decision 6: an outage costs a run at most a bounded number of requests",
                "consecutive_transient_failures": streak,
                "last_error_type": last_error,
                "unrequested_symbol_sessions": len(unrequested),
                "requests": requests,
                "correlation_id": correlation_id,
            },
        )
    return _Fetched(
        bars=fetched,
        faults=faults,
        requests=requests,
        unrequested=unrequested,
        abort_error_type=last_error if unrequested else None,
    )


def _to_write(
    plan: AuditPlan,
    result: GradeResult,
    attempted: Collection[tuple[str, date]],
    unrequested: Collection[tuple[str, date]],
) -> tuple[list[LabelOutcomeRecord], list[tuple[int, str]], list[tuple[int, str]]]:
    """Decision 3 above: hold back a ``price_unavailable`` a transient failure
    explains -- an ``attempted`` request that raised, or a pair left
    ``unrequested`` by decision 6's abort.

    While the window ended no more than :data:`~audit.TRAILING_WINDOW`
    before the run, either kind holds it. Past that, it is *released* --
    written ``price_unavailable`` like any other missing price -- only when
    one of its needed pairs is in ``attempted``: a fault that recurs
    identically every week must not be re-requested forever. A window that
    only ``unrequested`` pairs explain was never tried, and is held at any
    age (follow-up F2).

    Returns the records to write, the keys held back, and the keys released.
    """
    attempted_pairs = set(attempted)
    unrequested_pairs = set(unrequested)
    if not attempted_pairs and not unrequested_pairs:
        return list(result.records), [], []
    windows = {(g.label.label_id, g.window): g for g in plan.to_grade}
    kept: list[LabelOutcomeRecord] = []
    held: list[tuple[int, str]] = []
    released: list[tuple[int, str]] = []
    for record in result.records:
        key = (record.label_id, record.window)
        item = windows.get(key)
        if record.skip_reason is SkipReason.PRICE_UNAVAILABLE and item is not None:
            needed = _needed_sessions(item)
            tried = bool(needed & attempted_pairs)
            untried = bool(needed & unrequested_pairs)
            if tried or untried:
                if plan.run_at - item.outcome_at <= TRAILING_WINDOW or not tried:
                    held.append(key)
                    continue
                released.append(key)
        kept.append(record)
    return kept, held, released


def _detail(
    *,
    correlation_id: str,
    trigger: AuditTrigger,
    universe_size: int,
    labels_considered: int,
    plan: AuditPlan,
    result: GradeResult,
    written: Sequence[LabelOutcomeRecord],
    held: Sequence[tuple[int, str]],
    released: Sequence[tuple[int, str]],
    fetched: _Fetched,
    audits: Sequence[SourceAudit],
) -> dict[str, object]:
    """``audit_run.detail``: counts, symbols and ids only -- no vendor text, no secret."""
    capped_labels = {
        r.label_id for r in written if r.skip_reason is SkipReason.DISCOVERY_CAP
    }
    skipped = Counter(r.skip_reason.value for r in written if r.skip_reason is not None)
    faults = sorted(
        {f.fault for f in fetched.faults} | set(result.bar_faults),
        key=lambda f: (f.symbol, f.session_date, f.reason),
    )
    return {
        "correlation_id": correlation_id,
        "trigger": trigger,
        "watch_universe_size": universe_size,
        "labels_considered": labels_considered,
        "bar_requests": len(plan.bar_requests),
        "provider_requests": fetched.requests,
        "fetch_aborted": (
            None
            if not fetched.unrequested
            else {
                "after_consecutive_transient_failures": FETCH_ABORT_AFTER,
                "last_error_type": fetched.abort_error_type,
                "unrequested_symbol_sessions": len(fetched.unrequested),
            }
        ),
        "graded": sum(1 for r in written if r.skip_reason is None),
        "skipped": dict(sorted(skipped.items())),
        "skipped_total": sum(skipped.values()),
        "awaiting_bars": len(result.awaiting_bars) + len(held),
        "held_for_transient_fetch_failure": len(held),
        "released_past_hold_bound": len(released),
        "discovery_cap_skipped_labels": len(capped_labels),
        "discovery_cap_skipped_tickers": len(plan.discovery_capped_tickers),
        "discovery_capped_tickers": list(plan.discovery_capped_tickers),
        "bar_faults": [_fault_json(f) for f in faults],
        "unschedulable_label_ids": list(plan.unschedulable_label_ids),
        "unverdicted_label_counts": dict(plan.unverdicted_label_counts),
        "future_referenced": {
            a.source: {
                "count": len(a.future_referenced),
                "earliest": a.future_referenced[0].reference_at.isoformat(),
            }
            for a in audits
            if a.future_referenced
        },
        "transitions": [
            {"source": a.transition.source, "previous": a.transition.previous.value,
             "new": a.transition.new.value}
            for a in audits
            if a.transition is not None
        ],
    }


def _emit_all(
    emit: NoticeSink, transitions: Iterable[Transition], at: datetime, correlation_id: str
) -> None:
    """Step g. **Never raises**: the run has committed, and a notice cannot undo it."""
    for transition in transitions:
        if transition.notice is None:
            continue  # unaudited -> active: nothing was withdrawn or restored
        try:
            emit(transition.notice, at, correlation_id)
        except Exception as exc:
            logger.error(
                "a sentiment transition's notice could not be queued; the transition stands",
                extra={
                    "event": "sentiment_audit_notice_not_queued",
                    "source": transition.source,
                    "notification_event": transition.notice.event.value,
                    "error_type": type(exc).__name__,
                    "correlation_id": correlation_id,
                },
            )


async def run_sentiment_audit(
    deps: SentimentAuditDeps, *, trigger: AuditTrigger
) -> AuditRunResult:
    """One sentiment self-audit run, end to end (the module docstring's a-g).

    Raises :class:`AuditAlreadyRunning`, before touching the database, when
    another run is live in this process, and from step a when another
    process's ``running`` row is younger than :data:`ABANDONED_RUN_AFTER`.
    Raises :class:`WatchUniverseUnavailable`, with no row written, when the
    universe is degraded. Any failure after the run's row is inserted marks
    it ``failed`` -- ``detail`` names the stage and the exception's type,
    never its message -- and re-raises.
    """
    if trigger not in ("scheduled", "manual"):
        raise ValueError(f"unknown audit trigger {trigger!r}")
    if not _RUN_LOCK.acquire(blocking=False):
        raise AuditAlreadyRunning("a sentiment audit run is already in progress")
    try:
        return await _run_locked(deps, trigger)
    finally:
        _RUN_LOCK.release()


async def _open(
    deps: SentimentAuditDeps, started_at: datetime, trigger: AuditTrigger, correlation_id: str
) -> int:
    """Step a, logged either way.

    A cancellation here arrives after :func:`_on_worker` has waited for
    ``_open_run``'s thread -- which may well have committed this run's
    ``running`` row. Left open, that row would refuse every run for
    :data:`ABANDONED_RUN_AFTER`, so the id is noted as the thread commits it
    and, on the cancel path, that row is closed ``failed`` (``stage =
    "open"``) before the cancellation propagates: only this run's own row,
    by its id. A *second* cancellation during ``_on_worker``'s wait returns
    before the thread does; the row, if it then lands, is left for the
    abandonment bound to close -- shutdown is not held hostage.
    """
    opened: list[int] = []

    def open_and_note(
        session_factory: Callable[[], Session],
        at: datetime,
        run_trigger: AuditTrigger,
        cid: str,
    ) -> int:
        new_id = _open_run(session_factory, at, run_trigger, cid)
        opened.append(new_id)  # after the commit: this id names a row that exists
        return new_id

    try:
        run_id = await _on_worker(
            open_and_note, deps.session_factory, started_at, trigger, correlation_id
        )
    except asyncio.CancelledError as exc:
        if not opened:
            logger.error(
                "sentiment audit run cancelled before its row was confirmed "
                "written; a row that lands later is closed by the "
                "ABANDONED_RUN_AFTER bound",
                extra={
                    "event": "sentiment_audit_not_started",
                    "row_confirmed": False,
                    "trigger": trigger,
                    "error_type": type(exc).__name__,
                    "started_at": started_at.isoformat(),
                    "correlation_id": correlation_id,
                },
            )
            raise
        # Closes the row and logs; it re-raises only a further cancellation.
        await _record_failure(deps, opened[0], started_at, "open", exc, correlation_id)
        raise
    except Exception as exc:
        logger.error(
            "sentiment audit run could not start",
            extra={
                "event": "sentiment_audit_not_started",
                "trigger": trigger,
                "error_type": type(exc).__name__,
                "started_at": started_at.isoformat(),
                "correlation_id": correlation_id,
            },
        )
        raise
    logger.info(
        "sentiment audit run started",
        extra={
            "event": "sentiment_audit_started",
            "audit_run_id": run_id,
            "trigger": trigger,
            "started_at": started_at.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    return run_id


async def _run_locked(deps: SentimentAuditDeps, trigger: AuditTrigger) -> AuditRunResult:
    correlation_id = uuid.uuid4().hex
    started_at = _aware_utc(deps.clock(), "the clock's reading")
    # The universe first, before any row exists: a degraded one is a reason
    # not to run (WatchUniverseUnavailable), and leaves nothing behind.
    try:
        members = await deps.watch_universe()
    except WatchUniverseUnavailable as exc:
        logger.warning(
            "sentiment audit run not started: the watch universe is degraded",
            extra={
                "event": "sentiment_audit_not_started",
                "rule": (
                    "a label classified against a degraded watch universe is "
                    "written permanently; no run until it is whole"
                ),
                "reason": str(exc),
                "trigger": trigger,
                "started_at": started_at.isoformat(),
                "correlation_id": correlation_id,
            },
        )
        raise
    except Exception as exc:
        # Any other failure reading it is a plan-stage failure like the rest:
        # a row, closed ``failed``, naming the stage and the type.
        run_id = await _open(deps, started_at, trigger, correlation_id)
        await _record_failure(deps, run_id, started_at, "plan", exc, correlation_id)
        raise
    run_id = await _open(deps, started_at, trigger, correlation_id)

    stage = "plan"
    try:
        labels, written_keys, claimed = await _on_worker(_load_labels, deps.session_factory)
        universe = frozenset(members) | {MARKET_TICKER}
        off_watch = {label.ticker for label in labels if label.ticker not in universe}
        tradeable = await _on_worker(
            _load_verdicts,
            deps.session_factory,
            labels,
            off_watch,
            started_at.astimezone(NYSE_TZ).date(),
            correlation_id,
        )
        plan = plan_audit(
            labels,
            watch_universe=universe,
            tradeable=tradeable,
            written=written_keys,
            claimed_groups=claimed,
            run_at=started_at,
        )

        stage = "fetch"
        fetched = await _fetch(deps.bars, plan, correlation_id)

        stage = "grade"
        result = grade(plan, fetched.bars)
        to_write, held, released = _to_write(
            plan, result, fetched.attempted_transient(), fetched.unrequested
        )
        if released:
            logger.warning(
                "windows a transient fetch failure explains are past the hold-back "
                "bound and were written price_unavailable",
                extra={
                    "event": "sentiment_audit_hold_back_expired",
                    "rule": "decision 3: a transient failure holds a window back for the trailing window at most",
                    "windows": len(released),
                    "audit_run_id": run_id,
                    "correlation_id": correlation_id,
                },
            )
        await _on_worker(_write_outcomes, deps.session_factory, run_id, to_write)

        stage = "accuracy"
        statuses, outcomes = await _on_worker(
            _load_accuracy_inputs, deps.session_factory, started_at
        )
        audits = audit_sources(statuses, outcomes, started_at)

        stage = "close"
        detail = _detail(
            correlation_id=correlation_id,
            trigger=trigger,
            universe_size=len(universe),
            labels_considered=len(labels),
            plan=plan,
            result=result,
            written=to_write,
            held=held,
            released=released,
            fetched=fetched,
            audits=audits,
        )
        finished_at = _finishing_reading(deps.clock, started_at)
        await _on_worker(
            _close_run, deps.session_factory, run_id, audits, statuses, finished_at, detail
        )
    except BaseException as exc:
        await _record_failure(deps, run_id, started_at, stage, exc, correlation_id)
        raise

    transitions = tuple(a.transition for a in audits if a.transition is not None)
    for transition in transitions:
        logger.warning(
            "sentiment source %s: %s -> %s",
            transition.source,
            transition.previous.value,
            transition.new.value,
            extra={
                "event": "sentiment_source_transition",
                "source": transition.source,
                "previous": transition.previous.value,
                "new": transition.new.value,
                "reason": transition.reason,
                "audit_run_id": run_id,
                "correlation_id": correlation_id,
            },
        )
    for audit in audits:
        if audit.future_referenced:
            logger.error(
                "graded outcomes referenced after the run were left out of accuracy",
                extra={
                    "event": "sentiment_audit_future_referenced",
                    "rule": "decision 21: an outcome referenced after the run counts nowhere",
                    "source": audit.source,
                    "count": len(audit.future_referenced),
                    "earliest": audit.future_referenced[0].reference_at.isoformat(),
                    "audit_run_id": run_id,
                    "correlation_id": correlation_id,
                },
            )
    _emit_all(deps.emit, transitions, finished_at, correlation_id)
    logger.info(
        "sentiment audit run succeeded",
        extra={
            "event": "sentiment_audit_succeeded",
            "audit_run_id": run_id,
            "trigger": trigger,
            "graded": detail["graded"],
            "skipped_total": detail["skipped_total"],
            "awaiting_bars": detail["awaiting_bars"],
            "bar_faults": detail["bar_faults"],
            "fetch_aborted": detail["fetch_aborted"],
            "transitions": len(transitions),
            "finished_at": finished_at.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    return AuditRunResult(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        transitions=transitions,
        detail=detail,
    )


async def _record_failure(
    deps: SentimentAuditDeps,
    run_id: int,
    started_at: datetime,
    stage: str,
    exc: BaseException,
    correlation_id: str,
) -> None:
    """Mark the run failed and log it.

    Never raises over the original exception -- **except** a cancellation
    that arrives while the failure is being recorded, which propagates: it
    is the caller's to honour, and swallowing it would end a cancelled task
    with the run's own exception instead. :func:`_on_worker` has by then
    waited for the ``_fail_run`` thread, so the row is closed either way.
    """
    detail = {
        "correlation_id": correlation_id,
        "stage": stage,
        "error_type": type(exc).__name__,
    }
    try:
        try:
            finished_at = _finishing_reading(deps.clock, started_at)
        except Exception:
            finished_at = started_at + _TICK
        try:
            closed = await _on_worker(
                _fail_run, deps.session_factory, run_id, finished_at, detail
            )
        except asyncio.CancelledError:
            logger.error(
                "a sentiment audit run was cancelled while its failure was recorded",
                extra={
                    "event": "sentiment_audit_failure_recording_cancelled",
                    "audit_run_id": run_id,
                    "correlation_id": correlation_id,
                },
            )
            raise
        except Exception as close_exc:
            logger.error(
                "a failed sentiment audit run could not be marked failed; the next run closes it",
                extra={
                    "event": "sentiment_audit_failure_not_recorded",
                    "audit_run_id": run_id,
                    "error_type": type(close_exc).__name__,
                    "correlation_id": correlation_id,
                },
            )
        else:
            if not closed:
                # Another writer closed it first -- or, when the cancellation
                # arrived during step f, the run's own close committed: then
                # its transitions stand and their notices were never queued.
                logger.error(
                    "a failed sentiment audit run was no longer running; its row stands",
                    extra={
                        "event": "sentiment_audit_failure_not_applied",
                        "audit_run_id": run_id,
                        "stage": stage,
                        "correlation_id": correlation_id,
                    },
                )
    finally:
        logger.error(
            "sentiment audit run failed at %s",
            stage,
            extra={
                "event": "sentiment_audit_failed",
                "rule": "decision 12: a failed run is recorded failed; the engine's halt state is untouched",
                "audit_run_id": run_id,
                "stage": stage,
                "error_type": type(exc).__name__,
                "started_at": started_at.isoformat(),
                "correlation_id": correlation_id,
            },
        )
