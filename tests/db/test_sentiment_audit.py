"""Phase 3 step 6's self-audit schema (migration 0014, unit 6.1).

Design, *Database*, and decisions 12-14:

* ``label_outcome(label_id, window, reference_at, reference_price,
  outcome_at, outcome_price, benchmark_return, excess_return, correct,
  skip_reason, audit_run_id)``, UNIQUE ``(label_id, window)``. A skipped row
  carries no prices. Plus ``scope`` (``watch | discovery``), an orchestrator
  addition for unit 6.3: NULL exactly on the two membership skips.
* ``audit_run(id, started_at, finished_at, trigger, status, detail)``.
* ``sentiment_accuracy(audit_run_id, source, window, basis, scope, graded,
  correct, neutral)``, UNIQUE on everything but the counts. Counts only.
* ``sentiment_source_status(source, status, changed_at, reason,
  audit_run_id)``, one row per source, seeded ``unaudited``.
* ``audit_log.category`` admits ``sentiment``; ``notification_route`` gains
  ``sentiment_demoted`` and ``sentiment_repromoted``, bell on, Discord on.

Every CHECK runs against **both** ``create_all()`` and ``alembic upgrade
head``: autogenerate does not compare CHECK text, so the model/migration
match test cannot see a CHECK one side lacks.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corollary.db.models import (
    ACCURACY_BASES,
    ACCURACY_SCOPES,
    AUDIT_CATEGORIES,
    AUDIT_RUN_STATUSES,
    AUDIT_RUN_TRIGGERS,
    LABEL_OUTCOME_SCOPES,
    LABEL_SKIP_REASONS,
    SENTIMENT_SOURCE_STATUSES,
    SENTIMENT_SOURCES,
    SENTIMENT_WINDOWS,
    AuditLog,
    AuditRun,
    Base,
    LabelOutcome,
    NewsArticle,
    NotificationRoute,
    SentimentAccuracy,
    SentimentLabelRow,
    SentimentSourceStatus,
)
from corollary.db.seed import (
    NOTIFICATION_ROUTE_DEFAULTS,
    SENTIMENT_SOURCE_STATUS_DEFAULTS,
    seed,
)
from corollary.db.session import create_db_engine, sqlite_url
from tests.db.test_news_watchlist_tradeability import _normalised

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

T0 = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)  # Saturday 10:00 ET


def _config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(params=["models", "migration"])
def any_engine(request: pytest.FixtureRequest, db_path: Path) -> Iterator[Engine]:
    """Both ways a database gets built: ``create_all()`` + ``seed()``, and Alembic."""
    url = sqlite_url(db_path)
    if request.param == "migration":
        command.upgrade(_config(url), "head")
        eng = create_db_engine(url)
    else:
        eng = create_db_engine(url)
        Base.metadata.create_all(eng)
        with Session(eng) as sess:
            seed(sess)
            sess.commit()
    yield eng
    eng.dispose()


def _label(sess: Session, vendor_id: str = "1") -> int:
    article = NewsArticle(
        vendor="alpaca",
        vendor_id=vendor_id,
        feed="alpaca_news",
        canonical_id=None,
        url=f"https://example.com/story/{vendor_id}",
        url_key=f"https://example.com/story/{vendor_id}",
        headline="Acme beats estimates",
        headline_key="acme beats estimates",
        summary=None,
        publisher="Benzinga",
        published_at=T0 - timedelta(days=5),
        ingested_at=T0 - timedelta(days=5),
    )
    sess.add(article)
    sess.flush()
    label = SentimentLabelRow(
        article_id=article.id,
        ticker="ACME",
        source="rules",
        tier="rules",
        direction="bullish",
        reasoning="earnings_beat",
        rule_id="earnings_beat",
        labeled_at=T0 - timedelta(days=5),
    )
    sess.add(label)
    sess.flush()
    return label.id


def _run(sess: Session, **overrides: object) -> int:
    fields: dict[str, object] = {
        "started_at": T0,
        "finished_at": T0 + timedelta(minutes=4),
        "trigger": "scheduled",
        "status": "succeeded",
        "detail": '{"discovery_cap_skipped_labels": 0, "discovery_cap_skipped_tickers": 0}',
    }
    fields.update(overrides)
    run = AuditRun(**fields)
    sess.add(run)
    sess.flush()
    return run.id


def _graded(label_id: int, run_id: int, **overrides: object) -> LabelOutcome:
    fields: dict[str, object] = {
        "label_id": label_id,
        "window": "1h",
        "reference_at": T0 - timedelta(days=5),
        "reference_price": Decimal("101.25"),
        "outcome_at": T0 - timedelta(days=5) + timedelta(hours=1),
        "outcome_price": Decimal("102.50"),
        "benchmark_return": Decimal("0.0021"),
        "excess_return": Decimal("0.010245679012345679"),
        "correct": True,
        "skip_reason": None,
        "scope": "watch",
        "audit_run_id": run_id,
    }
    fields.update(overrides)
    return LabelOutcome(**fields)


def _skipped(label_id: int, run_id: int, **overrides: object) -> LabelOutcome:
    fields: dict[str, object] = {
        "label_id": label_id,
        "window": "1d",
        "reference_at": None,
        "reference_price": None,
        "outcome_at": None,
        "outcome_price": None,
        "benchmark_return": None,
        "excess_return": None,
        "correct": None,
        "skip_reason": "discovery_cap",
        "scope": None,
        "audit_run_id": run_id,
    }
    fields.update(overrides)
    return LabelOutcome(**fields)


def _accuracy(run_id: int, **overrides: object) -> SentimentAccuracy:
    fields: dict[str, object] = {
        "audit_run_id": run_id,
        "source": "rules",
        "window": "1h",
        "basis": "trailing_28d",
        "scope": "all",
        "graded": 31,
        "correct": 17,
        "neutral": 4,
    }
    fields.update(overrides)
    return SentimentAccuracy(**fields)


# --------------------------------------------------------------- vocabularies


def test_the_vocabularies_are_the_specs() -> None:
    assert SENTIMENT_WINDOWS == ("1h", "1d")
    # The third member is an orchestrator decision extending the spec's two.
    assert LABEL_SKIP_REASONS == (
        "off_watch_untradeable",
        "discovery_cap",
        "price_unavailable",
    )
    assert AUDIT_RUN_TRIGGERS == ("scheduled", "manual")
    assert AUDIT_RUN_STATUSES == ("running", "succeeded", "failed")
    assert ACCURACY_BASES == ("trailing_28d", "since_demotion")
    assert ACCURACY_SCOPES == ("all", "watch", "discovery")
    # ``all`` is an accuracy aggregate, never one outcome's classification.
    assert LABEL_OUTCOME_SCOPES == ("watch", "discovery")
    assert SENTIMENT_SOURCE_STATUSES == ("unaudited", "active", "demoted")
    assert AUDIT_CATEGORIES == ("risk", "feed", "notification", "watchlist", "sentiment")


def test_the_tables_have_exactly_the_specs_columns(any_engine: Engine) -> None:
    inspector = inspect(any_engine)
    columns = {
        table: {c["name"] for c in inspector.get_columns(table)}
        for table in (
            "label_outcome",
            "audit_run",
            "sentiment_accuracy",
            "sentiment_source_status",
        )
    }
    assert columns == {
        "label_outcome": {
            "label_id", "window", "reference_at", "reference_price", "outcome_at",
            "outcome_price", "benchmark_return", "excess_return", "correct",
            "skip_reason", "scope", "audit_run_id",
        },
        "audit_run": {"id", "started_at", "finished_at", "trigger", "status", "detail"},
        "sentiment_accuracy": {
            "audit_run_id", "source", "window", "basis", "scope", "graded",
            "correct", "neutral",
        },
        "sentiment_source_status": {
            "source", "status", "changed_at", "reason", "audit_run_id",
        },
    }  # fmt: skip


def test_no_column_is_a_float(any_engine: Engine) -> None:
    """Prices and returns are ``Money`` -- TEXT on SQLite -- never REAL."""
    inspector = inspect(any_engine)
    money = {
        "reference_price", "outcome_price", "benchmark_return", "excess_return",
    }  # fmt: skip
    for column in inspector.get_columns("label_outcome"):
        rendered = str(column["type"]).upper()
        assert "FLOAT" not in rendered and "REAL" not in rendered, column
        if column["name"] in money:
            assert "VARCHAR" in rendered or "TEXT" in rendered, column


# --------------------------------------------------------------- label_outcome


def test_a_graded_outcome_round_trips_exact_decimals(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        label_id = _label(sess)
        run_id = _run(sess)
        sess.add(_graded(label_id, run_id))
        sess.commit()
    with Session(any_engine) as sess:
        stored = sess.scalars(select(LabelOutcome)).one()
        assert stored.reference_price == Decimal("101.25")
        assert type(stored.reference_price) is Decimal
        assert stored.excess_return == Decimal("0.010245679012345679")
        assert type(stored.excess_return) is Decimal
        assert stored.reference_at == T0 - timedelta(days=5)
        assert stored.reference_at.tzinfo is not None
        raw = sess.execute(
            text("SELECT typeof(excess_return), typeof(reference_price) FROM label_outcome")
        ).one()
        assert tuple(raw) == ("text", "text")


_GRADED_ACCEPTED: list[dict[str, object]] = [
    {},
    {"correct": False, "excess_return": Decimal("-0.0042")},
    {"correct": None},  # a neutral label (Q5)
    {"window": "1d"},
    # A MARKET label grades against SPY's raw return (Q5): the benchmark *is*
    # the graded return, stored in both columns.
    {"benchmark_return": Decimal("0.0021"), "excess_return": Decimal("0.0021")},
    {"benchmark_return": Decimal("0")},
    {"excess_return": Decimal("0")},
    {"scope": "discovery"},
    {"scope": "discovery", "correct": None, "window": "1d"},
]

_SKIPPED_ACCEPTED: list[dict[str, object]] = [
    {},
    {"skip_reason": "off_watch_untradeable"},
    # ``price_unavailable`` is a label *in* a graded set -- its scope is known
    # and kept, so a missing bar never moves a label out of its split.
    {"skip_reason": "price_unavailable", "scope": "watch"},
    {"skip_reason": "price_unavailable", "scope": "discovery", "window": "1h"},
    {"window": "1h"},
]

_OUTCOME_REFUSED: list[tuple[str, dict[str, object], str]] = [
    ("graded", {"window": "1w"}, "ck_label_outcome_window"),
    ("graded", {"window": "1H"}, "ck_label_outcome_window"),
    ("skipped", {"skip_reason": "too_small"}, "ck_label_outcome_skip_reason"),
    # A skipped row carries no prices (decision 12) -- every column, one at a time.
    ("skipped", {"reference_at": T0}, "ck_label_outcome_shape"),
    ("skipped", {"reference_price": Decimal("1")}, "ck_label_outcome_shape"),
    ("skipped", {"outcome_at": T0}, "ck_label_outcome_shape"),
    ("skipped", {"outcome_price": Decimal("1")}, "ck_label_outcome_shape"),
    ("skipped", {"benchmark_return": Decimal("0.01")}, "ck_label_outcome_shape"),
    ("skipped", {"excess_return": Decimal("0.01")}, "ck_label_outcome_shape"),
    ("skipped", {"correct": False}, "ck_label_outcome_shape"),
    # ``price_unavailable`` is a skip like the others: no bar, so no price --
    # never a guessed one.
    (
        "skipped",
        {"skip_reason": "price_unavailable", "reference_price": Decimal("1")},
        "ck_label_outcome_shape",
    ),
    (
        "skipped",
        {"skip_reason": "price_unavailable", "outcome_price": Decimal("1")},
        "ck_label_outcome_shape",
    ),
    (
        "skipped",
        {"skip_reason": "price_unavailable", "excess_return": Decimal("0.01")},
        "ck_label_outcome_shape",
    ),
    # ...and a graded row carries everything it was graded on.
    ("graded", {"reference_at": None}, "ck_label_outcome_shape"),
    ("graded", {"reference_price": None}, "ck_label_outcome_shape"),
    ("graded", {"outcome_at": None}, "ck_label_outcome_shape"),
    ("graded", {"outcome_price": None}, "ck_label_outcome_shape"),
    ("graded", {"excess_return": None}, "ck_label_outcome_shape"),
    # A grader that failed to fetch SPY must not store a raw return as excess
    # (that credits beta, which Q5 rejects) -- so no graded row lacks one.
    ("graded", {"benchmark_return": None}, "ck_label_outcome_shape"),
    # Prices are non-negative.
    ("graded", {"reference_price": Decimal("-1")}, "ck_label_outcome_reference_price"),
    ("graded", {"outcome_price": Decimal("-0.01")}, "ck_label_outcome_outcome_price"),
    # ``scope`` is NULL exactly on the two membership skips (unit 6.3): a
    # graded row and a ``price_unavailable`` row carry it, the others do not.
    ("graded", {"scope": None}, "ck_label_outcome_scope"),
    ("graded", {"scope": "all"}, "ck_label_outcome_scope"),
    ("graded", {"scope": "WATCH"}, "ck_label_outcome_scope"),
    ("skipped", {"scope": "watch"}, "ck_label_outcome_scope"),
    ("skipped", {"scope": "discovery"}, "ck_label_outcome_scope"),
    (
        "skipped",
        {"skip_reason": "off_watch_untradeable", "scope": "discovery"},
        "ck_label_outcome_scope",
    ),
    ("skipped", {"skip_reason": "price_unavailable"}, "ck_label_outcome_scope"),
    (
        "skipped",
        {"skip_reason": "price_unavailable", "scope": "all"},
        "ck_label_outcome_scope",
    ),
]


@pytest.mark.parametrize("overrides", _GRADED_ACCEPTED)
def test_graded_outcomes_are_accepted(any_engine: Engine, overrides: dict[str, object]) -> None:
    with Session(any_engine) as sess:
        label_id = _label(sess)
        sess.add(_graded(label_id, _run(sess), **overrides))
        sess.commit()


@pytest.mark.parametrize("overrides", _SKIPPED_ACCEPTED)
def test_skipped_outcomes_are_accepted(any_engine: Engine, overrides: dict[str, object]) -> None:
    with Session(any_engine) as sess:
        label_id = _label(sess)
        sess.add(_skipped(label_id, _run(sess), **overrides))
        sess.commit()


@pytest.mark.parametrize(("kind", "overrides", "constraint"), _OUTCOME_REFUSED)
def test_invalid_outcomes_are_refused(
    any_engine: Engine, kind: str, overrides: dict[str, object], constraint: str
) -> None:
    build = _graded if kind == "graded" else _skipped
    with Session(any_engine) as sess:
        label_id = _label(sess)
        sess.add(build(label_id, _run(sess), **overrides))
        with pytest.raises(IntegrityError, match=constraint):
            sess.commit()


def test_a_money_column_refuses_non_numeric_text(any_engine: Engine) -> None:
    """Raw SQL past ``Money``'s binder still meets the shape CHECK."""
    with Session(any_engine) as sess:
        label_id = _label(sess)
        run_id = _run(sess)
        sess.commit()
        with pytest.raises(IntegrityError, match="ck_label_outcome_excess_return"):
            sess.execute(
                text(
                    "INSERT INTO label_outcome (label_id, \"window\", reference_at, "
                    "reference_price, outcome_at, outcome_price, benchmark_return, "
                    "excess_return, correct, skip_reason, scope, audit_run_id) VALUES "
                    "(:l, '1h', '2026-09-28 14:00:00.000000', '1', "
                    "'2026-09-28 15:00:00.000000', '1', '0', 'NaN', 1, NULL, 'watch', :r)"
                ),
                {"l": label_id, "r": run_id},
            )


