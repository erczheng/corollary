"""``scripts/void_audit_run.py`` (R4) on a temporary database.

Discord is the real ``DiscordNotifier`` over an ``httpx.MockTransport`` that
counts its POSTs, so the stack, the routing gate and the delivery rows are
the app's. The webhook URL is SYNTHETIC; no request leaves the process.
"""

import importlib.util
import io
import logging
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from corollary.data.news import audit_void
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
    # argparse re-wraps the epilog, so compare with the line breaks folded.
    out = " ".join(capsys.readouterr().out.split())
    assert "--run-id" in out
    assert "0 voided and notified (or --help)" in out
    assert "A Ctrl-C is re-raised instead of 4 or 5" in out
    assert "that exit 1 is not a refusal" in out


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
    out, err = capsys.readouterr()
    assert out.splitlines()[0] == f"database: {db_path.resolve()}"
    assert "restored massive: demoted -> unaudited (never audited)" in out
    # The success line is the script's to log, after the void returned (F1).
    assert '"event": "sentiment_audit_run_voided"' in err
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


def _main(script: ModuleType, argv: list[str], **kwargs: object) -> object:
    """``main``, with a ``KeyboardInterrupt`` that escapes it turned into a
    value: escaping a test, it would stop the whole pytest session."""
    try:
        return script.main(argv, **kwargs)
    except KeyboardInterrupt:
        return "a KeyboardInterrupt escaped main"


