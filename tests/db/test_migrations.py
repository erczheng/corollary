"""Alembic, wired for the first time.

The load-bearing test is ``test_upgrade_head_matches_the_models``: it
autogenerates a diff between the migrated database and ``Base.metadata`` and
requires it to be empty. Without it, a hand-written migration and the models
drift and nothing notices until a query fails at runtime.

The seed values are duplicated between the migration and ``seed.py`` on
purpose — a migration is a frozen record of what the schema was on the day,
so it must not import defaults that can change underneath it. The duplication
is safe only because ``test_migration_seed_matches_the_seed_module`` fails
the moment the two disagree.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect
from sqlalchemy.orm import Session

from corollary.db.models import Base, DataFeed, EngineState, NotificationRoute, RiskLimit
from corollary.db.seed import (
    DATA_FEED_DEFAULTS,
    NOTIFICATION_ROUTE_DEFAULTS,
    RISK_LIMIT_DEFAULTS,
)
from corollary.db.session import create_db_engine, sqlite_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

EXPECTED_TABLES = {
    # 0001 — config, audit, engine state
    "risk_limit",
    "data_feed",
    "notification_route",
    "audit_log",
    "engine_state",
    # 0002 — the ledger
    "fill",
    "realized_trade",
    "mleg_group",
    "mleg_leg",
    # 0004 — the refusals, kept so a gap in P&L can state its cause
    "ledger_rejection",
    # 0005 -- the bell's rows, and every delivery attempt behind them
    "notification",
    "notification_delivery",
    # 0007 -- FRED observations, the risk-free rate's source (decision 19)
    "fred_observation",
    # 0008 -- step 4: the article store, manual watches, the tradeability cache
    "news_article",
    "news_article_ticker",
    "watch_symbol",
    "ticker_tradeability",
    # 0009 -- owner decision Q12: the vendor's IPO date, cached per ticker
    "ticker_ipo_date",
    # 0010 -- step 4: the SPDR sector seed, built from SEC N-PORT
    "spdr_holdings_snapshot",
    "spdr_holding",
    # 0012 -- Q17: the OpenFIGI ISIN -> ticker cache
    "isin_ticker",
    # 0013 -- step 5: one label per (article, ticker, source)
    "sentiment_label",
}

#: Everything 0013 created.
_0013_TABLES = {"sentiment_label"}

#: Everything 0012 created.
_0012_TABLES = {"isin_ticker"}

#: Everything 0010 created.
_0010_TABLES = {"spdr_holdings_snapshot", "spdr_holding"}

#: Everything 0008 created, so a downgrade past it can say so once.
_0008_TABLES = {
    "news_article",
    "news_article_ticker",
    "watch_symbol",
    "ticker_tradeability",
}


def _config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def migrated(db_path: Path) -> Engine:
    """A database built by ``alembic upgrade head``, not by create_all()."""
    url = sqlite_url(db_path)
    command.upgrade(_config(url), "head")
    return create_db_engine(url)


def test_alembic_ini_exists() -> None:
    assert ALEMBIC_INI.is_file()


def test_upgrade_head_creates_every_table_the_models_declare(
    migrated: Engine,
) -> None:
    tables = set(inspect(migrated).get_table_names())
    assert EXPECTED_TABLES <= tables


def test_0005_downgrades_to_0004_and_back(db_path: Path) -> None:
    """The notification tables come and go alone; nothing earlier is touched."""
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0004")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert "notification" not in tables
    assert "notification_delivery" not in tables
    # Everything newer than 0004 goes with it -- 0007's and 0008's included.
    assert EXPECTED_TABLES - {
        "notification",
        "notification_delivery",
        "fred_observation",
        "ticker_ipo_date",
    } - _0008_TABLES - _0010_TABLES - _0012_TABLES - _0013_TABLES <= tables

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert EXPECTED_TABLES <= tables


def test_there_is_exactly_one_head(db_path: Path) -> None:
    """Two revisions revising 0001 breaks ``alembic upgrade head`` outright.

    Steps 5 and 6 both want new tables and both run against ``0002``, in
    parallel. Either one adding its own revision on top of ``0001`` gives
    Alembic two heads and an ambiguous target — which is why the ledger
    schema is one revision written ahead of both, and why this test exists
    rather than the convention being left to memory.

    ``0009`` is the head now: owner decision Q12's ``ticker_ipo_date``, the
    vendor's IPO date cached per ticker. ``0008`` before it: step 4's article store, manual watches, the
    tradeability cache, the ``watchlist`` audit category and the
    ``watchlist_changed`` routes. ``0007`` before it added
    ``fred_observation``, the risk-free rate's source. ``0006`` before it seeded the routing for the owner's own actions,
    ``0005`` added ``notification`` and ``notification_delivery``,
    the tables rule 9's halt alert lands in, and ``0004`` ``ledger_rejection``.
    """
    script = ScriptDirectory.from_config(_config(sqlite_url(db_path)))
    assert script.get_heads() == ["0013"]


def test_0009_downgrades_to_0008_and_back(db_path: Path) -> None:
    """``ticker_ipo_date`` comes and goes alone; 0008's tables are untouched."""
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0008")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert "ticker_ipo_date" not in tables
    assert EXPECTED_TABLES - {"ticker_ipo_date"} - _0010_TABLES - _0012_TABLES - _0013_TABLES <= tables

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert EXPECTED_TABLES <= tables



