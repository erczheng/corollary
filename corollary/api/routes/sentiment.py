"""The sentiment self-audit on Settings: what each source's status is, and a run on demand.

Design spec, *API*: ``GET /api/settings/sentiment`` -- per source: status and
since when, the accuracy per window with its graded count and basis, whether
the source is eligible to demote or re-promote and how many more labels it
needs, last and next run. ``POST /api/settings/sentiment/audit`` (hourly
limit). **No reinstate route** -- the audit is the only writer of the status
(Q6). Nothing in this module writes ``sentiment_source_status``; the
``POST`` starts the same runner the Saturday job runs, and that runner is
what writes it.

``GET /api/settings/sentiment``
-------------------------------

Read only, and counts only. Every figure comes from the **latest succeeded**
``audit_run``'s ``sentiment_accuracy`` rows, as integers: no percentage is
computed here, so no rounded number crosses the wire beside the counts it
came from, and no float exists anywhere on the path.

* **Since when.** ``changed_at`` is NULL on an ``unaudited`` row by design
  (``SentimentSourceStatus``): seeding is not a change. It is served as
  ``null`` with ``neverAudited: true`` -- never the migration's date, the
  row's insert time or "now".
* **A missing row** (both are seeded, so only a damaged database lacks one)
  is shown as ``unaudited`` for display, and says so: ``statusMissing:
  true``, so it never reads as a clean "never audited". That fallback is the
  page's only; nothing that *acts* on a status may take it (see
  ``routes.news.read_source_statuses``).
* **Eligibility** is decision 13's, from the status: an ``unaudited`` or
  ``active`` source counts toward *demotion* (``DEMOTION_FLOOR`` per window,
  ``trailing_28d``), a ``demoted`` one toward *re-promotion*
  (``REPROMOTION_FLOOR``, ``since_demotion``). ``needed`` is how many more
  graded directional labels a window needs to reach its floor, ``0`` once it
  has. A demoted source whose latest run is the one that demoted it has
  counted nothing since: that run judged it as ``active``, on
  ``trailing_28d``, so it wrote no ``since_demotion`` row. That reads as
  ``graded: 0`` with ``asOfRunId: null`` -- a true count, not a stand-in.
* **Next run** is the ``sentiment_audit`` job's next slot from the
  scheduler's status, ``null`` with no scheduler running.

``POST /api/settings/sentiment/audit``
--------------------------------------

**202, never a wait for the run.** A run takes minutes, so it is a task on
the engine's event loop, not the request. The task is the scheduler's
``run_sentiment_audit_now`` -- the one manual entry point, which composes the
run's deps and translates a degraded universe into
``SentimentAuditUnavailable``; nothing here composes a run. The route waits
only for the run to **open its row**: the runner's ``on_opened`` hook fires
once its ``running`` ``audit_run`` row is committed, and the 202 carries that
row's id. Every refusal the run itself makes happens before that, so each
reaches the caller as a 409 or a 503 -- never as a 202 for a run that never
was.

**The refusals, in the order they are checked**, each logged with its rule,
inputs and timestamp (CLAUDE.md rule 8's spirit: silent rejection is a bug):

1. **503** -- no context services (an app started without its scheduler).
2. **409** -- this app's own manual task is still in flight.
3. **409** -- a ``running`` ``audit_run`` row no older than
   ``ABANDONED_RUN_AFTER`` (a scheduled run, or another process's).
4. **429** -- a **manual** run started less than :data:`MANUAL_AUDIT_INTERVAL`
   ago, with ``Retry-After``. **The limit is read from ``audit_run``**, so a
   restart does not reset it (rule 4: enforced server-side, from stored
   state). Scheduled runs do not count against it: the limit is on the
   owner's button, and the unique index already stops two runs overlapping.

Then the run starts, and refuses in its own order before it opens a row:

5. **503** -- no market-data provider for the SIP minute bars
   (``run_sentiment_audit_now``, before the runner).
6. **409** -- the runner's in-process lock is held (a scheduled run here).
7. **503** -- the watch universe is degraded. Fixed text, never vendor text.
8. **409** -- another process's ``running`` row landed after check 3, and
   the runner's own step a refused beside it.

A refused request writes no row, so a 409 or a 503 costs no hour. A run whose
universe read *fails* (rather than reporting degraded) opens its row only to
close it ``failed``; that is a **500**, and the row does count.

**It notifies** (decision 20: every operator action notifies, once the
action has committed). ``sentiment_audit_requested``, ``info``, bell off and
Discord on -- the config events' terms -- scheduled as a response background
task only after ``on_opened``, so it never delays the 202 and never announces
a run that did not open. A refusal emits nothing. A transition the run makes
notifies on its own (``sentiment_demoted`` / ``sentiment_repromoted``),
through the scheduler's outbox.
"""

