"""Drain decision 27's missing tradeability verdicts once, off-hours (owner decision Q25 (b)).

    uv run --env-file .env python scripts/backfill_verdicts.py --max-pairs 2200 --dry-run
    uv run --env-file .env python scripts/backfill_verdicts.py --max-pairs 2200

**Stop the app first.** Alpaca's 200 requests a minute is per *account*, and
this script builds its own provider, whose limiter alone admits 190 a rolling
minute on top of whatever the app is spending -- the account reads, the
positions refresher, the tradeability job and its own backfill. Together they
draw 429s inside the app, and fifteen minutes of failed position reads reads
as a degraded watch universe. The audit's run lock is per process, so this
script cannot see an app's audit run either. So it **refuses** when anything
answers ``GET /api/health`` on 127.0.0.1:8000 (exit 5), the same check
``scripts/weekly_audit.py`` makes before it launches. It asks again **before
each session's refresh** -- once a session, never once a pair -- because
``scripts/weekday_session.py`` (08:30 ET) and ``scripts/weekly_audit.py``
(Saturday 09:45 ET) launch the headless app on their own schedules and can
do so mid-drain: it then stops there, prints what was stored so far, logs
the refusal with its rule, inputs and timestamp, and exits 5. What it stored
is kept and the rest is still pending.

**Why it exists.** The weekly audit backfills at most ``VERDICT_BACKFILL_CAP``
(100) missing verdicts a run. Live audit run 5 (2026-11-16) needed 2117
``(ticker, session)`` pairs across 1596 tickers, stored 100 and held 2017,
so the oldest held labels would pass decision 27's 28-day hold around Dec 1
and be written ``off_watch_unverdicted``, never graded. The owner chose to
raise the cap **once**, as this script, rather than permanently: the runner
keeps 100, so a start-up catch-up during market hours cannot spend the
``paper-api.`` bucket the order path will share. From Q25 on, the daily
``tradeability_cache`` job also fills the need, within its own bound.

**What it does.** It reads the need exactly as the audit reads it
(``audit_runner.load_verdicts_needed``, over the audit's own watch universe
from ``scheduler.sentiment_audit_deps``), and fills the first
``--max-pairs`` of it, oldest session first, through the audit's own backfill
(``audit_runner.backfill_verdicts``): one ``refresh_tradeability(session_date=S)``
per session, which reads only sessions strictly before S. A stale but
existing verdict on or before S is no need and is never replaced. Each
ticker's standard-root answer is asked once per invocation, since it is the
vendor's current answer for every session. It writes ``ticker_tradeability``
rows (and an IPO date where one is fetched) and **nothing else**: no
``audit_run`` row and no grade. The next audit run grades on what it stored.

**Requests.** One ``/v2/assets`` and one positions read on ``paper-api.``,
then at most one standard-root request per ticker, at most ``--max-pairs``;
bars on ``data.``, one per session per 200 tickers; IPO dates on Finnhub, at
most ``MAX_IPO_LOOKUPS_PER_RUN``. All through the providers' own rate
limiters.

**It never spends a request while a regular session is open**, per the market
calendar (``corollary.calendars.nyse_session_containing``), never fixed hours.
At launch it refuses, having asked nothing (exit 3). It asks again **before
each session's refresh**, so a drain launched at 09:22 stops at the first
session boundary after 09:30 rather than running on into the open: it logs
the refusal, prints what was stored so far, and exits 3. What it stored is
kept, and the rest is still pending -- run it again after the close.
``--dry-run`` refuses the same way at launch, so a dry run and the live run
that follows read the need under the same rule.

**It needs an IPO-date source** (``FINNHUB_API_KEY``). Without one a ticker
whose 20-session ADV window is partial is stored fail-closed
``ipo_date_unavailable`` for a past session, which is permanent for that
pair. So a live drain with none refuses, writing nothing (exit 7).
``--dry-run`` says whether one is configured.

**``--dry-run`` makes no vendor request at all.** It prints the need and
stops. The paper positions are a broker request, so a dry run does not read
them: a label on a held underlying that is not otherwise on watch counts as
needed there, and the count is an upper bound by at most those labels.

**Keys come only from the launcher**, ``uv run --env-file .env``. Nothing
here reads ``.env``. The database is ``COROLLARY_DATABASE_URL`` when set,
otherwise ``corollary.db`` in the working directory, and its absolute path
is the first line printed: several worktrees hold their own ``corollary.db``.
The backfill's own structured lines -- per-session failures, faults, the
stale-directory warning, each with its ``correlation_id`` -- are written to
stderr as JSON lines, one object a line, as is every refusal here.

Exit status, every refusal logged with its rule, its inputs and a timestamp:

* 0 -- success. Faults are printed, and their pairs stay pending for the
  next attempt.
* 2 -- a bad argument, or no database file.
* 3 -- a regular session is open: at launch, or reached during the drain.
* 4 -- the paper positions, the watch universe or the asset directory could
  not be read.
* 5 -- a Corollary app is running on 127.0.0.1:8000: at launch, or one that
  started during the drain; stop it first.
* 6 -- the Alpaca keys are not in the environment (launch with ``--env-file``).
* 7 -- no IPO-date source is configured (``FINNHUB_API_KEY``).
* 8 -- an unexpected error. Logged with its type and the rule, never its
  message or a traceback, which could carry vendor text. Every session's
  verdicts stored before it are kept; run it again.

No other status is returned: an ``Exception`` anywhere in the run is exit 8.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Final, Protocol, cast

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.engine import make_url  # noqa: E402

from corollary.calendars import nyse_session_containing  # noqa: E402
from corollary.data.news.audit_runner import (  # noqa: E402
    WatchUniverseUnavailable,
    backfill_verdicts,
    load_verdicts_needed,
)
from corollary.data.news.pollers import refresh_assets  # noqa: E402
from corollary.data.news.tradeability import IpoDateSource  # noqa: E402
from corollary.data.providers.alpaca import CredentialsError  # noqa: E402
from corollary.data.providers.finnhub import FINNHUB_API_KEY_ENV  # noqa: E402
from corollary.data.seeds import SpdrSeed  # noqa: E402
from corollary.data.seeds.nport import DatabaseSeedLoader  # noqa: E402
from corollary.db.session import (  # noqa: E402
    create_db_engine,
    database_url,
    session_factory,
    sqlite_url,
)
from corollary.engine.scheduler import (  # noqa: E402
    AlpacaContextSource,
    ContextServices,
    FinnhubNewsSource,
    HeldPositionUnderlyings,
    PositionUnderlyings,
    sentiment_audit_deps,
)

EXIT_BAD_INPUT: Final = 2
EXIT_SESSION_OPEN: Final = 3
EXIT_UNAVAILABLE: Final = 4
EXIT_APP_RUNNING: Final = 5
EXIT_CREDENTIALS_MISSING: Final = 6
EXIT_NO_IPO_SOURCE: Final = 7
EXIT_FAILED: Final = 8

#: Who asked, in every backfill log line (``audit_runner.backfill_verdicts``).
CALLER: Final = "backfill_script"

#: The app's health route, as ``scripts/weekly_audit.py`` reads it.
APP_HEALTH_URL: Final = "http://127.0.0.1:8000/api/health"
APP_HEALTH_TIMEOUT_SECONDS: Final = 2.0

SESSION_RULE: Final = (
    "owner decision Q25: the one-off verdict drain runs only outside a regular "
    "NYSE session (per the market calendar), so it never spends the paper-api "
    "bucket while the market is open; checked at launch and before each session's refresh"
)
APP_RULE: Final = (
    "Q25 audit M2: Alpaca's 200/min is per account, and this script's own limiter "
    "admits 190 a minute on top of the app's spend, so it refuses while an app answers "
    f"{APP_HEALTH_URL}; stop the app first"
)
CREDENTIALS_RULE: Final = (
    "rule 6: keys come only from the launcher's environment (uv run --env-file .env); "
    "a live drain needs ALPACA_PAPER_API_KEY and ALPACA_PAPER_SECRET_KEY"
)
IPO_RULE: Final = (
    "Q25 audit L6: without an IPO-date source a partial ADV window is stored "
    f"ipo_date_unavailable for a past session, permanently; a live drain needs {FINNHUB_API_KEY_ENV}"
)
FAILED_RULE: Final = (
    "Q25 re-audit finding 6: an unexpected error is a documented exit, logged with its "
    "type and never its message, which could carry vendor text; what was stored is kept"
)
UNAVAILABLE_RULE: Final = (
    "decision 27 / fix B: the need is read against the audit's own watch universe, "
    "and has_options against a held asset directory; without either it would be wrong"
)

_LOGGER: Final = logging.getLogger("corollary.scripts.backfill_verdicts")


class Vendors(Protocol):
    """What a live run asks: Alpaca, Finnhub's IPO dates, the paper positions."""

    @property
    def alpaca(self) -> AlpacaContextSource: ...

    @property
    def ipo_dates(self) -> IpoDateSource | None: ...

    async def positions(self) -> Iterable[str]: ...

    async def aclose(self) -> None: ...


