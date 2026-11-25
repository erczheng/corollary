"""The composite component store (step 8, U3): the recheck window, the load, the newest dates.

Spec ``2026-11-17-phase-3-step-8-composite.md``, C1 and unit U3. The recheck
window is counted on the NYSE calendar, so most dates here sit around
Thanksgiving 2026 (Thursday Nov 26, closed): the six sessions ending Monday
Nov 30 are Nov 20, 23, 24, 25, 27 and 30, so the floor five sessions before
Nov 30 is **Nov 20**. A count in calendar days would put it at Nov 25.
"""

import ast
import logging
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from corollary.calendars import nyse_sessions_between
from corollary.data.macro import store
from corollary.data.macro.components import ComponentKey
from corollary.data.macro.composite import ComponentRow
from corollary.data.macro.store import (
    RECHECK_SESSIONS,
    RECHECK_WINDOW_RULE,
    RecheckWindowRefused,
    load_window,
    newest_dates,
    upsert_values,
)
from corollary.db.models import Base, CompositeComponentValue
from corollary.db.session import create_db_engine, sqlite_url

AT = datetime(2026, 11, 30, 22, 0, tzinfo=UTC)
LATER = AT + timedelta(days=1)
NEWEST = date(2026, 11, 30)
FLOOR = date(2026, 11, 20)
BEFORE_FLOOR = date(2026, 11, 19)
SEED_DAY = date(2026, 9, 30)

M = ComponentKey.MOMENTUM
PC = ComponentKey.PUT_CALL
BR = ComponentKey.BREADTH


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_db_engine(sqlite_url(tmp_path / "corollary.db"))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _row(
    component: ComponentKey,
    session: date,
    value: str = "0.1000000000",
    *,
    at: datetime = AT,
    source: str = "alpaca_daily:sip",
    members: int | None = None,
) -> ComponentRow:
    universe = SEED_DAY if component in (ComponentKey.STRENGTH, ComponentKey.BREADTH) else None
    return ComponentRow(
        component=component,
        session=session,
        raw_value=Decimal(value),
        source=source,
        members=members,
        universe_as_of=universe,
        computed_at=at,
    )


def _sessions(start: date, end: date) -> tuple[date, ...]:
    return nyse_sessions_between(start, end)


def _write(engine: Engine, rows: Sequence[ComponentRow]) -> int:
    with Session(engine) as sess:
        written = upsert_values(sess, rows, newest_stored=newest_dates(sess))
        sess.commit()
    return written


def _stored(engine: Engine) -> dict[tuple[date, str], tuple[str, datetime]]:
    """Every row as (date, component) -> (raw text, computed_at), read as SQL text."""
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT date, component, raw_value, computed_at FROM composite_component_value")
        ).all()
    return {(date.fromisoformat(d), c): (raw, at) for d, c, raw, at in found}


def _history(engine: Engine, component: ComponentKey = M) -> tuple[date, ...]:
    """Store one row per session, Nov 2 .. Nov 30, for ``component``; return the dates."""
    days = _sessions(date(2026, 11, 2), NEWEST)
    assert _write(engine, [_row(component, d) for d in days]) == len(days)
    return days


def test_the_floor_is_counted_on_the_calendar() -> None:
    # The hand-computed dates the rest of this file relies on.
    assert RECHECK_SESSIONS == 5
    assert _sessions(FLOOR, NEWEST) == (
        date(2026, 11, 20),
        date(2026, 11, 23),
        date(2026, 11, 24),
        date(2026, 11, 25),
        date(2026, 11, 27),
        date(2026, 11, 30),
    )
    assert _sessions(BEFORE_FLOOR, FLOOR) == (BEFORE_FLOOR, FLOOR)


# --- The empty table and the history load ---------------------------------------------


def test_an_empty_table_accepts_the_whole_history(engine: Engine) -> None:
    days = _sessions(date(2024, 2, 1), NEWEST)
    rows = [_row(M, d) for d in days] + [_row(BR, d, members=497) for d in days]
    # More rows than one INSERT carries, so the chunking is exercised.
    assert len(rows) > 1000
    assert _write(engine, rows) == len(rows)
    assert len(_stored(engine)) == len(rows)


def test_a_component_with_no_rows_has_no_floor_even_when_others_do(engine: Engine) -> None:
    _history(engine, M)
    # put_call has never been written, so its whole history is accepted,
    # momentum's floor notwithstanding.
    old = date(2026, 3, 2)
    assert _write(engine, [_row(PC, old, members=4000)]) == 1
    assert (old, "put_call") in _stored(engine)


