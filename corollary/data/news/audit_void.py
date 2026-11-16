"""Voiding one sentiment audit run (R4): archive its grades, restore what it moved.

Owner decision R4 (``docs/superpowers/specs/2026-11-09-phase-3-steps-5-7-remainder.md``)
amends parent Q6's "the weekly audit run is the only writer". **The audit
remains the only thing that decides a status.** Voiding may only restore a
status an earlier, non-voided run decided, or the seed's ``unaudited``; it
can never set a status no run computed.

What :func:`void_audit_run` does, in **one transaction**:

1. marks the run: ``audit_run.voided_at`` and ``void_reason``. ``status``
   keeps recording what the run did;
2. moves every ``label_outcome`` row with ``audit_run_id = N`` to
   ``label_outcome_voided`` -- an exact ``INSERT ... SELECT`` copy, then the
   delete that frees ``(label_id, window)`` for the next run to grade afresh;
3. leaves run N's ``sentiment_accuracy`` rows where they are (readers
   exclude voided runs);
4. restores each source run N moved, from run N's own ``audit_log`` row for
   it (``previous_value``): to ``unaudited`` with NULLs, or to ``active`` /
   ``demoted`` exactly as it stood before run N (owner decision Q24c,
   amending R4): ``changed_at`` = the ``started_at`` of the **non-voided**
   run whose ``audit_log`` row made that earlier transition -- the value
   the runner wrote then -- ``audit_run_id`` = that run, and ``reason`` =
   :func:`restored_reason` (``audit_log`` never stored the original reason,
   and this does not invent one). **Never** that row's ``at``: the runner
   writes it at the run's ``finished_at``, later than ``changed_at``, and
   a demoted source's ``since_demotion`` basis counts from ``changed_at``
   (:func:`_restoration_to`). A source run N did not move is not touched;
5. writes one ``audit_log`` row per restored source, ``at`` = the void
   instant: ``previous_value`` run N's status, ``new_value`` the restored one.

Notifying is the caller's, **after** the commit (``scripts/void_audit_run.py``
emits ``sentiment_audit_voided``): this module never emits, so a refusal
cannot. So is the success log line (:func:`log_voided`): :func:`void_audit_run`
returns **straight after its COMMIT** (finding F1), so that nothing of its own
runs between the commit and the caller holding the result. A caller whose
void raised anyway (a Ctrl-C landing in the session's close, say) reads it
back with :func:`read_back_void` rather than assuming either outcome.

**Refusals write nothing and are logged** (rule 8's spirit) with the rule,
the inputs and the instant -- :class:`VoidRule` names every one. They are
decided by pure functions (:func:`check_reason`, :func:`check_run`,
:func:`plan_restorations`) over rows read inside the void's own transaction,
before its first write, and **logged only once that transaction has rolled
back** (finding F2): a log handler that blocks (a stderr under a conhost
QuickEdit selection, an unread pipe) must not hold SQLite's write lock while
it does, because every other writer in the app waits on that lock and the
watchdog's write is on the event loop.

**A failed run is voided like any other** (U4a-fix, ruling M3). A run's
transitions commit only in the transaction that closes it ``succeeded``
(``audit_runner._close_run``), so a ``failed`` run has never moved a source,
but it may hold graded outcomes: step d commits them before a later stage
fails. Voiding one archives whatever outcomes it has, restores nothing, and
marks it voided, and the notice says nothing was restored. A failed run
still counts as the latest terminal run. Voids therefore go newest first: a
failed run M after N (a deadline-cancelled run, or an abandoned ``running``
row ``_open_run`` closed ``failed``) is voided before N, and then N is the
latest. R4's refusals about the run and its reason are exactly: nonexistent,
running or any run running, already voided, not the latest non-voided
terminal run, and an empty reason. :class:`VoidRule`'s other two,
``clock_behind_a_stored_run`` and ``restore_unprovable``, refuse what cannot
be written correctly: a restore row inside a run's span, or a status
``audit_log`` cannot prove.

**Concurrency.** The void's first statement is ``BEGIN IMMEDIATE``
(:func:`corollary.db.session.take_write_lock`), before it reads its clock and
before any check,
and ``audit_runner._open_run`` does the same. The driver would otherwise run
every read before the first write outside any transaction, and a run's
``running`` row could commit between the void's checks and its writes, so
the "any run is running" refusal never fired and the restore rows landed in
that run's span. Under the lock the two serialize. Whichever takes the lock
first reads the earlier instant and commits. The other waits (up to
``SQLITE_BUSY_TIMEOUT_MS``), then reads the clock and sees the committed
result: a void after a run's open is refused as ``another_run_running``, and
a run after a void opens strictly later than ``voided_at``. Every write is
also conditional on the state it read and checked by row count, as a second
line.
"""

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Final, cast

