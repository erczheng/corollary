"""label_outcome, audit_run, sentiment_accuracy, sentiment_source_status; sentiment audit

Phase 3 step 6, unit 6.1 -- the self-audit's schema only (decisions 12, 13
and 14, and the design's *Database* section):

* ``audit_run(id, started_at, finished_at, trigger, status, detail)``.
  ``trigger`` in ``scheduled | manual`` (the Saturday job, or the
  once-an-hour ``POST /api/settings/sentiment/audit``); ``status`` in
  ``running | succeeded | failed``, with ``finished_at`` NULL exactly while
  ``running``. ``detail`` is a JSON object or NULL -- it carries the count of
  labels and tickers the discovery cap skipped. ``uq_audit_run_one_running``
  is a partial unique index on ``status`` where ``status = 'running'``: at
  most one run at a time, because the manual trigger's hourly limit governs
  manual runs only, and a POST at Saturday 10:01 ET would otherwise overlap
  the 10:00 scheduled run. **A stale ``running`` row -- a run that died --
  blocks the next run loudly** (its insert fails the index). Unit 6.4's
  scheduler must close it first, marking it ``failed`` with ``finished_at``,
  and only then insert the new run.
* ``label_outcome(label_id, window, ...)``, primary key ``(label_id,
  window)`` -- the spec's UNIQUE. ``window`` in ``1h | 1d``; ``skip_reason``
  in ``off_watch_untradeable | discovery_cap | price_unavailable`` or NULL
  when graded. **``price_unavailable`` extends the spec's two-member set** --
  an orchestrator decision from the unit 6.1 audit, which the owner is told:
  a matured label with no SIP bar at the reference or outcome instant (a
  renamed symbol, a vendor gap) otherwise has only wrong outcomes -- fail the
  run forever, stay silently ungraded (decision 12 forbids it), or be given
  an invented price. When the grader gives up and writes it is unit 6.2's
  policy (e.g. only once the window ended over a week before the run, so a
  transient outage is retried); see ``models.LABEL_SKIP_REASONS``.
  ``ck_label_outcome_shape``: a skipped row -- any reason -- carries no
  prices, no returns and no ``correct``; a graded row carries both instants,
  both prices, ``benchmark_return`` **and** ``excess_return``. A ``MARKET``
  label stores SPY's raw return in both (Q5); a nullable benchmark would let
  a grader that failed to fetch SPY store a raw return as excess, crediting
  beta. ``correct`` is NULL on a neutral label. Prices **and returns** are
  ``Money`` -- TEXT, exact, shape-CHECKed, prices non-negative. See
  ``models.LabelOutcome`` for why the returns are ``Money`` too, and why the
  grader must quantize them to fit 40 characters.

  **Amended in place for unit 6.3: ``label_outcome.scope``** (``watch |
  discovery``), an orchestrator decision extending the spec's column list.
  ``sentiment_accuracy``'s watch/discovery splits are recomputed every run
  from past outcome rows, so each row keeps the classification the run that
  graded it made; recomputing it from today's watch universe would let
  membership drift rewrite history. ``ck_label_outcome_scope``: NULL exactly
  when ``skip_reason`` is ``off_watch_untradeable`` or ``discovery_cap``, NOT
  NULL otherwise -- graded and ``price_unavailable`` rows both. Amended
  rather than added as 0015 because this revision is provisional (Q22) and
  unmerged, and step 6 should carry one migration to renumber at merge; a
  database already at an unamended 0014 must be downgraded to 0013 and
  upgraded again (it holds no graded rows yet -- the runner is unit 6.4).
* ``sentiment_accuracy(audit_run_id, source, window, basis, scope, graded,
  correct, neutral)``, primary key on the first five -- the spec's UNIQUE.
  Counts only, whole and non-negative, ``correct <= graded``.
* ``sentiment_source_status(source, status, changed_at, reason,
  audit_run_id)``, one row per source, **seeded here** as ``rules`` and
  ``massive``, both ``unaudited`` with no ``changed_at``, ``reason`` or run.
  Any other status must carry all three (decision 13: the weekly run is the
  only writer). ``changed_at`` NULL on ``unaudited`` is by design -- seeding
  is not a change -- so "since when" reads "never audited". Insert-if-missing,
  as ``seed.py`` writes the same rows.

  **No history table.** The row holds the latest transition and its
  ``reason``. A past transition is recovered from its ``audit_log`` row
  (category ``sentiment``, ``field`` = source, previous/new = the statuses),
  joined to the one ``audit_run`` whose half-open span ``started_at <
  audit_log.at <= finished_at`` contains it, then to that run's
  ``sentiment_accuracy`` row -- "the figure a transition acted on" --
  selected by ``source`` = ``field`` (which also separates two sources'
  transitions in one run), ``scope = 'all'`` (decision 12: "Transitions act
  on ``all``"), and ``basis`` from ``previous_value``: ``since_demotion``
  for ``demoted``, ``trailing_28d`` for ``unaudited`` / ``active``. A source
  transitions at most once per run. Each run starts no earlier than the
  previous one finished -- the one-running index plus closing a stale run
  before inserting the next -- and the half-open interval keeps the spans
  disjoint even when the two instants are equal. ``audit_log`` has
  no reason column; those accuracy rows are the history's reason.
  **Unit 6.4 (the runner) commits its ``audit_log`` rows and status updates
  in the same transaction that sets the run's terminal ``status`` and
  ``finished_at``**; otherwise a crash after committing a transition leaves
  it under a run with no ``finished_at``, and the join finds zero runs. Its
  ``audit_log.at`` is strictly after ``started_at`` -- the closing reading,
  not a ``changed_at`` equal to ``started_at``. See
  ``models.SentimentSourceStatus``.
* ``audit_log.category`` admits ``sentiment``, and ``audit_log`` gains
  ``ck_audit_log_sentiment_row``: a ``sentiment`` row has ``field`` in the
  sources, ``previous_value`` in the three statuses, and ``new_value`` in
  ``active | demoted`` -- nothing transitions into ``unaudited``. Other
  categories are untouched by it. SQLite cannot alter a CHECK in place, so
  both arrive in one batch rebuild, the one ``0008`` used, with
  ``copy_from`` restating 0001's table as 0008 left it; every existing row is
  copied across.
* ``notification_route`` gains ``sentiment_demoted`` and
  ``sentiment_repromoted``, **bell on, Discord on** for both (decision 14),
  insert-if-missing exactly as 0006/0008/0011 -- a human's edit stands.
  ``notification_route.event`` carries no CHECK (0005/0006), so the routes are
  rows, not a constraint change. ``watchlist`` and ``watchlist_changed``
  already exist (0008).

No FK here has an ``ON DELETE`` action: labels, outcomes and audit runs are
kept indefinitely (decision 21).

**Provisional number (Q22).** Step 7 also holds a provisional ``0013``;
migrations are numbered at merge, and whichever merges second renumbers its
migration and re-points ``down_revision``. No merge revision.

**Downgrade refuses rather than deletes** if any ``sentiment`` audit row
exists, before anything moves -- the reasoning is 0008's: the old CHECK cannot
hold the row, and destroying an audit record to make a downgrade fit is not a
migration's call. Otherwise it is the real reverse: the two route pairs are
deleted, ``ck_audit_log_sentiment_row`` dropped and the category CHECK
narrowed (one rebuild, rows kept), and the four tables dropped, discarding
every grade, run and status. Nothing rebuilds them -- there is no backfill
(Q7).

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copies, not imports, for the reason every earlier revision states: a
# migration is a frozen record of the schema on the day.
# tests/db/test_sentiment_audit.py runs every CHECK against both this and
# ``create_all()``, so drift fails a test.
_MONEY_TEXT_WIDTH = 40

_WINDOWS: tuple[str, ...] = ("1h", "1d")
_SKIP_REASONS: tuple[str, ...] = (
    "off_watch_untradeable",
    "discovery_cap",
    "price_unavailable",
)
_TRIGGERS: tuple[str, ...] = ("scheduled", "manual")
_RUN_STATUSES: tuple[str, ...] = ("running", "succeeded", "failed")
_BASES: tuple[str, ...] = ("trailing_28d", "since_demotion")
_SCOPES: tuple[str, ...] = ("all", "watch", "discovery")
_SOURCES: tuple[str, ...] = ("rules", "massive")
_SOURCE_STATUSES: tuple[str, ...] = ("unaudited", "active", "demoted")
_MEMBERSHIP_SKIP_REASONS: tuple[str, ...] = ("off_watch_untradeable", "discovery_cap")
_OUTCOME_SCOPES: tuple[str, ...] = ("watch", "discovery")

_AUDIT_CATEGORIES_BEFORE: tuple[str, ...] = (
    "risk",
    "feed",
    "notification",
    "watchlist",
)
_AUDIT_CATEGORIES_AFTER: tuple[str, ...] = (
    "risk",
    "feed",
    "notification",
    "watchlist",
    "sentiment",
)

_ROUTES: tuple[tuple[str, str, bool], ...] = (
    ("sentiment_demoted", "bell", True),
    ("sentiment_demoted", "discord", True),
    ("sentiment_repromoted", "bell", True),
    ("sentiment_repromoted", "discord", True),
)

_notification_route = sa.table(
    "notification_route",
    sa.column("event", sa.String(length=64)),
    sa.column("channel", sa.String(length=16)),
    sa.column("enabled", sa.Boolean()),
)

_sentiment_source_status = sa.table(
    "sentiment_source_status",
    sa.column("source", sa.String(length=16)),
    sa.column("status", sa.String(length=16)),
)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _money_shape(column: str, *, nullable: bool = False) -> str:
    """A signed plain decimal string: ``-7``, ``0.00``, ``123.45``. See 0002."""
    body = (
        f"(CASE WHEN substr({column}, 1, 1) = '-' "
        f"THEN substr({column}, 2) ELSE {column} END)"
    )
    shape = (
        f"typeof({column}) = 'text' "
        f"AND length({column}) BETWEEN 1 AND {_MONEY_TEXT_WIDTH} "
        f"AND {body} GLOB '[0-9]*' "
        f"AND NOT {body} GLOB '*[^0-9.]*' "
        f"AND NOT {body} GLOB '*.' "
        f"AND length({body}) - length(replace({body}, '.', '')) <= 1"
    )
    if nullable:
        return f"{column} IS NULL OR ({shape})"
    return shape


def _price_check(column: str) -> str:
    return (
        f"({_money_shape(column, nullable=True)}) "
        f"AND ({column} IS NULL OR substr({column}, 1, 1) <> '-')"
    )


def _non_negative_count(column: str) -> str:
    return f"typeof({column}) = 'integer' AND {column} >= 0"


_LABEL_OUTCOME_SHAPE = (
    "(skip_reason IS NOT NULL "
    "AND reference_at IS NULL AND reference_price IS NULL "
    "AND outcome_at IS NULL AND outcome_price IS NULL "
    "AND benchmark_return IS NULL AND excess_return IS NULL "
    "AND correct IS NULL) "
    "OR (skip_reason IS NULL "
    "AND reference_at IS NOT NULL AND reference_price IS NOT NULL "
    "AND outcome_at IS NOT NULL AND outcome_price IS NOT NULL "
    "AND benchmark_return IS NOT NULL AND excess_return IS NOT NULL)"
)

# Every branch guarded with ``IS [NOT] NULL``: a CHECK evaluating to NULL
# passes in SQLite, and ``scope IN (...)`` on a NULL scope is NULL.
_LABEL_OUTCOME_SCOPE = (
    "(skip_reason IS NOT NULL AND "
    + _in_list("skip_reason", _MEMBERSHIP_SKIP_REASONS)
    + " AND scope IS NULL) "
    "OR ((skip_reason IS NULL OR skip_reason = 'price_unavailable') "
    "AND scope IS NOT NULL AND "
    + _in_list("scope", _OUTCOME_SCOPES)
    + ")"
)

_AUDIT_LOG_SENTIMENT_ROW = (
    "category <> 'sentiment' OR ("
    + _in_list("field", _SOURCES)
    + " AND "
    + _in_list("previous_value", _SOURCE_STATUSES)
    + " AND "
    + _in_list("new_value", tuple(s for s in _SOURCE_STATUSES if s != "unaudited"))
    # Decision 13: an unchanged source writes no row, so a no-op transition
    # (``active`` to ``active``) is refused rather than logged.
    + " AND previous_value <> new_value"
    + ")"
)

_AUDIT_RUN_DETAIL = (
    "detail IS NULL OR "
    "(CASE WHEN json_valid(detail) THEN json_type(detail) = 'object' ELSE 0 END)"
)

_SOURCE_STATUS_TRANSITION = (
    "(status = 'unaudited' AND changed_at IS NULL AND reason IS NULL "
    "AND audit_run_id IS NULL) "
    "OR (status <> 'unaudited' AND changed_at IS NOT NULL "
    "AND reason IS NOT NULL AND reason <> '' AND audit_run_id IS NOT NULL)"
)


def _audit_log_table(categories: tuple[str, ...], *, sentiment_row: bool) -> sa.Table:
    """``audit_log`` as 0001 created it, with ``categories`` in its CHECK and,
    from this revision on, ``ck_audit_log_sentiment_row``."""
    checks = [
        sa.CheckConstraint(
            _in_list("category", categories), name="ck_audit_log_category"
        )
    ]
    if sentiment_row:
        checks.append(
            sa.CheckConstraint(
                _AUDIT_LOG_SENTIMENT_ROW, name="ck_audit_log_sentiment_row"
            )
        )
    return sa.Table(
        "audit_log",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("previous_value", sa.String(length=128), nullable=False),
        sa.Column("new_value", sa.String(length=128), nullable=False),
        *checks,
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_audit_log_at", "at"),
    )


def _upgrade_audit_log() -> None:
    """Widen the category CHECK and add the sentiment-row CHECK, in one
    rebuild. The batch copies every existing row across."""
    with op.batch_alter_table(
        "audit_log",
        copy_from=_audit_log_table(_AUDIT_CATEGORIES_BEFORE, sentiment_row=False),
    ) as batch_op:
        batch_op.drop_constraint("ck_audit_log_category", type_="check")
        batch_op.create_check_constraint(
            "ck_audit_log_category", _in_list("category", _AUDIT_CATEGORIES_AFTER)
        )
        batch_op.create_check_constraint(
            "ck_audit_log_sentiment_row", _AUDIT_LOG_SENTIMENT_ROW
        )


def _downgrade_audit_log() -> None:
    """The reverse: drop the sentiment-row CHECK and narrow the categories.
    Only reached once ``downgrade`` has refused any ``sentiment`` row."""
    with op.batch_alter_table(
        "audit_log",
        copy_from=_audit_log_table(_AUDIT_CATEGORIES_AFTER, sentiment_row=True),
    ) as batch_op:
        batch_op.drop_constraint("ck_audit_log_sentiment_row", type_="check")
        batch_op.drop_constraint("ck_audit_log_category", type_="check")
        batch_op.create_check_constraint(
            "ck_audit_log_category", _in_list("category", _AUDIT_CATEGORIES_BEFORE)
        )


def upgrade() -> None:
    op.create_table(
        "audit_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.CheckConstraint(
            _in_list('"trigger"', _TRIGGERS), name="ck_audit_run_trigger"
        ),
        sa.CheckConstraint(
            _in_list("status", _RUN_STATUSES), name="ck_audit_run_status"
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) "
            "OR (status <> 'running' AND finished_at IS NOT NULL)",
            name="ck_audit_run_finished",
        ),
        sa.CheckConstraint(_AUDIT_RUN_DETAIL, name="ck_audit_run_detail"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_audit_run_one_running",
        "audit_run",
        ["status"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "label_outcome",
        sa.Column("label_id", sa.Integer(), nullable=False),
        sa.Column("window", sa.String(length=4), nullable=False),
        sa.Column("reference_at", sa.DateTime(), nullable=True),
        sa.Column(
            "reference_price", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True
        ),
        sa.Column("outcome_at", sa.DateTime(), nullable=True),
        sa.Column(
            "outcome_price", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True
        ),
        sa.Column(
            "benchmark_return", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True
        ),
        sa.Column(
            "excess_return", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True
        ),
        sa.Column("correct", sa.Boolean(), nullable=True),
        sa.Column("skip_reason", sa.String(length=32), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=True),
        sa.Column("audit_run_id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            _in_list('"window"', _WINDOWS), name="ck_label_outcome_window"
        ),
        sa.CheckConstraint(
            f"skip_reason IS NULL OR {_in_list('skip_reason', _SKIP_REASONS)}",
            name="ck_label_outcome_skip_reason",
        ),
        sa.CheckConstraint(_LABEL_OUTCOME_SHAPE, name="ck_label_outcome_shape"),
        sa.CheckConstraint(_LABEL_OUTCOME_SCOPE, name="ck_label_outcome_scope"),
        sa.CheckConstraint(
            _price_check("reference_price"), name="ck_label_outcome_reference_price"
        ),
        sa.CheckConstraint(
            _price_check("outcome_price"), name="ck_label_outcome_outcome_price"
        ),
        sa.CheckConstraint(
            _money_shape("benchmark_return", nullable=True),
            name="ck_label_outcome_benchmark_return",
        ),
        sa.CheckConstraint(
            _money_shape("excess_return", nullable=True),
            name="ck_label_outcome_excess_return",
        ),
        sa.ForeignKeyConstraint(["label_id"], ["sentiment_label.id"]),
        sa.ForeignKeyConstraint(["audit_run_id"], ["audit_run.id"]),
        sa.PrimaryKeyConstraint("label_id", "window"),
    )

    op.create_table(
        "sentiment_accuracy",
        sa.Column("audit_run_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("window", sa.String(length=4), nullable=False),
        sa.Column("basis", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("graded", sa.Integer(), nullable=False),
        sa.Column("correct", sa.Integer(), nullable=False),
        sa.Column("neutral", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            _in_list("source", _SOURCES), name="ck_sentiment_accuracy_source"
        ),
        sa.CheckConstraint(
            _in_list('"window"', _WINDOWS), name="ck_sentiment_accuracy_window"
        ),
        sa.CheckConstraint(
            _in_list("basis", _BASES), name="ck_sentiment_accuracy_basis"
        ),
        sa.CheckConstraint(
            _in_list("scope", _SCOPES), name="ck_sentiment_accuracy_scope"
        ),
        sa.CheckConstraint(
            _non_negative_count("graded"), name="ck_sentiment_accuracy_graded"
        ),
        sa.CheckConstraint(
            f"{_non_negative_count('correct')} AND correct <= graded",
            name="ck_sentiment_accuracy_correct",
        ),
        sa.CheckConstraint(
            _non_negative_count("neutral"), name="ck_sentiment_accuracy_neutral"
        ),
        sa.ForeignKeyConstraint(["audit_run_id"], ["audit_run.id"]),
        sa.PrimaryKeyConstraint("audit_run_id", "source", "window", "basis", "scope"),
    )

    op.create_table(
        "sentiment_source_status",
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("audit_run_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            _in_list("source", _SOURCES), name="ck_sentiment_source_status_source"
        ),
        sa.CheckConstraint(
            _in_list("status", _SOURCE_STATUSES),
            name="ck_sentiment_source_status_status",
        ),
        sa.CheckConstraint(
            _SOURCE_STATUS_TRANSITION, name="ck_sentiment_source_status_transition"
        ),
        sa.ForeignKeyConstraint(["audit_run_id"], ["audit_run.id"]),
        sa.PrimaryKeyConstraint("source"),
    )
    # A table created in this revision cannot already hold a row, so a plain
    # insert is correct here -- unlike the routes below.
    op.bulk_insert(
        _sentiment_source_status,
        [{"source": source, "status": "unaudited"} for source in _SOURCES],
    )

    _upgrade_audit_log()

    bind = op.get_bind()
    existing = {
        (event, channel)
        for event, channel in bind.execute(
            sa.select(_notification_route.c.event, _notification_route.c.channel)
        )
    }
    missing = [
        {"event": event, "channel": channel, "enabled": enabled}
        for event, channel, enabled in _ROUTES
        if (event, channel) not in existing
    ]
    if missing:
        op.bulk_insert(_notification_route, missing)


def downgrade() -> None:
    # Refuse before touching anything -- see 0008's downgrade. Alembic's SQLite
    # impl is not transactional-DDL, so a failure part-way leaves the work
    # before it done, and a rebuild failing on the old CHECK strands
    # ``_alembic_tmp_audit_log``.
    bind = op.get_bind()
    blocking = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM audit_log WHERE NOT "
            + _in_list("category", _AUDIT_CATEGORIES_BEFORE)
        )
    ).scalar_one()
    if blocking:
        raise RuntimeError(
            f"refusing to downgrade 0014: audit_log holds {blocking} row(s) in a "
            f"category 0013 does not admit (sentiment). Deleting audit records "
            f"to make a downgrade fit is not this migration's call."
        )

    events = sorted({event for event, _channel, _enabled in _ROUTES})
    op.execute(
        _notification_route.delete().where(_notification_route.c.event.in_(events))
    )

    _downgrade_audit_log()

    op.drop_table("sentiment_source_status")
    op.drop_table("sentiment_accuracy")
    op.drop_table("label_outcome")
    op.drop_index("uq_audit_run_one_running", table_name="audit_run")
    op.drop_table("audit_run")
