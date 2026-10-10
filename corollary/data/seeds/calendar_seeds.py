"""The News calendar's committed seeds: central-bank decisions and release times.

Spec decision 8: central-bank decision dates are a committed seed per year,
``central_banks_<year>.csv`` beside this file, with a time only where the bank
publishes one. A year with no seed says so -- :func:`seed_gaps` and
:func:`banks_without_seed` answer it -- rather than going quietly empty on
1 January. Spec Q3: FRED's ``/releases/dates`` supplies economic-release
*dates*; ``econ_release_times.csv`` is the hand-kept table of each release's
usual ET *time*.

Both are human-verified data, sourced row by row from the publisher's own page
(the bank, the statistical agency), so every row carries its source URL. **No
row is inferred** -- not from a cadence, not from the previous year.

Central-bank file format
------------------------

Two blocks, each under its own header row, after a ``year`` row::

    year,2027
    bank,coverage,covers_from,retrieved,date_source_url,time_source_url,note
    FOMC,full,,2026-10-10,https://...,https://...,...
    ECB,unpublished,,2026-10-10,https://...,,ECB has not published 2027 dates
    ...
    bank,meeting_start,date,local_time,timezone,source_url,note
    FOMC,2027-01-26,2027-01-27,14:00,America/New_York,https://...,

The coverage block names **every** bank in :class:`Bank` exactly once, so a
bank's absence is always a stated fact, never a missing row:

* ``full`` -- the year's decisions are all in the file;
* ``from`` -- only decisions on or after ``covers_from`` are (the bank's page
  had already dropped the earlier ones when it was read);
* ``unpublished`` -- the bank had not published the year when the file was
  written; the ``note`` must say so and the bank may have no decision rows.

A ``full`` or ``from`` bank with **no** decision rows is rejected: it would
claim coverage while the panel showed nothing and reported no gap, which is
what a lost block of rows (a bad merge, a hand edit) looks like.

A decision's ``date`` is the decision day in the bank's own calendar.
``local_time`` is the bank's published announcement time in ``timezone`` (the
bank's own zone, checked against :data:`BANK_TIMEZONES`), blank where the bank
publishes none. A row is timed **exactly when** its bank's coverage row cites a
``time_source_url``: a time with no cited source is rejected, and so is a
blank time on a bank that cites one (a dropped cell, not a date-only meeting). :attr:`CentralBankDecision.at` is that instant in UTC,
converted with :mod:`zoneinfo` -- never by fixed offsets, since the US, the
UK/EU and Japan change clocks on different dates or not at all.

``date`` and ``at`` are kept **separate** on purpose: a date-only event stored
as an instant (midnight UTC) groups under the previous day in New York.

Validation is whole-file, as :mod:`corollary.data.seeds.nport` is: one bad row
raises :class:`~corollary.data.seeds.SeedError` naming the file and line, and
nothing from that file is returned.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from corollary.data.seeds import SeedError

#: Where the committed seeds live -- next to this file, inside the package.
SEEDS_DIR: Final[Path] = Path(__file__).resolve().parent

#: Display zone (CLAUDE.md: stored UTC, displayed ``America/New_York``).
ET_ZONE: Final = ZoneInfo("America/New_York")


class Bank(StrEnum):
    """The four central banks decision 8 seeds, in panel order."""

    FOMC = "FOMC"
    ECB = "ECB"
    BOE = "BOE"
    BOJ = "BOJ"


#: Each bank's own zone. A decision row must use its bank's zone, so a
#: London time cannot be typed against the Fed by accident. The ECB writes
#: "CET" for Frankfurt local time, which is CEST in summer -- Europe/Berlin.
BANK_TIMEZONES: Final[Mapping[Bank, str]] = MappingProxyType(
    {
        Bank.FOMC: "America/New_York",
        Bank.ECB: "Europe/Berlin",
        Bank.BOE: "Europe/London",
        Bank.BOJ: "Asia/Tokyo",
    }
)

#: Longest a seeded meeting runs, first day to decision day. Every bank here
#: meets for one or two days; a wider span is a typo, not a meeting.
MAX_MEETING_DAYS: Final = 2


class Coverage(StrEnum):
    """How much of a year one bank's rows cover. The values are the file's words."""

    FULL = "full"
    FROM = "from"
    UNPUBLISHED = "unpublished"


class GapKind(StrEnum):
    """Why the panel must say something about a bank for a year."""

    #: No ``central_banks_<year>.csv`` at all.
    NO_FILE = "no_file"
    #: The file says the bank had not published the year.
    UNPUBLISHED = "unpublished"
    #: The file covers the bank only from :attr:`SeedGap.covers_from`.
    PARTIAL = "partial"


CENTRAL_BANK_COVERAGE_COLUMNS: Final[tuple[str, ...]] = (
    "bank",
    "coverage",
    "covers_from",
    "retrieved",
    "date_source_url",
    "time_source_url",
    "note",
)
CENTRAL_BANK_EVENT_COLUMNS: Final[tuple[str, ...]] = (
    "bank",
    "meeting_start",
    "date",
    "local_time",
    "timezone",
    "source_url",
    "note",
)
ECON_RELEASE_COLUMNS: Final[tuple[str, ...]] = (
    "release_id",
    "release_name",
    "et_time",
    "fred_url",
    "time_source_url",
    "note",
)

ECON_RELEASE_TIMES_PATH: Final[Path] = SEEDS_DIR / "econ_release_times.csv"

#: ``HH:MM``, 24-hour, two digits each. ``time.fromisoformat`` alone also
#: accepts seconds and offsets, which a hand-kept table should not carry.
_HHMM_RE: Final = re.compile(r"\A([01][0-9]|2[0-3]):[0-5][0-9]\Z")
_YEAR_RE: Final = re.compile(r"\A[0-9]{4}\Z")
_RELEASE_ID_RE: Final = re.compile(r"\A[1-9][0-9]*\Z")


@dataclass(frozen=True)
class BankCoverage:
    """One bank's coverage row: how much of the year the file holds, and from where."""

    bank: Bank
    coverage: Coverage
    #: Set exactly when ``coverage`` is :attr:`Coverage.FROM`.
    covers_from: date | None
    #: When the source page was read.
    retrieved: date
    date_source_url: str
    #: Where the announcement time comes from; ``None`` when the bank publishes none.
    time_source_url: str | None
    note: str