from sqlalchemy import CursorResult, Table, delete, func, insert, literal, select, update
from sqlalchemy.orm import Session

from corollary.calendars import NYSE_TZ
from corollary.db.models import (
    SENTIMENT_SOURCE_STATUSES,
    AuditLog,
    Base,
    AuditRun,
    LabelOutcome,
    LabelOutcomeVoided,
    SentimentSourceStatus,
)
from corollary.db.session import take_write_lock
from corollary.db.types import UtcDateTime

__all__ = [
    "VOID_EVENT",
    "VOID_SEVERITY",
    "Refusal",
    "Restoration",
    "RunFacts",
    "SentimentLogRow",
    "SourceRow",
    "VoidRefused",
    "VoidResult",
    "VoidRule",
    "check_reason",
    "check_run",
    "latest_standing_run",
    "log_voided",
    "plan_restorations",
    "read_back_void",
    "restored_reason",
    "run_of",
    "void_audit_run",
    "void_notice_text",
]

logger = logging.getLogger(__name__)

#: The notification a void raises, after its commit. ``info``, bell on and
#: Discord on (``db.seed``, migration 0018). Never ``sentiment_repromoted``:
#: a void is not a re-promotion (R4, *Rejected*).
VOID_EVENT: Final = "sentiment_audit_voided"
VOID_SEVERITY: Final = "info"

#: ``audit_run.status`` values a run can be voided from. ``running`` has
#: decided nothing yet.
_TERMINAL: Final = ("succeeded", "failed")
_UNAUDITED: Final = "unaudited"
_SENTIMENT: Final = "sentiment"


class VoidRule(StrEnum):
    """Why a void was refused. Every refusal writes nothing."""

    #: The reason is empty or whitespace only.
    EMPTY_REASON = "empty_reason"
    #: No ``audit_run`` row has this id.
    RUN_NOT_FOUND = "run_not_found"
    #: This run is still ``running``.
    RUN_RUNNING = "run_running"
    #: Another run is ``running``: its transitions would be decided on the
    #: state this void is about to change.
    ANOTHER_RUN_RUNNING = "another_run_running"
    #: This run is already voided.
    ALREADY_VOIDED = "already_voided"
    #: Not the latest non-voided terminal run: voiding an older run would
    #: orphan the later runs that built on its statuses. A ``failed`` run
    #: counts, so it is voided first (U4a-fix, ruling M3).
    NOT_LATEST = "not_latest_non_voided_terminal_run"
    #: The clock reads no later than a stored run instant, so the restore
    #: rows' ``at`` could fall inside a run's span and join to it.
    CLOCK_BEHIND = "clock_behind_a_stored_run"
    #: The status to restore cannot be proven from ``audit_log``: a missing,
    #: duplicated or malformed row, or a source whose current state is not
    #: what run N left (ruling 5). Never guessed.
    RESTORE_UNPROVABLE = "restore_unprovable"


@dataclass(frozen=True, slots=True)
class Refusal:
    rule: VoidRule
    message: str
    #: The facts the rule was decided on, for the log line.
    facts: Mapping[str, object] = field(default_factory=dict)