def test_0011_seeds_the_spdr_seed_amended_routes_and_downgrades_to_0010(
    db_path: Path,
) -> None:
    """Bell off, Discord on (decision 20's info defaults); route rows only, no schema."""
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    routes = _route_rows(url)
    assert routes[("spdr_seed_amended", "bell")] is False
    assert routes[("spdr_seed_amended", "discord")] is True

    command.downgrade(cfg, "0010")
    routes = _route_rows(url)
    assert not {event for event, _channel in routes} & {"spdr_seed_amended"}
    assert len(routes) == 28
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert EXPECTED_TABLES - _0012_TABLES - _0013_TABLES <= tables  # 0011 dropped nothing

    command.upgrade(cfg, "head")
    assert _route_rows(url)[("spdr_seed_amended", "discord")] is True


def test_0011_leaves_rows_the_seed_already_wrote(db_path: Path) -> None:
    """``seed.py`` runs on every startup; an edited row must survive 0011."""
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "0010")
    eng = create_db_engine(url)
    with Session(eng) as sess:
        sess.add(NotificationRoute(event="spdr_seed_amended", channel="discord", enabled=False))
        sess.commit()
    eng.dispose()

    command.upgrade(cfg, "head")
    routes = _route_rows(url)
    assert routes[("spdr_seed_amended", "discord")] is False
    assert routes[("spdr_seed_amended", "bell")] is False


def test_0012_downgrades_to_0011_and_back(db_path: Path) -> None:
    """``isin_ticker`` comes and goes alone; 0011's routes and 0010's tables stay."""
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0011")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert tables & (_0012_TABLES | _0013_TABLES) == set()
    assert EXPECTED_TABLES - _0012_TABLES - _0013_TABLES <= tables
    assert _route_rows(url)[("spdr_seed_amended", "discord")] is True

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert EXPECTED_TABLES <= tables


# Autogenerate does not compare CHECK constraints; run the same rows against
# both the migrated schema and the models'.
_ISIN_TICKER_CASES = [
    # (isin, ticker, composite_figi, source, accepted)
    ("IE000S9YS762", "LIN", "BBG01FND0CC1", "openfigi", True),
    ("US0846707026", "BRK.B", None, "openfigi", True),
    ("IE000S9YS76", "LIN", None, "openfigi", False),  # ck_isin_ticker_isin: eleven
    ("ie000s9ys762", "LIN", None, "openfigi", False),  # ck_isin_ticker_isin: lower case
    ("IE000S9YS762", "", None, "openfigi", False),  # ck_isin_ticker_ticker: empty
    ("IE000S9YS762", "lin", None, "openfigi", False),  # ck_isin_ticker_ticker: lower
    ("IE000S9YS762", "LIN", "BBG01FND0CC", "openfigi", False),  # composite: eleven
    ("IE000S9YS762", "LIN", None, "name", False),  # ck_isin_ticker_source
]