# --- The recheck window (C1) ----------------------------------------------------------


def test_rows_inside_the_window_are_rewritten_and_older_ones_are_not(engine: Engine) -> None:
    days = _history(engine, M)
    before = _stored(engine)
    window = _sessions(FLOOR, date(2026, 12, 1))
    assert _write(engine, [_row(M, d, "0.2000000000", at=LATER) for d in window]) == len(window)
    after = _stored(engine)
    for d in days:
        if d >= FLOOR:
            assert after[(d, "momentum")][0] == "0.2000000000", d
        else:
            assert after[(d, "momentum")] == before[(d, "momentum")], d
    assert after[(date(2026, 12, 1), "momentum")][0] == "0.2000000000"


def test_the_boundary_session_can_be_rewritten(engine: Engine) -> None:
    _history(engine, M)
    assert _write(engine, [_row(M, FLOOR, "-0.3000000000", at=LATER)]) == 1
    assert _stored(engine)[(FLOOR, "momentum")][0] == "-0.3000000000"


def test_the_session_before_the_boundary_is_refused(engine: Engine) -> None:
    _history(engine, M)
    before = _stored(engine)
    with pytest.raises(RecheckWindowRefused):
        _write(engine, [_row(M, BEFORE_FLOOR, "-0.3000000000", at=LATER)])
    assert _stored(engine) == before