def test_one_outcome_per_label_and_window(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        label_id = _label(sess)
        run_id = _run(sess)
        sess.add(_graded(label_id, run_id, window="1h"))
        sess.add(_skipped(label_id, run_id, window="1d"))
        sess.commit()
        sess.add(_graded(label_id, run_id, window="1h", correct=False))
        with pytest.raises(IntegrityError, match="UNIQUE"):
            sess.commit()


def test_an_outcome_names_a_stored_label_and_run(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        run_id = _run(sess)
        sess.add(_graded(999, run_id))
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            sess.commit()
    with Session(any_engine) as sess:
        label_id = _label(sess)
        sess.add(_graded(label_id, 999))
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            sess.commit()


# --------------------------------------------------------------- audit_run


_RUN_ACCEPTED: list[dict[str, object]] = [
    {},
    {"trigger": "manual"},
    {"status": "running", "finished_at": None, "detail": None},
    {"status": "failed", "detail": '{"error": "bars unavailable"}'},
    {"detail": None},
]

_RUN_REFUSED: list[tuple[dict[str, object], str]] = [
    ({"trigger": "cron"}, "ck_audit_run_trigger"),
    ({"trigger": "Manual"}, "ck_audit_run_trigger"),
    ({"status": "done"}, "ck_audit_run_status"),
    ({"status": "running"}, "ck_audit_run_finished"),  # running, yet finished
    ({"status": "succeeded", "finished_at": None}, "ck_audit_run_finished"),
    ({"status": "failed", "finished_at": None}, "ck_audit_run_finished"),
    ({"detail": "skipped 3"}, "ck_audit_run_detail"),  # not JSON
    ({"detail": "[1, 2]"}, "ck_audit_run_detail"),  # JSON, not an object
]


@pytest.mark.parametrize("overrides", _RUN_ACCEPTED)
def test_valid_runs_are_accepted(any_engine: Engine, overrides: dict[str, object]) -> None:
    with Session(any_engine) as sess:
        _run(sess, **overrides)
        sess.commit()


@pytest.mark.parametrize(("overrides", "constraint"), _RUN_REFUSED)
def test_invalid_runs_are_refused(
    any_engine: Engine, overrides: dict[str, object], constraint: str
) -> None:
    with Session(any_engine) as sess:
        with pytest.raises(IntegrityError, match=constraint):
            _run(sess, **overrides)


def test_a_second_running_run_is_refused(any_engine: Engine) -> None:
    """``uq_audit_run_one_running``: a manual POST at Saturday 10:01 ET cannot
    start beside the 10:00 scheduled run, so no ``audit_log.at`` falls inside
    two runs' spans. A stale ``running`` row -- a run that died -- blocks the
    next run *loudly* until something closes it."""
    with Session(any_engine) as sess:
        _run(sess, status="running", finished_at=None, detail=None)
        sess.commit()
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed: audit_run.status"):
            _run(
                sess,
                started_at=T0 + timedelta(minutes=1),
                trigger="manual",
                status="running",
                finished_at=None,
                detail=None,
            )


def test_finished_runs_are_not_limited_by_the_one_running_index(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        _run(sess, status="succeeded")
        _run(sess, started_at=T0 + timedelta(days=7), status="succeeded")
        _run(sess, started_at=T0 + timedelta(days=14), status="failed", detail=None)
        _run(sess, started_at=T0 + timedelta(days=21), status="running", finished_at=None)
        sess.commit()
        assert len(sess.scalars(select(AuditRun)).all()) == 4


def test_closing_a_stale_running_run_unblocks_the_next(any_engine: Engine) -> None:
    """Unit 6.4's recovery: mark the dead run ``failed`` with ``finished_at``,
    then start the new one."""
    with Session(any_engine) as sess:
        stale_id = _run(sess, status="running", finished_at=None, detail=None)
        sess.commit()
        stale = sess.get(AuditRun, stale_id)
        assert stale is not None
        stale.status = "failed"
        stale.finished_at = T0 + timedelta(hours=1)
        stale.detail = '{"error": "process died mid-run"}'
        sess.flush()
        _run(
            sess,
            started_at=T0 + timedelta(hours=2),
            trigger="manual",
            status="running",
            finished_at=None,
            detail=None,
        )
        sess.commit()


# --------------------------------------------------------------- sentiment_accuracy


_ACCURACY_ACCEPTED: list[dict[str, object]] = [
    {},
    {"source": "massive", "window": "1d", "basis": "since_demotion", "scope": "discovery"},
    {"scope": "watch"},
    {"graded": 0, "correct": 0, "neutral": 0},
    {"graded": 30, "correct": 30},  # correct may equal graded
]

_ACCURACY_REFUSED: list[tuple[dict[str, object], str]] = [
    ({"source": "llm"}, "ck_sentiment_accuracy_source"),
    ({"window": "4h"}, "ck_sentiment_accuracy_window"),
    ({"basis": "trailing_7d"}, "ck_sentiment_accuracy_basis"),
    ({"scope": "manual"}, "ck_sentiment_accuracy_scope"),
    ({"graded": -1, "correct": 0}, "ck_sentiment_accuracy_graded"),
    ({"correct": -1}, "ck_sentiment_accuracy_correct"),
    ({"correct": 32}, "ck_sentiment_accuracy_correct"),  # more correct than graded
    ({"neutral": -1}, "ck_sentiment_accuracy_neutral"),
]


@pytest.mark.parametrize("overrides", _ACCURACY_ACCEPTED)
def test_valid_accuracy_rows_are_accepted(
    any_engine: Engine, overrides: dict[str, object]
) -> None:
    with Session(any_engine) as sess:
        sess.add(_accuracy(_run(sess), **overrides))
        sess.commit()


@pytest.mark.parametrize(("overrides", "constraint"), _ACCURACY_REFUSED)
def test_invalid_accuracy_rows_are_refused(
    any_engine: Engine, overrides: dict[str, object], constraint: str
) -> None:
    with Session(any_engine) as sess:
        sess.add(_accuracy(_run(sess), **overrides))
        with pytest.raises(IntegrityError, match=constraint):
            sess.commit()


def test_a_fractional_count_is_refused(any_engine: Engine) -> None:
    """INTEGER affinity stores 1.5 as REAL without complaint; the CHECK does not."""
    with Session(any_engine) as sess:
        run_id = _run(sess)
        sess.commit()
        with pytest.raises(IntegrityError, match="ck_sentiment_accuracy_graded"):
            sess.execute(
                text(
                    "INSERT INTO sentiment_accuracy VALUES "
                    "(:r, 'rules', '1h', 'trailing_28d', 'all', 30.5, 1, 0)"
                ),
                {"r": run_id},
            )


def test_one_accuracy_row_per_run_source_window_basis_scope(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        run_id = _run(sess)
        for scope in ACCURACY_SCOPES:
            for basis in ACCURACY_BASES:
                for window in SENTIMENT_WINDOWS:
                    for source in SENTIMENT_SOURCES:
                        sess.add(
                            _accuracy(
                                run_id, source=source, window=window, basis=basis, scope=scope
                            )
                        )
        sess.commit()
        assert len(sess.scalars(select(SentimentAccuracy)).all()) == 24
        sess.add(_accuracy(run_id, graded=40, correct=20))
        with pytest.raises(IntegrityError, match="UNIQUE"):
            sess.commit()


def test_an_accuracy_row_names_a_stored_run(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(_accuracy(999))
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            sess.commit()


# --------------------------------------------------------------- sentiment_source_status


def test_both_sources_start_unaudited(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        rows = sess.scalars(
            select(SentimentSourceStatus).order_by(SentimentSourceStatus.source)
        ).all()
        assert [(r.source, r.status, r.changed_at, r.reason, r.audit_run_id) for r in rows] == [
            ("massive", "unaudited", None, None, None),
            ("rules", "unaudited", None, None, None),
        ]


def test_the_seed_covers_every_label_source() -> None:
    """A source the labellers can write but the seed never lists has no status row."""
    assert set(SENTIMENT_SOURCE_STATUS_DEFAULTS) == set(SENTIMENT_SOURCES)


def test_reseeding_never_undoes_a_transition(any_engine: Engine) -> None:
    """The audit run is the only writer; ``seed()`` only fills an absent row."""
    with Session(any_engine) as sess:
        run_id = _run(sess)
        row = sess.get(SentimentSourceStatus, "rules")
        assert row is not None
        row.status = "demoted"
        row.changed_at = T0
        row.reason = "1h 48.4% of 31, 1d 51.6% of 31"
        row.audit_run_id = run_id
        sess.commit()
        assert seed(sess) == 0
        sess.commit()
        sess.expunge_all()
        kept = sess.get(SentimentSourceStatus, "rules")
        assert kept is not None and kept.status == "demoted"


_STATUS_REFUSED: list[tuple[dict[str, object], str]] = [
    ({"status": "suspended"}, "ck_sentiment_source_status_status"),
    # A transition is an event from a run: it has a time, a reason and a run.
    ({"status": "active"}, "ck_sentiment_source_status_transition"),
    ({"status": "active", "changed_at": T0, "reason": "r"}, "ck_sentiment_source_status_transition"),
    ({"status": "demoted", "changed_at": T0, "with_run": True}, "ck_sentiment_source_status_transition"),
    ({"status": "demoted", "reason": "r", "with_run": True}, "ck_sentiment_source_status_transition"),
    # Nothing transitions *into* unaudited, so an unaudited row carries none of it.
    ({"changed_at": T0}, "ck_sentiment_source_status_transition"),
    ({"reason": "r"}, "ck_sentiment_source_status_transition"),
    ({"with_run": True}, "ck_sentiment_source_status_transition"),
]


@pytest.mark.parametrize(("change", "constraint"), _STATUS_REFUSED)
def test_invalid_status_rows_are_refused(
    any_engine: Engine, change: dict[str, object], constraint: str
) -> None:
    with Session(any_engine) as sess:
        run_id = _run(sess)
        row = sess.get(SentimentSourceStatus, "massive")
        assert row is not None
        for name, value in change.items():
            if name == "with_run":
                row.audit_run_id = run_id
            else:
                setattr(row, name, value)
        with pytest.raises(IntegrityError, match=constraint):
            sess.commit()


@pytest.mark.parametrize("status", ["active", "demoted"])
def test_a_complete_transition_is_accepted(any_engine: Engine, status: str) -> None:
    with Session(any_engine) as sess:
        run_id = _run(sess)
        row = sess.get(SentimentSourceStatus, "massive")
        assert row is not None
        row.status = status
        row.changed_at = T0
        row.reason = "1h 54.8% of 31, 1d 53.1% of 32"
        row.audit_run_id = run_id
        sess.commit()
        sess.expunge_all()
        stored = sess.get(SentimentSourceStatus, "massive")
        assert stored is not None and stored.changed_at == T0


def test_an_unknown_source_is_refused(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(SentimentSourceStatus(source="llm", status="unaudited"))
        with pytest.raises(IntegrityError, match="ck_sentiment_source_status_source"):
            sess.commit()


def test_one_status_row_per_source(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(SentimentSourceStatus(source="rules", status="unaudited"))
        with pytest.raises(IntegrityError, match="UNIQUE"):
            sess.commit()


# --------------------------------------------------------------- audit_log, routes


def test_audit_log_admits_the_sentiment_category(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(
            AuditLog(
                at=T0,
                category="sentiment",
                field="rules",
                previous_value="active",
                new_value="demoted",
            )
        )
        sess.commit()
        sess.add(
            AuditLog(at=T0, category="sentimnet", field="rules", previous_value="a", new_value="b")
        )
        with pytest.raises(IntegrityError, match="ck_audit_log_category"):
            sess.commit()


_SENTIMENT_AUDIT_ACCEPTED: list[tuple[str, str, str]] = [
    ("rules", "unaudited", "active"),
    ("massive", "unaudited", "demoted"),
    ("rules", "active", "demoted"),
    ("massive", "demoted", "active"),
]

_SENTIMENT_AUDIT_REFUSED: list[tuple[str, str, str]] = [
    ("finbert", "active", "demoted"),  # not a source
    ("Rules", "active", "demoted"),
    ("rules", "", "active"),
    ("rules", "running", "active"),  # a run status, not a source status
    ("rules", "active", "unaudited"),  # nothing transitions into unaudited
    ("rules", "active", "Demoted"),
    ("rules", "active", "active"),  # decision 13: unchanged writes no row
    ("massive", "demoted", "demoted"),
]


@pytest.mark.parametrize(("field", "previous", "new"), _SENTIMENT_AUDIT_ACCEPTED)
def test_a_well_formed_sentiment_audit_row_is_accepted(
    any_engine: Engine, field: str, previous: str, new: str
) -> None:
    with Session(any_engine) as sess:
        sess.add(
            AuditLog(at=T0, category="sentiment", field=field, previous_value=previous, new_value=new)
        )
        sess.commit()


@pytest.mark.parametrize(("field", "previous", "new"), _SENTIMENT_AUDIT_REFUSED)
def test_a_malformed_sentiment_audit_row_is_refused(
    any_engine: Engine, field: str, previous: str, new: str
) -> None:
    with Session(any_engine) as sess:
        sess.add(
            AuditLog(at=T0, category="sentiment", field=field, previous_value=previous, new_value=new)
        )
        with pytest.raises(IntegrityError, match="ck_audit_log_sentiment_row"):
            sess.commit()


@pytest.mark.parametrize("category", ["risk", "feed", "notification", "watchlist"])
def test_the_sentiment_row_check_leaves_other_categories_alone(
    any_engine: Engine, category: str
) -> None:
    """Values the sentiment clause would refuse, under every other category."""
    with Session(any_engine) as sess:
        sess.add(
            AuditLog(at=T0, category=category, field="finbert", previous_value="", new_value="unaudited")
        )
        sess.commit()


def test_the_sentiment_routes_are_seeded_bell_on_discord_on(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        routes = {
            (r.event, r.channel): r.enabled
            for r in sess.scalars(
                select(NotificationRoute).where(NotificationRoute.event.like("sentiment_%"))
            )
        }
    assert routes == {
        ("sentiment_demoted", "bell"): True,
        ("sentiment_demoted", "discord"): True,
        ("sentiment_repromoted", "bell"): True,
        ("sentiment_repromoted", "discord"): True,
    }
    assert {(e, c): v for e, c, v in NOTIFICATION_ROUTE_DEFAULTS if e.startswith("sentiment_")} == routes


# --------------------------------------------------------------- migration


_0014_TABLES = {"label_outcome", "audit_run", "sentiment_accuracy", "sentiment_source_status"}


def _routes(url: str) -> dict[tuple[str, str], bool]:
    eng = create_db_engine(url)
    try:
        with Session(eng) as sess:
            return {(r.event, r.channel): r.enabled for r in sess.scalars(select(NotificationRoute))}
    finally:
        eng.dispose()


def test_0014_downgrades_to_0013_and_back(db_path: Path) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0013")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    with Session(eng) as sess:
        # The old CHECK is back: ``sentiment`` is no longer a category.
        sess.add(AuditLog(at=T0, category="sentiment", field="rules", previous_value="a", new_value="b"))
        with pytest.raises(IntegrityError, match="ck_audit_log_category"):
            sess.commit()
    eng.dispose()
    assert tables & _0014_TABLES == set()
    assert {"sentiment_label", "news_article", "audit_log"} <= tables
    routes = _routes(url)
    assert not {e for e, _c in routes} & {"sentiment_demoted", "sentiment_repromoted"}
    assert ("spdr_seed_amended", "discord") in routes  # 0011's rows untouched

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    with Session(eng) as sess:
        assert len(sess.scalars(select(SentimentSourceStatus)).all()) == 2
    eng.dispose()
    assert _0014_TABLES <= tables
    assert _routes(url)[("sentiment_demoted", "bell")] is True


def test_0014_leaves_route_rows_the_seed_already_wrote(db_path: Path) -> None:
    """``seed.py`` runs on every startup; an edited row must survive 0014."""
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "0013")
    eng = create_db_engine(url)
    with Session(eng) as sess:
        sess.add(NotificationRoute(event="sentiment_repromoted", channel="discord", enabled=False))
        sess.commit()
    eng.dispose()

    command.upgrade(cfg, "head")
    routes = _routes(url)
    assert routes[("sentiment_repromoted", "discord")] is False
    assert routes[("sentiment_repromoted", "bell")] is True


def test_0014_downgrade_refuses_to_destroy_a_sentiment_audit_row(db_path: Path) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    with Session(eng) as sess:
        sess.add(AuditLog(at=T0, category="sentiment", field="rules", previous_value="unaudited", new_value="active"))
        sess.commit()
    eng.dispose()

    with pytest.raises(RuntimeError, match="refusing to downgrade 0014"):
        command.downgrade(cfg, "0013")
    # Refused before anything moved: still at 0014, nothing stranded.
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert _0014_TABLES <= tables
    assert "_alembic_tmp_audit_log" not in tables


def _audit_rows(url: str) -> list[tuple[str, str, str, str]]:
    eng = create_db_engine(url)
    try:
        with Session(eng) as sess:
            return [
                (r.category, r.field, r.previous_value, r.new_value)
                for r in sess.scalars(select(AuditLog).order_by(AuditLog.id))
            ]
    finally:
        eng.dispose()


def _audit_log_sql(url: str) -> str:
    eng = create_db_engine(url)
    try:
        with eng.connect() as conn:
            sql: str = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'audit_log'")
            ).scalar_one()
            return sql
    finally:
        eng.dispose()


def test_0014_rebuild_keeps_existing_audit_rows_both_ways(db_path: Path) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "0013")
    eng = create_db_engine(url)
    with Session(eng) as sess:
        sess.add_all(
            [
                AuditLog(
                    at=T0,
                    category="risk",
                    field="max_risk_per_trade_pct",
                    previous_value="5",
                    new_value="7",
                ),
                AuditLog(at=T0, category="watchlist", field="ACME", previous_value="", new_value="added"),
            ]
        )
        sess.commit()
    eng.dispose()
    before = _audit_rows(url)
    assert len(before) == 2

    command.upgrade(cfg, "head")
    assert _audit_rows(url) == before
    assert "ck_audit_log_sentiment_row" in _audit_log_sql(url)

    command.downgrade(cfg, "0013")
    assert _audit_rows(url) == before
    assert "ck_audit_log_sentiment_row" not in _audit_log_sql(url)


# ------------------------------------------------------------ model vs 0014


#: ``audit_log`` too, because 0014 rebuilds it with a new CHECK. The step-4
#: comparison in test_news_watchlist_tradeability.py also reads ``audit_log``
#: at head; listing it here keeps 0014's comparison self-contained.
_0014_DDL_TABLES = (
    "audit_log",
    "audit_run",
    "label_outcome",
    "sentiment_accuracy",
    "sentiment_source_status",
)


def _master_0014(eng: Engine) -> dict[tuple[str, str], tuple[str, list[str]]]:
    names = ", ".join(f"'{name}'" for name in _0014_DDL_TABLES)
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT type, name, sql FROM sqlite_master "
                f"WHERE tbl_name IN ({names}) AND sql IS NOT NULL"
            )
        ).all()
    return {(kind, name): _normalised(sql) for kind, name, sql in rows}


def test_create_all_and_upgrade_head_write_the_same_0014_ddl(tmp_path: Path) -> None:
    """The models and 0014 each restate every CHECK, so compare what SQLite
    stored for both. The parametrised tests above prove each CHECK *refuses*
    on both sides; this proves the two texts are the same text, which is what
    catches a clause added to one side only (``price_unavailable``, the
    graded ``benchmark_return``)."""
    models_url = sqlite_url(tmp_path / "models.db")
    migration_url = sqlite_url(tmp_path / "migration.db")
    command.upgrade(_config(migration_url), "head")
    models_eng = create_db_engine(models_url)
    migration_eng = create_db_engine(migration_url)
    try:
        Base.metadata.create_all(models_eng)
        from_models = _master_0014(models_eng)
        from_migration = _master_0014(migration_eng)
    finally:
        models_eng.dispose()
        migration_eng.dispose()

    assert {name for kind, name in from_models if kind == "table"} == set(_0014_DDL_TABLES)
    label_outcome_clauses = from_models[("table", "label_outcome")][1]
    skip_reason = next(c for c in label_outcome_clauses if "ck_label_outcome_skip_reason" in c)
    assert "'price_unavailable'" in skip_reason
    shape = next(c for c in label_outcome_clauses if "ck_label_outcome_shape" in c)
    assert "benchmark_return IS NOT NULL" in shape
    scope = next(c for c in label_outcome_clauses if "ck_label_outcome_scope" in c)
    assert "'discovery_cap'" in scope and "'price_unavailable'" in scope
    audit_log_clauses = from_models[("table", "audit_log")][1]
    assert any("ck_audit_log_sentiment_row" in c for c in audit_log_clauses)
    running = from_models[("index", "uq_audit_run_one_running")][0]
    assert running.startswith("CREATE UNIQUE INDEX")
    assert running.endswith("WHERE status = 'running'")
    assert from_models == from_migration
