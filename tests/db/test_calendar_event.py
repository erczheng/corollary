"""``calendar_event`` (migration 0013) against the models and the migration.

The CHECKs are tested against **both** engines, because Alembic's
autogenerate comparison does not compare CHECK constraint text (see
``test_news_watchlist_tradeability.py``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corollary.data.calendar_event import (
    KIND_SOURCE,
    CalendarKind,
    CalendarSource,
    EarningsSession,
)
from corollary.db.models import (
    CALENDAR_EVENT_KINDS,
    CALENDAR_EVENT_SOURCES,
    EARNINGS_SESSIONS,
    Base,
    CalendarEvent,
)
from corollary.db.session import create_db_engine, sqlite_url
from corollary.db.types import MoneyComparisonError

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
T0 = datetime(2026, 10, 10, 14, 0, tzinfo=UTC)


@pytest.fixture(params=["models", "migration"])
def any_engine(request: pytest.FixtureRequest, db_path: Path) -> Iterator[Engine]:
    url = sqlite_url(db_path)
    if request.param == "migration":
        cfg = Config(str(ALEMBIC_INI))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        eng = create_db_engine(url)
    else:
        eng = create_db_engine(url)
        Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _row(**overrides: Any) -> CalendarEvent:
    values: dict[str, Any] = {
        "kind": "earnings",
        "source": "finnhub",
        "title": "NVDA earnings",
        "ticker": "NVDA",
        "date": date(2026, 11, 19),
        "at": None,
        "estimate": Decimal("1.25"),
        "prior": None,
        "actual": None,
        "unit": "USD",
        "vendor_id": "NVDA:2026Q3",
        "created_at": T0,
        "updated_at": T0,
        "deleted_at": None,
    }
    values.update(overrides)
    return CalendarEvent(**values)


def test_the_check_sets_match_the_enums() -> None:
    assert CALENDAR_EVENT_KINDS == (
        "earnings",
        "economic",
        "central-bank",
        "dividend",
        "geopolitical",
        "ipo",
    )
    assert CALENDAR_EVENT_SOURCES == ("finnhub", "alpaca", "fred", "seed", "manual")
    assert set(KIND_SOURCE) == set(CalendarKind)
    assert set(KIND_SOURCE.values()) == set(CalendarSource)
    # Finnhub's earnings ``hour`` spellings (decision 7). The migration
    # restates them; the both-engines tests below pin that copy.
    assert EARNINGS_SESSIONS == ("bmo", "amc", "dmh")
    assert EARNINGS_SESSIONS == tuple(s.value for s in EarningsSession)


def test_the_kinds_carry_the_frontends_spellings() -> None:
    types_ts = (Path(__file__).resolve().parents[2] / "web/src/lib/types.ts").read_text(
        encoding="utf-8"
    )
    line = next(l for l in types_ts.splitlines() if l.startswith("export type CalendarEventType"))
    frontend = {part.strip().strip("'") for part in line.split("=", 1)[1].split("|")}
    # Every frontend kind is a stored kind; ``ipo`` is the one the panel gains (Q15).
    assert frontend <= set(CALENDAR_EVENT_KINDS)
    assert set(CALENDAR_EVENT_KINDS) - frontend == {"ipo"}


def test_a_good_row_round_trips(any_engine: Engine) -> None:
    with Session(any_engine) as session:
        session.add(_row())
        session.commit()
    with Session(any_engine) as session:
        row = session.scalars(select(CalendarEvent)).one()
    assert row.date == date(2026, 11, 19)
    assert row.at is None
    assert type(row.estimate) is Decimal and str(row.estimate) == "1.25"
    assert row.created_at == T0


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"kind": "earning"}, id="unknown kind"),
        pytest.param({"kind": "central_bank"}, id="kind spelled with an underscore"),
        pytest.param({"source": "benzinga"}, id="unknown source"),
        pytest.param({"source": "alpaca"}, id="earnings from alpaca: pairing"),
        pytest.param(
            {"kind": "geopolitical", "source": "finnhub", "ticker": None, "estimate": None},
            id="geopolitical from a vendor: pairing",
        ),
        pytest.param(
            {"kind": "earnings", "source": "manual", "vendor_id": None},
            id="manual earnings: pairing",
        ),
        pytest.param({"vendor_id": None}, id="vendor row with no key"),
        pytest.param({"vendor_id": ""}, id="vendor row with a blank key"),
        pytest.param(
            {
                "kind": "geopolitical",
                "source": "manual",
                "ticker": None,
                "estimate": None,
                "vendor_id": "x",
            },
            id="manual row with a vendor key",
        ),
        pytest.param({"title": ""}, id="blank title"),
        pytest.param({"ticker": "nvda"}, id="lowercase ticker"),
        pytest.param({"ticker": None}, id="earnings without a ticker"),
        pytest.param(
            {"kind": "dividend", "source": "alpaca", "ticker": None, "estimate": None},
            id="dividend without a ticker",
        ),
        pytest.param(
            {"kind": "economic", "source": "fred", "ticker": None, "estimate": Decimal("3.1")},
            id="economic estimate (Q3: consensus unavailable)",
        ),
        pytest.param({"date": None}, id="no date"),
        pytest.param({"session": "pre"}, id="unknown earnings session"),
        pytest.param({"session": "BMO"}, id="earnings session in upper case"),
        pytest.param({"session": ""}, id="blank earnings session"),
        pytest.param(
            {
                "kind": "dividend",
                "source": "alpaca",
                "estimate": None,
                "vendor_id": "ca-1",
                "session": "bmo",
            },
            id="session on a dividend",
        ),
        pytest.param(
            {
                "kind": "ipo",
                "source": "finnhub",
                "estimate": None,
                "vendor_id": "ipo-1",
                "session": "amc",
            },
            id="session on an ipo",
        ),
        pytest.param({"created_at": None}, id="no created_at"),
    ],
)
def test_a_bad_row_is_refused(any_engine: Engine, overrides: dict[str, Any]) -> None:
    with Session(any_engine) as session:
        session.add(_row(**overrides))
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize("stored", [None, "bmo", "amc", "dmh"])
def test_an_earnings_row_carries_its_session(any_engine: Engine, stored: str | None) -> None:
    # Decision 7: "Before open" / "After close" -- a session, never a
    # placeholder instant, so ``at`` stays NULL beside it.
    with Session(any_engine) as session:
        session.add(_row(session=stored))
        session.commit()
    with any_engine.connect() as conn:
        assert conn.execute(text("SELECT session, at FROM calendar_event")).one() == (
            stored,
            None,
        )


def test_a_session_cannot_be_written_onto_a_non_earnings_row(any_engine: Engine) -> None:
    # The CHECK holds on UPDATE too, not only on the INSERT the model builds.
    with Session(any_engine) as session:
        session.add(_row(kind="dividend", source="alpaca", estimate=None, vendor_id="ca-1"))
        session.commit()
    with any_engine.connect() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(text("UPDATE calendar_event SET session = 'bmo'"))


@pytest.mark.parametrize("column", ["estimate", "prior", "actual"])
@pytest.mark.parametrize("stored", ["NaN", "1E+2", "abc", "7."])
def test_a_money_column_refuses_non_decimal_text(
    any_engine: Engine, column: str, stored: str
) -> None:
    with Session(any_engine) as session:
        session.add(_row())
        session.commit()
    with any_engine.connect() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(text(f"UPDATE calendar_event SET {column} = :v"), {"v": stored})


def test_one_row_per_vendor_key(any_engine: Engine) -> None:
    with Session(any_engine) as session:
        session.add(_row())
        session.commit()
    with Session(any_engine) as session:
        session.add(_row(title="NVDA earnings again"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_the_same_vendor_id_under_another_kind_is_another_row(any_engine: Engine) -> None:
    # Finnhub's earnings and IPO calendars are separate endpoints; a key one
    # synthesizes must not collide with the other's.
    with Session(any_engine) as session:
        session.add(_row(vendor_id="NEWCO:2026-11-19"))
        session.add(_row(kind="ipo", ticker="NEWCO", estimate=None, vendor_id="NEWCO:2026-11-19"))
        session.commit()
        assert session.scalar(select(func.count()).select_from(CalendarEvent)) == 2


def test_manual_rows_without_a_key_do_not_collide(any_engine: Engine) -> None:
    manual = {
        "kind": "geopolitical",
        "source": "manual",
        "ticker": None,
        "estimate": None,
        "unit": None,
        "vendor_id": None,
    }
    with Session(any_engine) as session:
        session.add(_row(title="Summit", **manual))
        session.add(_row(title="Summit", **manual))
        session.commit()
        assert session.scalar(select(func.count()).select_from(CalendarEvent)) == 2


def test_a_date_only_row_stores_no_instant(any_engine: Engine) -> None:
    with Session(any_engine) as session:
        session.add(_row(kind="dividend", source="alpaca", estimate=None, vendor_id="ca-1"))
        session.commit()
    with any_engine.connect() as conn:
        assert conn.execute(text("SELECT at, date FROM calendar_event")).one() == (
            None,
            "2026-11-19",
        )


@pytest.mark.parametrize("column", ["estimate", "prior", "actual"])
def test_sql_comparison_of_a_money_column_is_refused(column: str) -> None:
    col = getattr(CalendarEvent, column)
    with pytest.raises(MoneyComparisonError):
        select(CalendarEvent).where(col > Decimal("1"))
    with pytest.raises(MoneyComparisonError):
        select(CalendarEvent).where(col == Decimal("1"))


def test_ordering_or_aggregating_a_money_column_is_refused(any_engine: Engine) -> None:
    with Session(any_engine) as session:
        session.add(_row())
        session.commit()
        with pytest.raises(MoneyComparisonError):
            session.execute(select(CalendarEvent).order_by(CalendarEvent.actual))
        with pytest.raises(MoneyComparisonError):
            session.execute(select(func.max(CalendarEvent.estimate)))