def test_newer_inputs_never_reach_a_frozen_row_and_the_batch_writes_nothing(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    _history(engine, M)
    before = _stored(engine)
    batch = [
        _row(M, date(2026, 12, 1), "0.9000000000", at=LATER),
        _row(M, NEWEST, "0.9000000000", at=LATER),
        _row(M, BEFORE_FLOOR, "0.9000000000", at=LATER),
        _row(M, date(2026, 11, 2), "0.9000000000", at=LATER),
    ]
    with caplog.at_level(logging.WARNING, logger=store.__name__):
        with pytest.raises(RecheckWindowRefused) as refused:
            _write(engine, batch)
    # All or nothing: neither the new session nor the in-window rewrite landed.
    assert _stored(engine) == before
    assert {(r.component, r.session) for r in refused.value.refused} == {
        (M, BEFORE_FLOOR),
        (M, date(2026, 11, 2)),
    }
    records = [r for r in caplog.records if getattr(r, "rule", None) == RECHECK_WINDOW_RULE]
    assert RECHECK_WINDOW_RULE == "composite_recheck_window"
    assert len(records) == 2
    for record in records:
        assert record.levelno == logging.WARNING
        assert getattr(record, "component") == "momentum"
        assert getattr(record, "table_newest") == NEWEST.isoformat()
        assert getattr(record, "newest_stored") == NEWEST.isoformat()
        assert getattr(record, "floor") == FLOOR.isoformat()
    assert {getattr(r, "date") for r in records} == {
        BEFORE_FLOOR.isoformat(),
        date(2026, 11, 2).isoformat(),
    }


def test_the_floor_is_anchored_on_the_newest_before_the_batch(engine: Engine) -> None:
    # A batch that carries newer sessions does not move its own floor: the
    # window is five sessions before what was stored, not before the batch.
    _history(engine, M)
    batch = [_row(M, d, "0.5000000000", at=LATER) for d in _sessions(FLOOR, date(2026, 12, 9))]
    assert _write(engine, batch) == len(batch)


@pytest.mark.parametrize(
    "newest_stored",
    [
        pytest.param({}, id="missing"),
        pytest.param({M: date(2026, 11, 2)}, id="stale"),
        pytest.param({M: date(2024, 2, 1)}, id="ancient"),
    ],
)
def test_a_missing_or_stale_newest_stored_cannot_widen_the_window(
    engine: Engine,
    newest_stored: dict[ComponentKey, date],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _history(engine, M)
    before = _stored(engine)
    with caplog.at_level(logging.WARNING, logger=store.__name__):
        with Session(engine) as sess:
            with pytest.raises(RecheckWindowRefused):
                upsert_values(
                    sess, [_row(M, BEFORE_FLOOR, at=LATER)], newest_stored=newest_stored
                )
            sess.commit()
    assert _stored(engine) == before
    (record,) = [r for r in caplog.records if getattr(r, "rule", None) == RECHECK_WINDOW_RULE]
    assert getattr(record, "table_newest") == NEWEST.isoformat()
    assert getattr(record, "floor") == FLOOR.isoformat()
    expected = newest_stored.get(M)
    assert getattr(record, "newest_stored") == (expected.isoformat() if expected else None)


def test_a_later_newest_stored_narrows_the_window(engine: Engine) -> None:
    _history(engine, M)
    # Anchored on Dec 8 (the later of the two), the floor is Dec 1, so Nov 30
    # -- inside the table's own window -- is refused.
    with Session(engine) as sess:
        with pytest.raises(RecheckWindowRefused) as refused:
            upsert_values(sess, [_row(M, NEWEST, at=LATER)], newest_stored={M: date(2026, 12, 8)})
    (only,) = refused.value.refused
    assert only.floor == date(2026, 12, 1)


def test_newest_stored_on_an_empty_component_still_bounds_it(engine: Engine) -> None:
    with Session(engine) as sess:
        with pytest.raises(RecheckWindowRefused):
            upsert_values(sess, [_row(PC, BEFORE_FLOOR, members=10)], newest_stored={PC: NEWEST})
        assert upsert_values(sess, [_row(PC, FLOOR, members=10)], newest_stored={PC: NEWEST}) == 1


def test_a_rerun_of_the_same_batch_is_idempotent(engine: Engine) -> None:
    days = _history(engine, M)
    first = _stored(engine)
    batch = [_row(M, d) for d in _sessions(FLOOR, NEWEST)]
    assert _write(engine, batch) == len(batch)
    assert _write(engine, batch) == len(batch)
    assert _stored(engine) == first
    assert len(first) == len(days)


# --- What is refused before anything is written ---------------------------------------


def test_an_unquantized_value_is_refused_and_nothing_is_written(engine: Engine) -> None:
    with Session(engine) as sess:
        with pytest.raises(ValueError, match="RAW_QUANTUM"):
            upsert_values(
                sess,
                [_row(M, FLOOR), _row(M, NEWEST, "0.12345678901")],
                newest_stored={},
            )
        sess.commit()
    assert _stored(engine) == {}


def test_a_duplicate_in_one_batch_is_refused_and_nothing_is_written(engine: Engine) -> None:
    with Session(engine) as sess:
        with pytest.raises(ValueError, match="duplicate"):
            upsert_values(
                sess,
                [_row(M, NEWEST), _row(PC, NEWEST, members=1), _row(M, NEWEST, "0.2")],
                newest_stored={},
            )
        sess.commit()
    assert _stored(engine) == {}


def test_a_value_not_yet_at_ten_places_is_stored_in_its_quantized_text(engine: Engine) -> None:
    assert _write(engine, [_row(M, NEWEST, "0.5"), _row(PC, NEWEST, "-0", members=0)]) == 2
    stored = _stored(engine)
    assert stored[(NEWEST, "momentum")][0] == "0.5000000000"
    # Negative zero is stored as zero, so the text is one string per value.
    assert stored[(NEWEST, "put_call")][0] == "0.0000000000"


def test_a_date_that_is_not_an_nyse_session_is_refused(engine: Engine) -> None:
    with Session(engine) as sess:
        with pytest.raises(ValueError, match="not an NYSE session"):
            upsert_values(sess, [_row(M, date(2026, 11, 26))], newest_stored={})
    assert _stored(engine) == {}


@pytest.mark.parametrize(
    "component, universe",
    [(ComponentKey.STRENGTH, None), (BR, None), (M, SEED_DAY), (PC, SEED_DAY)],
)
def test_universe_as_of_is_set_exactly_for_strength_and_breadth(
    engine: Engine, component: ComponentKey, universe: date | None
) -> None:
    row = ComponentRow(
        component=component,
        session=NEWEST,
        raw_value=Decimal("0.1"),
        source="alpaca_daily:sip",
        members=1,
        universe_as_of=universe,
        computed_at=AT,
    )
    with Session(engine) as sess:
        with pytest.raises(ValueError, match="universe_as_of"):
            upsert_values(sess, [row], newest_stored={})
    assert _stored(engine) == {}


def test_a_blank_source_is_refused(engine: Engine) -> None:
    with Session(engine) as sess:
        with pytest.raises(ValueError, match="source"):
            upsert_values(sess, [_row(M, NEWEST, source="   ")], newest_stored={})
    assert _stored(engine) == {}


def test_a_row_that_is_not_a_component_row_is_refused(engine: Engine) -> None:
    with Session(engine) as sess:
        with pytest.raises(TypeError):
            upsert_values(sess, [object()], newest_stored={})  # type: ignore[list-item]


# --- Exact Decimal ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["-0.0123456789", "1234.5678901234", "-98765.4321098765", "0.0000000001", "0E-10"],
)
def test_decimal_round_trips_exactly(engine: Engine, value: str) -> None:
    _write(engine, [_row(M, NEWEST, value, members=None)])
    with Session(engine) as sess:
        (row,) = load_window(sess, [M], loaded_from=NEWEST)
    assert type(row.raw_value) is Decimal
    assert row.raw_value == Decimal(value)
    assert format(row.raw_value, "f") == format(Decimal(value), "f")
    with engine.connect() as conn:
        typ, raw = conn.execute(
            text("SELECT typeof(raw_value), raw_value FROM composite_component_value")
        ).one()
    assert typ == "text" and raw == format(Decimal(value), "f")


# --- load_window ------------------------------------------------------------------------


def test_load_window_returns_every_row_on_or_after_loaded_from_and_none_before(
    engine: Engine,
) -> None:
    days = _sessions(date(2026, 11, 2), date(2026, 12, 4))
    _write(engine, [_row(M, d) for d in days])
    _write(engine, [_row(BR, d, members=500) for d in days])
    _write(engine, [_row(PC, d, members=9) for d in days])
    loaded_from = date(2026, 11, 20)
    with Session(engine) as sess:
        rows = load_window(sess, [M, BR], loaded_from=loaded_from)
    expected = [d for d in days if d >= loaded_from]
    # Every row, including those after any as-of the caller expects: no end cap.
    assert [(r.session, r.component) for r in rows] == [
        (d, k) for d in expected for k in sorted([M, BR], key=lambda k: k.value)
    ]
    assert all(type(r.component) is ComponentKey for r in rows)
    breadth = next(r for r in rows if r.component is BR)
    assert breadth.members == 500 and breadth.universe_as_of == SEED_DAY
    assert breadth.computed_at == AT and breadth.source == "alpaca_daily:sip"


def test_load_window_on_a_non_session_bound_and_an_empty_component(engine: Engine) -> None:
    _history(engine, M)
    with Session(engine) as sess:
        # Thanksgiving bounds the load without being a row.
        rows = load_window(sess, [M], loaded_from=date(2026, 11, 26))
        assert [r.session for r in rows] == [date(2026, 11, 27), NEWEST]
        assert load_window(sess, [PC], loaded_from=date(2024, 2, 1)) == []
        assert load_window(sess, [], loaded_from=date(2024, 2, 1)) == []


# --- newest_dates ------------------------------------------------------------------------


def test_newest_dates_per_component(engine: Engine) -> None:
    with Session(engine) as sess:
        assert newest_dates(sess) == {}
    _write(engine, [_row(M, d) for d in _sessions(date(2026, 11, 2), NEWEST)])
    _write(engine, [_row(PC, d, members=3) for d in _sessions(date(2026, 11, 2), FLOOR)])
    with Session(engine) as sess:
        newest = newest_dates(sess)
    assert newest == {M: NEWEST, PC: FLOOR}
    assert all(type(k) is ComponentKey for k in newest)


# --- Rule 9: what this module may import -----------------------------------------------


_ALLOWED_PREFIXES = ("corollary.db.",)
_ALLOWED = {
    "corollary.db",
    "corollary.calendars",
    "corollary.data.macro.components",
    "corollary.data.macro.composite",
    "sqlalchemy",
}


def test_the_module_imports_only_db_calendars_and_its_pure_siblings() -> None:
    source = Path(store.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module is not None
            imported.add(node.module)
    outside = {
        name
        for name in imported
        if name.split(".")[0] not in sys.stdlib_module_names
        and name not in _ALLOWED
        and not name.startswith(_ALLOWED_PREFIXES)
        and not name.startswith("sqlalchemy.")
    }
    assert outside == set()
    assert any(name.startswith("corollary.db") for name in imported)


def test_raw_value_is_a_money_column() -> None:
    column: Any = CompositeComponentValue.__table__.c.raw_value
    assert type(column.type).__name__ == "Money"