@dataclass(frozen=True)
class CentralBankDecision:
    """One policy decision.

    ``date`` is the decision day in the bank's own calendar. ``at`` is the
    announcement instant in UTC, or ``None`` when the bank publishes no time
    -- a date-only event, never coerced to midnight.
    """

    bank: Bank
    meeting_start: date
    date: date
    #: The bank's published local time, in ``timezone``; ``None`` if none.
    local_time: time | None
    timezone: str
    #: Aware, UTC. ``None`` exactly when ``local_time`` is.
    at: datetime | None
    source_url: str
    note: str

    def at_et(self) -> datetime | None:
        """The announcement instant for display in ``America/New_York``."""
        return None if self.at is None else self.at.astimezone(ET_ZONE)


@dataclass(frozen=True)
class SeedGap:
    """Something the calendar panel must state about one bank in one year."""

    bank: Bank
    year: int
    kind: GapKind
    reason: str
    #: For :attr:`GapKind.PARTIAL`: the first date the seed covers.
    covers_from: date | None = None


@dataclass(frozen=True)
class CentralBankSeed:
    """One year's validated central-bank seed."""

    year: int
    #: One row per :class:`Bank`, in :class:`Bank` order.
    coverage: tuple[BankCoverage, ...]
    #: Sorted by ``(date, bank order)``.
    decisions: tuple[CentralBankDecision, ...]

    def coverage_of(self, bank: Bank) -> BankCoverage:
        for row in self.coverage:
            if row.bank is bank:
                return row
        raise KeyError(bank)  # unreachable for a parsed seed: every bank is present

    def gaps(self) -> tuple[SeedGap, ...]:
        """Banks this file does not fully cover, in :class:`Bank` order."""
        out: list[SeedGap] = []
        for row in self.coverage:
            if row.coverage is Coverage.UNPUBLISHED:
                out.append(SeedGap(row.bank, self.year, GapKind.UNPUBLISHED, row.note))
            elif row.coverage is Coverage.FROM:
                assert row.covers_from is not None
                reason = (
                    f"{row.bank.value} {self.year} decisions are seeded from "
                    f"{row.covers_from.isoformat()} only: {row.note}"
                )
                out.append(
                    SeedGap(row.bank, self.year, GapKind.PARTIAL, reason, row.covers_from)
                )
        return tuple(out)


