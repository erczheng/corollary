"""Owner decision Q12's ``ticker_ipo_date`` (migration 0009), against the models and the migration.

The CHECK is tested against **both** engines, because Alembic's autogenerate
comparison does not compare CHECK constraint text (see
``test_news_watchlist_tradeability.py``).
"""

from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corollary.db.models import Base, TickerIpoDate
from corollary.db.session import create_db_engine, sqlite_url

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
T0 = datetime(2026, 9, 26, 14, 30, tzinfo=timezone.utc)


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


def test_a_date_round_trips_with_a_utc_fetched_at(any_engine: Engine) -> None:
    with Session(any_engine) as session:
        session.add(TickerIpoDate(ticker="NEWCO", ipo_date=date(2026, 9, 15), fetched_at=T0))
        session.commit()
    with Session(any_engine) as session:
        row = session.scalars(select(TickerIpoDate)).one()
    assert row.ticker == "NEWCO"
    assert row.ipo_date == date(2026, 9, 15)
    assert row.fetched_at == T0
    assert row.fetched_at.tzinfo is not None


@pytest.mark.parametrize("ticker", ["", "newco", "NewCo"])
def test_a_blank_or_lowercase_ticker_is_refused(any_engine: Engine, ticker: str) -> None:
    with Session(any_engine) as session:
        session.add(TickerIpoDate(ticker=ticker, ipo_date=date(2026, 9, 15), fetched_at=T0))
        with pytest.raises(IntegrityError):
            session.commit()


def test_no_date_is_never_stored(any_engine: Engine) -> None:
    """Only a parsed date is an answer worth keeping forever; NULL is refused."""
    with Session(any_engine) as session:
        session.add(TickerIpoDate(ticker="NEWCO", ipo_date=None, fetched_at=T0))
        with pytest.raises(IntegrityError):
            session.commit()


def test_one_row_per_ticker(any_engine: Engine) -> None:
    with Session(any_engine) as session:
        session.add(TickerIpoDate(ticker="NEWCO", ipo_date=date(2026, 9, 15), fetched_at=T0))
        session.commit()
    with Session(any_engine) as session:
        session.add(TickerIpoDate(ticker="NEWCO", ipo_date=date(2026, 9, 16), fetched_at=T0))
        with pytest.raises(IntegrityError):
            session.commit()
