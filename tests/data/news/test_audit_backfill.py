"""Owner decision Q24a (2026-11-11): backfill a past session's verdict when none exists.

Decision 25 grades an off-watch label against the newest ``ticker_tradeability``
verdict dated on or before its reference session S. Verdicts are written only
by the daily refresh, for its own date, so a label whose S has none waited a
week and was then written ``off_watch_untradeable`` -- permanently, even when
the ticker was tradeable (live run 3: up to 181 such labels).

Now the audit computes the missing verdict for ``(ticker, S)`` with
``refresh_tradeability(session_date=S)``, which judges only sessions
**strictly before** S, stores it, and grades on it. A failed backfill never
writes "untradeable": the label stays pending, and once no verdict can be had
it is written ``off_watch_unverdicted``. All data here is SYNTHETIC.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.data.news import audit_runner
from corollary.data.news.audit import (
    DISCOVERY_TICKER_CAP,
    PRICE_UNAVAILABLE_AFTER,
    TRAILING_WINDOW,
    VERDICT_BACKFILL_CAP,
    AuditLabel,
    SkipReason,
    plan_audit,
)
from corollary.data.news.audit_runner import SentimentAuditDeps, VerdictBackfill
from corollary.data.news.labels import Direction
from corollary.data.news.tradeability import refresh_tradeability
from corollary.data.providers.interface import AssetDirectory, Bar, ProviderError
from corollary.db.models import EngineState, TickerTradeability
from tests.data.news.test_audit_runner import (
    MONDAY,
    MONDAY_10,
    SATURDAY_10,
    TUESDAY,
    UTC,
    FakeBars,
    Sink,
    TickingClock,
    _label,
    _outcomes_by_label,
    _run,
    _sessions,
    _universe,
    _verdict,
    db_engine,
)
from tests.data.news.test_tradeability import bar, history, sessions_before
from tests.data.news.test_tradeability_cache import directory

__all__ = ["db_engine"]  # the fixture, re-exported so pytest finds it here

WEDNESDAY = date(2026, 11, 18)
THURSDAY = date(2026, 11, 19)


# --------------------------------------------------------------------- fakes


def _liquid(symbol: str, session_date: date) -> list[Bar]:
    """25 sessions of 2,000,000 shares closing at $50 before ``session_date``: passes."""
    return history(25, before=session_date, symbol=symbol)


def _flips_on_monday(symbol: str, session_date: date) -> list[Bar]:
    """**Ignores** ``session_date``: $4 closes up to Friday the 13th, then $50
    from Monday the 16th on. Judged on sessions before Monday it fails on its
    close; any reading of Monday's bar or later would pass it."""
    before = [bar(day, close="4", symbol=symbol) for day in sessions_before(MONDAY, 25)]
    after = [bar(day, close="50", symbol=symbol) for day in (MONDAY, TUESDAY, WEDNESDAY)]
    return before + after


@dataclass
class FakeInputs:
    """:class:`~corollary.data.news.tradeability.TradeabilityInputs`, recorded."""

    bars: Callable[[str, date], list[Bar]] = _liquid
    bar_error: BaseException | None = None
    roots: Mapping[str, bool] = field(default_factory=dict)
    bar_calls: list[tuple[tuple[str, ...], date]] = field(default_factory=list)
    root_calls: list[str] = field(default_factory=list)

    async def adv_daily_bars(
        self, symbols: Sequence[str], *, session_date: date
    ) -> dict[str, list[Bar]]:
        self.bar_calls.append((tuple(symbols), session_date))
        if self.bar_error is not None:
            raise self.bar_error
        return {s: self.bars(s, session_date) for s in symbols if self.bars(s, session_date)}

    async def has_standard_root(self, ticker: str) -> bool:
        self.root_calls.append(ticker)
        return self.roots.get(ticker, True)


def _deps(
    engine: Engine,
    *,
    inputs: FakeInputs | None,
    assets: AssetDirectory | None = None,
    clock: Callable[[], datetime] | None = None,
) -> SentimentAuditDeps:
    held = assets if assets is not None else directory("XBACK", "XA", "XB", "XOLD")
    return SentimentAuditDeps(
        session_factory=_sessions(engine),
        bars=FakeBars(),
        watch_universe=_universe("AAPL", "MSFT"),
        emit=Sink(),
        clock=clock if clock is not None else TickingClock(SATURDAY_10),
        backfill=None
        if inputs is None
        else VerdictBackfill(inputs=inputs, assets=lambda: held, ipo_dates=None),
    )


