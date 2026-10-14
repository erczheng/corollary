"""The sentiment audit's runner (unit 6.4a) against a real SQLite database.

Every vendor is a fake: :class:`FakeBars` is the provider's ``stock_bars`` and
nothing else, so a runner that reached for any other provider surface fails
with ``AttributeError``. All articles, labels and prices here are SYNTHETIC.

The week used throughout: 30 ``rules`` labels on AAPL and 30 ``massive``
labels on MSFT, published Monday 2026-11-16 from 10:00 ET a minute apart; the
run is Saturday 2026-11-21 10:00 ET. SPY is flat at 100, and AAPL and MSFT
rise a cent a minute, so a bullish label is correct and a bearish one is not:
``rules`` (bullish) is 30/30 and goes ``unaudited -> active``, ``massive``
(bearish) is 0/30 and goes ``unaudited -> demoted``.
"""

import ast
import asyncio
import inspect
import json
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from corollary.data.news import audit_runner
from corollary.data.news.audit import (
    TRAILING_WINDOW,
    AuditLabel,
    TransitionNotice,
    plan_audit,
)
from corollary.data.news.audit_runner import (
    ABANDONED_RUN_AFTER,
    AuditAlreadyRunning,
    SentimentAuditDeps,
    run_sentiment_audit,
)
from corollary.data.news.labels import Direction
from corollary.data.news.signals import group_labels
from corollary.data.providers.interface import Bar, BarTimeframe, ProviderError
from corollary.db.models import (
    AuditLog,
    AuditRun,
    Base,
    EngineState,
    LabelOutcome,
    NewsArticle,
    SentimentAccuracy,
    SentimentLabelRow,
    SentimentSourceStatus,
    TickerTradeability,
)
from corollary.db.seed import seed
from corollary.db.session import create_db_engine, sqlite_url
from corollary.wire import WireFormatError

UTC = timezone.utc
_ET = ZoneInfo("America/New_York")

#: Monday 2026-11-16 10:00 ET (EST, UTC-5).
MONDAY_10 = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
#: Saturday 2026-11-21 10:00 ET.
SATURDAY_10 = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)
MONDAY = date(2026, 11, 16)
TUESDAY = date(2026, 11, 17)
#: The price epoch: Monday's open.
_EPOCH = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)

LABELS_PER_SOURCE = 30


# --------------------------------------------------------------------- fakes


@dataclass
class FakeBars:
    """``stock_bars`` only, multi-symbol like the provider. Records every call.

    ``raises`` fails any request that **includes** that (symbol, session) --
    a whole batch, as the vendor fails a whole multi-symbol request -- and
    ``outage`` fails every request.
    """

    raises: Mapping[tuple[str, date], BaseException] = field(default_factory=dict)
    outage: BaseException | None = None
    calls: list[tuple[tuple[str, ...], BarTimeframe, datetime | None, datetime | None]] = field(
        default_factory=list
    )

    async def stock_bars(
        self,
        symbols: Sequence[str],
        *,
        timeframe: BarTimeframe = BarTimeframe.DAY,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, list[Bar]]:
        self.calls.append((tuple(symbols), timeframe, start, end))
        assert start is not None and end is not None
        day = start.astimezone(_ET).date()
        if self.outage is not None:
            raise self.outage
        for symbol in symbols:
            if (symbol, day) in self.raises:
                raise self.raises[(symbol, day)]
        return {symbol: _series(symbol, start, end) for symbol in symbols}


def _series(symbol: str, start: datetime, end: datetime) -> list[Bar]:
    out: list[Bar] = []
    at = start
    while at < end:
        price = _price(symbol, at)
        out.append(
            Bar(
                symbol=symbol,
                at=at,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1000,
                trade_count=10,
                vwap=None,
            )
        )
        at += timedelta(minutes=1)
    return out


