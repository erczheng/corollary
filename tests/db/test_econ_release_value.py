"""``econ_release_value`` and ``econ_release_fetch`` (U6d, spec R6): migration and models.

Autogenerate does not compare CHECK constraints, so every CHECK case below
runs against both builds -- ``alembic upgrade head`` and ``create_all()`` --
and a CHECK one side has and the other lacks fails here. Column parity is
``tests/db/test_migrations.py::test_upgrade_head_matches_the_models``.
"""

from collections.abc import Iterator
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from corollary.data.econ_values import ATTEMPT_DETAIL_MAX, DETAILED_OUTCOMES, FETCH_OUTCOMES
from corollary.db.models import (
    ECON_FETCH_DETAIL_MAX,
    ECON_FETCH_OUTCOMES,
    Base,
    EconReleaseFetch,
    EconReleaseValue,
)
from corollary.db.session import create_db_engine, sqlite_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
AT = datetime(2026, 10, 14, 12, 40, tzinfo=timezone.utc)
CPI_DAY = date(2026, 10, 14)
TABLES = {"econ_release_value", "econ_release_fetch"}


def _config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(params=["models", "migration"])
def any_engine(request: pytest.FixtureRequest, db_path: Path) -> Iterator[Engine]:
    url = sqlite_url(db_path)
    if request.param == "migration":
        command.upgrade(_config(url), "head")
        eng = create_db_engine(url)
    else:
        eng = create_db_engine(url)
        Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _value(**overrides: Any) -> EconReleaseValue:
    fields: dict[str, Any] = {
        "release_id": 10,
        "release_date": CPI_DAY,
        "series_id": "CPIAUCSL",
        "period": date(2026, 9, 1),
        "actual": Decimal("0.6485"),
        "prior": Decimal("0.3960"),
        "unit_label": "% m/m",
        "vintage_date": CPI_DAY,
        "fetched_at": AT,
    }
    fields.update(overrides)
    return EconReleaseValue(**fields)


def _fetch(**overrides: Any) -> EconReleaseFetch:
    fields: dict[str, Any] = {
        "release_id": 10,
        "release_date": CPI_DAY,
        "attempted_at": AT,
        "outcome": "not_posted",
        "detail": None,
    }
    fields.update(overrides)
    return EconReleaseFetch(**fields)


def _add(eng: Engine, *rows: object) -> None:
    with Session(eng) as sess:
        sess.add_all(rows)
        sess.commit()


def test_0017_revises_0016() -> None:
    revision = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_revision("0017")
    assert revision is not None
    assert revision.down_revision == "0016"


def test_the_outcome_lists_agree() -> None:
    # The models carry a copy so they need not import the FRED provider.
    assert ECON_FETCH_OUTCOMES == FETCH_OUTCOMES
    assert ECON_FETCH_DETAIL_MAX == ATTEMPT_DETAIL_MAX
    assert set(DETAILED_OUTCOMES) == {"no_value", "refused", "error"}


@pytest.mark.parametrize("actual", ["-105.0470", "0.6485", "0.0000", "339000.0000"])
def test_money_round_trips_exactly_as_text(any_engine: Engine, actual: str) -> None:
    _add(any_engine, _value(actual=Decimal(actual), prior=Decimal("-78.3120")))
    with Session(any_engine) as sess:
        row = sess.scalars(select(EconReleaseValue)).one()
        assert type(row.actual) is Decimal and type(row.prior) is Decimal
        assert str(row.actual) == actual
        assert str(row.prior) == "-78.3120"
        raw = sess.execute(
            text("SELECT typeof(actual), actual, typeof(prior), prior FROM econ_release_value")
        ).one()
    assert tuple(raw) == ("text", actual, "text", "-78.3120")


@pytest.mark.parametrize("column", ["actual", "prior"])
@pytest.mark.parametrize("raw", ["NaN", "1E+2", "", "abc", "1.", "--1", "1.5.0"])
def test_the_money_columns_refuse_what_is_not_a_plain_decimal_text(
    any_engine: Engine, column: str, raw: object
) -> None:
    _add(any_engine, _value())
    with Session(any_engine) as sess, pytest.raises(IntegrityError):
        sess.execute(text(f"UPDATE econ_release_value SET {column} = :raw"), {"raw": raw})


@pytest.mark.parametrize("column", ["actual", "prior"])
def test_a_float_never_reaches_the_money_columns(any_engine: Engine, column: str) -> None:
    # A raw numeric bind is coerced to text by the column's TEXT affinity
    # before the CHECK runs, so the float guard is Money's bind, not SQL.
    with pytest.raises(StatementError, match="money must be a Decimal"):
        _add(any_engine, _value(**{column: 0.6485}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"vintage_date": date(2026, 10, 13)},  # read from another day's vintage
        {"release_id": 0},
        {"series_id": "  "},
        {"unit_label": ""},
    ],
)
def test_the_value_row_checks(any_engine: Engine, overrides: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError):
        _add(any_engine, _value(**overrides))


def test_one_value_row_per_release_and_date(any_engine: Engine) -> None:
    _add(any_engine, _value())
    _add(any_engine, _value(release_date=date(2026, 11, 13), vintage_date=date(2026, 11, 13)))
    with pytest.raises(IntegrityError):
        _add(any_engine, _value(actual=Decimal("9.9")))


@pytest.mark.parametrize(
    ("outcome", "detail"),
    [
        ("figure", None),
        ("not_posted", None),
        ("no_value", "x(t-1) (2026-08-01) is FRED's missing-observation marker '.'"),
        ("refused", "Bad Request. The series does not exist in ALFRED but may exist in FRED."),
        ("error", "FredError: GET /series/observations returned 503"),
        ("refused", "x" * ECON_FETCH_DETAIL_MAX),
    ],
)
def test_the_fetch_row_admits_each_outcome(
    any_engine: Engine, outcome: str, detail: str | None
) -> None:
    _add(any_engine, _fetch(outcome=outcome, detail=detail))


@pytest.mark.parametrize(
    ("outcome", "detail"),
    [
        ("posted", None),  # not an outcome
        ("FIGURE", None),
        ("", None),
        ("figure", "a figure carries no detail"),
        ("not_posted", ""),
        ("no_value", None),  # the other three must say why
        ("refused", None),
        ("error", "   "),
        ("refused", "x" * (ECON_FETCH_DETAIL_MAX + 1)),
    ],
)
def test_the_fetch_row_refuses_anything_else(
    any_engine: Engine, outcome: str, detail: str | None
) -> None:
    with pytest.raises(IntegrityError):
        _add(any_engine, _fetch(outcome=outcome, detail=detail))


def test_one_fetch_row_per_release_and_date(any_engine: Engine) -> None:
    _add(any_engine, _fetch())
    with pytest.raises(IntegrityError):
        _add(any_engine, _fetch(outcome="figure"))


def test_0017_downgrades_to_0016_and_back(db_path: Path) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    _add(eng, _value(), _fetch(outcome="figure"))
    assert TABLES <= set(inspect(eng).get_table_names())
    eng.dispose()

    command.downgrade(cfg, "0016")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert not tables & TABLES
    assert "calendar_event" in tables  # 0015's table is untouched

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    assert TABLES <= set(inspect(eng).get_table_names())
    _add(eng, _value(), _fetch())
    eng.dispose()
