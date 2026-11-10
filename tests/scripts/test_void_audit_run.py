"""``scripts/void_audit_run.py`` (R4) on a temporary database.

Discord is the real ``DiscordNotifier`` over an ``httpx.MockTransport`` that
counts its POSTs, so the stack, the routing gate and the delivery rows are
the app's. The webhook URL is SYNTHETIC; no request leaves the process.
"""

import importlib.util
import io
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from corollary.db.models import (
    AuditRun,
    Base,
    NotificationDelivery,
    NotificationRecord,
    SentimentSourceStatus,
)
from corollary.db.seed import seed
from corollary.db.session import DATABASE_URL_ENV, create_db_engine, sqlite_url
from corollary.engine.runtime import DISCORD_WEBHOOK_ENV

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "void_audit_run.py"
UTC = timezone.utc
STARTED = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 9, 20, 0, tzinfo=UTC)
WEBHOOK = "https://discord.com/api/webhooks/1/synthetic-token"


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("void_audit_run", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "void_audit_run", module)
    spec.loader.exec_module(module)
    yield module


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run 1 moved ``massive`` unaudited -> demoted, as ``_close_run`` writes it."""
    path = tmp_path / "void.db"
    engine = create_db_engine(sqlite_url(path))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
        session.add(
            AuditRun(
                started_at=STARTED,
                finished_at=STARTED + timedelta(minutes=5),
                trigger="scheduled",
                status="succeeded",
            )
        )
        session.flush()
        from corollary.db.models import AuditLog

        session.add(
            AuditLog(
                at=STARTED + timedelta(minutes=5),
                category="sentiment",
                field="massive",
                previous_value="unaudited",
                new_value="demoted",
            )
        )
        row = session.get(SentimentSourceStatus, "massive")
        assert row is not None
        row.status, row.changed_at, row.reason, row.audit_run_id = (
            "demoted",
            STARTED,
            "synthetic",
            1,
        )
        session.commit()
    engine.dispose()
    monkeypatch.setenv(DATABASE_URL_ENV, sqlite_url(path))
    return path


class Recorder:
    def __init__(self, status: int) -> None:
        self.status = status
        self.posts: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.posts.append(request)
        return httpx.Response(self.status)


def _rows(path: Path) -> tuple[list[NotificationRecord], list[NotificationDelivery], AuditRun]:
    engine = create_db_engine(sqlite_url(path))
    try:
        with Session(engine) as session:
            notes = list(session.scalars(select(NotificationRecord)))
            deliveries = list(
                session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.channel))
            )
            run = session.get(AuditRun, 1)
            assert run is not None
            session.expunge_all()
            return notes, deliveries, run
    finally:
        engine.dispose()


def test_help_exits_cleanly(script: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        script.main(["--help"])
    assert exited.value.code == 0
    assert "--run-id" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "rule"),
    [
        (["--run-id", "99", "--reason", "x"], "run_not_found"),
        (["--run-id", "1", "--reason", " 	 "], "empty_reason"),
    ],
    ids=["nonexistent", "whitespace_reason"],
)
def test_a_refusal_exits_1_and_writes_and_sends_nothing(
    script: ModuleType,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    rule: str,
) -> None:
    recorder = Recorder(204)
    code = script.main(
        argv,
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == script.EXIT_REFUSED == 1
    out, err = capsys.readouterr()
    assert out.splitlines()[0] == f"database: {db_path.resolve()}"
    assert f"refused ({rule})" in err
    assert f'"rule": "{rule}"' in err  # the JSON rejection line
    notes, deliveries, run = _rows(db_path)
    assert (notes, deliveries, run.voided_at) == ([], [], None)
    assert recorder.posts == []


def test_a_void_writes_one_notification_to_the_bell_and_discord_once(
    script: ModuleType, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = Recorder(204)
    code = script.main(
        ["--run-id", "1", "--reason", "graded twins twice"],
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"database: {db_path.resolve()}"
    assert "restored massive: demoted -> unaudited (never audited)" in out
    notes, deliveries, run = _rows(db_path)
    assert (run.voided_at, run.void_reason) == (NOW, "graded twins twice")
    [note] = notes
    assert (note.event, note.severity, note.account) == ("sentiment_audit_voided", "info", None)
    assert note.title == "Sentiment audit run 1 voided"
    assert "graded twins twice" in note.body and "massive: demoted -> unaudited" in note.body
    assert [(d.channel, d.status) for d in deliveries] == [
        ("bell", "delivered"),
        ("discord", "delivered"),
    ]
    assert len(recorder.posts) == 1


def test_a_second_void_of_the_same_run_is_refused_and_sends_nothing_more(
    script: ModuleType, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = Recorder(204)
    first = script.main(
        ["--run-id", "1", "--reason", "graded twins twice"],
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert first == script.EXIT_OK
    capsys.readouterr()
    again = script.main(
        ["--run-id", "1", "--reason", "again"],
        clock=lambda: NOW + timedelta(minutes=1),
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert again == script.EXIT_REFUSED
    assert "refused (already_voided)" in capsys.readouterr().err
    notes, deliveries, run = _rows(db_path)
    assert (run.voided_at, run.void_reason) == (NOW, "graded twins twice")
    assert len(notes) == 1 and len(deliveries) == 2
    assert len(recorder.posts) == 1


def test_a_discord_failure_leaves_the_void_and_exits_3(
    script: ModuleType, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = Recorder(500)
    code = script.main(
        ["--run-id", "1", "--reason", "graded twins twice"],
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == script.EXIT_NOT_DELIVERED == 3
    err = capsys.readouterr().err
    assert "IS voided" in err and "not delivered -- discord" in err
    assert WEBHOOK not in err and "synthetic-token" not in err  # rule 6
    _notes, deliveries, run = _rows(db_path)
    assert run.voided_at == NOW
    assert ("bell", "delivered") in [(d.channel, d.status) for d in deliveries]
    assert "delivered" not in {d.status for d in deliveries if d.channel == "discord"}


def test_a_discord_sink_that_cannot_start_leaves_the_bell_and_exits_3(
    script: ModuleType,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2: ``DiscordNotifier.start()`` raises on this machine when
    ``SSL_CERT_FILE`` is stale (``FileNotFoundError``). The app's lifespan
    guards it; so must the script -- Discord unavailable means bell only."""

    def stale_certificates(self: object) -> None:
        raise FileNotFoundError("SYNTHETIC: SSL_CERT_FILE names no file")

    monkeypatch.setattr(script.DiscordNotifier, "start", stale_certificates)
    recorder = Recorder(204)
    code = script.main(
        ["--run-id", "1", "--reason", "graded twins twice"],
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == script.EXIT_NOT_DELIVERED == 3
    err = capsys.readouterr().err
    assert "IS voided" in err and "Do not re-run the void." in err
    assert "Traceback" not in err
    assert '"event": "notification_discord_not_started"' in err
    assert '"error_type": "FileNotFoundError"' in err
    notes, deliveries, run = _rows(db_path)
    assert run.voided_at == NOW
    [note] = notes
    assert note.event == "sentiment_audit_voided"
    assert [(d.channel, d.status) for d in deliveries] == [
        ("bell", "delivered"),
        ("discord", "dropped"),
    ]
    assert recorder.posts == []


def test_any_failure_after_the_commit_exits_3_and_says_the_run_is_voided(
    script: ModuleType,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 1 means "refused, nothing written". Once the void has committed,
    nothing that goes wrong may say that: it exits 3, naming the run voided."""

    async def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("SYNTHETIC failure while notifying")

    monkeypatch.setattr(script, "notify", broken)
    code = script.main(
        ["--run-id", "1", "--reason", "graded twins twice"],
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(Recorder(204)),
    )
    assert code == script.EXIT_NOT_DELIVERED
    err = capsys.readouterr().err
    assert "audit run 1 IS voided" in err and "Do not re-run the void." in err
    assert "RuntimeError" in err and "Traceback" not in err
    _notes, _deliveries, run = _rows(db_path)
    assert run.voided_at == NOW


def test_a_reason_the_console_cannot_encode_is_notified_and_printed_replaced(
    script: ModuleType, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: with stdout piped, a Windows console is cp1252 and strict, so a
    reason holding ``→`` used to raise ``UnicodeEncodeError`` after the
    commit -- exit 1, nothing sent. The notification goes first, and the
    console gets a replacement character rather than a traceback."""
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    reason = "graded twins twice → run 1 is wrong"
    recorder = Recorder(204)
    code = script.main(
        ["--run-id", "1", "--reason", reason],
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == script.EXIT_OK
    notes, deliveries, run = _rows(db_path)
    assert run.void_reason == reason
    [note] = notes
    assert reason in note.body  # the bell keeps the exact text
    assert [(d.channel, d.status) for d in deliveries] == [
        ("bell", "delivered"),
        ("discord", "delivered"),
    ]
    assert len(recorder.posts) == 1
    stdout.flush()
    printed = stdout.buffer.getvalue().decode("cp1252")
    assert "reason: graded twins twice ? run 1 is wrong" in printed