@pytest.mark.parametrize("built_by", ["alembic", "models"])
@pytest.mark.parametrize(
    ("isin", "ticker", "composite_figi", "source", "accepted"), _ISIN_TICKER_CASES
)
def test_0012_isin_ticker_checks(
    db_path: Path,
    built_by: str,
    isin: str,
    ticker: str,
    composite_figi: str | None,
    source: str,
    accepted: bool,
) -> None:
    from datetime import datetime, timezone

    from sqlalchemy.exc import IntegrityError

    from corollary.db.models import IsinTicker

    url = sqlite_url(db_path)
    if built_by == "alembic":
        command.upgrade(_config(url), "head")
        eng = create_db_engine(url)
    else:
        eng = create_db_engine(url)
        Base.metadata.create_all(eng)
    row = IsinTicker(
        isin=isin,
        ticker=ticker,
        composite_figi=composite_figi,
        source=source,
        resolved_at=datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc),
    )
    try:
        if not accepted:
            with Session(eng) as sess:
                sess.add(row)
                with pytest.raises(IntegrityError):
                    sess.commit()
            return
        with Session(eng) as sess:
            sess.add(row)
            sess.commit()
        with Session(eng) as sess:
            stored = sess.get(IsinTicker, isin)
            assert stored is not None
            assert stored.resolved_at == datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
            assert stored.resolved_at.tzinfo is not None
    finally:
        eng.dispose()


def test_0010_downgrades_to_0009_and_back(db_path: Path) -> None:
    """The two snapshot tables come and go together; 0009's are untouched."""
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0009")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert tables & _0010_TABLES == set()
    assert EXPECTED_TABLES - _0010_TABLES - _0012_TABLES - _0013_TABLES <= tables

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert EXPECTED_TABLES <= tables

_OPERATOR_EVENTS = {
    "operator_halt",
    "operator_resume",
    "risk_limits_changed",
    "data_feeds_changed",
    "notification_routes_changed",
}


def _route_rows(url: str) -> dict[tuple[str, str], bool]:
    eng = create_db_engine(url)
    try:
        with Session(eng) as sess:
            return {
                (row.event, row.channel): row.enabled
                for row in sess.query(NotificationRoute).all()
            }
    finally:
        eng.dispose()


def test_0006_seeds_the_operator_routes_and_downgrades_to_0005(
    db_path: Path,
) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    routes = _route_rows(url)
    assert routes[("operator_halt", "bell")] is True
    assert routes[("operator_resume", "discord")] is True
    assert routes[("risk_limits_changed", "bell")] is False
    assert routes[("notification_routes_changed", "discord")] is True

    command.downgrade(cfg, "0005")
    routes = _route_rows(url)
    assert not {event for event, _channel in routes} & _OPERATOR_EVENTS
    assert len(routes) == 16  # 0001's table, untouched

    command.upgrade(cfg, "head")
    # 0006's ten back, plus 0008's two ``watchlist_changed`` rows and
    # 0011's two ``spdr_seed_amended`` rows.
    assert len(_route_rows(url)) == 30


def test_0006_leaves_rows_the_seed_already_wrote(db_path: Path) -> None:
    """``seed.py`` runs on every startup; an edited row must survive 0006."""
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "0005")
    eng = create_db_engine(url)
    with Session(eng) as sess:
        sess.add(NotificationRoute(event="operator_halt", channel="bell", enabled=False))
        sess.commit()
    eng.dispose()

    command.upgrade(cfg, "head")
    routes = _route_rows(url)
    assert routes[("operator_halt", "bell")] is False
    assert routes[("operator_halt", "discord")] is True


