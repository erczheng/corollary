"""``scripts/backfill_verdicts.py`` (owner decision Q25 (b), once), on a temp database.

The database is a file under ``tmp_path`` built from the ORM metadata, found
the way the app finds its own -- ``COROLLARY_DATABASE_URL``. The vendors are
injected (``vendors``) and the clock too (``now``), so no test opens a
socket or reads a key. What is pinned: the absolute database path is the
first line; the script refuses while a regular session is open, per the
market calendar, and exits nonzero having asked nothing; ``--dry-run``
prints the need and makes no vendor request at all; a live run fills at most
``--max-pairs`` pairs, oldest first, prints needed / attempted / stored /
held / faults, and writes no ``audit_run`` row and no grade. All data is
SYNTHETIC.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import sys
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corollary.db.models import AuditRun, Base, LabelOutcome
from corollary.db.seed import seed
from corollary.db.session import DATABASE_URL_ENV, create_db_engine, sqlite_url
from tests.api.news_support import make_seed
from corollary.data.providers.alpaca import CredentialsError
from corollary.data.providers.finnhub import FINNHUB_API_KEY_ENV
from tests.data.news.test_audit_backfill import FakeInputs, _stored
from tests.data.news.test_audit_runner import MONDAY, MONDAY_10, _label
from tests.data.news.test_tradeability_cache import directory

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_verdicts.py"
UTC = timezone.utc
#: Saturday 21 Nov 2026 10:00 ET: no session.
SATURDAY_10 = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)
#: Monday 16 Nov 2026 11:00 ET: a regular session is open.
MONDAY_11 = datetime(2026, 11, 16, 16, 0, tzinfo=UTC)
#: Monday 16 Nov 2026 17:00 ET: after the close.
MONDAY_EVENING = datetime(2026, 11, 16, 22, 0, tzinfo=UTC)
TUESDAY = date(2026, 11, 17)
#: Monday 23 Nov 2026 09:20 ET: before the open.
MONDAY_PRE_OPEN = datetime(2026, 11, 23, 14, 20, tzinfo=UTC)
#: Monday 23 Nov 2026 09:31 ET: the session is open.
MONDAY_OPENED = datetime(2026, 11, 23, 14, 31, tzinfo=UTC)


def _load(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the script by path; its ``sys.path`` edit is undone at teardown."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("backfill_verdicts", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "backfill_verdicts", module)
    spec.loader.exec_module(module)
    return module


@dataclasses.dataclass
class FakeAlpaca(FakeInputs):
    """The two tradeability requests (recorded) plus the asset list."""

    directory_calls: int = 0
    #: Called after each daily-bars request: a test moves its clock here.
    after_bars: Callable[[], None] | None = None

    async def active_equities(self) -> Any:
        self.directory_calls += 1
        return directory("XA", "XB", "XC")

    async def adv_daily_bars(self, symbols: Any, *, session_date: date) -> Any:
        answer = await super().adv_daily_bars(symbols, session_date=session_date)
        if self.after_bars is not None:
            self.after_bars()
        return answer


class FakeIpoDates:
    """An IPO-date source that knows no date (every window here is full anyway)."""

    async def ipo_date(self, symbol: str) -> date | None:
        return None


@dataclasses.dataclass
class FakeVendors:
    alpaca: FakeAlpaca = dataclasses.field(default_factory=FakeAlpaca)
    ipo_dates: FakeIpoDates | None = dataclasses.field(default_factory=FakeIpoDates)
    position_error: BaseException | None = None
    position_reads: int = 0
    closed: bool = False

    async def positions(self) -> Iterable[str]:
        self.position_reads += 1
        if self.position_error is not None:
            raise self.position_error
        return ("TSLA",)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "corollary.db"
    engine = create_db_engine(sqlite_url(path))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
        _label(session, ticker="XC", source="rules", direction="bullish", at=MONDAY_10 + timedelta(days=1))
        _label(session, ticker="XB", source="rules", direction="bullish", at=MONDAY_10)
        _label(session, ticker="XA", source="rules", direction="bullish", at=MONDAY_10)
        session.commit()
    engine.dispose()
    monkeypatch.setenv(DATABASE_URL_ENV, sqlite_url(path))
    return path


def _main(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    *,
    now: datetime | Callable[[], datetime],
    vendors: FakeVendors | None = None,
    factory: Callable[[], FakeVendors] | None = None,
    app_running: bool | Callable[[], bool] = False,
) -> int:
    script = _load(monkeypatch)

    def default_factory() -> FakeVendors:
        if vendors is None:
            raise AssertionError("no vendor may be built on this path")
        return vendors

    clock = now if callable(now) else (lambda: now)
    result: int = script.main(
        argv,
        now=clock,
        vendors=factory or default_factory,
        seed_loader=make_seed,
        app_running=app_running if callable(app_running) else (lambda: app_running),
    )
    return result


def _verdicts(path: Path) -> list[tuple[str, date]]:
    engine = create_db_engine(sqlite_url(path))
    try:
        return [(t, d) for t, d, *_ in _stored(engine)]
    finally:
        engine.dispose()


def _json_lines(err: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for line in err.splitlines():
        if line.startswith("{"):
            lines.append(json.loads(line))
    return lines


def _counts(path: Path) -> tuple[int, int]:
    engine = create_db_engine(sqlite_url(path))
    try:
        with Session(engine) as session:
            runs = session.scalar(select(func.count()).select_from(AuditRun)) or 0
            grades = session.scalar(select(func.count()).select_from(LabelOutcome)) or 0
        return runs, grades
    finally:
        engine.dispose()


def test_it_refuses_while_a_regular_session_is_open(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vendors = FakeVendors()
    code = _main(monkeypatch, ["--max-pairs", "10"], now=MONDAY_11, vendors=vendors)
    out, err = capsys.readouterr()
    assert code != 0
    assert out.splitlines()[0] == f"database: {database.resolve()}"
    assert "session" in err and "2026-11-16" in err and "rule" in err
    assert vendors.position_reads == 0 and vendors.alpaca.bar_calls == []


def test_after_the_close_it_is_not_refused(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The calendar decides, not a fixed hour: Monday 17:00 ET is off-hours."""
    code = _main(monkeypatch, ["--max-pairs", "10", "--dry-run"], now=MONDAY_EVENING)
    assert code == 0