import asyncio
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Final

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from corollary.api.deps import ApiError, SchedulerStatusDep, SessionDep
from corollary.api.operator import notify_after_response, sentiment_audit_notice
from corollary.api.schemas import (
    AuditRunStatus,
    AuditRunSummary,
    AuditRunTrigger,
    NewsLabelSource,
    NewsSourceStatus,
    SentimentAccuracyBasis,
    SentimentAuditStarted,
    SentimentAuditStatus,
    SentimentCounts,
    SentimentEligibility,
    SentimentEligibilityKind,
    SentimentSourceAudit,
    SentimentWindow,
    SentimentWindowAccuracy,
    SentimentWindowProgress,
)
from corollary.data.news.audit import DEMOTION_FLOOR, REPROMOTION_FLOOR
from corollary.data.news.audit_runner import (
    ABANDONED_RUN_AFTER,
    AuditAlreadyRunning,
    AuditRunResult,
    WatchUniverseUnavailable,
)
from corollary.db.models import (
    SENTIMENT_SOURCES,
    SENTIMENT_WINDOWS,
    AuditRun,
    SentimentAccuracy,
    SentimentSourceStatus,
)
from corollary.engine.scheduler import (
    SENTIMENT_AUDIT_JOB,
    ContextServices,
    SentimentAuditUnavailable,
    run_sentiment_audit_now,
)

__all__ = [
    "MANUAL_AUDIT_INTERVAL",
    "audit_now",
    "router",
]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/sentiment", tags=["settings"])

#: The manual trigger's limit: one **manual** run per this interval, measured
#: from the last manual ``audit_run.started_at`` (design spec, *API*).
MANUAL_AUDIT_INTERVAL: Final = timedelta(hours=1)

#: The 503 body with no scheduler. Fixed text: what failed and what did not
#: happen. The run's own 503s carry the scheduler's fixed text.
_NO_SERVICES: Final = (
    "the sentiment audit runs on the context services, and this app was started "
    "without its scheduler; nothing was started"
)

_WIRE_SOURCE: Final[Mapping[str, NewsLabelSource]] = {"rules": "rules", "massive": "massive"}
_WIRE_STATUS: Final[Mapping[str, NewsSourceStatus]] = {
    "unaudited": "unaudited",
    "active": "active",
    "demoted": "demoted",
}
_WIRE_WINDOW: Final[Mapping[str, SentimentWindow]] = {"1h": "1h", "1d": "1d"}
_WIRE_BASIS: Final[Mapping[str, SentimentAccuracyBasis]] = {
    "trailing_28d": "trailing_28d",
    "since_demotion": "since_demotion",
}
_WIRE_TRIGGER: Final[Mapping[str, AuditRunTrigger]] = {
    "scheduled": "scheduled",
    "manual": "manual",
}
_WIRE_RUN_STATUS: Final[Mapping[str, AuditRunStatus]] = {
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
}
_BASIS_ORDER: Final[tuple[str, ...]] = ("trailing_28d", "since_demotion")
_SCOPES: Final[tuple[str, ...]] = ("all", "watch", "discovery")


def audit_now() -> datetime:
    """The request's clock, aware UTC. A dependency so a test can fix it."""
    return datetime.now(timezone.utc)