class VoidRefused(Exception):
    """A refused void. Nothing was written and nothing emitted."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(f"{refusal.rule.value}: {refusal.message}")
        self.refusal = refusal


@dataclass(frozen=True, slots=True)
class RunFacts:
    """One ``audit_run`` row, as the checks read it. Instants aware UTC."""

    id: int
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    voided_at: datetime | None


@dataclass(frozen=True, slots=True)
class SentimentLogRow:
    """One ``sentiment`` ``audit_log`` row, with the run whose span holds it."""

    id: int
    at: datetime
    source: str
    previous_value: str
    new_value: str
    #: The one run with ``started_at < at <= finished_at`` (the history join
    #: in ``models.SentimentSourceStatus``), or ``None`` -- a void's own
    #: restore row lies inside no run's span.
    run_id: int | None


@dataclass(frozen=True, slots=True)
class SourceRow:
    """One ``sentiment_source_status`` row."""

    source: str
    status: str
    changed_at: datetime | None
    reason: str | None
    audit_run_id: int | None


@dataclass(frozen=True, slots=True)
class Restoration:
    """What one source goes back to."""

    source: str
    #: Run N's status for it -- the status the void replaces.
    from_status: str
    to_status: str
    #: NULL exactly when ``to_status`` is ``unaudited``.
    changed_at: datetime | None
    reason: str | None
    audit_run_id: int | None


@dataclass(frozen=True, slots=True)
class VoidResult:
    run_id: int
    run_trigger: str
    run_status: str
    run_started_at: datetime
    reason: str
    voided_at: datetime
    #: ``label_outcome`` rows moved to ``label_outcome_voided``.
    archived: int
    restorations: tuple[Restoration, ...]
    correlation_id: str


def _aware_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("a void instant must be timezone-aware")
    return moment.astimezone(timezone.utc)


def _eastern(moment: datetime) -> str:
    return moment.astimezone(NYSE_TZ).strftime("%Y-%m-%d %H:%M %Z")


# --------------------------------------------------------------------- pure


def restored_reason(run_id: int, reason: str) -> str:
    """The ``reason`` a source restored to ``active`` / ``demoted`` carries (R4)."""
    return (
        f"restored when run {run_id} was voided: {reason.strip()}; "
        "the original reason text is not retained"
    )


def check_reason(reason: str) -> Refusal | None:
    if not reason.strip():
        return Refusal(VoidRule.EMPTY_REASON, "a void needs a reason, and this one is empty")
    return None


def latest_standing_run(runs: Sequence[RunFacts]) -> RunFacts | None:
    """The newest terminal run that is not voided: ``started_at``, then ``id``."""
    standing = [r for r in runs if r.status in _TERMINAL and r.voided_at is None]
    if not standing:
        return None
    return max(standing, key=lambda r: (r.started_at, r.id))


def check_run(run_id: int, runs: Sequence[RunFacts], now: datetime) -> Refusal | None:
    """Every refusal about the run itself, in the order the spec lists them."""
    by_id = {r.id: r for r in runs}
    run = by_id.get(run_id)
    if run is None:
        return Refusal(VoidRule.RUN_NOT_FOUND, f"no audit run has id {run_id}")
    if run.status == "running":
        return Refusal(VoidRule.RUN_RUNNING, f"audit run {run_id} is still running")
    running = sorted(r.id for r in runs if r.status == "running")
    if running:
        return Refusal(
            VoidRule.ANOTHER_RUN_RUNNING,
            f"audit run {running[0]} is running; void only while no run is",
            {"running_run_ids": running},
        )
    if run.voided_at is not None:
        return Refusal(
            VoidRule.ALREADY_VOIDED,
            f"audit run {run_id} was already voided at {run.voided_at.isoformat()}",
            {"voided_at": run.voided_at.isoformat()},
        )
    latest = latest_standing_run(runs)
    if latest is None or latest.id != run_id:
        latest_id = None if latest is None else latest.id
        return Refusal(
            VoidRule.NOT_LATEST,
            f"audit run {run_id} is not the latest non-voided terminal run "
            f"(that is run {latest_id}); voiding it would orphan the runs after it",
            {"latest_standing_run_id": latest_id},
        )
    instants = [
        moment
        for r in runs
        for moment in (r.started_at, r.finished_at, r.voided_at)
        if moment is not None
    ]
    newest = max(instants)
    if now <= newest:
        return Refusal(
            VoidRule.CLOCK_BEHIND,
            f"the clock reads {now.isoformat()}, no later than a stored run instant "
            f"{newest.isoformat()}; the restore rows would fall inside a run's span",
            {"newest_run_instant": newest.isoformat()},
        )
    return None


def run_of(at: datetime, runs: Sequence[RunFacts]) -> int | None:
    """The run whose half-open span ``(started_at, finished_at]`` holds ``at``.

    The spans are disjoint by construction (``models.AuditRun``); two matches
    mean the history itself is corrupt, which raises rather than picks one.
    """
    matches = [
        r.id
        for r in runs
        if r.finished_at is not None and r.started_at < at <= r.finished_at
    ]
    if len(matches) > 1:
        raise ValueError(f"audit_log instant {at.isoformat()} lies inside runs {matches}")
    return matches[0] if matches else None


def _unprovable(message: str, **facts: object) -> Refusal:
    return Refusal(VoidRule.RESTORE_UNPROVABLE, message, facts)


def _previous_transition(
    move: SentimentLogRow,
    logs: Sequence[SentimentLogRow],
    voided_run_id: int,
    standing: Collection[int],
) -> SentimentLogRow | None:
    """The source's newest ``audit_log`` row before ``move`` in a ``standing``
    run other than the one voided: the transition a restore to ``active`` /
    ``demoted`` puts back, via :func:`_restoration_to`. ``None`` if there is
    none. Shared by :func:`plan_restorations` and :func:`read_back_void`, so
    the two cannot disagree about it."""
    earlier = [
        row
        for row in logs
        if row.source == move.source
        and row.run_id is not None
        and row.run_id != voided_run_id
        and row.run_id in standing
        and row.at < move.at
    ]
    if not earlier:
        return None
    return max(earlier, key=lambda row: (row.at, row.id))


def _restoration_to(
    source: str,
    from_status: str,
    previous: SentimentLogRow,
    runs: Sequence[RunFacts],
    voided_run_id: int,
    reason: str,
) -> Restoration:
    """A restore to ``active`` / ``demoted``: exactly the status row
    ``previous``'s transition left, the reason aside.

    Owner decision Q24c (2026-11-11), amending R4 step 4. The runner writes a
    transition's ``changed_at`` as its run's ``started_at`` -- the run
    instant, ``audit.Transition.changed_at`` -- and that transition's
    ``audit_log`` row at the run's ``finished_at`` (``audit_runner._close_run``).
    So ``previous.at`` is the run's *finish*, not the ``changed_at`` that
    stood. The restored ``changed_at`` is the ``started_at`` of the run the
    history join puts ``previous`` in, which is also the restored
    ``audit_run_id``. Restoring ``previous.at`` instead would start a
    demoted source's ``since_demotion`` basis later by the run's length, and
    drop every label referenced inside that span from the re-promotion count.

    ``to_status`` is ``previous.new_value``; both callers have checked it
    equals the status being restored. ``reason`` is :func:`restored_reason`,
    because ``audit_log`` never stored the original. Shared by
    :func:`plan_restorations` and :func:`read_back_void`, so the two cannot
    disagree about it.
    """
    run = next((r for r in runs if r.id == previous.run_id), None)
    if run is None:
        # :func:`_previous_transition` returns only a row :func:`run_of`
        # placed in one of these very runs, so this is a corrupt input.
        raise ValueError(
            f"audit_log row {previous.id} names run {previous.run_id}, which is not "
            "among the runs read"
        )
    return Restoration(
        source=source,
        from_status=from_status,
        to_status=previous.new_value,
        changed_at=run.started_at,
        reason=restored_reason(voided_run_id, reason),
        audit_run_id=run.id,
    )


def plan_restorations(
    run: RunFacts,
    reason: str,
    logs: Sequence[SentimentLogRow],
    sources: Sequence[SourceRow],
    runs: Sequence[RunFacts],
) -> tuple[Restoration, ...] | Refusal:
    """What each source run N moved goes back to, or why that cannot be proven.

    Only ``audit_log`` rows of run N and of earlier **non-voided** runs are
    read (ruling 5). Run N's row gives the status to restore; for ``active``
    / ``demoted``, the source's newest earlier row in a standing run must
    have produced that very status, and names the run whose ``started_at``
    and id are restored (Q24c, :func:`_restoration_to`).
    """
    moves: dict[str, SentimentLogRow] = {}
    for row in logs:
        if row.run_id != run.id:
            continue
        if row.source in moves:
            return _unprovable(
                f"run {run.id} has two audit_log rows for {row.source}; a source "
                "transitions at most once per run",
                source=row.source,
            )
        moves[row.source] = row
    by_source = {s.source: s for s in sources}
    for source in sorted(by_source):
        if by_source[source].audit_run_id == run.id and source not in moves:
            return _unprovable(
                f"{source}'s status names run {run.id}, but run {run.id} has no "
                f"audit_log row for {source}",
                source=source,
            )
    standing = {r.id for r in runs if r.voided_at is None}
    plan: list[Restoration] = []
    for source in sorted(moves):
        move = moves[source]
        current = by_source.get(source)
        if current is None:
            return _unprovable(f"{source} has no sentiment_source_status row", source=source)
        if (
            move.previous_value not in SENTIMENT_SOURCE_STATUSES
            or move.new_value not in SENTIMENT_SOURCE_STATUSES
            or move.previous_value == move.new_value
        ):
            return _unprovable(
                f"run {run.id}'s audit_log row for {source} reads "
                f"{move.previous_value!r} -> {move.new_value!r}, not a transition",
                source=source,
                audit_log_id=move.id,
            )
        if current.status != move.new_value or current.audit_run_id != run.id:
            return _unprovable(
                f"{source} is {current.status} from run {current.audit_run_id}, not "
                f"{move.new_value} from run {run.id}; something changed it since",
                source=source,
            )
        if move.previous_value == _UNAUDITED:
            plan.append(
                Restoration(
                    source=source,
                    from_status=move.new_value,
                    to_status=_UNAUDITED,
                    changed_at=None,
                    reason=None,
                    audit_run_id=None,
                )
            )
            continue
        previous = _previous_transition(move, logs, run.id, standing)
        if previous is None:
            return _unprovable(
                f"run {run.id} moved {source} from {move.previous_value}, but no earlier "
                f"non-voided run's audit_log row put it there",
                source=source,
            )
        if previous.new_value != move.previous_value:
            return _unprovable(
                f"run {run.id} moved {source} from {move.previous_value}, but its previous "
                f"non-voided transition (run {previous.run_id}) left it {previous.new_value}",
                source=source,
                audit_log_id=previous.id,
            )
        plan.append(_restoration_to(source, move.new_value, previous, runs, run.id, reason))
    return tuple(plan)


def void_notice_text(result: VoidResult) -> tuple[str, str]:
    """The ``sentiment_audit_voided`` notice's title and body: the run, the
    reason, and each source's restoration. ASCII only, so it prints on any
    console the script runs in."""
    title = f"Sentiment audit run {result.run_id} voided"
    lines = [
        f"Run {result.run_id} ({result.run_trigger}, started "
        f"{_eastern(result.run_started_at)}, {result.run_status}) was voided at "
        f"{_eastern(result.voided_at)}. Reason: {result.reason}",
        f"{result.archived} label outcome(s) archived; the next run grades those "
        "labels afresh. Its accuracy rows are kept and no longer read.",
    ]
    if not result.restorations:
        # A failed run never moves a source (its transitions commit only with
        # a ``succeeded`` close); a succeeded one may simply have moved none.
        how = "failed and moved none" if result.run_status == "failed" else "moved none"
        lines.append(f"No source's status was restored: run {result.run_id} {how}.")
    for r in result.restorations:
        if r.audit_run_id is None:
            lines.append(f"{r.source}: {r.from_status} -> {r.to_status} (never audited)")
        else:
            assert r.changed_at is not None
            lines.append(
                f"{r.source}: {r.from_status} -> {r.to_status} (run {r.audit_run_id}'s "
                f"transition at {_eastern(r.changed_at)})"
            )
    return title, "\n".join(lines)


# -------------------------------------------------------------------- store


def _load_runs(session: Session) -> list[RunFacts]:
    return [
        RunFacts(
            id=row.id,
            trigger=row.trigger,
            status=row.status,
            started_at=_aware_utc(row.started_at),
            finished_at=None if row.finished_at is None else _aware_utc(row.finished_at),
            voided_at=None if row.voided_at is None else _aware_utc(row.voided_at),
        )
        for row in session.scalars(select(AuditRun).order_by(AuditRun.id))
    ]


def _load_logs(session: Session, runs: Sequence[RunFacts]) -> list[SentimentLogRow]:
    rows = session.scalars(
        select(AuditLog).where(AuditLog.category == _SENTIMENT).order_by(AuditLog.id)
    )
    out: list[SentimentLogRow] = []
    for row in rows:
        at = _aware_utc(row.at)
        out.append(
            SentimentLogRow(
                id=row.id,
                at=at,
                source=row.field,
                previous_value=row.previous_value,
                new_value=row.new_value,
                run_id=run_of(at, runs),
            )
        )
    return out


def _load_sources(session: Session) -> list[SourceRow]:
    return [
        SourceRow(
            source=row.source,
            status=row.status,
            changed_at=None if row.changed_at is None else _aware_utc(row.changed_at),
            reason=row.reason,
            audit_run_id=row.audit_run_id,
        )
        for row in session.scalars(
            select(SentimentSourceStatus).order_by(SentimentSourceStatus.source)
        )
    ]


def _rowcount(result: object) -> int:
    # A DML statement's result is a ``CursorResult``; ``Session.execute`` is
    # typed as the general ``Result`` (audit_runner._set_terminal's cast).
    return cast(CursorResult[Any], result).rowcount


def _table_of(model: type[Base]) -> Table:
    """A mapped class's ``Table``. ``DeclarativeBase.__table__`` is typed as the
    general ``FromClause``, which ``insert`` / ``delete`` do not accept; every
    model here is mapped to a plain table, and this proves it rather than
    casting it."""
    table = model.__table__
    if not isinstance(table, Table):
        raise TypeError(f"{model.__name__} is not mapped to a Table: {table!r}")
    return table


def _archive_outcomes(session: Session, run_id: int, voided_at: datetime) -> int:
    """Copy run N's outcomes to the archive exactly, then delete them. Their count."""
    live = _table_of(LabelOutcome)
    archive = _table_of(LabelOutcomeVoided)
    names = [column.name for column in live.columns]
    expected = int(
        session.scalar(
            select(func.count()).select_from(live).where(live.c.audit_run_id == run_id)
        )
        or 0
    )
    copied = _rowcount(
        session.execute(
            insert(archive).from_select(
                [*names, "voided_at"],
                select(
                    *(live.c[name] for name in names),
                    literal(voided_at, UtcDateTime()).label("voided_at"),
                ).where(live.c.audit_run_id == run_id),
            )
        )
    )
    removed = _rowcount(session.execute(delete(live).where(live.c.audit_run_id == run_id)))
    if not copied == removed == expected:
        raise RuntimeError(
            f"archiving run {run_id}'s outcomes moved {copied} and removed {removed} of "
            f"{expected}; not committing a partial move"
        )
    return expected