@dataclass(frozen=True)
class EconReleaseTime:
    """One FRED release and the ET time its publisher releases it at.

    One time per release, applied to every date FRED lists for it. Where a
    publisher times an occasional issue differently -- G.17's annual revision
    is issued at noon, not 09:15 -- that issue's day is **not distinguished**:
    FRED's release dates do not say which date it is. The row's ``note``
    states the exception; the time is the regular release's.
    """

    release_id: int
    release_name: str
    et_time: time
    fred_url: str
    time_source_url: str
    note: str


@dataclass(frozen=True)
class EconReleaseTimes:
    """The whole Q3 table, validated: one row per FRED release id."""

    rows: tuple[EconReleaseTime, ...]

    def by_id(self, release_id: int) -> EconReleaseTime | None:
        for row in self.rows:
            if row.release_id == release_id:
                return row
        return None

    def at(self, release_id: int, day: date) -> datetime | None:
        """The release's regular instant on ``day``, in UTC; ``None`` for an unknown ``release_id``.

        Every row carries a time, so ``None`` only ever means the id is not in
        the table. ``day`` is not checked against the publisher's schedule, and
        an exceptionally timed issue (see :class:`EconReleaseTime`) gets the
        regular time.
        """
        row = self.by_id(release_id)
        if row is None:
            return None
        return _local_to_utc(day, row.et_time, ET_ZONE)


# --- parsing ---------------------------------------------------------------


def _records(text: str) -> list[tuple[int, list[str]]]:
    reader = csv.reader(io.StringIO(text, newline=""))
    return [(reader.line_num, record) for record in reader if record]