AuditNowDep = Annotated[datetime, Depends(audit_now)]


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def _latest_run(session: Session, *, succeeded_only: bool) -> AuditRun | None:
    """The newest run, or the newest succeeded one.

    ``started_at`` is ``UtcDateTime``, whose SQL ordering is chronological
    (see ``audit.earliest_reference_needed``); ``id`` breaks a tie.
    """
    stmt = select(AuditRun)
    if succeeded_only:
        stmt = stmt.where(AuditRun.status == "succeeded")
    stmt = stmt.order_by(AuditRun.started_at.desc(), AuditRun.id.desc()).limit(1)
    return session.scalars(stmt).first()


def _summary(run: AuditRun | None) -> AuditRunSummary | None:
    if run is None:
        return None
    return AuditRunSummary(
        id=run.id,
        trigger=_WIRE_TRIGGER[run.trigger],
        status=_WIRE_RUN_STATUS[run.status],
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _last_manual_start(session: Session) -> datetime | None:
    stmt = (
        select(AuditRun.started_at)
        .where(AuditRun.trigger == "manual")
        .order_by(AuditRun.started_at.desc(), AuditRun.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _manual_available_at(last_manual: datetime | None, now: datetime) -> datetime | None:
    """When the limit next admits a manual run; ``None`` when it admits one now.

    Admitted at exactly :data:`MANUAL_AUDIT_INTERVAL` after the last manual
    start -- "less than an hour ago" is what refuses.
    """
    if last_manual is None:
        return None
    available = last_manual + MANUAL_AUDIT_INTERVAL
    return available if available > now else None


def _retry_after_seconds(available: datetime, now: datetime) -> int:
    """Whole seconds until ``available``, rounded **up** -- a client that waits
    exactly this long is admitted, never refused by a fraction of a second."""
    remaining = available - now
    whole = remaining.days * 86_400 + remaining.seconds
    if remaining.microseconds:
        whole += 1
    return max(whole, 1)


def _live_running(session: Session, now: datetime) -> AuditRun | None:
    """A ``running`` row the runner would refuse to start beside.

    The runner's own test (``_open_run``): a row started no more than
    ``ABANDONED_RUN_AFTER`` ago is live; an older one is closed as abandoned
    by the next run, so it does not block one.
    """
    row = session.scalars(select(AuditRun).where(AuditRun.status == "running")).first()
    if row is None or now - row.started_at > ABANDONED_RUN_AFTER:
        return None
    return row


def _counts(row: SentimentAccuracy) -> SentimentCounts:
    return SentimentCounts(graded=row.graded, correct=row.correct, neutral=row.neutral)


def _accuracy_by_source(
    session: Session, run: AuditRun | None, correlation_id: str
) -> dict[str, dict[tuple[str, str], SentimentWindowAccuracy]]:
    """``{source: {(window, basis): accuracy}}`` for one run, every scope present.

    A (source, window, basis) missing any of its three scopes is a damaged
    run -- the runner writes all three, zeros included -- and is left out and
    logged rather than filled with zeros nobody counted.
    """
    if run is None:
        return {}
    grouped: dict[tuple[str, str, str], dict[str, SentimentAccuracy]] = {}
    for row in session.scalars(
        select(SentimentAccuracy).where(SentimentAccuracy.audit_run_id == run.id)
    ):
        grouped.setdefault((row.source, row.window, row.basis), {})[row.scope] = row
    out: dict[str, dict[tuple[str, str], SentimentWindowAccuracy]] = {}
    for (source, window, basis), scopes in grouped.items():
        if any(scope not in scopes for scope in _SCOPES):
            logger.error(
                "a sentiment_accuracy cell is missing a scope; not served",
                extra={
                    "event": "sentiment_accuracy_incomplete",
                    "rule": "every (source, window, basis) has all, watch and discovery rows",
                    "audit_run_id": run.id,
                    "source": source,
                    "window": window,
                    "basis": basis,
                    "scopes": sorted(scopes),
                    "correlation_id": correlation_id,
                },
            )
            continue
        out.setdefault(source, {})[(window, basis)] = SentimentWindowAccuracy(
            window=_WIRE_WINDOW[window],
            basis=_WIRE_BASIS[basis],
            combined=_counts(scopes["all"]),
            watch=_counts(scopes["watch"]),
            discovery=_counts(scopes["discovery"]),
        )
    return out


def _eligibility(
    status: str,
    cells: Mapping[tuple[str, str], SentimentWindowAccuracy],
    run: AuditRun | None,
) -> SentimentEligibility:
    """Decision 13's eligibility for the status the source is in now."""
    if status == "demoted":
        kind: SentimentEligibilityKind = "repromote"
        basis = "since_demotion"
        floor = REPROMOTION_FLOOR
    else:
        kind = "demote"
        basis = "trailing_28d"
        floor = DEMOTION_FLOOR
    windows: list[SentimentWindowProgress] = []
    counted_on_basis = False
    for window in SENTIMENT_WINDOWS:
        cell = cells.get((window, basis))
        graded = 0 if cell is None else cell.combined.graded
        counted_on_basis = counted_on_basis or cell is not None
        windows.append(
            SentimentWindowProgress(
                window=_WIRE_WINDOW[window],
                graded=graded,
                needed=max(floor - graded, 0),
            )
        )
    return SentimentEligibility(
        kind=kind,
        basis=_WIRE_BASIS[basis],
        floor=floor,
        eligible=all(w.graded >= floor for w in windows),
        windows=windows,
        as_of_run_id=run.id if run is not None and counted_on_basis else None,
    )


def _source_audit(
    source: str,
    row: SentimentSourceStatus | None,
    cells: Mapping[tuple[str, str], SentimentWindowAccuracy],
    run: AuditRun | None,
    correlation_id: str,
) -> SentimentSourceAudit:
    if row is None:
        # Both rows are seeded (migration 0014, ``db.seed``): a missing one is
        # a damaged database. Shown as unaudited for display only, with
        # ``status_missing`` saying so -- never as a clean "never audited".
        logger.error(
            "a labelling source has no sentiment_source_status row; shown as unaudited",
            extra={
                "event": "sentiment_source_status_missing",
                "rule": (
                    "every source has one seeded sentiment_source_status row; "
                    "a missing one is shown as unaudited, never active"
                ),
                "source": source,
                "correlation_id": correlation_id,
            },
        )
        status, changed_at, reason = "unaudited", None, None
    else:
        status, changed_at, reason = row.status, row.changed_at, row.reason
    accuracy = [
        cells[(window, basis)]
        for window in SENTIMENT_WINDOWS
        for basis in _BASIS_ORDER
        if (window, basis) in cells
    ]
    return SentimentSourceAudit(
        source=_WIRE_SOURCE[source],
        status=_WIRE_STATUS[status],
        changed_at=changed_at,
        # A missing row is not evidence of never having been audited.
        never_audited=row is not None and changed_at is None,
        status_missing=row is None,
        reason=reason,
        accuracy=accuracy,
        eligibility=_eligibility(status, cells, run),
    )


@router.get("", summary="Each sentiment source's audit status, sample counts and eligibility")
def read_sentiment_audit(
    session: SessionDep, scheduler: SchedulerStatusDep, now: AuditNowDep
) -> SentimentAuditStatus:
    correlation_id = uuid.uuid4().hex
    accuracy_run = _latest_run(session, succeeded_only=True)
    last_run = _latest_run(session, succeeded_only=False)
    by_source = _accuracy_by_source(session, accuracy_run, correlation_id)
    rows = {row.source: row for row in session.scalars(select(SentimentSourceStatus))}
    job = None if scheduler is None else scheduler.get(SENTIMENT_AUDIT_JOB)
    return SentimentAuditStatus(
        sources=[
            _source_audit(
                source, rows.get(source), by_source.get(source, {}), accuracy_run, correlation_id
            )
            for source in SENTIMENT_SOURCES
        ],
        last_run=_summary(last_run),
        accuracy_run=_summary(accuracy_run),
        next_run_at=None if job is None else job.next_run,
        manual_available_at=_manual_available_at(_last_manual_start(session), now),
    )


# --------------------------------------------------------------------------
# The manual trigger
# --------------------------------------------------------------------------


def _refuse(
    *,
    status_code: int,
    code: str,
    message: str,
    rule: str,
    inputs: Mapping[str, object],
    at: datetime,
    correlation_id: str,
    headers: Mapping[str, str] | None = None,
) -> ApiError:
    """Build the refusal, and record why it happened (rule 8's spirit).

    Returns the error rather than raising it, so each call site reads
    ``raise _refuse(...)`` at the point of the decision.
    """
    logger.warning(
        "manual sentiment audit refused: %s",
        message,
        extra={
            "event": "sentiment_audit_refused",
            "rule": rule,
            "code": code,
            "status": status_code,
            "inputs": dict(inputs),
            "at": at.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    return ApiError(status_code=status_code, code=code, message=message, headers=headers)


#: What a run raises when it refuses before opening its row. The route logs
#: each as a refusal (``_refuse``); the task's end is not a second failure.
_START_REFUSALS: Final = (AuditAlreadyRunning, SentimentAuditUnavailable, WatchUniverseUnavailable)


def _log_outcome(correlation_id: str, task: "asyncio.Task[AuditRunResult]") -> None:
    """The background run's end, whichever way it went. Retrieves the exception,
    so asyncio never reports one as never retrieved."""
    if task.cancelled():
        logger.warning(
            "manual sentiment audit run cancelled (shutdown); the runner closes "
            "its row failed on the cancel, and only a row whose commit it never "
            "saw is left for a later run to close as abandoned",
            extra={"event": "sentiment_audit_manual_cancelled", "correlation_id": correlation_id},
        )
        return
    exc = task.exception()
    if isinstance(exc, _START_REFUSALS):
        # Already logged by the route as a refusal, with its rule and inputs.
        logger.info(
            "manual sentiment audit run refused before it opened a row",
            extra={
                "event": "sentiment_audit_manual_refused",
                "error_type": type(exc).__name__,
                "correlation_id": correlation_id,
            },
        )
        return
    if exc is None:
        result = task.result()
        logger.info(
            "manual sentiment audit run finished",
            extra={
                "event": "sentiment_audit_manual_finished",
                "audit_run_id": result.run_id,
                "transitions": len(result.transitions),
                "finished_at": result.finished_at.isoformat(),
                "correlation_id": correlation_id,
            },
        )
        return
    # Type only: the runner records a failure's stage and type in its row,
    # and an exception's text is not ours to vouch for.
    logger.error(
        "manual sentiment audit run did not succeed",
        extra={
            "event": "sentiment_audit_manual_failed",
            "error_type": type(exc).__name__,
            "correlation_id": correlation_id,
        },
    )


@router.post(
    "/audit",
    status_code=202,
    summary="Start a sentiment audit run now -- at most one manual run an hour",
)
async def start_sentiment_audit(
    request: Request, background: BackgroundTasks, session: SessionDep, now: AuditNowDep
) -> SentimentAuditStarted:
    correlation_id = uuid.uuid4().hex
    state = request.app.state
    services: ContextServices | None = getattr(state, "context_services", None)
    if services is None:
        raise _refuse(
            status_code=503,
            code="sentiment_audit_unavailable",
            message=_NO_SERVICES,
            rule="a run needs the context services the lifespan builds with the scheduler",
            inputs={"context_services": None},
            at=now,
            correlation_id=correlation_id,
        )
    # Checked and set with no ``await`` between, so two requests on one event
    # loop cannot both pass it.
    in_flight: asyncio.Task[AuditRunResult] | None = getattr(state, "sentiment_audit_task", None)
    if in_flight is not None and not in_flight.done():
        raise _refuse(
            status_code=409,
            code="sentiment_audit_running",
            message="a manual sentiment audit run started here is still in progress",
            rule="one audit run at a time",
            inputs={"in_flight": True},
            at=now,
            correlation_id=correlation_id,
        )
    running = _live_running(session, now)
    if running is not None:
        raise _refuse(
            status_code=409,
            code="sentiment_audit_running",
            message=(
                f"sentiment audit run {running.id} ({running.trigger}) is still in progress"
            ),
            rule=f"one audit run at a time; a running row is live for {ABANDONED_RUN_AFTER}",
            inputs={
                "running_run_id": running.id,
                "running_trigger": running.trigger,
                "running_started_at": running.started_at.isoformat(),
            },
            at=now,
            correlation_id=correlation_id,
        )
    last_manual = _last_manual_start(session)
    available = _manual_available_at(last_manual, now)
    if available is not None:
        assert last_manual is not None  # available is None without one
        retry_after = _retry_after_seconds(available, now)
        raise _refuse(
            status_code=429,
            code="sentiment_audit_rate_limited",
            message=(
                "a manual sentiment audit run already started in the last hour; "
                f"the next is admitted at {available.isoformat()}"
            ),
            rule=f"at most one manual audit run per {MANUAL_AUDIT_INTERVAL}",
            inputs={
                "last_manual_started_at": last_manual.isoformat(),
                "available_at": available.isoformat(),
                "retry_after_seconds": retry_after,
            },
            at=now,
            correlation_id=correlation_id,
            headers={"Retry-After": str(retry_after)},
        )

    opened: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    def on_opened(run_id: int) -> None:
        # Called on this loop, from inside the run's task, once its row is
        # committed. Never raises: a resolved future is left as it is.
        if not opened.done():
            opened.set_result(run_id)

    task = asyncio.create_task(
        run_sentiment_audit_now(services, trigger="manual", on_opened=on_opened),
        name="sentiment_audit_manual",
    )
    state.sentiment_audit_task = task
    task.add_done_callback(lambda done: _log_outcome(correlation_id, done))
    waiters: list[asyncio.Future[Any]] = [task, opened]
    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)

    if not opened.done():
        # The run ended without opening its row: a refusal, or a failure.
        opened.cancel()
        exc = None if task.cancelled() else task.exception()
        if isinstance(exc, AuditAlreadyRunning):
            raise _refuse(
                status_code=409,
                code="sentiment_audit_running",
                message="another sentiment audit run is in progress",
                rule=(
                    "one audit run at a time (the runner's in-process lock, or "
                    "another process's running row at its step a)"
                ),
                inputs={"refused_by": "runner", "error_type": type(exc).__name__},
                at=now,
                correlation_id=correlation_id,
            )
        if isinstance(exc, (SentimentAuditUnavailable, WatchUniverseUnavailable)):
            degraded = isinstance(exc, WatchUniverseUnavailable) or isinstance(
                exc.__cause__, WatchUniverseUnavailable
            )
            raise _refuse(
                status_code=503,
                code="sentiment_audit_unavailable",
                # The scheduler's fixed text, never vendor text.
                message=str(exc),
                rule=(
                    "a label classified against a degraded watch universe is written permanently"
                    if degraded
                    else "a run needs a market-data provider for its SIP minute bars"
                ),
                inputs={"watch_universe": "degraded"} if degraded else {"provider": None},
                at=now,
                correlation_id=correlation_id,
            )
        logger.error(
            "manual sentiment audit run failed before it started grading",
            extra={
                "event": "sentiment_audit_manual_failed_to_start",
                "error_type": "cancelled" if exc is None else type(exc).__name__,
                "at": now.isoformat(),
                "correlation_id": correlation_id,
            },
        )
        raise ApiError(
            status_code=500,
            code="sentiment_audit_failed",
            message=(
                "the sentiment audit run failed before it started grading; "
                "a run that opened its row has it recorded failed"
            ),
        )

    run_id = opened.result()
    logger.info(
        "manual sentiment audit run started",
        extra={
            "event": "sentiment_audit_manual_started",
            "audit_run_id": run_id,
            "requested_at": now.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    # Decision 20: the notice follows the committed action -- the row exists.
    notify_after_response(
        request, background, sentiment_audit_notice(at=now, correlation_id=correlation_id)
    )
    return SentimentAuditStarted(run_id=run_id, requested_at=now)