def _refuse(refusal: Refusal, inputs: Mapping[str, object]) -> VoidRefused:
    """Log one refusal -- rule, inputs, instant -- and build its exception."""
    logger.warning(
        "sentiment audit void refused (%s): %s; nothing was written",
        refusal.rule.value,
        refusal.message,
        extra={
            "event": "sentiment_audit_void_refused",
            "rule": refusal.rule.value,
            # ``detail``, not ``message``: that name is reserved on a LogRecord.
            "detail": refusal.message,
            "inputs": dict(inputs),
            "facts": dict(refusal.facts),
            "at": inputs["at"],
            "correlation_id": inputs["correlation_id"],
        },
    )
    return VoidRefused(refusal)


def _decide(
    session: Session, run_id: int, reason: str, at: datetime
) -> Refusal | tuple[RunFacts, tuple[Restoration, ...]]:
    """Every check, on rows read inside the void's transaction: the refusal,
    or the run and what each source goes back to. Writes nothing."""
    runs = _load_runs(session)
    refusal = check_run(run_id, runs, at)
    if refusal is not None:
        return refusal
    run = next(r for r in runs if r.id == run_id)
    planned = plan_restorations(
        run, reason, _load_logs(session, runs), _load_sources(session), runs
    )
    if isinstance(planned, Refusal):
        return planned
    return run, planned