_ARGV = ["--run-id", "1", "--reason", "graded twins twice"]


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, RuntimeError])
def test_a_void_interrupted_after_its_commit_is_read_back_notified_and_exits_3(
    script: ModuleType,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    """F1, the auditor's reproduction: a Ctrl-C between the void's COMMIT and
    its return used to escape ``main``, leaving the run voided with no
    notification and no exit 3. The script reads the run back; this
    invocation's void landed, so it notifies and exits 3."""
    real = script.void_audit_run

    def interrupted_after_the_commit(*args: object, **kwargs: object) -> object:
        real(*args, **kwargs)
        raise interruption("SYNTHETIC: interrupted after the commit")

    monkeypatch.setattr(script, "void_audit_run", interrupted_after_the_commit)
    recorder = Recorder(204)
    code = _main(
        script,
        _ARGV,
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == script.EXIT_NOT_DELIVERED == 3
    out, err = capsys.readouterr()
    assert "restored massive: demoted -> unaudited (never audited)" in out
    assert "audit run 1 IS voided" in err and "Do not re-run the void." in err
    assert '"event": "sentiment_audit_void_interrupted_after_commit"' in err
    assert '"event": "sentiment_audit_run_voided"' in err
    assert "Traceback" not in err
    notes, deliveries, run = _rows(db_path)
    assert (run.voided_at, run.void_reason) == (NOW, "graded twins twice")
    [note] = notes
    assert note.event == "sentiment_audit_voided"
    assert "massive: demoted -> unaudited" in note.body
    assert [(d.channel, d.status) for d in deliveries] == [
        ("bell", "delivered"),
        ("discord", "delivered"),
    ]
    assert len(recorder.posts) == 1


def _fail_before_the_commit(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    """Raise from inside the void, after the run is marked and before the
    commit: the archive step."""

    def broken(*_args: object, **_kwargs: object) -> int:
        raise failure

    monkeypatch.setattr(audit_void, "_archive_outcomes", broken)


def _nothing_written_or_sent(db_path: Path, recorder: Recorder) -> None:
    notes, deliveries, run = _rows(db_path)
    assert (notes, deliveries, run.voided_at, run.void_reason) == ([], [], None, None)
    assert recorder.posts == []


def test_a_void_that_fails_before_its_commit_exits_4(
    script: ModuleType,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_before_the_commit(monkeypatch, RuntimeError("SYNTHETIC archive failure"))
    recorder = Recorder(204)
    code = _main(
        script,
        _ARGV,
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == script.EXIT_VOID_FAILED == 4
    err = capsys.readouterr().err
    assert "was rolled back" in err and "nothing was written and nothing was sent" in err
    _nothing_written_or_sent(db_path, recorder)


def test_a_ctrl_c_before_the_commit_says_it_rolled_back_and_is_re_raised(
    script: ModuleType,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read back, the void did not land: that is said, and the interrupt is
    the operator's, so it propagates rather than becoming an exit status."""
    _fail_before_the_commit(monkeypatch, KeyboardInterrupt())
    recorder = Recorder(204)
    with pytest.raises(KeyboardInterrupt):
        script.main(
            _ARGV,
            clock=lambda: NOW,
            environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
            transport=httpx.MockTransport(recorder),
        )
    err = capsys.readouterr().err
    assert "was rolled back" in err and "nothing was written and nothing was sent" in err
    _nothing_written_or_sent(db_path, recorder)


def test_a_void_whose_outcome_cannot_be_read_back_exits_5(
    script: ModuleType,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither "voided" (3) nor "rolled back" (4) can be claimed when the read
    back itself fails, so it says which row to check instead."""
    _fail_before_the_commit(monkeypatch, RuntimeError("SYNTHETIC archive failure"))

    def unreadable(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("SYNTHETIC: the database cannot be read")

    monkeypatch.setattr(script, "read_back_void", unreadable)
    recorder = Recorder(204)
    code = _main(
        script,
        _ARGV,
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == script.EXIT_UNKNOWN == 5
    err = capsys.readouterr().err
    assert "could not be read back" in err and "audit_run 1" in err
    assert "Traceback" not in err
    _nothing_written_or_sent(db_path, recorder)


@pytest.mark.parametrize(
    "url",
    ["not a database url", "sqlite+nosuchdriver:///{path}"],
    ids=["unparseable", "unknown_driver"],
)
def test_a_database_url_that_cannot_be_set_up_exits_2(
    script: ModuleType,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    """F3: a malformed ``COROLLARY_DATABASE_URL`` used to escape as
    ``ArgumentError``, exit 1, which says "refused". Any failure before the
    void opens the database is bad input: exit 2, with the variable named
    and the URL itself not echoed (it may carry a password, rule 6)."""
    configured = url.format(path=db_path.as_posix())
    monkeypatch.setenv(DATABASE_URL_ENV, configured)
    recorder = Recorder(204)
    code = _main(
        script,
        _ARGV,
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == script.EXIT_BAD_INPUT == 2
    err = capsys.readouterr().err
    assert DATABASE_URL_ENV in err and "Nothing was opened" in err
    assert configured not in err and "Traceback" not in err
    _nothing_written_or_sent(db_path, recorder)


def _interrupt_after_the_commit(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, interruption: type[BaseException]
) -> None:
    """The void commits, then ``interruption`` is raised before it returns."""
    real = script.void_audit_run

    def interrupted_after_the_commit(*args: object, **kwargs: object) -> object:
        real(*args, **kwargs)
        raise interruption("SYNTHETIC: interrupted after the commit")

    monkeypatch.setattr(script, "void_audit_run", interrupted_after_the_commit)


def _voided_and_unsent(db_path: Path, recorder: Recorder) -> None:
    notes, deliveries, run = _rows(db_path)
    assert (run.voided_at, run.void_reason) == (NOW, "graded twins twice")
    assert (notes, deliveries, recorder.posts) == ([], [], [])


@pytest.mark.parametrize("first", [KeyboardInterrupt, RuntimeError])
def test_a_second_ctrl_c_during_the_read_back_says_check_voided_at_and_propagates(
    script: ModuleType,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    first: type[BaseException],
) -> None:
    """E2: the read back was guarded by ``except Exception`` only, so a second
    Ctrl-C there escaped ``main`` with the void landed and nothing said: no
    "check voided_at", and a re-run exits 1 (``already_voided``), so the
    notification is never sent. Whether the void landed is then unknown, so
    exit 5's sentence is printed before the interrupt propagates."""
    _interrupt_after_the_commit(script, monkeypatch, first)

    def second_ctrl_c(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(script, "read_back_void", second_ctrl_c)
    recorder = Recorder(204)
    code = _main(
        script,
        _ARGV,
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == "a KeyboardInterrupt escaped main"
    err = capsys.readouterr().err
    assert "could not be read back (KeyboardInterrupt)" in err
    assert "Check audit_run 1's voided_at" in err
    assert '"event": "sentiment_audit_void_outcome_unknown"' in err
    _voided_and_unsent(db_path, recorder)


def test_a_second_ctrl_c_in_the_post_commit_log_says_the_run_is_voided_and_exits_3(
    script: ModuleType,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2: the "interrupted after its commit" log line sat outside every
    ``try``, so a Ctrl-C there escaped ``main`` with the void read back as
    landed and nothing said. It is inside the ``try`` that guards
    ``_after_commit`` now: the run IS voided, exit 3, do not re-run."""
    _interrupt_after_the_commit(script, monkeypatch, KeyboardInterrupt)

    class CtrlCOnTheLogLine(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if getattr(record, "event", None) == "sentiment_audit_void_interrupted_after_commit":
                raise KeyboardInterrupt
            return True

    tripwire = CtrlCOnTheLogLine()
    script._LOGGER.addFilter(tripwire)
    try:
        recorder = Recorder(204)
        code = _main(
            script,
            _ARGV,
            clock=lambda: NOW,
            environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
            transport=httpx.MockTransport(recorder),
        )
    finally:
        script._LOGGER.removeFilter(tripwire)
    assert code == script.EXIT_NOT_DELIVERED == 3
    err = capsys.readouterr().err
    assert "audit run 1 IS voided" in err and "Do not re-run the void." in err
    assert '"event": "sentiment_audit_void_after_commit_failed"' in err
    _voided_and_unsent(db_path, recorder)


class _CtrlCOnEvent(logging.Filter):
    """A Ctrl-C delivered while one log line is being emitted."""

    def __init__(self, event: str) -> None:
        super().__init__()
        self.event = event

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "event", None) == self.event:
            raise KeyboardInterrupt
        return True


def test_a_ctrl_c_on_the_voided_but_log_line_still_says_the_run_is_voided(
    script: ModuleType,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: ``_voided_but`` logged first, guarded by ``except Exception`` only,
    so a second Ctrl-C on its log call skipped "IS voided ... Do not re-run"
    -- the one sentence that stops a re-run. The sentence goes out first now,
    and the Ctrl-C is re-raised once it is out."""

    async def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("SYNTHETIC failure while notifying")

    monkeypatch.setattr(script, "notify", broken)
    tripwire = _CtrlCOnEvent("sentiment_audit_void_after_commit_failed")
    script._LOGGER.addFilter(tripwire)
    try:
        recorder = Recorder(204)
        code = _main(
            script,
            _ARGV,
            clock=lambda: NOW,
            environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
            transport=httpx.MockTransport(recorder),
        )
    finally:
        script._LOGGER.removeFilter(tripwire)
    assert code == "a KeyboardInterrupt escaped main"
    err = capsys.readouterr().err
    assert "audit run 1 IS voided" in err and "Do not re-run the void." in err
    assert "(RuntimeError)" in err
    _voided_and_unsent(db_path, recorder)


def test_a_ctrl_c_on_the_outcome_unknown_log_line_still_says_check_voided_at(
    script: ModuleType,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: ``_outcome_unknown`` logged first, guarded by ``except Exception``
    only, so a Ctrl-C on its log call skipped "check ``voided_at``" with the
    void landed -- and a re-run is refused ``already_voided``, so nothing
    would ever send the notification. The sentence goes out first now, and
    the Ctrl-C is re-raised once it is out."""
    _interrupt_after_the_commit(script, monkeypatch, RuntimeError)

    def unreadable(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("SYNTHETIC: the database cannot be read")

    monkeypatch.setattr(script, "read_back_void", unreadable)
    tripwire = _CtrlCOnEvent("sentiment_audit_void_outcome_unknown")
    script._LOGGER.addFilter(tripwire)
    try:
        recorder = Recorder(204)
        code = _main(
            script,
            _ARGV,
            clock=lambda: NOW,
            environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
            transport=httpx.MockTransport(recorder),
        )
    finally:
        script._LOGGER.removeFilter(tripwire)
    assert code == "a KeyboardInterrupt escaped main"
    err = capsys.readouterr().err
    assert "could not be read back (RuntimeError)" in err
    assert "Check audit_run 1's voided_at" in err
    _voided_and_unsent(db_path, recorder)


class _BrokenStream(io.TextIOBase):
    """A console that can no longer be written: every write raises."""

    encoding = "utf-8"

    def write(self, _text: str) -> int:
        raise OSError("SYNTHETIC: the console is gone")


@pytest.mark.parametrize(
    ("readable", "expected"),
    [(False, 5), (True, 4)],
    ids=["unreadable_exits_5", "not_landed_exits_4"],
)
def test_a_broken_stderr_after_a_failed_void_keeps_its_exit_status(
    script: ModuleType,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    readable: bool,
    expected: int,
) -> None:
    """E4: the two sentences of a failed void were printed unguarded, so a
    stderr that could not be written escaped ``main`` as Python's default
    status 1, which says "refused". On the unreadable path the void may have
    landed. Each keeps its own status now."""
    _fail_before_the_commit(monkeypatch, RuntimeError("SYNTHETIC archive failure"))
    if not readable:

        def unreadable(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("SYNTHETIC: the database cannot be read")

        monkeypatch.setattr(script, "read_back_void", unreadable)
    monkeypatch.setattr(sys, "stderr", _BrokenStream())
    recorder = Recorder(204)
    code = _main(
        script,
        _ARGV,
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == expected
    _nothing_written_or_sent(db_path, recorder)


def test_a_broken_stderr_on_a_refusal_still_exits_1(
    script: ModuleType, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal's sentence is guarded too, so 1 still says "refused" when
    the console cannot take it, rather than a stray error deciding."""
    monkeypatch.setattr(sys, "stderr", _BrokenStream())
    recorder = Recorder(204)
    code = _main(
        script,
        ["--run-id", "99", "--reason", "x"],
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == script.EXIT_REFUSED == 1
    _nothing_written_or_sent(db_path, recorder)


@pytest.mark.parametrize(
    "url",
    ["not a database url", "sqlite+nosuchdriver:///{path}", "sqlite:///{path}.missing"],
    ids=["unparseable", "unknown_driver", "missing_file"],
)
def test_a_broken_stderr_on_bad_input_still_exits_2(
    script: ModuleType,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    """E4: bad input says so on stderr. A stderr that cannot take it must
    not turn 2 ("nothing was opened") into the 5 of an unknown outcome, nor
    into Python's 1."""
    monkeypatch.setenv(DATABASE_URL_ENV, url.format(path=db_path.as_posix()))
    monkeypatch.setattr(sys, "stderr", _BrokenStream())
    recorder = Recorder(204)
    code = _main(
        script,
        _ARGV,
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == script.EXIT_BAD_INPUT == 2
    _nothing_written_or_sent(db_path, recorder)


def test_any_stray_exception_exits_5_never_1(
    script: ModuleType,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E4: Python's own status for an uncaught ``Exception`` is 1, the
    status that says "refused, nothing written". ``main`` maps anything that
    escapes its handling to 5 instead, with the row to check."""
    _interrupt_after_the_commit(script, monkeypatch, RuntimeError)

    def a_bug(*_args: object, **_kwargs: object) -> int:
        raise LookupError("SYNTHETIC: a bug in the handler")

    monkeypatch.setattr(script, "_void_raised", a_bug)
    recorder = Recorder(204)
    code = _main(
        script,
        _ARGV,
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == script.EXIT_UNKNOWN == 5
    err = capsys.readouterr().err
    assert "(LookupError)" in err and "Check audit_run 1's voided_at" in err
    assert "SYNTHETIC" not in err  # class name only (rule 6)
    assert '"event": "sentiment_audit_void_unhandled"' in err  # the JSON line too
    _voided_and_unsent(db_path, recorder)


def test_a_stray_exception_with_a_broken_stderr_still_exits_5(
    script: ModuleType, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E4: the guard's own sentence is guarded. A console that cannot take
    it leaves the status to say it, rather than escaping as 1."""
    _interrupt_after_the_commit(script, monkeypatch, RuntimeError)

    def a_bug(*_args: object, **_kwargs: object) -> int:
        raise LookupError("SYNTHETIC: a bug in the handler")

    monkeypatch.setattr(script, "_void_raised", a_bug)
    monkeypatch.setattr(sys, "stderr", _BrokenStream())
    recorder = Recorder(204)
    code = _main(
        script,
        _ARGV,
        clock=lambda: NOW,
        environ={DISCORD_WEBHOOK_ENV: WEBHOOK},
        transport=httpx.MockTransport(recorder),
    )
    assert code == script.EXIT_UNKNOWN == 5
    _voided_and_unsent(db_path, recorder)
