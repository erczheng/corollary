"""``GET /api/news/watch`` and ``POST``/``DELETE /api/news/watch/{ticker}``.

The spec's *Testing* section, verbatim: *"add, remove, the 409 one past the
100-symbol ceiling, and refusal to remove a non-manual member; exactly one
``audit_log`` row and one ``watchlist_changed`` emit per change; nothing
emitted on a refused request"*. Beyond that: a 503 with no asset list (never
an unvalidated ticker), a 422 for a ticker that is not an active US equity,
re-adding after a removal, and the audit row rendering through the existing
``/api/settings/audit`` route in the words ``web/src/lib/settings.ts`` and
``mockData.ts`` already assume (``not watched`` -> ``watched``).

Emits are counted as ``notification`` rows written by the real runtime the
lifespan starts -- the one path every operator action takes -- so "exactly
one" is a count of what reached the bell/Discord fan-out, not of calls to a
spy.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.api.routes.markets import UNIVERSE_SYMBOLS
from corollary.api.routes.news import request_now
from corollary.data.news.watchlist import WATCH_UNIVERSE_CAP
from corollary.db.models import (
    AuditLog,
    NotificationDelivery,
    NotificationRecord,
    WatchSymbol,
)
from corollary.engine.runtime import DISCORD_WEBHOOK_ENV

from .news_support import (
    SEED_LEADERS,
    add_manual_watches,
    fill_directory,
    filler_symbols,
    make_seed,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
FAKE_WEBHOOK = "https://discord.com/api/webhooks/1234/not-a-real-webhook-token"

#: The universe before positions with the test seed and no manual watches.
BASE = frozenset(UNIVERSE_SYMBOLS) | SEED_LEADERS
FILLER = filler_symbols(WATCH_UNIVERSE_CAP, exclude=BASE)
#: Everything the test asset list names: the base, the filler, and two
#: ordinary names to watch. ``QZNOTLISTED`` shaped names are absent on purpose.
LISTED = BASE | set(FILLER) | {"PLTR", "BRK.B"}


def _start(app: FastAPI, *, directory: bool = True, seed: Any = "default") -> TestClient:
    chosen = make_seed() if seed == "default" else seed
    app.state.spdr_seed_loader = lambda: chosen
    app.dependency_overrides[request_now] = lambda: NOW
    if directory:
        fill_directory(app.state.asset_directory, LISTED)
    return TestClient(app)


@pytest.fixture
def watch(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Read by the lifespan's Discord sink; the app is built with
    # ``discord=False``, so nothing is posted and a dropped delivery is
    # recorded -- which is how a test sees Discord was routed.
    monkeypatch.setenv(DISCORD_WEBHOOK_ENV, FAKE_WEBHOOK)
    with _start(app) as client:
        yield client


def audit_rows(engine: Engine) -> list[AuditLog]:
    with Session(engine) as session:
        rows = list(session.scalars(select(AuditLog).order_by(AuditLog.id)))
        for row in rows:
            session.expunge(row)
    return rows


def watch_notices(engine: Engine) -> list[NotificationRecord]:
    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(NotificationRecord)
                .where(NotificationRecord.event == "watchlist_changed")
                .order_by(NotificationRecord.at)
            )
        )
        for row in rows:
            session.expunge(row)
    return rows


def watch_rows(engine: Engine) -> list[WatchSymbol]:
    with Session(engine) as session:
        rows = list(session.scalars(select(WatchSymbol).order_by(WatchSymbol.id)))
        for row in rows:
            session.expunge(row)
    return rows


def _state(engine: Engine) -> tuple[int, int, int]:
    """(audit rows, watchlist notices, watch rows) -- what a refusal must not move."""
    return len(audit_rows(engine)), len(watch_notices(engine)), len(watch_rows(engine))


def _error(response: Any) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["error"]
    return body


# --------------------------------------------------------------------------
# GET
# --------------------------------------------------------------------------


def test_the_watch_list_states_the_universe_it_sits_in(watch: TestClient) -> None:
    body = watch.get("/api/news/watch").json()

    assert body == {
        "manual": [],
        "symbols": len(BASE),
        "countBeforePositions": len(BASE),
        "cap": 100,
        "remaining": 100 - len(BASE),
        "positionUnderlyings": 0,
        "positionsAsOf": None,
        "seedMissing": False,
        "assetListAvailable": True,
        "assetListFetchedAt": body["assetListFetchedAt"],
    }
    assert body["assetListFetchedAt"] is not None


def test_position_underlyings_widen_the_universe_but_not_the_capped_count(
    watch: TestClient,
) -> None:
    app: Any = watch.app
    app.state.position_underlyings.replace(["QZPO", "AAPL"], at=NOW)

    body = watch.get("/api/news/watch").json()

    assert body["symbols"] == len(BASE) + 1
    assert body["countBeforePositions"] == len(BASE)
    assert body["positionUnderlyings"] == 2
    assert body["positionsAsOf"] is not None


def test_a_missing_seed_is_stated_and_shrinks_the_universe(
    app: FastAPI,
) -> None:
    with _start(app, seed=None) as client:
        body = client.get("/api/news/watch").json()

    assert body["seedMissing"] is True
    assert body["countBeforePositions"] == len(set(UNIVERSE_SYMBOLS))


def test_no_asset_list_is_stated(app: FastAPI) -> None:
    with _start(app, directory=False) as client:
        body = client.get("/api/news/watch").json()

    assert body["assetListAvailable"] is False
    assert body["assetListFetchedAt"] is None


# --------------------------------------------------------------------------
# Add and remove: one row, one audit entry, one notice each
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_an_add_writes_one_watch_one_audit_row_and_one_notice(
    watch: TestClient, db_engine: Engine
) -> None:
    response = watch.post("/api/news/watch/PLTR")

    assert response.status_code == 200, response.text
    assert response.json()["manual"] == [
        {"ticker": "PLTR", "addedAt": "2026-09-26T15:00:00Z"}
    ]
    (row,) = watch_rows(db_engine)
    assert (row.ticker, row.removed_at) == ("PLTR", None)

    (audit,) = audit_rows(db_engine)
    assert (audit.category, audit.field, audit.previous_value, audit.new_value) == (
        "watchlist",
        "PLTR",
        "not watched",
        "watched",
    )

    (notice,) = watch_notices(db_engine)
    assert notice.severity == "info"
    assert notice.account is None
    assert notice.title == "News watchlist changed"
    assert notice.body == "PLTR: not watched → watched"
    with Session(db_engine) as session:
        channels = set(
            session.scalars(
                select(NotificationDelivery.channel).where(
                    NotificationDelivery.notification_id == notice.id
                )
            )
        )
    # Decision 20's terms for the config events: bell off, Discord on.
    assert channels == {"discord"}


@pytest.mark.risk
def test_a_remove_closes_the_row_and_writes_one_audit_row_and_one_notice(
    watch: TestClient, db_engine: Engine
) -> None:
    watch.post("/api/news/watch/PLTR")

    response = watch.delete("/api/news/watch/PLTR")

    assert response.status_code == 200, response.text
    assert response.json()["manual"] == []
    (row,) = watch_rows(db_engine)
    assert row.removed_at == NOW
    audits = audit_rows(db_engine)
    assert len(audits) == 2
    assert (audits[1].field, audits[1].previous_value, audits[1].new_value) == (
        "PLTR",
        "watched",
        "not watched",
    )
    notices = watch_notices(db_engine)
    assert len(notices) == 2
    assert notices[1].body == "PLTR: watched → not watched"


def test_a_removed_watch_can_be_added_again_as_a_new_row(
    watch: TestClient, db_engine: Engine
) -> None:
    assert watch.post("/api/news/watch/PLTR").status_code == 200
    assert watch.delete("/api/news/watch/PLTR").status_code == 200
    assert watch.post("/api/news/watch/PLTR").status_code == 200

    rows = watch_rows(db_engine)
    assert [(row.ticker, row.removed_at is None) for row in rows] == [
        ("PLTR", False),
        ("PLTR", True),
    ]
    assert len(audit_rows(db_engine)) == 3
    assert len(watch_notices(db_engine)) == 3


def test_the_path_ticker_is_normalised(watch: TestClient, db_engine: Engine) -> None:
    assert watch.post("/api/news/watch/brk-b").status_code == 200

    (row,) = watch_rows(db_engine)
    assert row.ticker == "BRK.B"
    assert watch.delete("/api/news/watch/brk.b").status_code == 200


def test_the_audit_row_renders_through_the_settings_audit_route(
    watch: TestClient,
) -> None:
    watch.post("/api/news/watch/PLTR")

    page = watch.get("/api/settings/audit").json()

    (entry,) = [item for item in page["items"] if item["category"] == "watchlist"]
    assert (entry["field"], entry["previousValue"], entry["newValue"]) == (
        "PLTR",
        "not watched",
        "watched",
    )


# --------------------------------------------------------------------------
# The ceiling: 100 before position underlyings, inclusive
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_the_hundredth_symbol_is_permitted_and_the_hundred_and_first_is_a_409(
    watch: TestClient, db_engine: Engine
) -> None:
    room = WATCH_UNIVERSE_CAP - len(BASE)
    add_manual_watches(db_engine, FILLER[: room - 1], at=NOW - timedelta(days=1))
    assert watch.get("/api/news/watch").json()["countBeforePositions"] == 99

    at_boundary = watch.post("/api/news/watch/PLTR")
    assert at_boundary.status_code == 200, at_boundary.text
    assert at_boundary.json()["countBeforePositions"] == 100
    assert at_boundary.json()["remaining"] == 0

    before = _state(db_engine)
    refused = watch.post(f"/api/news/watch/{FILLER[room]}")

    assert refused.status_code == 409
    error = _error(refused)
    assert error["code"] == "watch_cap_reached"
    assert FILLER[room] in error["message"]
    assert "100" in error["message"]
    assert _state(db_engine) == before


def test_position_underlyings_never_block_a_watch_at_the_ceiling(
    watch: TestClient, db_engine: Engine
) -> None:
    room = WATCH_UNIVERSE_CAP - len(BASE)
    add_manual_watches(db_engine, FILLER[: room - 1], at=NOW - timedelta(days=1))
    app: Any = watch.app
    app.state.position_underlyings.replace(["QZPO", "QZPP", "QZPQ"], at=NOW)

    assert watch.post("/api/news/watch/PLTR").status_code == 200


# --------------------------------------------------------------------------
# Refusals: nothing written, nothing emitted
# --------------------------------------------------------------------------


@pytest.mark.risk
@pytest.mark.parametrize(
    ("ticker", "code", "named"),
    [
        ("AAPL", "watch_not_manual", "markets"),
        ("NVDA", "watch_not_manual", "sector_leader"),
        ("MARKET", "watch_reserved", "MARKET"),
    ],
)
def test_a_non_manual_member_cannot_be_removed(
    watch: TestClient, db_engine: Engine, ticker: str, code: str, named: str
) -> None:
    before = _state(db_engine)

    response = watch.delete(f"/api/news/watch/{ticker}")

    assert response.status_code == 409
    assert _error(response)["code"] == code
    assert named in _error(response)["message"]
    assert _state(db_engine) == before


def test_a_position_only_member_cannot_be_removed(
    watch: TestClient, db_engine: Engine
) -> None:
    app: Any = watch.app
    app.state.position_underlyings.replace(["QZPO"], at=NOW)

    response = watch.delete("/api/news/watch/QZPO")

    assert response.status_code == 409
    assert "position" in _error(response)["message"]
    assert _state(db_engine) == (0, 0, 0)


def test_removing_a_non_member_is_a_404(watch: TestClient, db_engine: Engine) -> None:
    response = watch.delete("/api/news/watch/PLTR")

    assert response.status_code == 404
    assert _error(response)["code"] == "watch_not_member"
    assert _state(db_engine) == (0, 0, 0)


@pytest.mark.parametrize(
    ("ticker", "status", "code"),
    [
        ("AAPL", 409, "watch_already_member"),
        ("MARKET", 409, "watch_reserved"),
        ("AAPL1", 422, "watch_invalid_symbol"),
        ("QZNOTLISTED", 422, "watch_invalid_symbol"),
        ("QZZZZ", 422, "not_an_active_us_equity"),
    ],
)
def test_an_add_that_is_refused_writes_and_emits_nothing(
    watch: TestClient, db_engine: Engine, ticker: str, status: int, code: str
) -> None:
    response = watch.post(f"/api/news/watch/{ticker}")

    assert response.status_code == status, response.text
    assert _error(response)["code"] == code
    assert _state(db_engine) == (0, 0, 0)


def test_adding_an_existing_manual_watch_is_a_409(
    watch: TestClient, db_engine: Engine
) -> None:
    watch.post("/api/news/watch/PLTR")
    before = _state(db_engine)

    response = watch.post("/api/news/watch/pltr")

    assert response.status_code == 409
    assert _error(response)["code"] == "watch_already_manual"
    assert _state(db_engine) == before


@pytest.mark.risk
def test_with_no_asset_list_an_add_is_a_503_never_an_unvalidated_watch(
    app: FastAPI, db_engine: Engine
) -> None:
    with _start(app, directory=False) as client:
        response = client.post("/api/news/watch/PLTR")

    assert response.status_code == 503
    error = _error(response)
    assert error["code"] == "asset_list_unavailable"
    assert "asset list" in error["message"]
    assert _state(db_engine) == (0, 0, 0)


def test_a_remove_needs_no_asset_list(app: FastAPI, db_engine: Engine) -> None:
    add_manual_watches(db_engine, ["PLTR"], at=NOW - timedelta(days=1))

    with _start(app, directory=False) as client:
        response = client.delete("/api/news/watch/PLTR")

    assert response.status_code == 200
    assert len(watch_notices(db_engine)) == 1