def _write_void(
    session: Session,
    run_id: int,
    planned: Sequence[Restoration],
    at: datetime,
    reason: str,
) -> int:
    """Every write of the void, uncommitted: the mark, the archive, each
    restoration and its ``audit_log`` row. Returns the outcomes archived."""
    # Every read before this ran inside the write lock, so nothing has
    # committed since. The conditional writes and row-count checks below
    # are a second line, not the serialization.
    marked = session.execute(
        update(AuditRun)
        .where(AuditRun.id == run_id)
        .where(AuditRun.voided_at.is_(None))
        .where(AuditRun.status.in_(_TERMINAL))
        .values(voided_at=at, void_reason=reason)
        .execution_options(synchronize_session=False)
    )
    if _rowcount(marked) != 1:
        raise RuntimeError(f"audit run {run_id} changed under the void; nothing committed")
    archived = _archive_outcomes(session, run_id, at)
    for restoration in planned:
        restored = session.execute(
            update(SentimentSourceStatus)
            .where(SentimentSourceStatus.source == restoration.source)
            .where(SentimentSourceStatus.status == restoration.from_status)
            .where(SentimentSourceStatus.audit_run_id == run_id)
            .values(
                status=restoration.to_status,
                changed_at=restoration.changed_at,
                reason=restoration.reason,
                audit_run_id=restoration.audit_run_id,
            )
            .execution_options(synchronize_session=False)
        )
        if _rowcount(restored) != 1:
            raise RuntimeError(
                f"{restoration.source}'s status changed under the void; nothing committed"
            )
        session.add(
            AuditLog(
                at=at,
                category=_SENTIMENT,
                field=restoration.source,
                previous_value=restoration.from_status,
                new_value=restoration.to_status,
            )
        )
    return archived