def _parse_date(value: str, column: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{column} {value!r} is not an ISO date YYYY-MM-DD") from None


def _parse_hhmm(value: str, column: str) -> time:
    if not _HHMM_RE.fullmatch(value):
        raise ValueError(f"{column} {value!r} is not a 24-hour time HH:MM")
    return time.fromisoformat(value)


def _require_url(value: str, column: str) -> str:
    if not value.startswith("https://"):
        raise ValueError(f"{column} {value!r} must be an https:// URL")
    return value


def _local_to_utc(day: date, local: time, zone: ZoneInfo) -> datetime:
    """``day`` at wall-clock ``local`` in ``zone``, as an aware UTC instant.

    Refuses a wall time that does not exist (spring-forward gap) or exists
    twice (fall-back fold): the file cannot say which instant it meant.
    """
    first = datetime.combine(day, local, tzinfo=zone)
    second = first.replace(fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError(
            f"{day.isoformat()} {local.isoformat('minutes')} in {zone.key} is ambiguous "
            "or does not exist (a clock change)"
        )
    instant = first.astimezone(UTC)
    if instant.astimezone(zone).replace(tzinfo=None) != first.replace(tzinfo=None):
        raise ValueError(
            f"{day.isoformat()} {local.isoformat('minutes')} does not exist in {zone.key}"
        )
    return instant


def _parse_bank(value: str) -> Bank:
    try:
        return Bank(value)
    except ValueError:
        raise ValueError(f"bank {value!r} is not one of {[b.value for b in Bank]}") from None


def _parse_coverage_row(record: list[str], year: int) -> BankCoverage:
    bank_text, coverage_text, from_text, retrieved_text, date_url, time_url, note = record
    bank = _parse_bank(bank_text)
    try:
        coverage = Coverage(coverage_text)
    except ValueError:
        raise ValueError(
            f"coverage {coverage_text!r} is not one of {[c.value for c in Coverage]}"
        ) from None
    covers_from: date | None = None
    if coverage is Coverage.FROM:
        if not from_text:
            raise ValueError(f"{bank}: coverage 'from' needs a covers_from date")
        covers_from = _parse_date(from_text, "covers_from")
        if covers_from.year != year:
            raise ValueError(f"covers_from {from_text} is not in {year}")
    elif from_text:
        raise ValueError(f"{bank}: covers_from is only for coverage 'from', not {coverage}")
    if coverage is not Coverage.FULL and not note.strip():
        raise ValueError(f"{bank}: coverage {coverage} needs a note saying why")
    return BankCoverage(
        bank=bank,
        coverage=coverage,
        covers_from=covers_from,
        retrieved=_parse_date(retrieved_text, "retrieved"),
        date_source_url=_require_url(date_url, "date_source_url"),
        time_source_url=_require_url(time_url, "time_source_url") if time_url else None,
        note=note,
    )


def _parse_event_row(
    record: list[str], year: int, coverage: Mapping[Bank, BankCoverage]
) -> CentralBankDecision:
    bank_text, start_text, date_text, time_text, zone_text, url, note = record
    bank = _parse_bank(bank_text)
    cover = coverage[bank]
    if cover.coverage is Coverage.UNPUBLISHED:
        raise ValueError(f"{bank}: coverage says unpublished, so it may have no decision rows")
    day = _parse_date(date_text, "date")
    if day.year != year:
        raise ValueError(f"date {date_text} is not in the file's year {year}")
    if cover.covers_from is not None and day < cover.covers_from:
        raise ValueError(
            f"date {date_text} is before {bank}'s covers_from {cover.covers_from.isoformat()}"
        )
    start = _parse_date(start_text, "meeting_start")
    span = (day - start).days
    if not 0 <= span < MAX_MEETING_DAYS:
        raise ValueError(
            f"meeting_start {start_text} must be the decision date or up to "
            f"{MAX_MEETING_DAYS - 1} day before it ({date_text})"
        )
    if zone_text != BANK_TIMEZONES[bank]:
        raise ValueError(f"timezone {zone_text!r} is not {bank}'s zone {BANK_TIMEZONES[bank]!r}")
    try:
        zone = ZoneInfo(zone_text)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - BANK_TIMEZONES are real zones
        raise ValueError(f"timezone {zone_text!r} is not an IANA zone") from None
    if time_text and cover.time_source_url is None:
        raise ValueError(
            f"{bank}: local_time {time_text!r} given, but {bank}'s coverage row cites no "
            "time_source_url -- a displayed time needs a source"
        )
    if not time_text and cover.time_source_url is not None:
        raise ValueError(
            f"{bank}: local_time is blank, but {bank}'s coverage row cites a "
            "time_source_url -- every row of a timed bank needs its time"
        )
    local_time: time | None = None
    at: datetime | None = None
    if time_text:
        local_time = _parse_hhmm(time_text, "local_time")
        at = _local_to_utc(day, local_time, zone)
    return CentralBankDecision(
        bank=bank,
        meeting_start=start,
        date=day,
        local_time=local_time,
        timezone=zone_text,
        at=at,
        source_url=_require_url(url, "source_url"),
        note=note,
    )


def parse_central_bank_seed(text: str, source: str = "<string>") -> CentralBankSeed:
    """Parse one year's file. Raises :class:`SeedError` naming ``source`` and the line."""
    records = _records(text)

    def fail(line: int, message: str) -> SeedError:
        return SeedError(f"{source}, line {line}: {message}")

    if not records:
        raise SeedError(f"{source}: empty file")
    line, first = records[0]
    if len(first) != 2 or first[0] != "year" or not _YEAR_RE.fullmatch(first[1]):
        raise fail(line, f"the first row must be 'year,YYYY', not {first!r}")
    year = int(first[1])
    if len(records) < 2 or tuple(records[1][1]) != CENTRAL_BANK_COVERAGE_COLUMNS:
        got = records[1][1] if len(records) > 1 else None
        raise fail(
            records[1][0] if len(records) > 1 else line,
            f"the second row must be {','.join(CENTRAL_BANK_COVERAGE_COLUMNS)!r}, not {got!r}",
        )

    index = 2
    coverage: dict[Bank, BankCoverage] = {}
    coverage_lines: dict[Bank, int] = {}
    while index < len(records) and tuple(records[index][1]) != CENTRAL_BANK_EVENT_COLUMNS:
        line, record = records[index]
        if len(record) != len(CENTRAL_BANK_COVERAGE_COLUMNS):
            raise fail(
                line,
                f"expected {len(CENTRAL_BANK_COVERAGE_COLUMNS)} coverage fields, "
                f"got {len(record)}: {record!r}",
            )
        try:
            row = _parse_coverage_row(record, year)
        except ValueError as exc:
            raise fail(line, str(exc)) from None
        if row.bank in coverage:
            raise fail(line, f"{row.bank} appears twice in the coverage block")
        coverage[row.bank] = row
        coverage_lines[row.bank] = line
        index += 1
    if index >= len(records):
        raise SeedError(
            f"{source}: no decision header row {','.join(CENTRAL_BANK_EVENT_COLUMNS)!r}"
        )
    missing = [bank.value for bank in Bank if bank not in coverage]
    if missing:
        raise fail(
            records[index][0],
            f"coverage block has no row for {missing}: every bank must be stated, "
            "'unpublished' if it has no dates yet",
        )

    decisions: list[CentralBankDecision] = []
    seen: set[tuple[Bank, date]] = set()
    for line, record in records[index + 1 :]:
        if len(record) != len(CENTRAL_BANK_EVENT_COLUMNS):
            raise fail(
                line,
                f"expected {len(CENTRAL_BANK_EVENT_COLUMNS)} decision fields, "
                f"got {len(record)}: {record!r}",
            )
        try:
            decision = _parse_event_row(record, year, coverage)
        except ValueError as exc:
            raise fail(line, str(exc)) from None
        key = (decision.bank, decision.date)
        if key in seen:
            raise fail(line, f"{decision.bank} {decision.date.isoformat()} appears twice")
        seen.add(key)
        decisions.append(decision)

    banks_with_rows = {decision.bank for decision in decisions}
    for bank in Bank:
        cover = coverage[bank]
        if cover.coverage is not Coverage.UNPUBLISHED and bank not in banks_with_rows:
            raise fail(
                coverage_lines[bank],
                f"{bank}: coverage {cover.coverage.value!r} but no decision rows -- a bank "
                "with no dates is 'unpublished', with a note saying why",
            )

    bank_order = list(Bank)
    decisions.sort(key=lambda d: (d.date, bank_order.index(d.bank)))
    return CentralBankSeed(
        year=year,
        coverage=tuple(coverage[bank] for bank in Bank),
        decisions=tuple(decisions),
    )


def central_bank_seed_path(year: int, directory: Path | None = None) -> Path:
    return (SEEDS_DIR if directory is None else directory) / f"central_banks_{year}.csv"


def _read(path: Path) -> str:
    try:
        # utf-8-sig: Excel's "CSV UTF-8" prepends a byte-order mark.
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SeedError(f"{path}: not UTF-8 text ({exc})") from None


def load_central_bank_seed(year: int, directory: Path | None = None) -> CentralBankSeed | None:
    """The seed for ``year``, or ``None`` if no file exists. Malformed raises :class:`SeedError`."""
    path = central_bank_seed_path(year, directory)
    if not path.exists():
        return None
    seed = parse_central_bank_seed(_read(path), source=str(path))
    if seed.year != year:
        raise SeedError(f"{path}: its year row says {seed.year}, but the file is for {year}")
    return seed


def seed_gaps(year: int, directory: Path | None = None) -> tuple[SeedGap, ...]:
    """Everything the panel must say about ``year``'s central-bank coverage.

    No file: every bank, :attr:`GapKind.NO_FILE`. Otherwise the file's own
    :meth:`CentralBankSeed.gaps` -- unpublished banks and partial coverage.
    """
    seed = load_central_bank_seed(year, directory)
    if seed is None:
        return tuple(
            SeedGap(
                bank,
                year,
                GapKind.NO_FILE,
                f"no central-bank seed for {year}: {bank.value} decisions are not on the calendar",
            )
            for bank in Bank
        )
    return seed.gaps()


def banks_without_seed(year: int, directory: Path | None = None) -> tuple[SeedGap, ...]:
    """The banks with no seeded dates at all for ``year`` -- no file, or unpublished.

    Partial coverage is not here: that bank has a seed. :func:`seed_gaps`
    carries both.
    """
    return tuple(gap for gap in seed_gaps(year, directory) if gap.kind is not GapKind.PARTIAL)


def parse_econ_release_times(text: str, source: str = "<string>") -> EconReleaseTimes:
    """Parse the Q3 table. Raises :class:`SeedError` naming ``source`` and the line."""
    records = _records(text)
    if not records:
        raise SeedError(f"{source}: empty file")
    line, header = records[0]
    if tuple(header) != ECON_RELEASE_COLUMNS:
        raise SeedError(
            f"{source}, line {line}: the first row must be "
            f"{','.join(ECON_RELEASE_COLUMNS)!r}, not {header!r}"
        )
    rows: list[EconReleaseTime] = []
    seen: set[int] = set()
    for line, record in records[1:]:
        if len(record) != len(ECON_RELEASE_COLUMNS):
            raise SeedError(
                f"{source}, line {line}: expected {len(ECON_RELEASE_COLUMNS)} fields, "
                f"got {len(record)}: {record!r}"
            )
        id_text, name, time_text, fred_url, time_url, note = record
        try:
            if not _RELEASE_ID_RE.fullmatch(id_text):
                raise ValueError(f"release_id {id_text!r} is not a positive integer")
            if not name.strip():
                raise ValueError("release_name is empty")
            row = EconReleaseTime(
                release_id=int(id_text),
                release_name=name,
                et_time=_parse_hhmm(time_text, "et_time"),
                fred_url=_require_url(fred_url, "fred_url"),
                time_source_url=_require_url(time_url, "time_source_url"),
                note=note,
            )
        except ValueError as exc:
            raise SeedError(f"{source}, line {line}: {exc}") from None
        if row.release_id in seen:
            raise SeedError(f"{source}, line {line}: release_id {row.release_id} appears twice")
        seen.add(row.release_id)
        rows.append(row)
    if not rows:
        raise SeedError(f"{source}: no release rows")
    return EconReleaseTimes(rows=tuple(rows))


def load_econ_release_times(path: Path | None = None) -> EconReleaseTimes:
    """The committed Q3 table. It ships with the package, so a missing file raises."""
    target = ECON_RELEASE_TIMES_PATH if path is None else path
    if not target.exists():
        raise SeedError(f"{target}: the economic release times table is missing")
    return parse_econ_release_times(_read(target), source=str(target))
