"""Migration 0016: the ``account`` audit category and the Cash session routes.

Autogenerate does not compare CHECK constraints, so the account-row CHECK is
run against both ways a database is built -- ``create_all()`` + ``seed()``,
and Alembic -- with the same rows.
"""

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corollary.db.models import AUDIT_CATEGORIES, AuditLog, Base, NotificationRoute
from corollary.db.seed import NOTIFICATION_ROUTE_DEFAULTS, seed
from corollary.db.session import create_db_engine, sqlite_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
AT = datetime(2026, 10, 23, 14, 30, tzinfo=timezone.utc)

_ROUTES = {
    ("cash_session_confirmed", "bell"): True,
    ("cash_session_confirmed", "discord"): True,
    ("cash_session_ended", "bell"): True,
    ("cash_session_ended", "discord"): True,
}


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
        with Session(eng) as sess:
            seed(sess)
            sess.commit()
    yield eng
    eng.dispose()


def _routes(eng: Engine) -> dict[tuple[str, str], bool]:
    with Session(eng) as sess:
        return {
            (row.event, row.channel): row.enabled
            for row in sess.scalars(select(NotificationRoute))
        }


def _insert(eng: Engine, *, field: str, previous: str, new: str) -> None:
    with Session(eng) as sess:
        sess.add(
            AuditLog(
                at=AT,
                category="account",
                field=field,
                previous_value=previous,
                new_value=new,
            )
        )
        sess.commit()


def test_the_head_is_0016() -> None:
    cfg = Config(str(ALEMBIC_INI))
    assert ScriptDirectory.from_config(cfg).get_heads() == ["0016"]


def test_account_is_an_audit_category() -> None:
    assert "account" in AUDIT_CATEGORIES


def test_both_builds_route_the_two_events_bell_and_discord(any_engine: Engine) -> None:
    routes = _routes(any_engine)
    for key, enabled in _ROUTES.items():
        assert routes[key] is enabled, key
    seeded = {(e, c): on for e, c, on in NOTIFICATION_ROUTE_DEFAULTS}
    assert {k: seeded[k] for k in _ROUTES} == _ROUTES


@pytest.mark.parametrize(
    ("previous", "new"),
    [
        ("inactive", "active ••••1234"),
        ("active ••••1234", "inactive"),
        ("active ••••1234", "active ••••1234"),  # a re-confirmation
        ("active ••••AB12", "active ••••ab12"),
    ],
)
def test_the_account_row_check_admits_the_two_shapes(
    any_engine: Engine, previous: str, new: str
) -> None:
    _insert(any_engine, field="cash_session", previous=previous, new=new)


@pytest.mark.parametrize(
    ("field", "previous", "new"),
    [
        # Rule 6: a full account number cannot be stored, in either column.
        ("cash_session", "inactive", "active PA3ABCD1234"),
        ("cash_session", "inactive", "active 612345678"),
        ("cash_session", "612345678", "inactive"),
        ("cash_session", "inactive", "active ••••12345"),
        ("cash_session", "inactive", "active ••••12-4"),
        ("cash_session", "inactive", "ACTIVE ••••1234"),
        # Ending a session that did not exist writes nothing.
        ("cash_session", "inactive", "inactive"),
        ("account_number", "inactive", "active ••••1234"),
    ],
)
def test_the_account_row_check_refuses_anything_else(
    any_engine: Engine, field: str, previous: str, new: str
) -> None:
    with pytest.raises(IntegrityError):
        _insert(any_engine, field=field, previous=previous, new=new)


def test_the_account_row_check_leaves_other_categories_alone(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(
            AuditLog(
                at=AT,
                category="risk",
                field="max_daily_loss_pct",
                previous_value="20",
                new_value="15",
            )
        )
        sess.commit()


def test_0016_downgrades_to_0015_and_back(db_path: Path) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    with Session(eng) as sess:
        sess.add(
            AuditLog(
                at=AT,
                category="risk",
                field="max_daily_loss_pct",
                previous_value="20",
                new_value="15",
            )
        )
        sess.commit()
    eng.dispose()

    command.downgrade(cfg, "0015")
    eng = create_db_engine(url)
    routes = _routes(eng)
    assert not {event for event, _channel in routes} & {
        "cash_session_confirmed",
        "cash_session_ended",
    }
    with Session(eng) as sess:
        assert len(list(sess.scalars(select(AuditLog)))) == 1  # rows kept
    # 0015's CHECK refuses the category again.
    with pytest.raises(IntegrityError):
        _insert(eng, field="cash_session", previous="inactive", new="active ••••1234")
    eng.dispose()

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    assert {k: _routes(eng)[k] for k in _ROUTES} == _ROUTES
    _insert(eng, field="cash_session", previous="inactive", new="active ••••1234")
    eng.dispose()


def test_0016_leaves_routes_the_seed_already_wrote(db_path: Path) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "0015")
    eng = create_db_engine(url)
    with Session(eng) as sess:
        sess.add(
            NotificationRoute(event="cash_session_ended", channel="bell", enabled=False)
        )
        sess.commit()
    eng.dispose()

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    routes = _routes(eng)
    eng.dispose()
    assert routes[("cash_session_ended", "bell")] is False
    assert routes[("cash_session_ended", "discord")] is True


def test_0016_downgrade_refuses_while_an_account_row_exists(db_path: Path) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    _insert(eng, field="cash_session", previous="inactive", new="active ••••1234")
    eng.dispose()

    with pytest.raises(RuntimeError, match="refusing to downgrade 0016"):
        command.downgrade(cfg, "0015")

    eng = create_db_engine(url)
    with Session(eng) as sess:
        assert len(list(sess.scalars(select(AuditLog)))) == 1
    assert _routes(eng)[("cash_session_confirmed", "bell")] is True
    eng.dispose()