def _stored(engine: Engine) -> list[tuple[str, date, bool, str, Decimal | None]]:
    with Session(engine) as session:
        return [
            (row.ticker, row.session_date, row.passes, row.failures, row.last_close)
            for row in session.scalars(
                select(TickerTradeability).order_by(
                    TickerTradeability.ticker, TickerTradeability.session_date
                )
            )
        ]


def _engine_state(engine: Engine) -> list[tuple[object, ...]]:
    with Session(engine) as session:
        return [
            tuple(getattr(row, c.key) for c in EngineState.__table__.columns)
            for row in session.scalars(select(EngineState))
        ]


def _monday_label(engine: Engine, ticker: str = "XBACK") -> int:
    with Session(engine) as session:
        label_id = _label(session, ticker=ticker, source="rules", direction="bullish", at=MONDAY_10)
        session.commit()
    return label_id


_GRADED = [("1d", None, "discovery"), ("1h", None, "discovery")]


# ------------------------------------------------------- the no-look-ahead pin


def test_refresh_tradeability_reads_nothing_from_its_session_or_after(db_engine: Engine) -> None:
    """The guarantee Q24a rests on, on the function itself. The fake answers
    with bars on and after Monday whatever it is asked; judged for Monday the
    verdict ignores them, judged for Thursday -- where Monday to Wednesday are
    the past -- the same bars pass. So the fixture can flip the verdict, and
    for Monday it does not."""
    inputs = FakeInputs(bars=_flips_on_monday)
    directory_ = directory("XBACK")

    def judged(day: date) -> tuple[bool, tuple[str, ...], Decimal | None]:
        result = asyncio.run(
            refresh_tradeability(
                provider=inputs,
                assets=directory_,
                tickers=["XBACK"],
                session_date=day,
                session_factory=_sessions(db_engine),
                now=lambda: SATURDAY_10,
            )
        )
        (verdict,) = result.results
        return verdict.passes, tuple(f.value for f in verdict.failures), verdict.last_close

    assert judged(MONDAY) == (False, ("low_close",), Decimal("4"))
    assert judged(THURSDAY) == (True, (), Decimal("50"))
    assert [day for _, day in inputs.bar_calls] == [MONDAY, THURSDAY]


def test_the_backfilled_verdict_ignores_bars_on_or_after_the_labels_session(
    db_engine: Engine,
) -> None:
    label_id = _monday_label(db_engine)
    inputs = FakeInputs(bars=_flips_on_monday)
    _run(_deps(db_engine, inputs=inputs))

    assert inputs.bar_calls == [(("XBACK",), MONDAY)]
    assert inputs.root_calls == []  # failed on its close; no root question asked
    assert _stored(db_engine) == [("XBACK", MONDAY, False, "low_close", Decimal("4"))]
    # A verdict that says untradeable, so the label is written so.
    assert _outcomes_by_label(db_engine)[label_id] == [
        ("1d", "off_watch_untradeable", None),
        ("1h", "off_watch_untradeable", None),
    ]


# ---------------------------------------------------------------- backfill


def test_a_label_with_no_verdict_gets_one_backfilled_and_is_graded(db_engine: Engine) -> None:
    """Runner level: the backfilled verdict, the graded label and the stored row."""
    label_id = _monday_label(db_engine)
    inputs = FakeInputs()
    result = _run(_deps(db_engine, inputs=inputs))

    assert inputs.bar_calls == [(("XBACK",), MONDAY)]
    assert inputs.root_calls == ["XBACK"]
    assert _stored(db_engine) == [("XBACK", MONDAY, True, "", Decimal("50"))]
    assert _outcomes_by_label(db_engine)[label_id] == _GRADED
    backfill = result.detail["verdict_backfill"]
    assert isinstance(backfill, dict)
    assert backfill["needed"] == 1
    assert backfill["stored"] == 1
    assert backfill["faults"] == []
    assert result.detail["unverdicted_label_counts"] == {}


def test_an_existing_older_verdict_is_used_and_nothing_is_backfilled(db_engine: Engine) -> None:
    """Stale but existing: decision 25's to use, never replaced by a backfill."""
    label_id = _monday_label(db_engine, ticker="XOLD")
    with Session(db_engine) as session:
        _verdict(session, "XOLD", date(2026, 11, 10), True)
        session.commit()
    inputs = FakeInputs()
    _run(_deps(db_engine, inputs=inputs))

    assert inputs.bar_calls == [] and inputs.root_calls == []
    assert [(t, d) for t, d, *_ in _stored(db_engine)] == [("XOLD", date(2026, 11, 10))]
    assert _outcomes_by_label(db_engine)[label_id] == _GRADED


