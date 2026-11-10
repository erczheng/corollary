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
   ``demoted`` with ``changed_at`` = the ``at`` of that source's previous
   transition row in a **non-voided** run, ``audit_run_id`` = that run, and
   ``reason`` = :func:`restored_reason` (``audit_log`` never stored the
   original reason, and this does not invent one). A source run N did not
   move is not touched;
5. writes one ``audit_log`` row per restored source, ``at`` = the void
   instant: ``previous_value`` run N's status, ``new_value`` the restored one.

Notifying is the caller's, **after** the commit (``scripts/void_audit_run.py``
emits ``sentiment_audit_voided``): this module never emits, so a refusal
cannot.

**Refusals write nothing and are logged** (rule 8's spirit) with the rule,
the inputs and the instant -- :class:`VoidRule` names every one. They are
decided by pure functions (:func:`check_reason`, :func:`check_run`,
:func:`plan_restorations`) over rows read inside the void's own transaction,
before its first write.

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
(:func:`take_write_lock`), before it reads its clock and before any check,
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
from collections.abc import Callable, Mapping, Sequence
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
    "plan_restorations",
    "restored_reason",
    "run_of",
    "take_write_lock",
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
    have produced that very status, and supplies ``changed_at`` and the run.
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
        earlier = [
            row
            for row in logs
            if row.source == source
            and row.run_id is not None
            and row.run_id != run.id
            and row.run_id in standing
            and row.at < move.at
        ]
        if not earlier:
            return _unprovable(
                f"run {run.id} moved {source} from {move.previous_value}, but no earlier "
                f"non-voided run's audit_log row put it there",
                source=source,
            )
        previous = max(earlier, key=lambda row: (row.at, row.id))
        if previous.new_value != move.previous_value:
            return _unprovable(
                f"run {run.id} moved {source} from {move.previous_value}, but its previous "
                f"non-voided transition (run {previous.run_id}) left it {previous.new_value}",
                source=source,
                audit_log_id=previous.id,
            )
        plan.append(
            Restoration(
                source=source,
                from_status=move.new_value,
                to_status=move.previous_value,
                changed_at=previous.at,
                reason=restored_reason(run.id, reason),
                audit_run_id=previous.run_id,
            )
        )
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


def take_write_lock(session: Session) -> None:
    """Begin ``session``'s transaction with ``BEGIN IMMEDIATE``: SQLite's write
    lock, taken **before** any read.

    Needed because of how the driver opens transactions. ``sqlite3``'s legacy
    transaction control (``autocommit == LEGACY_TRANSACTION_CONTROL``, the
    default, and what ``db.session``'s engine gets) issues an implicit
    *deferred* ``BEGIN`` only before an INSERT, UPDATE or DELETE. Every SELECT
    before a session's first write therefore runs outside any transaction,
    each in its own autocommit snapshot, and another connection may commit
    between those reads and the first write without anything noticing.

    ``BEGIN IMMEDIATE`` takes the RESERVED lock at once. Any other writer
    then waits for this transaction's commit or rollback (up to
    ``SQLITE_BUSY_TIMEOUT_MS``, then ``database is locked``); readers carry
    on under WAL. Every later statement runs inside this one transaction, and
    the driver, seeing a transaction already open, issues no ``BEGIN`` of its
    own. ``session.commit()`` commits it, and closing the session without a
    commit rolls it back.

    The two writers that must not interleave, the void
    (:func:`void_audit_run`) and a run's open (``audit_runner._open_run``),
    each call this first and read their clock after it. Whichever takes the
    lock first therefore has the earlier instant, and the other sees its
    committed result before deciding anything.

    No other ``BEGIN IMMEDIATE`` exists under ``corollary/``. This is the
    first, and it is explicit on the session's own connection rather than an
    engine-wide ``begin`` hook, which would change every transaction in the
    app. It must be the session's first statement. A transaction already open
    on the connection would make ``BEGIN`` fail, so that raises here,
    naming the cause.
    """
    connection = session.connection()
    if connection.dialect.name != "sqlite":
        raise NotImplementedError(
            f"take_write_lock is written for SQLite, not {connection.dialect.name}"
        )
    driver = connection.connection.driver_connection
    if getattr(driver, "in_transaction", False):
        raise RuntimeError(
            "a transaction is already open on this connection; the write lock must be "
            "the session's first statement"
        )
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def void_audit_run(
    session_factory: Callable[[], Session],
    *,
    run_id: int,
    reason: str,
    clock: Callable[[], datetime],
    correlation_id: str,
) -> VoidResult:
    """Void run ``run_id`` in one transaction (see the module docstring).

    ``clock`` is read once, **after** :func:`take_write_lock`. That makes the
    void's instant later than everything committed before it and earlier
    than any run that opens after it. Raises :class:`VoidRefused` (logged,
    nothing written) on every refusal. Any other exception rolls the whole
    void back.
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
    with session_factory() as session:
        take_write_lock(session)
        at = _aware_utc(clock())
        inputs["at"] = at.isoformat()
        runs = _load_runs(session)
        refusal = check_run(run_id, runs, at)
        if refusal is not None:
            raise _refuse(refusal, inputs)
        run = next(r for r in runs if r.id == run_id)
        planned = plan_restorations(
            run, clean, _load_logs(session, runs), _load_sources(session), runs
        )
        if isinstance(planned, Refusal):
            raise _refuse(planned, inputs)

        # Every read above ran inside the write lock, so nothing has
        # committed since. The conditional writes and row-count checks
        # below are a second line, not the serialization.
        marked = session.execute(
            update(AuditRun)
            .where(AuditRun.id == run_id)
            .where(AuditRun.voided_at.is_(None))
            .where(AuditRun.status.in_(_TERMINAL))
            .values(voided_at=at, void_reason=clean)
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
        session.commit()

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
    logger.info(
        "sentiment audit run %d voided",
        run_id,
        extra={
            "event": "sentiment_audit_run_voided",
            "audit_run_id": run_id,
            "reason": clean,
            "archived_outcomes": archived,
            "restorations": [
                {
                    "source": r.source,
                    "from": r.from_status,
                    "to": r.to_status,
                    "audit_run_id": r.audit_run_id,
                }
                for r in planned
            ],
            "at": at.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    return result