@dataclass
class _EnvVendors:
    """The app's own providers, from the process environment (``--env-file``)."""

    alpaca: AlpacaContextSource
    ipo_dates: IpoDateSource | None
    _positions: Callable[[], Awaitable[Iterable[str]]]
    _aclose: Callable[[], Awaitable[None]]

    async def positions(self) -> Iterable[str]:
        return await self._positions()

    async def aclose(self) -> None:
        await self._aclose()


def _vendors_from_env() -> Vendors:
    """The registry the app builds, read for the three things a live run needs.

    Imported here, not at the top, so a refusal or a dry run never builds a
    client. ``positions`` is a read-only broker call on the **paper** book
    (rule 5), mapped to underlyings exactly as the app's refresher maps them.
    Raises :class:`CredentialsError` when the Alpaca keys are not set.
    """
    from corollary.api.deps import ServiceRegistry, position_underlying
    from corollary.api.schemas import AccountMode

    registry = ServiceRegistry.from_env()
    alpaca = cast(AlpacaContextSource, registry.provider)
    fundamentals = registry.fundamentals
    ipo_dates = (
        cast(IpoDateSource, fundamentals)
        if callable(getattr(fundamentals, "ipo_date", None))
        else None
    )

    async def positions() -> Iterable[str]:
        held = await registry.broker(AccountMode.PAPER).positions()
        return [position_underlying(row.symbol) for row in held]

    return _EnvVendors(alpaca, ipo_dates, positions, registry.aclose)