def test_a_dry_run_prints_the_need_and_makes_no_vendor_request(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ``vendors=None``: building one raises, so any vendor use fails the test.
    code = _main(monkeypatch, ["--max-pairs", "2", "--dry-run"], now=SATURDAY_10)
    out, _ = capsys.readouterr()
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == f"database: {database.resolve()}"
    assert "needed: 3" in out
    assert "attempted: 0" in out
    assert _counts(database) == (0, 0)


def test_a_live_run_fills_at_most_max_pairs_oldest_first(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vendors = FakeVendors()
    code = _main(monkeypatch, ["--max-pairs", "2"], now=SATURDAY_10, vendors=vendors)
    out, _ = capsys.readouterr()
    assert code == 0
    assert out.splitlines()[0] == f"database: {database.resolve()}"
    assert vendors.alpaca.bar_calls == [(("XA", "XB"), MONDAY)]
    assert vendors.alpaca.directory_calls == 1
    for line in ("needed: 3", "attempted: 2", "stored: 2", "held: 1", "faults: 0"):
        assert line in out, line
    # Verdicts only: the next audit run grades them.
    assert _counts(database) == (0, 0)
    assert vendors.closed


def test_a_second_live_run_takes_the_remainder(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _main(monkeypatch, ["--max-pairs", "2"], now=SATURDAY_10, vendors=FakeVendors())
    capsys.readouterr()
    vendors = FakeVendors()
    _main(monkeypatch, ["--max-pairs", "2"], now=SATURDAY_10, vendors=vendors)
    out, _ = capsys.readouterr()
    assert vendors.alpaca.bar_calls == [(("XC",), TUESDAY)]
    assert "needed: 1" in out and "stored: 1" in out


def test_max_pairs_must_be_positive(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SystemExit) as raised:
        _main(monkeypatch, ["--max-pairs", "0"], now=SATURDAY_10)
    assert raised.value.code == 2


def test_the_script_reads_no_env_file() -> None:
    """Keys come from the launcher's --env-file, never from the script."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "load_dotenv" not in text and "dotenv" not in text
    assert 'open(".env"' not in text


def test_asyncio_is_not_left_running(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _main(monkeypatch, ["--max-pairs", "1", "--dry-run"], now=SATURDAY_10)
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()


def test_a_dry_run_refuses_while_a_regular_session_is_open(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _main(monkeypatch, ["--max-pairs", "10", "--dry-run"], now=MONDAY_11)
    _, err = capsys.readouterr()
    assert code == 3
    assert "2026-11-16" in err and "rule" in err


def test_a_drain_that_reaches_the_open_stops_before_the_next_session(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Audit M1: launched at 09:20 ET, the clock passes 09:30 during Monday's refresh.

    Monday's pairs are stored and kept; Tuesday's refresh is never asked.
    The refusal is logged with the rule, the session and the timestamp, and
    what was stored so far is printed.
    """
    clock = [MONDAY_PRE_OPEN]
    vendors = FakeVendors()

    def opened() -> None:
        clock[0] = MONDAY_OPENED

    vendors.alpaca.after_bars = opened
    code = _main(monkeypatch, ["--max-pairs", "10"], now=lambda: clock[0], vendors=vendors)
    out, err = capsys.readouterr()
    assert code == 3
    assert vendors.alpaca.bar_calls == [(("XA", "XB"), MONDAY)]
    assert _verdicts(database) == [("XA", MONDAY), ("XB", MONDAY)]
    assert "stored: 2" in out and "held: 1" in out
    (refusal,) = [r for r in _json_lines(err) if r.get("event") == "backfill_script_session_opened"]
    assert refusal["rule"] and refusal["at"] == MONDAY_OPENED.isoformat()
    assert refusal["inputs"]["session_date"] == "2026-11-23"
    assert refusal["exit_code"] == 3
    assert vendors.closed


def test_it_refuses_while_the_app_answers_health(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Audit M2: one account, one 200/min ceiling -- stop the app first."""
    code = _main(monkeypatch, ["--max-pairs", "10"], now=SATURDAY_10, app_running=True)
    _, err = capsys.readouterr()
    assert code == 5
    assert "Stop it first" in err and "rule" in err and SATURDAY_10.isoformat() in err
    assert _verdicts(database) == []


def test_missing_alpaca_keys_are_a_documented_refusal_naming_the_variables_only(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Audit L5: not exit 1 and a traceback."""

    def no_keys() -> FakeVendors:
        raise CredentialsError("SYNTHETIC: no key pair")

    code = _main(monkeypatch, ["--max-pairs", "10"], now=SATURDAY_10, factory=no_keys)
    _, err = capsys.readouterr()
    assert code == 6
    assert "ALPACA_PAPER_API_KEY" in err and "ALPACA_PAPER_SECRET_KEY" in err
    assert "Traceback" not in err
    assert _verdicts(database) == []


def test_an_unreadable_position_book_refuses_with_its_rule_and_timestamp(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vendors = FakeVendors(position_error=RuntimeError("SYNTHETIC"))
    code = _main(monkeypatch, ["--max-pairs", "10"], now=SATURDAY_10, vendors=vendors)
    _, err = capsys.readouterr()
    assert code == 4
    (refusal,) = [r for r in _json_lines(err) if r.get("event") == "backfill_script_positions_unavailable"]
    assert refusal["rule"] and refusal["at"] == SATURDAY_10.isoformat()
    assert _verdicts(database) == []


def test_a_live_drain_without_an_ipo_date_source_refuses_and_writes_nothing(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Audit L6: fail-closed ipo_date_unavailable for a past session is permanent."""
    vendors = FakeVendors(ipo_dates=None)
    code = _main(monkeypatch, ["--max-pairs", "10"], now=SATURDAY_10, vendors=vendors)
    _, err = capsys.readouterr()
    assert code == 7
    assert FINNHUB_API_KEY_ENV in err and "rule" in err
    assert vendors.position_reads == 0 and vendors.alpaca.bar_calls == []
    assert _verdicts(database) == []


@pytest.mark.parametrize("configured", [True, False])
def test_a_dry_run_says_whether_an_ipo_date_source_is_configured(
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    configured: bool,
) -> None:
    if configured:
        monkeypatch.setenv(FINNHUB_API_KEY_ENV, "SYNTHETIC-not-a-key")
    else:
        monkeypatch.delenv(FINNHUB_API_KEY_ENV, raising=False)
    code = _main(monkeypatch, ["--max-pairs", "2", "--dry-run"], now=SATURDAY_10)
    out, _ = capsys.readouterr()
    assert code == 0
    expected = "IPO-date source: configured" if configured else "IPO-date source: NOT configured"
    assert expected in out
    assert "SYNTHETIC-not-a-key" not in out


def test_the_backfills_own_lines_are_emitted_as_json(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Audit L5: a handler is installed, so the structured lines are not lost."""
    code = _main(monkeypatch, ["--max-pairs", "2"], now=SATURDAY_10, vendors=FakeVendors())
    _, err = capsys.readouterr()
    assert code == 0
    summary = [r for r in _json_lines(err) if r.get("event") == "sentiment_audit_verdict_backfill"]
    assert len(summary) == 1
    assert summary[0]["caller"] == "backfill_script" and summary[0]["correlation_id"]


def test_an_app_that_starts_mid_drain_stops_it_before_the_next_session(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Q25 re-audit 5: weekday_session.py (08:30) or weekly_audit.py can launch mid-drain.

    Nothing answers at launch; the app answers once Monday's refresh has
    asked its bars. Monday's pairs are kept, Tuesday's refresh is never
    asked, and the stop is exit 5 with the partial counts, logged with the
    rule, the inputs and the timestamp.
    """
    answering = [False]
    probes: list[bool] = []

    def app_running() -> bool:
        probes.append(answering[0])
        return answering[0]

    vendors = FakeVendors()

    def started() -> None:
        answering[0] = True

    vendors.alpaca.after_bars = started
    code = _main(
        monkeypatch, ["--max-pairs", "10"], now=SATURDAY_10, vendors=vendors, app_running=app_running
    )
    out, err = capsys.readouterr()
    assert code == 5
    assert vendors.alpaca.bar_calls == [(("XA", "XB"), MONDAY)]
    assert _verdicts(database) == [("XA", MONDAY), ("XB", MONDAY)]
    assert "stored: 2" in out and "held: 1" in out
    # Once at launch, then once before each session -- never once per pair.
    assert probes == [False, False, True]
    (refusal,) = [r for r in _json_lines(err) if r.get("event") == "backfill_script_app_started"]
    assert refusal["rule"] and refusal["at"] == SATURDAY_10.isoformat()
    assert refusal["exit_code"] == 5
    assert refusal["inputs"]["stored"] == 2 and refusal["inputs"]["held"] == 1
    assert vendors.closed


def test_an_unexpected_error_is_a_documented_exit_with_no_traceback(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Q25 re-audit 6: its type and the rule, never its text, which may carry vendor data."""
    script = _load(monkeypatch)

    def broken(*_: object, **__: object) -> object:
        raise RuntimeError("SYNTHETIC-secret-looking-text")

    monkeypatch.setattr(script, "load_verdicts_needed", broken)
    vendors = FakeVendors()
    code = script.main(
        ["--max-pairs", "10"],
        now=lambda: SATURDAY_10,
        vendors=lambda: vendors,
        seed_loader=make_seed,
        app_running=lambda: False,
    )
    out, err = capsys.readouterr()
    assert code == script.EXIT_FAILED == 8
    assert "Traceback" not in err and "SYNTHETIC-secret-looking-text" not in err + out
    (refusal,) = [r for r in _json_lines(err) if r.get("event") == "backfill_script_failed"]
    assert refusal["inputs"]["error_type"] == "RuntimeError"
    assert refusal["rule"] and refusal["at"] == SATURDAY_10.isoformat()
    assert vendors.closed
    assert "8 --" in (script.__doc__ or "")
    # The exit-8 line carries the drain's correlation id, so it can be matched
    # to the backfill lines the drain wrote before it failed.
    correlation_id = refusal["inputs"]["correlation_id"]
    assert isinstance(correlation_id, str) and len(correlation_id) == 32


def test_an_error_before_the_drain_starts_is_exit_8_too(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Q25 re-audit nit: the set-up (database URL, calendar) is under the same handler.

    Before, anything raised ahead of ``asyncio.run`` escaped ``main`` as a
    traceback and exit 1 -- and a malformed ``COROLLARY_DATABASE_URL`` would
    have printed the URL.
    """
    script = _load(monkeypatch)

    def broken() -> str:
        raise ValueError("SYNTHETIC-url-looking-text")

    monkeypatch.setattr(script, "database_url", broken)
    built: list[FakeVendors] = []

    def make_vendors() -> FakeVendors:
        built.append(FakeVendors())
        return built[-1]

    code = script.main(
        ["--max-pairs", "10"],
        now=lambda: SATURDAY_10,
        vendors=make_vendors,
        seed_loader=make_seed,
        app_running=lambda: False,
    )
    out, err = capsys.readouterr()
    assert code == script.EXIT_FAILED == 8
    assert "Traceback" not in err and "SYNTHETIC-url-looking-text" not in err + out
    (refusal,) = [r for r in _json_lines(err) if r.get("event") == "backfill_script_failed"]
    assert refusal["inputs"]["error_type"] == "ValueError"
    assert refusal["inputs"]["correlation_id"]
    assert built == []  # failed before any vendor was built