def test_a_backfill_failure_keeps_the_label_pending_and_records_the_fault(
    db_engine: Engine,
) -> None:
    label_id = _monday_label(db_engine)
    state_before = _engine_state(db_engine)
    inputs = FakeInputs(bar_error=ProviderError("GET /v2/stocks/bars failed: 503"))
    result = _run(_deps(db_engine, inputs=inputs))

    assert label_id not in _outcomes_by_label(db_engine)  # pending, never untradeable
    assert _stored(db_engine) == []
    backfill = result.detail["verdict_backfill"]
    assert isinstance(backfill, dict)
    assert backfill["stored"] == 0
    assert backfill["held"] == 1
    assert backfill["faults"] == [
        {"ticker": "XBACK", "session_date": "2026-11-16", "reason": "daily_bars:ProviderError"}
    ]
    # Rule 9: a context job's vendor failure is no producer of a halt.
    assert _engine_state(db_engine) == state_before


def test_no_asset_directory_is_a_fault_not_a_verdict(db_engine: Engine) -> None:
    label_id = _monday_label(db_engine)
    inputs = FakeInputs()
    deps = _deps(db_engine, inputs=inputs)
    assert deps.backfill is not None
    deps = SentimentAuditDeps(
        session_factory=deps.session_factory,
        bars=deps.bars,
        watch_universe=deps.watch_universe,
        emit=deps.emit,
        clock=deps.clock,
        backfill=VerdictBackfill(inputs=inputs, assets=lambda: None, ipo_dates=None),
    )
    result = _run(deps)

    assert label_id not in _outcomes_by_label(db_engine)
    assert inputs.bar_calls == []
    backfill = result.detail["verdict_backfill"]
    assert isinstance(backfill, dict)
    assert backfill["faults"] == [
        {"ticker": "XBACK", "session_date": "2026-11-16", "reason": "refresh_skipped"}
    ]


def test_the_cap_is_a_stated_number_beside_the_discovery_cap() -> None:
    assert VERDICT_BACKFILL_CAP == 100
    assert DISCOVERY_TICKER_CAP == 250