def test_0007_downgrades_to_0006_and_back(db_path: Path) -> None:
    """``fred_observation`` comes and goes alone; nothing earlier is touched."""
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0006")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert "fred_observation" not in tables
    assert (
        EXPECTED_TABLES
        - {"fred_observation", "ticker_ipo_date"}
        - _0008_TABLES
        - _0010_TABLES
        - _0012_TABLES - _0013_TABLES
        <= tables
    )

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert EXPECTED_TABLES <= tables


def test_upgrade_head_matches_the_models(migrated: Engine) -> None:
    def _skip_alembic_version(
        obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
    ) -> bool:
        return not (type_ == "table" and name == "alembic_version")

    with migrated.connect() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts={"compare_type": True, "include_object": _skip_alembic_version},
        )
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_migration_seed_matches_the_seed_module(migrated: Engine) -> None:
    with Session(migrated) as sess:
        limits = {row.key: row.value for row in sess.query(RiskLimit).all()}
        feeds = {row.key: row.value for row in sess.query(DataFeed).all()}
        routes = {
            (row.event, row.channel): row.enabled
            for row in sess.query(NotificationRoute).all()
        }
        state = sess.query(EngineState).one()

    assert limits == dict(RISK_LIMIT_DEFAULTS)
    assert all(type(v) is Decimal for v in limits.values())
    assert feeds == dict(DATA_FEED_DEFAULTS)
    assert routes == {(e, c): enabled for e, c, enabled in NOTIFICATION_ROUTE_DEFAULTS}
    assert state.id == 1
    assert state.halted is True


def test_downgrade_removes_everything(db_path: Path) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert tables & EXPECTED_TABLES == set()


# Autogenerate does not compare CHECK constraints, so the model/migration match
# test above cannot see a CHECK that one side has and the other lacks. These run
# the same rows against both.
_SPDR_ID_CASES = [
    # (cusip, isin, accepted)
    ("67066G104", None, True),  # a CUSIP line
    (None, "IE000S9YS762", True),  # an ISIN-only line resolved through the seam
    ("67066G104", "US67066G1040", True),
    (None, None, False),  # ck_spdr_holding_identifier: nothing to trace it by
    ("67066G10", None, False),  # ck_spdr_holding_cusip: eight characters
    ("67066g104", None, False),  # ck_spdr_holding_cusip: lower case
    (None, "IE000S9YS76", False),  # ck_spdr_holding_isin: eleven characters
    (None, "ie000s9ys762", False),  # ck_spdr_holding_isin: lower case
]


@pytest.mark.parametrize("built_by", ["alembic", "models"])
@pytest.mark.parametrize(("cusip", "isin", "accepted"), _SPDR_ID_CASES)
def test_0010_spdr_holding_identifier_checks(
    db_path: Path, built_by: str, cusip: str | None, isin: str | None, accepted: bool
) -> None:
    from datetime import date, datetime, timezone

    from sqlalchemy.exc import IntegrityError

    from corollary.db.models import SpdrHoldingRow, SpdrHoldingsSnapshot

    url = sqlite_url(db_path)
    if built_by == "alembic":
        command.upgrade(_config(url), "head")
        eng = create_db_engine(url)
    else:
        eng = create_db_engine(url)
        Base.metadata.create_all(eng)
    try:
        with Session(eng) as sess, sess.begin():
            snap = SpdrHoldingsSnapshot(
                report_date=date(2026, 6, 30), filed_date=date(2026, 8, 28),
                built_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
                status="accepted", rule=None, reason=None, skipped_lines=0,
            )
            sess.add(snap)
            sess.flush()
            snapshot_id = snap.id
        row = SpdrHoldingRow(
            snapshot_id=snapshot_id, etf="XLB", symbol="SYNA", sector="Materials",
            series_id="S000006414", accession="0000000000-26-000000",
            cusip=cusip, isin=isin, name="SYNTHETIC", weight=Decimal("1.5"),
        )
        with Session(eng) as sess:
            sess.add(row)
            if accepted:
                sess.commit()
            else:
                with pytest.raises(IntegrityError):
                    sess.commit()
    finally:
        eng.dispose()