def _price(symbol: str, at: datetime) -> Decimal:
    if symbol == "SPY":
        return Decimal("100")
    minutes = int((at - _EPOCH).total_seconds() // 60)
    return Decimal("100") + Decimal(minutes) / Decimal(100)


class TickingClock:
    """Each reading one second after the last, from ``start``."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        reading = self.now
        self.now += timedelta(seconds=1)
        return reading


@dataclass
class Sink:
    notices: list[tuple[TransitionNotice, datetime, str]] = field(default_factory=list)

    def __call__(self, notice: TransitionNotice, at: datetime, correlation_id: str) -> None:
        self.notices.append((notice, at, correlation_id))


def _universe(*symbols: str) -> Callable[[], Awaitable[tuple[str, ...]]]:
    async def source() -> tuple[str, ...]:
        return symbols

    return source


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(sqlite_url(tmp_path / "audit_runner.db"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
        session.commit()
    yield engine
    engine.dispose()


def _sessions(engine: Engine) -> Callable[[], Session]:
    return lambda: Session(engine)


_vendor_ids = iter(range(1, 1_000_000))


def _article(session: Session, *, at: datetime, canonical_id: int | None = None) -> int:
    """A stored article; ``canonical_id`` links it into that article's group (decision 3)."""
    vendor_id = str(next(_vendor_ids))
    article = NewsArticle(
        vendor="alpaca",
        vendor_id=vendor_id,
        feed="alpaca_news",
        canonical_id=canonical_id,
        url=f"https://example.com/synthetic/{vendor_id}",
        url_key=f"https://example.com/synthetic/{vendor_id}",
        headline=f"SYNTHETIC headline {vendor_id}",
        headline_key=f"synthetic headline {vendor_id}",
        summary=None,
        publisher="Synthetic",
        published_at=at,
        ingested_at=at,
    )
    session.add(article)
    session.flush()
    return article.id


def _label_on(session: Session, article_id: int, *, ticker: str, at: datetime) -> int:
    """A bullish ``rules`` label on an existing article."""
    row = SentimentLabelRow(
        article_id=article_id,
        ticker=ticker,
        source="rules",
        tier="rules",
        direction="bullish",
        reasoning="synthetic",
        rule_id="synthetic_rule",
        labeled_at=at,
    )
    session.add(row)
    session.flush()
    return row.id


def _label(
    session: Session, *, ticker: str, source: str, direction: str, at: datetime
) -> int:
    vendor_id = str(next(_vendor_ids))
    article = NewsArticle(
        vendor="alpaca",
        vendor_id=vendor_id,
        feed="alpaca_news",
        canonical_id=None,
        url=f"https://example.com/synthetic/{vendor_id}",
        url_key=f"https://example.com/synthetic/{vendor_id}",
        headline=f"SYNTHETIC headline {vendor_id}",
        headline_key=f"synthetic headline {vendor_id}",
        summary=None,
        publisher="Synthetic",
        published_at=at,
        ingested_at=at,
    )
    session.add(article)
    session.flush()
    row = SentimentLabelRow(
        article_id=article.id,
        ticker=ticker,
        source=source,
        tier="rules" if source == "rules" else "vendor",
        direction=direction,
        reasoning="synthetic",
        rule_id="synthetic_rule" if source == "rules" else None,
        labeled_at=at,
    )
    session.add(row)
    session.flush()
    return row.id


def _the_week(engine: Engine) -> None:
    with Session(engine) as session:
        for i in range(LABELS_PER_SOURCE):
            at = MONDAY_10 + timedelta(minutes=i)
            _label(session, ticker="AAPL", source="rules", direction="bullish", at=at)
            _label(session, ticker="MSFT", source="massive", direction="bearish", at=at)
        session.commit()


def _deps(
    engine: Engine,
    *,
    bars: FakeBars | None = None,
    sink: Sink | None = None,
    clock: Callable[[], datetime] | None = None,
    universe: Callable[[], object] | None = None,
) -> SentimentAuditDeps:
    return SentimentAuditDeps(
        session_factory=_sessions(engine),
        bars=bars if bars is not None else FakeBars(),
        watch_universe=universe if universe is not None else _universe("AAPL", "MSFT"),  # type: ignore[arg-type]
        emit=sink if sink is not None else Sink(),
        clock=clock if clock is not None else TickingClock(SATURDAY_10),
    )


def _count(engine: Engine, model: type[Base]) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _run(deps: SentimentAuditDeps, trigger: str = "scheduled") -> audit_runner.AuditRunResult:
    return asyncio.run(run_sentiment_audit(deps, trigger=trigger))  # type: ignore[arg-type]


# --------------------------------------------------------------------- tests


def test_one_run_writes_outcomes_accuracy_and_transitions(db_engine: Engine) -> None:
    _the_week(db_engine)
    with Session(db_engine) as session:
        # Off-watch: one with no verdict (pending, reported), one failing.
        _label(session, ticker="ZZZZ", source="rules", direction="bullish", at=MONDAY_10)
        untradeable = _label(
            session, ticker="QQQQ", source="rules", direction="bullish", at=MONDAY_10
        )
        session.add(
            TickerTradeability(
                ticker="QQQQ",
                # The label's own session: a verdict from after it no longer
                # applies (the verdict is the label's, not the run's).
                session_date=MONDAY,
                has_options=False,
                standard_root=None,
                avg_volume_20d=None,
                last_close=None,
                sessions_available=0,
                passes=False,
                failures="no_options",
                checked_at=datetime(2026, 11, 20, 13, 0, tzinfo=UTC),
            )
        )
        session.commit()
    bars, sink = FakeBars(), Sink()
    result = _run(_deps(db_engine, bars=bars, sink=sink))

    with Session(db_engine) as session:
        run = session.get(AuditRun, result.run_id)
        assert run is not None
        assert (run.status, run.trigger) == ("succeeded", "scheduled")
        assert run.started_at == SATURDAY_10 == result.started_at
        assert run.finished_at == result.finished_at and result.finished_at > result.started_at

        graded = session.scalars(
            select(LabelOutcome).where(LabelOutcome.skip_reason.is_(None))
        ).all()
        assert len(graded) == 2 * 2 * LABELS_PER_SOURCE
        assert {o.scope for o in graded} == {"watch"}
        assert {o.audit_run_id for o in graded} == {result.run_id}
        skipped = session.scalars(
            select(LabelOutcome).where(LabelOutcome.skip_reason.is_not(None))
        ).all()
        assert [(o.label_id, o.skip_reason, o.scope) for o in skipped] == [
            (untradeable, "off_watch_untradeable", None),
            (untradeable, "off_watch_untradeable", None),
        ]

        accuracy = {
            (a.source, a.window, a.basis, a.scope): (a.graded, a.correct, a.neutral)
            for a in session.scalars(
                select(SentimentAccuracy).where(SentimentAccuracy.audit_run_id == result.run_id)
            )
        }
        assert len(accuracy) == 2 * 2 * 3  # source x window x scope, trailing_28d only
        for window in ("1h", "1d"):
            assert accuracy[("rules", window, "trailing_28d", "all")] == (30, 30, 0)
            assert accuracy[("rules", window, "trailing_28d", "watch")] == (30, 30, 0)
            assert accuracy[("rules", window, "trailing_28d", "discovery")] == (0, 0, 0)
            assert accuracy[("massive", window, "trailing_28d", "all")] == (30, 0, 0)

        statuses = {s.source: s for s in session.scalars(select(SentimentSourceStatus))}
        assert statuses["rules"].status == "active"
        assert statuses["massive"].status == "demoted"
        for status in statuses.values():
            # changed_at is the run instant; audit_log.at is the finishing reading.
            assert status.changed_at == result.started_at
            assert status.audit_run_id == result.run_id
            assert status.reason

        logs = session.scalars(
            select(AuditLog).where(AuditLog.category == "sentiment").order_by(AuditLog.field)
        ).all()
        assert [(r.field, r.previous_value, r.new_value) for r in logs] == [
            ("massive", "unaudited", "demoted"),
            ("rules", "unaudited", "active"),
        ]
        assert all(result.started_at < r.at <= result.finished_at for r in logs)
        assert all(r.at == result.finished_at for r in logs)

    # Exactly one notice per transition that has one: unaudited -> active emits none.
    assert len(result.transitions) == 2
    assert [(n.event.value, n.severity) for n, _, _ in sink.notices] == [
        ("sentiment_demoted", "warning")
    ]
    _, at, correlation_id = sink.notices[0]
    assert at == result.finished_at
    assert correlation_id == result.detail["correlation_id"]

    detail = json.loads(run.detail or "{}")
    assert detail["graded"] == 120
    assert detail["skipped"] == {"off_watch_untradeable": 2}
    assert detail["unverdicted_label_counts"] == {"ZZZZ": 1}
    assert detail["discovery_cap_skipped_labels"] == 0
    assert detail["discovery_cap_skipped_tickers"] == 0
    assert detail["bar_faults"] == []
    assert detail["unschedulable_label_ids"] == []
    assert detail["future_referenced"] == {}
    assert detail["trigger"] == "scheduled"


def test_the_history_join_finds_the_run_and_the_figures_acted_on(db_engine: Engine) -> None:
    """The join in ``models.SentimentSourceStatus``'s docstring, run as written."""
    _the_week(db_engine)
    result = _run(_deps(db_engine))
    join = text(
        """
        SELECT al.field, al.previous_value, al.new_value, ar.id AS run_id,
               sa."window", sa.basis, sa.graded, sa.correct
        FROM audit_log AS al
        JOIN audit_run AS ar
          ON ar.started_at < al.at AND al.at <= ar.finished_at
        JOIN sentiment_accuracy AS sa
          ON sa.audit_run_id = ar.id
         AND sa.source = al.field
         AND sa.scope = 'all'
         AND sa.basis = CASE al.previous_value
                          WHEN 'demoted' THEN 'since_demotion'
                          ELSE 'trailing_28d' END
        WHERE al.category = 'sentiment'
        ORDER BY al.field, sa."window"
        """
    )
    with Session(db_engine) as session:
        rows = [tuple(r) for r in session.execute(join)]
    assert rows == [
        ("massive", "unaudited", "demoted", result.run_id, "1d", "trailing_28d", 30, 0),
        ("massive", "unaudited", "demoted", result.run_id, "1h", "trailing_28d", 30, 0),
        ("rules", "unaudited", "active", result.run_id, "1d", "trailing_28d", 30, 30),
        ("rules", "unaudited", "active", result.run_id, "1h", "trailing_28d", 30, 30),
    ]
    # The figures acted on are the transition's own.
    acted = {
        (t.source, r.window): (r.graded, r.correct)
        for t in result.transitions
        for r in t.acted_on
    }
    assert acted == {(row[0], row[4]): (row[6], row[7]) for row in rows}


def test_re_running_is_a_no_op(db_engine: Engine) -> None:
    _the_week(db_engine)
    clock = TickingClock(SATURDAY_10)
    sink = Sink()
    first = _run(_deps(db_engine, clock=clock, sink=sink))
    outcomes, logs = _count(db_engine, LabelOutcome), _count(db_engine, AuditLog)
    bars = FakeBars()
    second = _run(_deps(db_engine, clock=clock, sink=sink, bars=bars))

    assert second.run_id != first.run_id
    assert second.started_at >= first.finished_at
    assert _count(db_engine, LabelOutcome) == outcomes
    assert _count(db_engine, AuditLog) == logs
    assert second.transitions == ()
    assert len(sink.notices) == 1  # the first run's demotion, and nothing since
    assert bars.calls == []  # nothing pending, nothing fetched
    with Session(db_engine) as session:
        run = session.get(AuditRun, second.run_id)
        assert run is not None and run.status == "succeeded"
        status = session.get(SentimentSourceStatus, "massive")
        assert status is not None and status.audit_run_id == first.run_id


def test_a_stale_running_row_is_closed_as_failed_before_the_new_run(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        stale = AuditRun(
            started_at=SATURDAY_10 - timedelta(days=7),
            finished_at=None,
            trigger="scheduled",
            status="running",
        )
        session.add(stale)
        session.commit()
        stale_id = stale.id
    result = _run(_deps(db_engine))
    with Session(db_engine) as session:
        closed = session.get(AuditRun, stale_id)
        assert closed is not None
        assert closed.status == "failed"
        assert closed.finished_at == result.started_at
        detail = json.loads(closed.detail or "{}")
        assert detail["abandoned"] is True
        assert detail["closed_by_correlation_id"] == result.detail["correlation_id"]
        current = session.get(AuditRun, result.run_id)
        assert current is not None and current.status == "succeeded"


def test_a_provider_error_on_one_symbol_leaves_the_others_graded(db_engine: Engine) -> None:
    """A NaN bar raises ``WireFormatError`` inside the provider (audit.py's note)."""
    _the_week(db_engine)
    bars = FakeBars(raises={("MSFT", TUESDAY): WireFormatError("not finite")})
    result = _run(_deps(db_engine, bars=bars))
    with Session(db_engine) as session:
        written = {
            (row.ticker, o.window)
            for o, row in session.execute(
                select(LabelOutcome, SentimentLabelRow).join(
                    SentimentLabelRow, SentimentLabelRow.id == LabelOutcome.label_id
                )
            )
        }
        # MSFT's 1d windows need Tuesday's MSFT bars: they wait (under a week old).
        assert written == {("AAPL", "1h"), ("AAPL", "1d"), ("MSFT", "1h")}
        assert _count(db_engine, LabelOutcome) == 3 * LABELS_PER_SOURCE
    assert result.detail["bar_faults"] == [
        {"symbol": "MSFT", "session_date": "2026-11-17", "reason": "vendor_data:WireFormatError"}
    ]
    assert result.detail["awaiting_bars"] == LABELS_PER_SOURCE
    # Massive's 1d has no grades, so it is ineligible and does not move.
    assert [t.source for t in result.transitions] == ["rules"]


def _late_run(
    engine: Engine, exc: BaseException, late: timedelta = timedelta(days=7)
) -> audit_runner.AuditRunResult:
    """The week graded ``late`` after its Saturday. The default is ten days
    after Tuesday: every missing price is past its week, and inside the
    trailing window's hold-back bound."""
    _the_week(engine)
    bars = FakeBars(raises={("AAPL", TUESDAY): exc})
    return _run(_deps(engine, bars=bars, clock=TickingClock(SATURDAY_10 + late)))


def _aapl_1d(engine: Engine) -> list[str | None]:
    with Session(engine) as session:
        return [
            o.skip_reason
            for o in session.scalars(
                select(LabelOutcome)
                .join(SentimentLabelRow, SentimentLabelRow.id == LabelOutcome.label_id)
                .where(SentimentLabelRow.ticker == "AAPL")
                .where(LabelOutcome.window == "1d")
            )
        ]


def test_a_transient_fetch_failure_never_writes_price_unavailable(db_engine: Engine) -> None:
    result = _late_run(db_engine, ProviderError("GET /v2/stocks/bars failed: timed out"))
    assert _aapl_1d(db_engine) == []  # held back, retried next run
    assert result.detail["held_for_transient_fetch_failure"] == LABELS_PER_SOURCE
    assert result.detail["bar_faults"] == [
        {"symbol": "AAPL", "session_date": "2026-11-17", "reason": "fetch_failed:ProviderError"}
    ]
    # No vendor text reaches the detail.
    assert "timed out" not in json.dumps(result.detail)


def test_a_vendor_data_fault_past_its_week_is_price_unavailable(db_engine: Engine) -> None:
    wrapped = ProviderError("bad bar")
    wrapped.__cause__ = WireFormatError("not finite")
    result = _late_run(db_engine, wrapped)
    assert _aapl_1d(db_engine) == ["price_unavailable"] * LABELS_PER_SOURCE
    assert result.detail["held_for_transient_fetch_failure"] == 0


@pytest.mark.risk
@pytest.mark.parametrize("halted", [True, False])
def test_a_failed_run_is_marked_failed_and_the_halt_state_is_untouched(
    db_engine: Engine, halted: bool
) -> None:
    halted_at = datetime(2026, 11, 20, 21, 0, tzinfo=UTC) if halted else None
    reason = "cold start" if halted else None
    with Session(db_engine) as session:
        state = session.get(EngineState, 1)
        assert state is not None  # seeded
        state.halted, state.halted_reason, state.halted_at = halted, reason, halted_at
        session.commit()
    _the_week(db_engine)

    async def broken() -> tuple[str, ...]:
        raise RuntimeError("seed unreadable: s3cr3t-in-message")

    with pytest.raises(RuntimeError):
        _run(_deps(db_engine, universe=broken))
    with Session(db_engine) as session:
        run = session.scalars(select(AuditRun)).one()
        assert run.status == "failed" and run.finished_at is not None
        assert run.finished_at > run.started_at
        detail = json.loads(run.detail or "{}")
        assert (detail["stage"], detail["error_type"]) == ("plan", "RuntimeError")
        assert "s3cr3t" not in (run.detail or "")
        state = session.get(EngineState, 1)
        assert state is not None
        assert (state.halted, state.halted_reason, state.halted_at) == (
            halted,
            reason,
            halted_at,
        )
    assert _count(db_engine, LabelOutcome) == 0

    # The next run starts (the failed row is closed, not running) and
    # *succeeds* with both transitions and a notice -- the branch that writes
    # ``sentiment_source_status`` and ``audit_log`` and emits -- and the halt
    # state is still exactly what it was (fix B, finding 4).
    sink = Sink()
    succeeded = _run(
        _deps(db_engine, sink=sink, clock=TickingClock(SATURDAY_10 + timedelta(hours=1)))
    )
    assert [(t.source, t.new.value) for t in succeeded.transitions] == [
        ("massive", "demoted"),
        ("rules", "active"),
    ]
    assert [n.event.value for n, _, _ in sink.notices] == ["sentiment_demoted"]
    with Session(db_engine) as session:
        state = session.get(EngineState, 1)
        assert state is not None
        assert (state.halted, state.halted_reason, state.halted_at) == (
            halted,
            reason,
            halted_at,
        )


def test_a_second_concurrent_run_is_refused_and_touches_nothing(db_engine: Engine) -> None:
    assert audit_runner._RUN_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(AuditAlreadyRunning):
            _run(_deps(db_engine))
    finally:
        audit_runner._RUN_LOCK.release()
    assert _count(db_engine, AuditRun) == 0


def test_bars_are_requested_only_through_the_provider_interface(db_engine: Engine) -> None:
    _the_week(db_engine)
    bars = FakeBars()
    _run(_deps(db_engine, bars=bars))
    assert {timeframe for _, timeframe, _, _ in bars.calls} == {BarTimeframe.MINUTE}
    # One request per session over every symbol it needs (fix B: the spec's
    # page budget assumes multi-symbol requests).
    assert [(symbols, start) for symbols, _, start, _ in bars.calls] == [
        (("AAPL", "MSFT", "SPY"), _EPOCH),
        (("AAPL", "MSFT", "SPY"), _EPOCH + timedelta(days=1)),
    ]
    tree = ast.parse(inspect.getsource(audit_runner))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {"alpaca", "httpx", "requests"}


# ------------------------------------------------- duplicate copies (fix A)


def _outcomes_by_label(engine: Engine) -> dict[int, list[tuple[str, str | None, str | None]]]:
    with Session(engine) as session:
        rows = session.scalars(
            select(LabelOutcome).order_by(LabelOutcome.label_id, LabelOutcome.window)
        ).all()
        found: dict[int, list[tuple[str, str | None, str | None]]] = {}
        for o in rows:
            found.setdefault(o.label_id, []).append((o.window, o.skip_reason, o.scope))
        return found


def _group_labels_pick(engine: Engine) -> set[int]:
    with Session(engine) as session:
        picked = group_labels()
        return set(session.scalars(select(picked.c.label_id).where(picked.c.pick == 1)))


def test_two_stored_copies_of_one_story_are_graded_once(db_engine: Engine) -> None:
    """The member's label is written first (lower label id); group_labels picks the canonical's."""
    with Session(db_engine) as session:
        canonical = _article(session, at=MONDAY_10)
        member = _article(session, at=MONDAY_10, canonical_id=canonical)
        member_label = _label_on(session, member, ticker="AAPL", at=MONDAY_10)
        canonical_label = _label_on(session, canonical, ticker="AAPL", at=MONDAY_10)
        session.commit()
    assert member_label < canonical_label
    result = _run(_deps(db_engine))

    assert _outcomes_by_label(db_engine) == {
        canonical_label: [("1d", None, "watch"), ("1h", None, "watch")],
        member_label: [("1d", "duplicate_copy", None), ("1h", "duplicate_copy", None)],
    }
    # The graded copy is exactly the one the feed, Movers and the refresh show.
    assert _group_labels_pick(db_engine) == {canonical_label}
    with Session(db_engine) as session:
        run = session.get(AuditRun, result.run_id)
        assert run is not None and run.detail is not None
        assert json.loads(run.detail)["skipped"] == {"duplicate_copy": 2}
        rules_all = session.scalars(
            select(SentimentAccuracy).where(
                SentimentAccuracy.audit_run_id == result.run_id,
                SentimentAccuracy.source == "rules",
                SentimentAccuracy.scope == "all",
            )
        ).all()
        assert {(a.window, a.graded) for a in rules_all} == {("1h", 1), ("1d", 1)}


def test_a_later_labelled_canonical_does_not_regrade_the_story(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        canonical = _article(session, at=MONDAY_10)
        member = _article(session, at=MONDAY_10, canonical_id=canonical)
        member_label = _label_on(session, member, ticker="AAPL", at=MONDAY_10)
        session.commit()
    clock = TickingClock(SATURDAY_10)
    _run(_deps(db_engine, clock=clock))
    # Only now is the canonical copy labelled; group_labels' pick moves to it.
    with Session(db_engine) as session:
        canonical_label = _label_on(session, canonical, ticker="AAPL", at=MONDAY_10)
        session.commit()
    assert _group_labels_pick(db_engine) == {canonical_label}
    bars = FakeBars()
    _run(_deps(db_engine, clock=clock, bars=bars))

    assert _outcomes_by_label(db_engine) == {
        member_label: [("1d", None, "watch"), ("1h", None, "watch")],
        canonical_label: [("1d", "duplicate_copy", None), ("1h", "duplicate_copy", None)],
    }
    assert bars.calls == []  # a duplicate needs no bars


# ------------------------------------------ the label's own verdict (fix A)


def _verdict(session: Session, ticker: str, day: date, passes: bool) -> None:
    session.add(
        TickerTradeability(
            ticker=ticker,
            session_date=day,
            has_options=passes,
            standard_root=True if passes else None,
            avg_volume_20d=5_000_000 if passes else None,
            last_close=Decimal("50") if passes else None,
            sessions_available=20,
            passes=passes,
            failures="" if passes else "no_options",
            checked_at=datetime.combine(day, datetime.min.time(), tzinfo=_ET).astimezone(UTC)
            + timedelta(hours=9),
        )
    )


def test_each_off_watch_label_reads_the_verdict_of_its_own_session(db_engine: Engine) -> None:
    """Q7: a Monday label graded Saturday reads Monday's verdict, never Friday's."""
    friday = date(2026, 11, 20)
    with Session(db_engine) as session:
        fails_then = _label(session, ticker="XFAIL", source="rules", direction="bullish", at=MONDAY_10)
        passes_then = _label(session, ticker="XPASS", source="rules", direction="bullish", at=MONDAY_10)
        old_pass = _label(session, ticker="XOLD", source="rules", direction="bullish", at=MONDAY_10)
        _verdict(session, "XFAIL", MONDAY, False)
        _verdict(session, "XFAIL", friday, True)
        _verdict(session, "XPASS", MONDAY, True)
        _verdict(session, "XPASS", friday, False)
        # Only a verdict from the week before: still the newest on or before Monday.
        _verdict(session, "XOLD", date(2026, 11, 10), True)
        session.commit()
    _run(_deps(db_engine))

    outcomes = _outcomes_by_label(db_engine)
    assert outcomes[fails_then] == [
        ("1d", "off_watch_untradeable", None),
        ("1h", "off_watch_untradeable", None),
    ]
    assert outcomes[passes_then] == [("1d", None, "discovery"), ("1h", None, "discovery")]
    assert outcomes[old_pass] == [("1d", None, "discovery"), ("1h", None, "discovery")]


def test_a_verdict_only_after_the_label_leaves_it_pending(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        _label(session, ticker="XLATE", source="rules", direction="bullish", at=MONDAY_10)
        _verdict(session, "XLATE", date(2026, 11, 20), True)
        session.commit()
    result = _run(_deps(db_engine))
    assert _outcomes_by_label(db_engine) == {}
    with Session(db_engine) as session:
        run = session.get(AuditRun, result.run_id)
        assert run is not None and run.detail is not None
        assert "XLATE" in run.detail


# ------------------------------------------- fix B: the hold-back is bounded


def test_a_transient_failure_past_the_trailing_window_is_price_unavailable(
    db_engine: Engine,
) -> None:
    """A fault that fails the same way every week (a 422, a bar missing a
    field: a bare ``ProviderError`` either way, indistinguishable from a 5xx)
    is held back only while the window is inside the trailing window."""
    result = _late_run(
        db_engine, ProviderError("GET /v2/stocks/bars returned 422"), late=TRAILING_WINDOW
    )
    assert _aapl_1d(db_engine) == ["price_unavailable"] * LABELS_PER_SOURCE
    assert result.detail["held_for_transient_fetch_failure"] == 0
    assert result.detail["released_past_hold_bound"] == LABELS_PER_SOURCE
    assert result.detail["bar_faults"] == [
        {"symbol": "AAPL", "session_date": "2026-11-17", "reason": "fetch_failed:ProviderError"}
    ]


def _aapl_1d_outcome_at() -> datetime:
    """The 1d window's end for one AAPL label at Monday 10:00, from the pure planner."""
    label = AuditLabel(
        label_id=1,
        ticker="AAPL",
        source="rules",
        direction=Direction.BULLISH,
        published_at=MONDAY_10,
        labeled_at=MONDAY_10,
        group_id=1,
        group_pick=True,
    )
    plan = plan_audit(
        [label],
        watch_universe={"AAPL"},
        tradeable={},
        written=set(),
        claimed_groups=set(),
        run_at=SATURDAY_10,
    )
    (outcome_at,) = [g.outcome_at for g in plan.to_grade if g.window == "1d"]
    return outcome_at


def test_the_hold_back_bound_holds_at_the_boundary_and_releases_past_it(
    db_engine: Engine,
) -> None:
    with Session(db_engine) as session:
        _label(session, ticker="AAPL", source="rules", direction="bullish", at=MONDAY_10)
        session.commit()
    bound = _aapl_1d_outcome_at() + TRAILING_WINDOW
    bars = FakeBars(raises={("AAPL", TUESDAY): ProviderError("timed out")})
    clock = TickingClock(bound)

    at_bound = _run(_deps(db_engine, bars=bars, clock=clock))
    assert at_bound.started_at == bound
    assert at_bound.detail["held_for_transient_fetch_failure"] == 1
    assert _aapl_1d(db_engine) == []

    past = _run(_deps(db_engine, bars=bars, clock=clock))
    assert past.started_at > bound
    assert past.detail["released_past_hold_bound"] == 1
    assert _aapl_1d(db_engine) == ["price_unavailable"]


def test_a_bar_without_its_stamp_is_a_vendor_data_fault(db_engine: Engine) -> None:
    """The provider decodes ``payload["t"]`` bare: a missing stamp is a ``KeyError``."""
    result = _late_run(db_engine, KeyError("t"))
    assert _aapl_1d(db_engine) == ["price_unavailable"] * LABELS_PER_SOURCE
    assert result.detail["held_for_transient_fetch_failure"] == 0
    assert result.detail["bar_faults"] == [
        {"symbol": "AAPL", "session_date": "2026-11-17", "reason": "vendor_data:KeyError"}
    ]


# ------------------------------------------ fix B: one request per session


def _engine_at(path: Path) -> Engine:
    engine = create_db_engine(sqlite_url(path))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
        session.commit()
    return engine


def test_one_request_per_session_split_at_the_symbol_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert audit_runner.BAR_BATCH_MAX_SYMBOLS == 200
    # The per-symbol ranges, from a run that may batch nothing.
    single = _engine_at(tmp_path / "single.db")
    monkeypatch.setattr(audit_runner, "BAR_BATCH_MAX_SYMBOLS", 1)
    _the_week(single)
    one_by_one = FakeBars()
    _run(_deps(single, bars=one_by_one))
    single.dispose()
    ends = {(symbols[0], start): end for symbols, _, start, end in one_by_one.calls}
    assert len(ends) == len(one_by_one.calls) == 6

    batched_engine = _engine_at(tmp_path / "batched.db")
    monkeypatch.setattr(audit_runner, "BAR_BATCH_MAX_SYMBOLS", 2)
    _the_week(batched_engine)
    batched = FakeBars()
    result = _run(_deps(batched_engine, bars=batched))
    batched_engine.dispose()

    tuesday_open = _EPOCH + timedelta(days=1)
    expected = []
    for start in (_EPOCH, tuesday_open):
        for chunk in (("AAPL", "MSFT"), ("SPY",)):
            # A batch starts at the session's open and ends at the latest end
            # any of its symbols needs there.
            expected.append((chunk, start, max(ends[(s, start)] for s in chunk)))
    assert [(symbols, start, end) for symbols, _, start, end in batched.calls] == expected
    assert result.detail["provider_requests"] == 4
    assert result.detail["graded"] == 4 * LABELS_PER_SOURCE


def test_a_failed_batch_is_retried_one_symbol_at_a_time(db_engine: Engine) -> None:
    _the_week(db_engine)
    bars = FakeBars(raises={("MSFT", TUESDAY): WireFormatError("not finite")})
    result = _run(_deps(db_engine, bars=bars))
    tuesday_open = _EPOCH + timedelta(days=1)
    assert [(symbols, start) for symbols, _, start, _ in bars.calls] == [
        (("AAPL", "MSFT", "SPY"), _EPOCH),
        (("AAPL", "MSFT", "SPY"), tuesday_open),
        (("AAPL",), tuesday_open),
        (("MSFT",), tuesday_open),
        (("SPY",), tuesday_open),
    ]
    # Each retry asks for its own range, never more than the batch did.
    batch_end = bars.calls[1][3]
    assert batch_end is not None
    assert all(end is not None and end <= batch_end for _, _, _, end in bars.calls[2:])
    # The fault stays MSFT's: AAPL's and SPY's Tuesday bars arrived and graded.
    assert result.detail["bar_faults"] == [
        {"symbol": "MSFT", "session_date": "2026-11-17", "reason": "vendor_data:WireFormatError"}
    ]
    assert result.detail["provider_requests"] == 5
    assert result.detail["graded"] == 3 * LABELS_PER_SOURCE
    assert result.detail["fetch_aborted"] is None


def test_an_outage_stops_the_fetch_stage_and_holds_every_window(db_engine: Engine) -> None:
    assert audit_runner.FETCH_ABORT_AFTER == 5
    _the_week(db_engine)
    bars = FakeBars(outage=ProviderError("GET /v2/stocks/bars returned 503: s3cr3t"))
    result = _run(
        _deps(db_engine, bars=bars, clock=TickingClock(SATURDAY_10 + timedelta(days=7)))
    )
    # Monday's batch, its three retries, Tuesday's batch: five in a row, stop.
    assert [len(symbols) for symbols, _, _, _ in bars.calls] == [3, 1, 1, 1, 3]
    assert _count(db_engine, LabelOutcome) == 0
    assert result.detail["held_for_transient_fetch_failure"] == 4 * LABELS_PER_SOURCE
    assert result.detail["fetch_aborted"] == {
        "after_consecutive_transient_failures": 5,
        "last_error_type": "ProviderError",
        "unrequested_symbol_sessions": 3,
    }
    assert "s3cr3t" not in json.dumps(result.detail)
    with Session(db_engine) as session:
        run = session.get(AuditRun, result.run_id)
        assert run is not None and run.status == "succeeded"


# ------------------------------------- fix B: overlap, status writes, cancel


def _running_row(engine: Engine, started_at: datetime) -> int:
    with Session(engine) as session:
        row = AuditRun(started_at=started_at, finished_at=None, trigger="manual", status="running")
        session.add(row)
        session.commit()
        return row.id


def test_a_recent_running_row_is_another_processes_live_run(db_engine: Engine) -> None:
    """``dev_app`` starts the scheduler too: a young ``running`` row is not abandoned."""
    live = _running_row(db_engine, SATURDAY_10 - ABANDONED_RUN_AFTER)  # exactly at the bound
    with pytest.raises(AuditAlreadyRunning):
        _run(_deps(db_engine))
    with Session(db_engine) as session:
        rows = session.scalars(select(AuditRun)).all()
        assert [(r.id, r.status, r.finished_at) for r in rows] == [(live, "running", None)]


def test_a_running_row_older_than_the_bound_is_abandoned(db_engine: Engine) -> None:
    assert ABANDONED_RUN_AFTER == timedelta(hours=2)
    stale = _running_row(
        db_engine, SATURDAY_10 - ABANDONED_RUN_AFTER - timedelta(microseconds=1)
    )
    result = _run(_deps(db_engine))
    with Session(db_engine) as session:
        closed = session.get(AuditRun, stale)
        assert closed is not None and closed.status == "failed"
        assert json.loads(closed.detail or "{}")["abandoned"] is True
        current = session.get(AuditRun, result.run_id)
        assert current is not None and current.status == "succeeded"


def test_status_writes_never_overwrite_a_terminal_row(db_engine: Engine) -> None:
    factory = _sessions(db_engine)
    with Session(db_engine) as session:
        done = AuditRun(
            started_at=MONDAY_10,
            finished_at=MONDAY_10 + timedelta(minutes=1),
            trigger="scheduled",
            status="succeeded",
            detail='{"kept": true}',
        )
        failed = AuditRun(
            started_at=MONDAY_10 + timedelta(minutes=2),
            finished_at=MONDAY_10 + timedelta(minutes=3),
            trigger="scheduled",
            status="failed",
            detail='{"stage": "fetch"}',
        )
        session.add_all([done, failed])
        session.commit()
        done_id, failed_id = done.id, failed.id
    assert audit_runner._fail_run(factory, done_id, SATURDAY_10, {"stage": "close"}) is False
    with pytest.raises(RuntimeError, match="no longer running"):
        audit_runner._close_run(factory, failed_id, [], [], SATURDAY_10, {"graded": 0})
    with Session(db_engine) as session:
        rows = {r.id: (r.status, r.detail) for r in session.scalars(select(AuditRun))}
    assert rows == {
        done_id: ("succeeded", '{"kept": true}'),
        failed_id: ("failed", '{"stage": "fetch"}'),
    }


def _blocking(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> tuple[threading.Event, threading.Event]:
    """Make ``audit_runner.<name>`` block on its worker thread until released."""
    entered, release = threading.Event(), threading.Event()
    real = getattr(audit_runner, name)

    def blocked(*args: object) -> object:
        entered.set()
        release.wait(10)
        return real(*args)

    monkeypatch.setattr(audit_runner, name, blocked)
    return entered, release


async def _cancel_while_blocked(
    deps: SentimentAuditDeps, entered: threading.Event, release: threading.Event
) -> None:
    task = asyncio.create_task(run_sentiment_audit(deps, trigger="scheduled"))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        # The worker thread is still in flight: the run still holds the lock.
        assert audit_runner._RUN_LOCK.locked()
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not audit_runner._RUN_LOCK.locked()


def test_a_cancelled_run_waits_for_its_worker_thread_before_releasing(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _the_week(db_engine)
    entered, release = _blocking(monkeypatch, "_write_outcomes")
    asyncio.run(_cancel_while_blocked(_deps(db_engine), entered, release))
    with Session(db_engine) as session:
        run = session.scalars(select(AuditRun)).one()
        # The write finished, and only then was the run closed failed.
        assert run.status == "failed"
        assert json.loads(run.detail or "{}")["error_type"] == "CancelledError"
    assert _count(db_engine, LabelOutcome) == 4 * LABELS_PER_SOURCE


def test_a_cancellation_while_recording_a_failure_is_not_swallowed(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run fails (RuntimeError); shutdown cancels it while that is being
    recorded. The task must end *cancelled*, not with the RuntimeError."""

    async def broken() -> tuple[str, ...]:
        raise RuntimeError("universe down")

    entered, release = _blocking(monkeypatch, "_fail_run")
    asyncio.run(_cancel_while_blocked(_deps(db_engine, universe=broken), entered, release))
    with Session(db_engine) as session:
        run = session.scalars(select(AuditRun)).one()
        assert run.status == "failed"
        assert json.loads(run.detail or "{}")["error_type"] == "RuntimeError"


# ---------------------------- follow-up F1: a cancel during step a leaves no row


def test_a_cancellation_while_opening_closes_the_runs_own_row(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_on_worker`` waits for ``_open_run``'s thread, which commits the
    ``running`` row, and then re-raises: that row must be closed, or it
    refuses every run for :data:`ABANDONED_RUN_AFTER`."""
    _the_week(db_engine)
    entered, release = _blocking(monkeypatch, "_open_run")
    asyncio.run(_cancel_while_blocked(_deps(db_engine), entered, release))
    with Session(db_engine) as session:
        rows = session.scalars(select(AuditRun)).all()
        assert [r.status for r in rows] == ["failed"]
        (run,) = rows
        assert run.finished_at is not None
        detail = json.loads(run.detail or "{}")
        assert (detail["stage"], detail["error_type"]) == ("open", "CancelledError")
    assert _count(db_engine, LabelOutcome) == 0
    # Nothing blocks the next run.
    monkeypatch.undo()
    later = _run(_deps(db_engine, clock=TickingClock(SATURDAY_10 + timedelta(minutes=1))))
    assert later.detail["graded"] == 4 * LABELS_PER_SOURCE


# ------------- follow-up F2: the hold-back bound releases only attempted faults

#: Wednesday 2026-11-18 10:00 ET.
WEDNESDAY_10 = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)


def _outcomes_of(engine: Engine, label_id: int) -> dict[str, str | None]:
    with Session(engine) as session:
        return {
            o.window: o.skip_reason
            for o in session.scalars(select(LabelOutcome).where(LabelOutcome.label_id == label_id))
        }


def test_an_abort_releases_attempted_old_windows_and_holds_unrequested_ones(
    db_engine: Engine,
) -> None:
    """Past the hold-back bound, a window whose missing bars an *attempted*
    transient request explains is released ``price_unavailable``; one that
    only an unrequested pair explains was never tried, and stays pending."""
    assert audit_runner.FETCH_ABORT_AFTER == 5
    with Session(db_engine) as session:
        monday = _label(session, ticker="AAPL", source="rules", direction="bullish", at=MONDAY_10)
        wednesday = _label(
            session, ticker="AAPL", source="rules", direction="bullish", at=WEDNESDAY_10
        )
        session.commit()
    bars = FakeBars(outage=ProviderError("GET /v2/stocks/bars returned 503"))
    # Every window, Wednesday's included, ended more than TRAILING_WINDOW ago.
    run_at = WEDNESDAY_10 + TRAILING_WINDOW + timedelta(days=2)
    result = _run(_deps(db_engine, bars=bars, clock=TickingClock(run_at)))

    tuesday_open = _EPOCH + timedelta(days=1)
    # Monday's batch, its two retries, Tuesday's batch, AAPL's retry: stop.
    assert [(symbols, start) for symbols, _, start, _ in bars.calls] == [
        (("AAPL", "SPY"), _EPOCH),
        (("AAPL",), _EPOCH),
        (("SPY",), _EPOCH),
        (("AAPL", "SPY"), tuesday_open),
        (("AAPL",), tuesday_open),
    ]
    # Monday's 1h reads Monday only and 1d Monday and Tuesday: each window
    # includes an attempted transient fault, so both are released.
    assert _outcomes_of(db_engine, monday) == {"1h": "price_unavailable", "1d": "price_unavailable"}
    # Wednesday's windows read Wednesday and Thursday, never requested: held.
    assert _outcomes_of(db_engine, wednesday) == {}
    assert result.detail["released_past_hold_bound"] == 2
    assert result.detail["held_for_transient_fetch_failure"] == 2
    assert result.detail["fetch_aborted"] == {
        "after_consecutive_transient_failures": 5,
        "last_error_type": "ProviderError",
        "unrequested_symbol_sessions": 5,
    }