def test_the_cap_is_honoured_oldest_session_first_and_logged(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(audit_runner, "VERDICT_BACKFILL_CAP", 2)
    with Session(db_engine) as session:
        tuesday_label = _label(
            session, ticker="XA", source="rules", direction="bullish", at=MONDAY_10 + timedelta(days=1)
        )
        monday_b = _label(session, ticker="XB", source="rules", direction="bullish", at=MONDAY_10)
        monday_a = _label(session, ticker="XA", source="rules", direction="bullish", at=MONDAY_10)
        session.commit()
    inputs = FakeInputs()
    with caplog.at_level(logging.WARNING, logger="corollary.data.news.audit_runner"):
        result = _run(_deps(db_engine, inputs=inputs))

    # (session, ticker) order: Monday's XA and XB fit; Tuesday's XA is capped.
    assert inputs.bar_calls == [(("XA", "XB"), MONDAY)]
    outcomes = _outcomes_by_label(db_engine)
    assert outcomes[monday_a] == _GRADED and outcomes[monday_b] == _GRADED
    # Tuesday's XA label now reads Monday's backfilled verdict -- a verdict on
    # or before its session, which decision 25 uses (see audit.py's decision 27).
    assert outcomes[tuesday_label] == _GRADED
    backfill = result.detail["verdict_backfill"]
    assert isinstance(backfill, dict)
    assert (backfill["cap"], backfill["needed"], backfill["capped"]) == (2, 3, 1)
    capped = [r for r in caplog.records if getattr(r, "event", None) == "sentiment_audit_verdict_backfill_capped"]
    assert len(capped) == 1
    assert getattr(capped[0], "cap") == 2 and getattr(capped[0], "capped") == 1


def test_a_capped_pair_with_no_verdict_at_all_stays_pending(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit_runner, "VERDICT_BACKFILL_CAP", 1)
    with Session(db_engine) as session:
        first = _label(session, ticker="XA", source="rules", direction="bullish", at=MONDAY_10)
        second = _label(session, ticker="XB", source="rules", direction="bullish", at=MONDAY_10)
        session.commit()
    _run(_deps(db_engine, inputs=FakeInputs()))
    outcomes = _outcomes_by_label(db_engine)
    assert outcomes[first] == _GRADED
    assert second not in outcomes


# ------------------------------------------- past the wait, never "untradeable"


def _late(days: int) -> TickingClock:
    return TickingClock(SATURDAY_10 + timedelta(days=days))


_UNVERDICTED = [("1d", "off_watch_unverdicted", None), ("1h", "off_watch_unverdicted", None)]


def test_past_the_wait_with_no_backfill_configured_is_unverdicted(db_engine: Engine) -> None:
    """No verdict, none could be sought: the honest reason, never untradeable."""
    label_id = _monday_label(db_engine)
    _run(_deps(db_engine, inputs=None, clock=_late(7)))
    assert _outcomes_by_label(db_engine)[label_id] == _UNVERDICTED


def test_a_failing_backfill_holds_the_label_through_the_trailing_window(
    db_engine: Engine,
) -> None:
    """Decision 3's bound, for verdicts: a failure this run holds a label past
    the week's wait, for up to the trailing window, then releases it as
    unverdicted -- never as untradeable."""
    label_id = _monday_label(db_engine)
    failing = FakeInputs(bar_error=ProviderError("GET /v2/stocks/bars failed: 503"))
    _run(_deps(db_engine, inputs=failing, clock=_late(7)))
    assert label_id not in _outcomes_by_label(db_engine)

    # The 1d window ends Tuesday 10:00 ET; 28 days on, plus a run's slack.
    _run(_deps(db_engine, inputs=failing, clock=_late(25)))
    assert _outcomes_by_label(db_engine)[label_id] == _UNVERDICTED
    assert _stored(db_engine) == []


# ------------------------------------- an unreadable verdict is no verdict at all


FRIDAY = date(2026, 11, 13)


def _unreadable_verdict(session: Session, ticker: str, day: date) -> None:
    """A row every CHECK admits and ``CachedVerdict`` refuses -- a pass with no
    ADV or close: a damaged cache row, SYNTHETIC. Movers reads it as failing
    (decision 21, fail closed); the audit must not, since a failing verdict is
    what ``off_watch_untradeable`` asserts (decision 27)."""
    session.add(
        TickerTradeability(
            ticker=ticker,
            session_date=day,
            has_options=True,
            standard_root=True,
            avg_volume_20d=None,
            last_close=None,
            sessions_available=20,
            passes=True,
            failures="",
            checked_at=MONDAY_10,
        )
    )


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == event]