def void_audit_run(
    session_factory: Callable[[], Session],
    *,
    run_id: int,
    reason: str,
    clock: Callable[[], datetime],
    correlation_id: str,
) -> VoidResult:
    """Void run ``run_id`` in one transaction (see the module docstring).

    ``clock`` is read once, **after** :func:`~corollary.db.session.take_write_lock`.
    That makes the void's instant later than everything committed before it
    and earlier than any run that opens after it. Raises :class:`VoidRefused`
    on every refusal, having written nothing, and logs the refusal once the
    lock is released (F2). If releasing it raises, the refusal is still
    logged and the release's error is what propagates (E3). Any other
    exception rolls the whole void back.

    Returns straight after the COMMIT (F1). The success line
    (:func:`log_voided`) and the notification are the caller's.
    """
    clean = reason.strip()
    inputs: dict[str, object] = {
        "run_id": run_id,
        "reason": clean,
        "correlation_id": correlation_id,
    }
    refusal = check_reason(reason)
    if refusal is not None:
        # Nothing is read or written, so no lock: the instant is the refusal's own.
        inputs["at"] = _aware_utc(clock()).isoformat()
        raise _refuse(refusal, inputs)
    # Set only on the refusal path: what the ``finally`` below logs.
    declined: Refusal | None = None
    refused: VoidRefused | None = None
    try:
        with session_factory() as session:
            take_write_lock(session)
            at = _aware_utc(clock())
            inputs["at"] = at.isoformat()
            decided = _decide(session, run_id, clean, at)
            if not isinstance(decided, Refusal):
                run, planned = decided
                archived = _write_void(session, run_id, planned, at, clean)
                # Built before the commit, so that only the session's close
                # stands between the COMMIT and the caller holding it (F1).
                result = VoidResult(
                    run_id=run_id,
                    run_trigger=run.trigger,
                    run_status=run.status,
                    run_started_at=run.started_at,
                    reason=clean,
                    voided_at=at,
                    archived=archived,
                    restorations=planned,
                    correlation_id=correlation_id,
                )
                session.commit()
                return result
            # Refused, with nothing written. Release the write lock
            # **before** the refusal is logged (F2): this rollback, then the
            # ``with``'s close. The rule, the inputs and the instant were all
            # decided above, under the lock; logging them later changes none
            # of them.
            declined = decided
            session.rollback()
    finally:
        # In a ``finally`` so that a rollback or close that raises cannot
        # cost rule 8's record (E3). When the release succeeds this still
        # runs after it, so F2 holds. When it raises, the refusal is logged
        # and the release's error propagates: a database fault, not a
        # refusal, and the caller reads the run back as for any other.
        if declined is not None:
            refused = _refuse(declined, inputs)
    if refused is None:  # pragma: no cover - the ``with`` returns, raises or refuses
        raise RuntimeError(f"the void of audit run {run_id} neither returned nor refused")
    raise refused