def _app_answers_health() -> bool:
    """Whether anything answers ``GET /api/health`` on 127.0.0.1:8000 -- any status at all."""
    import httpx

    try:
        httpx.get(APP_HEALTH_URL, timeout=APP_HEALTH_TIMEOUT_SECONDS)
    except httpx.HTTPError:
        return False
    return True


class _NoRequests:
    """The dry run's Alpaca: any use is a bug, and says so rather than asking."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"a dry run makes no vendor request (asked for {name!r})")


def _log_fields(record: logging.LogRecord) -> dict[str, object]:
    """The structured fields this script's lines and the backfill's carry, **by literal name**.

    Not ``vars(record)``: the rule 1 scan (``tests/test_hard_rules.py``)
    refuses ``vars``, ``__dict__`` and a computed ``getattr`` in every
    ``scripts/*.py``. A field not listed here appears only in the message.
    """
    fields: dict[str, object] = {
        "event": getattr(record, "event", None),
        "rule": getattr(record, "rule", None),
        "reason": getattr(record, "reason", None),
        "inputs": getattr(record, "inputs", None),
        "at": getattr(record, "at", None),
        "exit_code": getattr(record, "exit_code", None),
        "caller": getattr(record, "caller", None),
        "session_date": getattr(record, "session_date", None),
        "tickers": getattr(record, "tickers", None),
        "error_type": getattr(record, "error_type", None),
        "needed": getattr(record, "needed", None),
        "attempted": getattr(record, "attempted", None),
        "stored": getattr(record, "stored", None),
        "capped": getattr(record, "capped", None),
        "held": getattr(record, "held", None),
        "remaining": getattr(record, "remaining", None),
        "stopped": getattr(record, "stopped", None),
        "faults": getattr(record, "faults", None),
        "cap": getattr(record, "cap", None),
        "first_capped_session": getattr(record, "first_capped_session", None),
        "age_seconds": getattr(record, "age_seconds", None),
        "fetched_at": getattr(record, "fetched_at", None),
        "user": getattr(record, "user", None),
        "variable": getattr(record, "variable", None),
        "audit_run_id": getattr(record, "audit_run_id", None),
        "correlation_id": getattr(record, "correlation_id", None),
    }
    return {key: value for key, value in fields.items() if value is not None}


class _JsonLines(logging.Formatter):
    """One JSON object per line: the message and the structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "logged_at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                **_log_fields(record),
            },
            default=str,
        )


@contextmanager
def _json_logs_to_stderr() -> Iterator[None]:
    """``corollary.*`` at INFO as JSON lines on stderr, for this run only."""
    target = logging.getLogger("corollary")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonLines())
    level = target.level
    target.addHandler(handler)
    target.setLevel(logging.INFO)
    try:
        yield
    finally:
        target.removeHandler(handler)
        target.setLevel(level)


def _refuse(
    code: int,
    event: str,
    message: str,
    *,
    rule: str,
    at: datetime,
    inputs: dict[str, object] | None = None,
) -> int:
    """A refusal, printed for the operator and logged for the record: rule, inputs, timestamp."""
    print(f"refusing at {at.isoformat()}: {message} rule: {rule}", file=sys.stderr)
    _LOGGER.error(
        "refusing: %s",
        message,
        extra={
            "event": event,
            "rule": rule,
            "inputs": inputs or {},
            "at": at.isoformat(),
            "exit_code": code,
        },
    )
    return code


def _sqlite_file(url: str) -> Path | None:
    """The absolute path of the SQLite file ``url`` names, or ``None`` when it names none."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return None
    database = parsed.database
    if not database or database == ":memory:":
        return None
    return Path(database).resolve()


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {value}")
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _describe_need(needed: Sequence[tuple[str, date]]) -> str:
    if not needed:
        return "needed: 0 (ticker, session) pairs"
    tickers = len({ticker for ticker, _ in needed})
    first, last = needed[0][1], needed[-1][1]
    return (
        f"needed: {len(needed)} (ticker, session) pairs across {tickers} tickers, "
        f"sessions {first.isoformat()} to {last.isoformat()}"
    )


def _ipo_source_line() -> str:
    """The dry run's report on the IPO-date source: whether its key is set, never its value."""
    if (os.environ.get(FINNHUB_API_KEY_ENV) or "").strip():
        return f"IPO-date source: configured ({FINNHUB_API_KEY_ENV} is set)"
    return (
        f"IPO-date source: NOT configured ({FINNHUB_API_KEY_ENV} is not set); "
        f"a live run would refuse (exit {EXIT_NO_IPO_SOURCE})"
    )


async def _run(
    *,
    db_path: Path,
    max_pairs: int,
    dry_run: bool,
    now: Callable[[], datetime],
    vendors: Callable[[], Vendors],
    seed_loader: Callable[[], SpdrSeed | None] | None,
    app_running: Callable[[], bool],
    correlation_id: str,
) -> int:
    at = now()
    engine = create_db_engine(sqlite_url(db_path))
    sessions = session_factory(engine)
    built: Vendors | None = None
    inputs: dict[str, object] = {
        "max_pairs": max_pairs,
        "database": str(db_path),
        "correlation_id": correlation_id,
    }
    try:
        holder = PositionUnderlyings()
        if dry_run:
            holder.replace((), at=at)
            alpaca: AlpacaContextSource = cast(AlpacaContextSource, _NoRequests())
            ipo_dates: IpoDateSource | None = None
            print(
                "dry run: no vendor request is made; the paper positions are not read, "
                "so the need is an upper bound by at most the labels on held underlyings"
            )
            print(_ipo_source_line())
        else:
            try:
                built = vendors()
            except CredentialsError as exc:
                return _refuse(
                    EXIT_CREDENTIALS_MISSING,
                    "backfill_script_credentials_missing",
                    f"the Alpaca keys are not in the environment ({type(exc).__name__}). "
                    "Nothing was requested or written.",
                    rule=CREDENTIALS_RULE,
                    at=now(),
                    inputs=inputs,
                )
            if built.ipo_dates is None:
                return _refuse(
                    EXIT_NO_IPO_SOURCE,
                    "backfill_script_no_ipo_source",
                    f"no IPO-date source is configured ({FINNHUB_API_KEY_ENV} is not set). "
                    "Nothing was requested or written.",
                    rule=IPO_RULE,
                    at=now(),
                    inputs=inputs,
                )
            try:
                held = list(await built.positions())
            except CredentialsError as exc:
                return _refuse(
                    EXIT_CREDENTIALS_MISSING,
                    "backfill_script_credentials_missing",
                    f"the Alpaca paper keys are not in the environment ({type(exc).__name__}). "
                    "Nothing was written.",
                    rule=CREDENTIALS_RULE,
                    at=now(),
                    inputs=inputs,
                )
            except Exception as exc:
                return _refuse(
                    EXIT_UNAVAILABLE,
                    "backfill_script_positions_unavailable",
                    f"could not read the paper positions ({type(exc).__name__}); without "
                    "them the watch universe is degraded and the need would be wrong. "
                    "Nothing was written.",
                    rule=UNAVAILABLE_RULE,
                    at=now(),
                    inputs={**inputs, "error_type": type(exc).__name__},
                )
            holder.replace(held, at=at)
            alpaca, ipo_dates = built.alpaca, built.ipo_dates

        services = ContextServices(
            funds=_universe_funds(),
            session_factory=sessions,
            alpaca=alpaca,
            finnhub=cast(FinnhubNewsSource | None, ipo_dates),
            markets=_universe_markets(),
            position_underlyings=HeldPositionUnderlyings(holder),
            seed_loader=seed_loader if seed_loader is not None else DatabaseSeedLoader(sessions),
        )
        deps = sentiment_audit_deps(services, clock=now)
        assert deps is not None and deps.backfill is not None  # alpaca is never None here
        try:
            members = await deps.watch_universe()
        except WatchUniverseUnavailable as exc:
            return _refuse(
                EXIT_UNAVAILABLE,
                "backfill_script_watch_universe_degraded",
                f"the watch universe is degraded ({exc}); labels would be classified "
                "against the wrong universe. Nothing was written.",
                rule=UNAVAILABLE_RULE,
                at=now(),
                inputs=inputs,
            )
        needed = await asyncio.to_thread(
            load_verdicts_needed, sessions, members, at, correlation_id
        )
        print(_describe_need(needed))
        if dry_run:
            print(f"attempted: 0 (a live run would attempt {min(len(needed), max_pairs)})")
            return 0

        try:
            await refresh_assets(services.assets, alpaca)
        except Exception as exc:
            return _refuse(
                EXIT_UNAVAILABLE,
                "backfill_script_asset_directory_unavailable",
                f"could not fetch the asset directory ({type(exc).__name__}); has_options "
                "would be unknown. Nothing was written.",
                rule=UNAVAILABLE_RULE,
                at=now(),
                inputs={**inputs, "error_type": type(exc).__name__},
            )

        stops: list[_Stop] = []

        def stop_check() -> str | None:
            """Asked before each session's refresh: the calendar, then the app."""
            moment = now()
            open_session = nyse_session_containing(moment)
            if open_session is not None:
                stops.append(_Stop("session", moment, open_session))
                return f"the regular NYSE session of {open_session.isoformat()} is open"
            if app_running():
                stops.append(_Stop("app", moment, None))
                return f"a Corollary app now answers {APP_HEALTH_URL}"
            return None

        outcome = await backfill_verdicts(
            deps.backfill,
            needed,
            session_factory=sessions,
            clock=now,
            max_pairs=max_pairs,
            caller=CALLER,
            correlation_id=correlation_id,
            stop=stop_check,
        )
        print(f"attempted: {outcome.attempted}")
        print(f"stored: {len(outcome.stored)}")
        print(f"held: {len(outcome.held)}")
        print(f"faults: {len(outcome.faults)}")
        for reason, count in sorted(Counter(f.reason for f in outcome.faults).items()):
            print(f"  {reason}: {count}")
        print("no audit_run row and no grade written; the next audit run grades these")
        if outcome.stopped is not None:
            stop = stops[-1]
            partial: dict[str, object] = {
                **inputs,
                "attempted": outcome.attempted,
                "stored": len(outcome.stored),
                "held": len(outcome.held),
                "correlation_id": correlation_id,
            }
            if stop.kind == "app":
                return _refuse(
                    EXIT_APP_RUNNING,
                    "backfill_script_app_started",
                    f"{outcome.stopped} at {stop.at.isoformat()}, so the app and this drain "
                    "would spend the same account's Alpaca budget. The drain stopped before the "
                    f"next session's refresh; {len(outcome.stored)} stored so far are kept and the "
                    "rest are still pending. Stop the app and run it again.",
                    rule=APP_RULE,
                    at=stop.at,
                    inputs=partial,
                )
            assert stop.session is not None  # a session stop names its session
            return _refuse(
                EXIT_SESSION_OPEN,
                "backfill_script_session_opened",
                f"{outcome.stopped} at {stop.at.isoformat()} "
                "(corollary.calendars.nyse_session_containing). The drain stopped before the "
                f"next session's refresh; {len(outcome.stored)} stored so far are kept and the "
                "rest are still pending. Run it again after the close.",
                rule=SESSION_RULE,
                at=stop.at,
                inputs={**partial, "session_date": stop.session.isoformat()},
            )
        return 0
    finally:
        if built is not None:
            await built.aclose()
        engine.dispose()


@dataclass(frozen=True)
class _Stop:
    """Why the drain's stop check ended it: the calendar's session, or an app that answered."""

    kind: str  # "session" or "app"
    at: datetime
    session: date | None


def _universe_markets() -> tuple[str, ...]:
    from corollary.api.routes.markets import UNIVERSE_SYMBOLS

    return UNIVERSE_SYMBOLS


def _universe_funds() -> tuple[str, ...]:
    from corollary.api.routes.markets import UNIVERSE_FUND_SYMBOLS

    return UNIVERSE_FUND_SYMBOLS


def main(
    argv: Sequence[str] | None = None,
    *,
    now: Callable[[], datetime] = _utc_now,
    vendors: Callable[[], Vendors] = _vendors_from_env,
    seed_loader: Callable[[], SpdrSeed | None] | None = None,
    app_running: Callable[[], bool] = _app_answers_health,
) -> int:
    """Run the drain. ``now``, ``vendors``, ``seed_loader`` and ``app_running`` are injectable for tests."""
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        epilog=(
            "Opens COROLLARY_DATABASE_URL, or corollary.db in the working directory, and "
            "prints its absolute path first. Keys come from `uv run --env-file .env` only. "
            "Refuses while a regular NYSE session is open, and while the app answers on "
            "127.0.0.1:8000 -- stop the app first. Exit codes are in the module docstring."
        ),
    )
    parser.add_argument(
        "--max-pairs",
        required=True,
        type=_positive,
        metavar="N",
        help="fill at most N (ticker, session) pairs, the oldest session first",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the need and stop; makes no vendor request and writes nothing",
    )
    args = parser.parse_args(argv)
    max_pairs: int = args.max_pairs
    dry_run: bool = args.dry_run

    with _json_logs_to_stderr():
        correlation_id = uuid.uuid4().hex
        try:
            return _drain(
                max_pairs=max_pairs,
                dry_run=dry_run,
                now=now,
                vendors=vendors,
                seed_loader=seed_loader,
                app_running=app_running,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            # The type only: an exception's text can carry vendor data, and a
            # traceback from a malformed COROLLARY_DATABASE_URL would print it.
            # This covers the set-up before the drain as well as the drain.
            return _refuse(
                EXIT_FAILED,
                "backfill_script_failed",
                f"the drain failed with {type(exc).__name__}; any session's verdicts "
                "stored before it are kept and the rest are still pending. Run it again.",
                rule=FAILED_RULE,
                at=now(),
                inputs={
                    "max_pairs": max_pairs,
                    "dry_run": dry_run,
                    "correlation_id": correlation_id,
                    "error_type": type(exc).__name__,
                },
            )


def _drain(
    *,
    max_pairs: int,
    dry_run: bool,
    now: Callable[[], datetime],
    vendors: Callable[[], Vendors],
    seed_loader: Callable[[], SpdrSeed | None] | None,
    app_running: Callable[[], bool],
    correlation_id: str,
) -> int:
    """Everything after argument parsing, under :func:`main`'s exit-8 handler."""
    url = database_url()
    path = _sqlite_file(url)
    if path is None:
        return _refuse(
            EXIT_BAD_INPUT,
            "backfill_script_not_sqlite",
            f"{make_url(url).render_as_string(hide_password=True)} is not a SQLite "
            "database file; this script works on the app's SQLite file only.",
            rule="Q25: the drain works on the app's SQLite file",
            at=now(),
        )
    if not path.exists():
        return _refuse(
            EXIT_BAD_INPUT,
            "backfill_script_no_database",
            f"database file {path} does not exist; run from the repo root or set "
            "COROLLARY_DATABASE_URL. Nothing was created.",
            rule="Q25: the drain works on the app's existing SQLite file",
            at=now(),
        )
    print(f"database: {path}")
    inputs: dict[str, object] = {
        "max_pairs": max_pairs,
        "dry_run": dry_run,
        "database": str(path),
        "correlation_id": correlation_id,
    }

    at = now()
    open_session = nyse_session_containing(at)
    if open_session is not None:
        return _refuse(
            EXIT_SESSION_OPEN,
            "backfill_script_session_open",
            f"the regular NYSE session of {open_session.isoformat()} is open at "
            f"{at.isoformat()} (corollary.calendars.nyse_session_containing). "
            "Nothing was requested or written; run it after the close.",
            rule=SESSION_RULE,
            at=at,
            inputs={**inputs, "session_date": open_session.isoformat()},
        )
    if app_running():
        return _refuse(
            EXIT_APP_RUNNING,
            "backfill_script_app_running",
            f"something answers {APP_HEALTH_URL}, so a Corollary app is running and "
            "spending the same account's Alpaca budget. Stop it first. Nothing was "
            "requested or written.",
            rule=APP_RULE,
            at=at,
            inputs=inputs,
        )

    print(f"max pairs: {max_pairs}{' (dry run)' if dry_run else ''}")
    return asyncio.run(
        _run(
            db_path=path,
            max_pairs=max_pairs,
            dry_run=dry_run,
            now=now,
            vendors=vendors,
            seed_loader=seed_loader,
            app_running=app_running,
            correlation_id=correlation_id,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