def test_an_unreadable_verdict_is_backfilled_never_graded_untradeable(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Monday's row is unreadable and Friday's, older, fails. The label reads
    neither: the newest verdict on or before its session is unknown, so it has
    none, and the backfill's upsert replaces the damaged row with Monday's."""
    label_id = _monday_label(db_engine)
    with Session(db_engine) as session:
        _verdict(session, "XBACK", FRIDAY, False)
        _unreadable_verdict(session, "XBACK", MONDAY)
        session.commit()
    inputs = FakeInputs()
    with caplog.at_level(logging.ERROR):
        result = _run(_deps(db_engine, inputs=inputs))

    assert inputs.bar_calls == [(("XBACK",), MONDAY)]
    assert _stored(db_engine) == [
        ("XBACK", FRIDAY, False, "no_options", None),
        ("XBACK", MONDAY, True, "", Decimal("50")),
    ]
    assert _outcomes_by_label(db_engine)[label_id] == _GRADED
    backfill = result.detail["verdict_backfill"]
    assert isinstance(backfill, dict)
    assert (backfill["needed"], backfill["stored"]) == (1, 1)
    (record,) = _events(caplog, "sentiment_audit_verdict_unreadable")
    assert (getattr(record, "ticker"), getattr(record, "session_date")) == ("XBACK", "2026-11-16")
    # Movers' log says "treated as failing"; nothing on the audit path says so.
    assert _events(caplog, "discovery_verdict_unreadable") == []


def test_an_unreadable_verdict_whose_backfill_fails_is_held_as_a_fault(
    db_engine: Engine,
) -> None:
    label_id = _monday_label(db_engine)
    with Session(db_engine) as session:
        _unreadable_verdict(session, "XBACK", MONDAY)
        session.commit()
    failing = FakeInputs(bar_error=ProviderError("GET /v2/stocks/bars failed: 503"))
    result = _run(_deps(db_engine, inputs=failing, clock=_late(7)))

    assert label_id not in _outcomes_by_label(db_engine)  # held, never untradeable
    backfill = result.detail["verdict_backfill"]
    assert isinstance(backfill, dict)
    assert backfill["held"] == 1
    assert backfill["faults"] == [
        {"ticker": "XBACK", "session_date": "2026-11-16", "reason": "daily_bars:ProviderError"}
    ]


def test_an_unreadable_verdict_with_no_backfill_waits_then_is_unverdicted(
    db_engine: Engine,
) -> None:
    label_id = _monday_label(db_engine)
    with Session(db_engine) as session:
        _unreadable_verdict(session, "XBACK", MONDAY)
        session.commit()
    _run(_deps(db_engine, inputs=None))
    assert label_id not in _outcomes_by_label(db_engine)  # pending inside the wait
    _run(_deps(db_engine, inputs=None, clock=_late(7)))
    assert _outcomes_by_label(db_engine)[label_id] == _UNVERDICTED


def test_the_plan_reads_an_unreadable_newest_verdict_as_none_not_an_older_one() -> None:
    """``None`` in a history is a row that could not be read: the label whose
    newest verdict it is has no verdict, rather than falling back to Friday's."""
    plan = plan_audit(
        [_label_at(1, "XA", MONDAY_10)],
        watch_universe=(),
        tradeable={"XA": [(FRIDAY, False), (MONDAY, None)]},
        written=(),
        claimed_groups=(),
        run_at=SATURDAY_10,
    )
    assert plan.verdicts_needed == (("XA", MONDAY),)
    assert plan.skipped == () and plan.to_grade == ()


# ------------------------------------------------------------- the pure plan


def _label_at(label_id: int, ticker: str, at: datetime) -> AuditLabel:
    return AuditLabel(
        label_id=label_id,
        ticker=ticker,
        source="rules",
        direction=Direction.BULLISH,
        published_at=at,
        labeled_at=at,
        group_id=label_id,
        group_pick=True,
    )


def test_the_plan_lists_the_verdicts_it_needs_oldest_session_first() -> None:
    plan = plan_audit(
        [_label_at(1, "XB", MONDAY_10 + timedelta(days=1)), _label_at(2, "XA", MONDAY_10)],
        watch_universe=(),
        tradeable={},
        written=(),
        claimed_groups=(),
        run_at=SATURDAY_10,
    )
    assert plan.verdicts_needed == (("XA", MONDAY), ("XB", TUESDAY))
    assert plan.skipped == ()


def test_the_plan_never_writes_untradeable_without_a_verdict() -> None:
    last_end = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)  # XB's 1d window ends Wednesday 10:00 ET
    just_past = last_end + PRICE_UNAVAILABLE_AFTER + timedelta(seconds=1)
    plan = plan_audit(
        [_label_at(2, "XB", MONDAY_10 + timedelta(days=1))],
        watch_universe=(),
        tradeable={},
        written=(),
        claimed_groups=(),
        run_at=just_past,
    )
    assert {(r.label_id, r.window, r.skip_reason) for r in plan.skipped} == {
        (2, "1h", SkipReason.OFF_WATCH_UNVERDICTED),
        (2, "1d", SkipReason.OFF_WATCH_UNVERDICTED),
    }
    held = plan_audit(
        [_label_at(2, "XB", MONDAY_10 + timedelta(days=1))],
        watch_universe=(),
        tradeable={},
        written=(),
        claimed_groups=(),
        run_at=just_past,
        verdict_held=frozenset({("XB", TUESDAY)}),
    )
    assert held.skipped == ()
    # Held only through the trailing window, measured like decision 3's.
    released = plan_audit(
        [_label_at(2, "XB", MONDAY_10 + timedelta(days=1))],
        watch_universe=(),
        tradeable={},
        written=(),
        claimed_groups=(),
        run_at=last_end + TRAILING_WINDOW + timedelta(seconds=1),
        verdict_held=frozenset({("XB", TUESDAY)}),
    )
    assert {r.skip_reason for r in released.skipped} == {SkipReason.OFF_WATCH_UNVERDICTED}
    at_bound = plan_audit(
        [_label_at(2, "XB", MONDAY_10 + timedelta(days=1))],
        watch_universe=(),
        tradeable={},
        written=(),
        claimed_groups=(),
        run_at=last_end + TRAILING_WINDOW,
        verdict_held=frozenset({("XB", TUESDAY)}),
    )
    assert at_bound.skipped == ()