def log_voided(result: VoidResult) -> None:
    """The void's success line: the run, the reason, what was archived and
    what was restored. The caller's to write, once :func:`void_audit_run` has
    returned (F1), so a log handler that raises or blocks can never stand
    between the commit and the caller holding the result."""
    logger.info(
        "sentiment audit run %d voided",
        result.run_id,
        extra={
            "event": "sentiment_audit_run_voided",
            "audit_run_id": result.run_id,
            "reason": result.reason,
            "archived_outcomes": result.archived,
            "restorations": [
                {
                    "source": r.source,
                    "from": r.from_status,
                    "to": r.to_status,
                    "audit_run_id": r.audit_run_id,
                }
                for r in result.restorations
            ],
            "at": result.voided_at.isoformat(),
            "correlation_id": result.correlation_id,
        },
    )


def read_back_void(
    session_factory: Callable[[], Session],
    *,
    run_id: int,
    reason: str,
    voided_at: datetime,
    correlation_id: str,
) -> VoidResult | None:
    """One void, read back from what it committed: the :class:`VoidResult`
    :func:`void_audit_run` returned, or ``None`` if that void did not land.

    For a caller whose :func:`void_audit_run` raised something other than
    :class:`VoidRefused`, which may have come after the COMMIT (F1).
    ``voided_at`` is the one reading of that call's clock. The void landed
    exactly when run ``run_id`` carries that ``voided_at`` and this
    ``reason``. That match is strict because voids serialize on the write
    lock and a run is voided at most once, not because of the clock check.
    Another void of the run that committed before this call took the lock
    would have had this call refused -- ``already_voided``, or
    ``another_run_running`` first if a run was open, since :func:`check_run`
    tests that before it looks at ``voided_at`` -- so this call would not be
    reading back. One that committed after this call released the lock read
    its clock only after taking it, so no earlier than this call's reading on
    a clock that does not run backwards. ``clock_behind`` is not the
    reason: :func:`check_run` refuses a void of an already-voided run before
    it compares the clock at all.

    Reads only, and takes no lock. ``archived`` counts run ``run_id``'s rows
    in ``label_outcome_voided``; a run is voided at most once. The
    restorations are the void's own ``audit_log`` rows (``at`` =
    ``voided_at``). Each one not to ``unaudited`` is rebuilt from
    :func:`_previous_transition` and :func:`_restoration_to`, as the planner
    did, over the runs that stood at ``voided_at``: ``changed_at`` is that
    transition's run's ``started_at`` (Q24c), never its ``audit_log.at``. ``audit_log`` is only ever appended to,
    so that answer cannot have changed since. A history that no longer
    yields it raises rather than reporting a guess.
    """
    clean = reason.strip()
    at = _aware_utc(voided_at)
    with session_factory() as session:
        row = session.get(AuditRun, run_id)
        if (
            row is None
            or row.voided_at is None
            or _aware_utc(row.voided_at) != at
            or row.void_reason != clean
        ):
            return None
        runs = _load_runs(session)
        logs = _load_logs(session, runs)
        archived = int(
            session.scalar(
                select(func.count())
                .select_from(LabelOutcomeVoided)
                .where(LabelOutcomeVoided.audit_run_id == run_id)
            )
            or 0
        )
    run = next(r for r in runs if r.id == run_id)
    # The runs that stood when the void planned: not voided by then.
    standing = {
        r.id for r in runs if r.id != run_id and (r.voided_at is None or r.voided_at > at)
    }
    restorations: list[Restoration] = []
    own_rows = sorted(
        (log for log in logs if log.at == at and log.run_id is None),
        key=lambda log: log.source,
    )
    for restore in own_rows:
        if restore.new_value == _UNAUDITED:
            restorations.append(
                Restoration(
                    source=restore.source,
                    from_status=restore.previous_value,
                    to_status=_UNAUDITED,
                    changed_at=None,
                    reason=None,
                    audit_run_id=None,
                )
            )
            continue
        moves = [log for log in logs if log.source == restore.source and log.run_id == run_id]
        previous = (
            _previous_transition(moves[0], logs, run_id, standing) if len(moves) == 1 else None
        )
        if previous is None or previous.new_value != restore.new_value:
            raise RuntimeError(
                f"run {run_id} is voided, but its restoration of {restore.source} to "
                f"{restore.new_value} cannot be read back from audit_log"
            )
        restorations.append(
            _restoration_to(restore.source, restore.previous_value, previous, runs, run_id, clean)
        )
    return VoidResult(
        run_id=run_id,
        run_trigger=run.trigger,
        run_status=run.status,
        run_started_at=run.started_at,
        reason=clean,
        voided_at=at,
        archived=archived,
        restorations=tuple(restorations),
        correlation_id=correlation_id,
    )
