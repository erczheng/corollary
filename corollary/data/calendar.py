"""The News calendar's storage layer: ``calendar_event`` reads and writes.

Phase 3 step 7, units 7.2a and 7.2c-1. Five jobs, and nothing else:

1. **Upsert vendor and seed rows** (:func:`upsert_events`) by their key
   ``(source, kind, vendor_id)``. The input is
   :class:`~corollary.data.calendar_event.CalendarEventInput`, which the
   fetchers (next unit) produce.
2. **Read a date range** (:func:`read_range`, :func:`group_by_date`) by the
   ``date`` column -- the Eastern session -- never by ``at``'s UTC day, and
   never returning a soft-deleted row.
3. **Import the central-bank seed** (:func:`import_central_bank_seed`,
   :func:`import_central_bank_year`) and keep its gaps readable
   (:func:`seed_gaps_between`), so the panel can say *"ECB 2026 is only
   seeded from ..."* and *"no 2027 seed"* rather than going quietly empty
   (decision 8).
4. **Manual geopolitical rows** (:func:`create_manual`,
   :func:`update_manual`, :func:`delete_manual`), decision 9: ``source =
   'manual'``, removal is a soft delete, ``created_at``/``updated_at``
   stamped, and **nothing written to the configuration audit log** -- a note
   that a summit is on Thursday governs nothing. Editing or deleting any
   other row raises :class:`CalendarEventNotEditableError`.
5. **Replace a vendor window** (:func:`replace_window`): upsert one
   complete fetch and withdraw the rows of that source and those kinds in
   the fetched window that it no longer lists -- the vendor counterpart of
   the seed import's withdrawal. Only ever after a successful, complete
   fetch.

Every function flushes and none commits: the caller owns the transaction, as
in :mod:`corollary.data.news.ingest`. Every timestamp is passed in (``now``)
and must be aware, so the tests and the scheduler say what time it is rather
than this module asking the clock. ``now`` is converted to UTC on the way in
(:func:`_utc_now`), so a row a caller still holds reads back in the zone it
is stored in -- not in the caller's zone until the next reload.

Reads return :class:`StoredCalendarEvent`, a frozen value detached from the
session, so a route can serialise it after the session closes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from corollary.data.calendar_event import (
    ET_ZONE,
    KIND_SOURCE,
    CalendarEventInput,
    CalendarKind,
    CalendarSource,
    EarningsSession,
    IpoStatus,
    check_title,
    check_when,
)
from corollary.data.seeds.calendar_seeds import (
    Bank,
    CentralBankDecision,
    CentralBankSeed,
    Coverage,
    SeedGap,
    load_central_bank_seed,
    seed_gaps,
)
from corollary.db.models import CalendarEvent

logger = logging.getLogger(__name__)

__all__ = [
    "CalendarEventNotEditableError",
    "CalendarEventNotFoundError",
    "CalendarStoreError",
    "SeedImport",
    "StoredCalendarEvent",
    "UpsertCounts",
    "WindowReplace",
    "WindowReplaceError",
    "central_bank_inputs",
    "create_manual",
    "delete_manual",
    "group_by_date",
    "import_central_bank_seed",
    "import_central_bank_year",
    "read_range",
    "replace_window",
    "seed_gaps_between",
    "update_manual",
    "upsert_events",
]

#: The ``Money`` columns an upsert writes.
_MONEY_FIELDS: Final = ("estimate", "prior", "actual", "price_low", "price_high")

#: How each seeded bank is named in a row's title.
_BANK_LABEL: Final = {
    Bank.FOMC: "FOMC",
    Bank.ECB: "ECB",
    Bank.BOE: "BoE",
    Bank.BOJ: "BoJ",
}


# --- errors -----------------------------------------------------------------


class CalendarStoreError(Exception):
    """Base for the refusals a route maps to a 4xx."""


class CalendarEventNotFoundError(CalendarStoreError):
    """No live row with this id: never created, or already removed. A 404."""

    def __init__(self, event_id: int) -> None:
        super().__init__(f"calendar event {event_id} does not exist or was removed")
        self.event_id = event_id


class CalendarEventNotEditableError(CalendarStoreError):
    """The row is a vendor or seed row; only manual geopolitical rows may change.

    Not a subclass of :class:`CalendarEventNotFoundError`: the row exists, and
    the route answers differently (a refusal, not a 404).
    """

    def __init__(self, event_id: int, source: str, kind: str) -> None:
        super().__init__(
            f"calendar event {event_id} is a {source} {kind} row; only manual "
            "geopolitical rows can be edited or removed"
        )
        self.event_id = event_id
        self.source = source
        self.kind = kind


class WindowReplaceError(ValueError):
    """A :func:`replace_window` call the store refuses; nothing was written.

    A ``ValueError``, not a :class:`CalendarStoreError`: no route reaches
    this, and a refusal here is a caller bug (an event outside its own
    window, a kind from the wrong source), not a client's request to map to
    a 4xx.
    """


# --- values -----------------------------------------------------------------


@dataclass(frozen=True)
class StoredCalendarEvent:
    """One live ``calendar_event`` row, detached from its session."""

    id: int
    kind: CalendarKind
    source: CalendarSource
    title: str
    ticker: str | None
    date: date
    at: datetime | None
    estimate: Decimal | None
    prior: Decimal | None
    actual: Decimal | None
    unit: str | None
    session: EarningsSession | None
    #: Q15's IPO-only fields; ``None`` on every other kind.
    exchange: str | None
    shares: int | None
    price_low: Decimal | None
    price_high: Decimal | None
    ipo_status: IpoStatus | None
    vendor_id: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def editable(self) -> bool:
        """Whether the manual routes may change this row (decision 9)."""
        return self.source is CalendarSource.MANUAL

    def at_et(self) -> datetime | None:
        """The instant for display in ``America/New_York``; ``None`` if date-only."""
        return None if self.at is None else self.at.astimezone(ET_ZONE)


@dataclass(frozen=True)
class UpsertCounts:
    """What one upsert or import did. ``withdrawn`` is the seed import's only."""

    inserted: int
    updated: int
    unchanged: int
    withdrawn: int = 0


@dataclass(frozen=True)
class WindowReplace:
    """What one :func:`replace_window` did.

    ``inserted + updated + unchanged + revived`` is the number of events
    passed in; ``updated`` excludes revivals. ``withdrawn_keys`` names every
    row withdrawn, as ``(kind, vendor_id)`` sorted, so the scheduler can log
    which events went away and not only how many.
    """

    inserted: int
    updated: int
    unchanged: int
    withdrawn: int
    revived: int
    withdrawn_keys: tuple[tuple[CalendarKind, str], ...]


@dataclass(frozen=True)
class SeedImport:
    """One year's central-bank import: what it wrote, and what it cannot cover.

    ``counts`` is ``None`` when the year has no seed file -- nothing was
    written, and ``gaps`` names every bank as :attr:`GapKind.NO_FILE`.
    """

    year: int
    counts: UpsertCounts | None
    gaps: tuple[SeedGap, ...]


# --- helpers ----------------------------------------------------------------


def _utc_now(now: datetime) -> datetime:
    """``now`` as UTC, refused if naive.

    Every function here stamps rows with ``now``. Stamped as given, a caller
    passing an Eastern ``now`` held rows reading back in Eastern until the
    session reloaded them; converting once on the way in is the fix.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"now must be a timezone-aware datetime, got {now!r}")
    return now.astimezone(UTC)


def _utc(at: datetime | None) -> datetime | None:
    """``at`` as UTC, so a value read back before a reload is in the zone it is stored in."""
    return None if at is None else at.astimezone(UTC)


def _stored(row: CalendarEvent) -> StoredCalendarEvent:
    return StoredCalendarEvent(
        id=row.id,
        kind=CalendarKind(row.kind),
        source=CalendarSource(row.source),
        title=row.title,
        ticker=row.ticker,
        date=row.date,
        at=row.at,
        estimate=row.estimate,
        prior=row.prior,
        actual=row.actual,
        unit=row.unit,
        session=None if row.session is None else EarningsSession(row.session),
        exchange=row.exchange,
        shares=row.shares,
        price_low=row.price_low,
        price_high=row.price_high,
        ipo_status=None if row.ipo_status is None else IpoStatus(row.ipo_status),
        vendor_id=row.vendor_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _money_text(value: Decimal | None) -> str | None:
    """The spelling ``Money`` stores. ``1.25`` and ``1.2500`` are equal and differ here."""
    return None if value is None else format(value, "f")


def _stored_form(
    *,
    title: str,
    ticker: str | None,
    day: date,
    at: datetime | None,
    estimate: Decimal | None,
    prior: Decimal | None,
    actual: Decimal | None,
    unit: str | None,
    session: str | None,
    exchange: str | None,
    shares: int | None,
    price_low: Decimal | None,
    price_high: Decimal | None,
    ipo_status: str | None,
) -> tuple[object, ...]:
    """Everything an upsert may change, as the database would hold it."""
    return (
        title,
        ticker,
        day,
        at,
        _money_text(estimate),
        _money_text(prior),
        _money_text(actual),
        unit,
        session,
        exchange,
        shares,
        _money_text(price_low),
        _money_text(price_high),
        ipo_status,
    )


def _form_of_input(event: CalendarEventInput) -> tuple[object, ...]:
    return _stored_form(
        title=event.title,
        ticker=event.ticker,
        day=event.date,
        at=event.at,
        estimate=event.estimate,
        prior=event.prior,
        actual=event.actual,
        unit=event.unit,
        session=None if event.session is None else event.session.value,
        exchange=event.exchange,
        shares=event.shares,
        price_low=event.price_low,
        price_high=event.price_high,
        ipo_status=None if event.ipo_status is None else event.ipo_status.value,
    )


def _form_of_row(row: CalendarEvent) -> tuple[object, ...]:
    return _stored_form(
        title=row.title,
        ticker=row.ticker,
        day=row.date,
        at=row.at,
        estimate=row.estimate,
        prior=row.prior,
        actual=row.actual,
        unit=row.unit,
        session=row.session,
        exchange=row.exchange,
        shares=row.shares,
        price_low=row.price_low,
        price_high=row.price_high,
        ipo_status=row.ipo_status,
    )


def _apply(row: CalendarEvent, event: CalendarEventInput) -> None:
    row.title = event.title
    row.ticker = event.ticker
    row.date = event.date
    row.at = _utc(event.at)
    row.unit = event.unit
    row.session = None if event.session is None else event.session.value
    row.exchange = event.exchange
    row.shares = event.shares
    row.ipo_status = None if event.ipo_status is None else event.ipo_status.value
    for name in _MONEY_FIELDS:
        old: Decimal | None = getattr(row, name)
        new: Decimal | None = getattr(event, name)
        setattr(row, name, new)
        # SQLAlchemy skips an UPDATE when the new value == the old one, and
        # Decimal('1.25') == Decimal('1.2500'). The stored text would keep the
        # old spelling while the upsert reported an update. Force the write
        # when the spelling, not the number, is what changed.
        if _money_text(old) != _money_text(new):
            flag_modified(row, name)


# --- upsert -----------------------------------------------------------------


def upsert_events(
    session: Session, events: Sequence[CalendarEventInput], *, now: datetime
) -> UpsertCounts:
    """Insert or update each event by ``(source, kind, vendor_id)``.

    An unchanged event leaves its row untouched, ``updated_at`` included. A
    changed one is rewritten and stamped. A row a seed import had withdrawn
    (``deleted_at`` set) is revived, since the producer is saying it exists
    again. One batch naming one key twice is refused rather than resolved by
    order -- that is a fetcher bug, and last-wins would hide it.
    """
    now = _utc_now(now)
    by_key: dict[tuple[CalendarSource, CalendarKind, str], CalendarEventInput] = {}
    for event in events:
        key = (event.source, event.kind, event.vendor_id)
        if key in by_key:
            raise ValueError(
                f"{event.source.value} {event.kind.value} {event.vendor_id!r} appears "
                "twice in one batch"
            )
        by_key[key] = event

    existing: dict[tuple[str, str, str], CalendarEvent] = {}
    groups: dict[tuple[CalendarSource, CalendarKind], list[str]] = {}
    for source, kind, vendor_id in by_key:
        groups.setdefault((source, kind), []).append(vendor_id)
    for (source, kind), vendor_ids in groups.items():
        rows = session.scalars(
            select(CalendarEvent).where(
                CalendarEvent.source == source.value,
                CalendarEvent.kind == kind.value,
                CalendarEvent.vendor_id.in_(vendor_ids),
            )
        )
        for found in rows:
            assert found.vendor_id is not None  # the WHERE above
            existing[(found.source, found.kind, found.vendor_id)] = found

    inserted = updated = unchanged = 0
    for (source, kind, vendor_id), event in by_key.items():
        current = existing.get((source.value, kind.value, vendor_id))
        if current is None:
            row = CalendarEvent(
                kind=kind.value,
                source=source.value,
                vendor_id=vendor_id,
                created_at=now,
                updated_at=now,
                deleted_at=None,
            )
            _apply(row, event)
            session.add(row)
            inserted += 1
        elif current.deleted_at is not None or _form_of_row(current) != _form_of_input(event):
            _apply(current, event)
            current.deleted_at = None
            current.updated_at = now
            updated += 1
        else:
            unchanged += 1
    session.flush()
    return UpsertCounts(inserted=inserted, updated=updated, unchanged=unchanged)


# --- vendor window replacement --------------------------------------------------


def _check_window(
    events: Sequence[CalendarEventInput],
    *,
    source: CalendarSource,
    kinds: frozenset[CalendarKind],
    window_start: date,
    window_end: date,
) -> None:
    """Every refusal :func:`replace_window` makes, all before anything is written."""
    for name, day in (("window_start", window_start), ("window_end", window_end)):
        if isinstance(day, datetime) or not isinstance(day, date):
            raise WindowReplaceError(f"{name} must be a calendar date, got {day!r}")
    if window_end < window_start:
        raise WindowReplaceError(
            f"window_end {window_end.isoformat()} is before window_start "
            f"{window_start.isoformat()}"
        )
    if not isinstance(source, CalendarSource):
        raise WindowReplaceError(f"source must be a CalendarSource, got {source!r}")
    if source in (CalendarSource.MANUAL, CalendarSource.SEED):
        raise WindowReplaceError(
            f"{source.value} rows are not replaced by window: manual rows are a "
            "person's, and the seed import withdraws inside its own covered window"
        )
    if not kinds:
        raise WindowReplaceError("kinds is empty; a window replacement must name what it covers")
    for kind in sorted(kinds):
        if not isinstance(kind, CalendarKind):
            raise WindowReplaceError(f"kinds must hold CalendarKind values, got {kind!r}")
        if KIND_SOURCE[kind] is not source:
            raise WindowReplaceError(
                f"{kind.value} events come from {KIND_SOURCE[kind].value}, not "
                f"{source.value}; a {source.value} fetch cannot replace them"
            )
    seen: set[tuple[CalendarKind, str]] = set()
    for event in events:
        label = f"{event.source.value} {event.kind.value} {event.vendor_id!r}"
        if event.source is not source:
            raise WindowReplaceError(f"{label} is not a {source.value} event")
        if event.kind not in kinds:
            raise WindowReplaceError(
                f"{label} is outside the kinds being replaced "
                f"({', '.join(sorted(k.value for k in kinds))})"
            )
        if not window_start <= event.date <= window_end:
            raise WindowReplaceError(
                f"{label} is dated {event.date.isoformat()}, outside the window "
                f"{window_start.isoformat()}..{window_end.isoformat()}"
            )
        key = (event.kind, event.vendor_id)
        if key in seen:
            raise WindowReplaceError(f"{label} appears twice in one batch")
        seen.add(key)


def replace_window(
    session: Session,
    events: Sequence[CalendarEventInput],
    *,
    source: CalendarSource,
    kinds: frozenset[CalendarKind],
    window_start: date,
    window_end: date,
    now: datetime,
) -> WindowReplace:
    """Make ``events`` the complete set of live ``source``/``kinds`` rows in the window.

    **Call this only with the result of a successful, complete fetch of
    exactly this window.** Every live row of ``source``, of a kind in
    ``kinds``, dated ``window_start <= date <= window_end`` (both inclusive,
    by the Eastern-session ``date`` column) whose ``vendor_id`` is not in
    ``events`` is **withdrawn** -- soft-deleted, ``deleted_at`` and
    ``updated_at`` set to ``now``. So a failed fetch, a fetch that stopped
    short (a page missing, a 429 mid-way), or a fetch of a narrower window
    than the one stated here would withdraw real events. A caller that is not
    sure its fetch was complete must not call this; it may still call
    :func:`upsert_events`, which withdraws nothing. An *empty* complete
    fetch is a real answer and withdraws the whole window.

    The vendor equivalent of the seed import's withdrawal. It closes the
    stale-row gap the fetcher audits reported: an IPO first keyed by name and
    later given a symbol, a FRED release moved to another date (its
    ``vendor_id`` carries the date), and a cancelled earnings report each
    leave a row behind under plain upsert.

    The steps:

    1. **Refuse** (:class:`WindowReplaceError`, nothing written) an inverted
       window, empty ``kinds``, a kind ``source`` does not produce
       (:data:`~corollary.data.calendar_event.KIND_SOURCE`), ``source``
       ``manual`` or ``seed``, or any event of another source, of a kind
       outside ``kinds``, dated outside the window, or keyed twice.
    2. **Upsert** ``events``. A previously withdrawn row that reappears is
       **revived** -- the same row, ``deleted_at`` cleared.
    3. **Withdraw** the live rows in scope that ``events`` does not list.
       Each withdrawal is logged. Rows outside the window, of other sources
       or other kinds -- manual and seed rows always among them -- are never
       read for withdrawal.

    The four upsert counts partition ``events``: ``inserted + updated +
    unchanged + revived == len(events)``; a revived row is counted as
    revived only, even if its content also changed.

    Flushes, never commits: the caller owns the transaction.
    """
    now = _utc_now(now)
    _check_window(
        events, source=source, kinds=kinds, window_start=window_start, window_end=window_end
    )

    # Which listed keys are currently withdrawn rows -- they revive on upsert.
    listed: dict[CalendarKind, set[str]] = {kind: set() for kind in kinds}
    for event in events:
        listed[event.kind].add(event.vendor_id)
    revived = 0
    for kind, vendor_ids in listed.items():
        if not vendor_ids:
            continue
        revived += len(
            session.scalars(
                select(CalendarEvent.id).where(
                    CalendarEvent.source == source.value,
                    CalendarEvent.kind == kind.value,
                    CalendarEvent.vendor_id.in_(sorted(vendor_ids)),
                    CalendarEvent.deleted_at.is_not(None),
                )
            ).all()
        )

    counts = upsert_events(session, events, now=now)

    stale = session.scalars(
        select(CalendarEvent).where(
            CalendarEvent.source == source.value,
            CalendarEvent.kind.in_(sorted(kind.value for kind in kinds)),
            CalendarEvent.date >= window_start,
            CalendarEvent.date <= window_end,
            CalendarEvent.deleted_at.is_(None),
        )
    )
    withdrawn: list[tuple[CalendarKind, str]] = []
    for row in stale:
        assert row.vendor_id is not None  # ck_calendar_event_vendor_id: vendor rows carry one
        kind = CalendarKind(row.kind)
        if row.vendor_id in listed[kind]:
            continue
        row.deleted_at = now
        row.updated_at = now
        withdrawn.append((kind, row.vendor_id))
    withdrawn.sort()
    for kind, vendor_id in withdrawn:
        logger.info(
            "withdrew %s %s %s: absent from a complete fetch of its window",
            source.value,
            kind.value,
            vendor_id,
            extra={
                "event": "calendar_window_withdrawn",
                "rule": (
                    "unit 7.2c-1: a vendor row in a completely fetched window that the "
                    "fetch no longer lists is soft-deleted"
                ),
                "source": source.value,
                "kind": kind.value,
                "vendor_id": vendor_id,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "at": now.isoformat(),
            },
        )
    session.flush()
    return WindowReplace(
        inserted=counts.inserted,
        updated=counts.updated - revived,
        unchanged=counts.unchanged,
        withdrawn=len(withdrawn),
        revived=revived,
        withdrawn_keys=tuple(withdrawn),
    )


# --- reads ------------------------------------------------------------------


def _display_order(event: StoredCalendarEvent) -> tuple[date, bool, datetime, int]:
    # Within a day, date-only (all-day) rows lead the timed ones; ``id``
    # breaks ties so the order is deterministic.
    return (event.date, event.at is not None, event.at or event.created_at, event.id)


def read_range(session: Session, start: date, end: date) -> tuple[StoredCalendarEvent, ...]:
    """Every live event whose ``date`` is in ``[start, end]``, inclusive.

    By ``date``, the Eastern session -- **not** by ``at``: an event at 20:30
    ET on 11 Aug is ``00:30Z`` on 12 Aug and belongs to the 11th. Soft-deleted
    rows are excluded. Ordered by date, then date-only before timed, then
    instant, then id.
    """
    if end < start:
        raise ValueError(f"end {end.isoformat()} is before start {start.isoformat()}")
    rows = session.scalars(
        select(CalendarEvent).where(
            CalendarEvent.date >= start,
            CalendarEvent.date <= end,
            CalendarEvent.deleted_at.is_(None),
        )
    )
    return tuple(sorted((_stored(row) for row in rows), key=_display_order))


def group_by_date(
    events: Iterable[StoredCalendarEvent],
) -> dict[date, tuple[StoredCalendarEvent, ...]]:
    """Events keyed by their ``date``, in date order, each day in display order."""
    out: dict[date, list[StoredCalendarEvent]] = {}
    for event in sorted(events, key=_display_order):
        out.setdefault(event.date, []).append(event)
    return {day: tuple(day_events) for day, day_events in out.items()}


# --- the central-bank seed ----------------------------------------------------


def _vendor_id(bank: Bank, bank_date: date) -> str:
    """``FOMC:2027-01-27`` -- the bank and its decision day in its own calendar."""
    return f"{bank.value}:{bank_date.isoformat()}"


def _parse_vendor_id(vendor_id: str) -> tuple[Bank, date]:
    bank_text, sep, date_text = vendor_id.partition(":")
    if not sep:
        raise ValueError(f"seed calendar row has a malformed vendor_id {vendor_id!r}")
    return Bank(bank_text), date.fromisoformat(date_text)


def _central_bank_input(decision: CentralBankDecision) -> CalendarEventInput:
    # ``date`` is the Eastern session. For a timed decision that is the
    # instant's ET date (a Tokyo noon is the previous evening in New York);
    # for a date-only one it is the bank's own date, the only one there is.
    day = decision.date
    if decision.at is not None:
        day = decision.at.astimezone(ET_ZONE).date()
    return CalendarEventInput(
        kind=CalendarKind.CENTRAL_BANK,
        source=CalendarSource.SEED,
        vendor_id=_vendor_id(decision.bank, decision.date),
        title=f"{_BANK_LABEL[decision.bank]} policy decision",
        date=day,
        at=decision.at,
    )


def central_bank_inputs(seed: CentralBankSeed) -> tuple[CalendarEventInput, ...]:
    """One ``central-bank`` input per seeded decision, in the seed's order."""
    return tuple(_central_bank_input(decision) for decision in seed.decisions)


def _covered_window(seed: CentralBankSeed, bank: Bank) -> tuple[date, date] | None:
    """The bank-calendar dates this file claims to list completely, or ``None``."""
    cover = seed.coverage_of(bank)
    year_end = date(seed.year, 12, 31)
    if cover.coverage is Coverage.FULL:
        return date(seed.year, 1, 1), year_end
    if cover.coverage is Coverage.FROM:
        assert cover.covers_from is not None
        return cover.covers_from, year_end
    return None


def import_central_bank_seed(
    session: Session, seed: CentralBankSeed, *, now: datetime
) -> UpsertCounts:
    """Upsert one year's decisions, and withdraw the ones the file contradicts.

    A seeded row whose decision falls inside the window the file claims to
    cover completely (``full``: the year; ``from``: on or after
    ``covers_from``) but which the file no longer lists is **withdrawn** --
    soft-deleted, so a later file that restores it revives the same row. A row
    outside that window is left alone: a file covering March onward says
    nothing about February. An ``unpublished`` bank claims nothing.
    """
    now = _utc_now(now)
    inputs = central_bank_inputs(seed)
    counts = upsert_events(session, inputs, now=now)

    listed = {event.vendor_id for event in inputs}
    windows = {bank: _covered_window(seed, bank) for bank in Bank}
    withdrawn = 0
    live_seed_rows = session.scalars(
        select(CalendarEvent).where(
            CalendarEvent.source == CalendarSource.SEED.value,
            CalendarEvent.kind == CalendarKind.CENTRAL_BANK.value,
            CalendarEvent.deleted_at.is_(None),
        )
    )
    for row in live_seed_rows:
        assert row.vendor_id is not None  # ck_calendar_event_vendor_id
        if row.vendor_id in listed:
            continue
        bank, bank_date = _parse_vendor_id(row.vendor_id)
        window = windows[bank]
        if window is None or not window[0] <= bank_date <= window[1]:
            continue
        row.deleted_at = now
        row.updated_at = now
        withdrawn += 1
    session.flush()
    return UpsertCounts(
        inserted=counts.inserted,
        updated=counts.updated,
        unchanged=counts.unchanged,
        withdrawn=withdrawn,
    )


def import_central_bank_year(
    session: Session, year: int, *, now: datetime, directory: Path | None = None
) -> SeedImport:
    """Import ``year``'s committed seed if it exists, and report its gaps either way.

    A malformed seed raises :class:`~corollary.data.seeds.SeedError` and
    writes nothing.
    """
    seed = load_central_bank_seed(year, directory)
    if seed is None:
        return SeedImport(year=year, counts=None, gaps=seed_gaps(year, directory))
    counts = import_central_bank_seed(session, seed, now=now)
    return SeedImport(year=year, counts=counts, gaps=seed.gaps())


def seed_gaps_between(
    start: date, end: date, directory: Path | None = None
) -> tuple[SeedGap, ...]:
    """Every central-bank gap for every year ``[start, end]`` touches, year by year.

    What the panel states beside a range: an unpublished bank, a partially
    seeded one, or a year with no seed file at all.
    """
    if end < start:
        raise ValueError(f"end {end.isoformat()} is before start {start.isoformat()}")
    gaps: list[SeedGap] = []
    for year in range(start.year, end.year + 1):
        gaps.extend(seed_gaps(year, directory))
    return tuple(gaps)


# --- manual geopolitical rows -------------------------------------------------


def _live_manual_row(session: Session, event_id: int) -> CalendarEvent:
    row = session.get(CalendarEvent, event_id)
    if row is None or row.deleted_at is not None:
        raise CalendarEventNotFoundError(event_id)
    if row.source != CalendarSource.MANUAL.value:
        raise CalendarEventNotEditableError(event_id, row.source, row.kind)
    return row


def create_manual(
    session: Session, *, title: str, date: date, at: datetime | None = None, now: datetime
) -> StoredCalendarEvent:
    """Add a geopolitical note (decision 9). Not audit-logged."""
    now = _utc_now(now)
    check_title(title)
    check_when(date, at)
    row = CalendarEvent(
        kind=CalendarKind.GEOPOLITICAL.value,
        source=CalendarSource.MANUAL.value,
        title=title,
        ticker=None,
        date=date,
        at=_utc(at),
        estimate=None,
        prior=None,
        actual=None,
        unit=None,
        session=None,
        vendor_id=None,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(row)
    session.flush()
    return _stored(row)


def update_manual(
    session: Session,
    event_id: int,
    *,
    title: str,
    date: date,
    at: datetime | None = None,
    now: datetime,
) -> StoredCalendarEvent:
    """Replace a manual row's title, date and time. Vendor and seed rows are refused."""
    now = _utc_now(now)
    row = _live_manual_row(session, event_id)
    check_title(title)
    check_when(date, at)
    if (row.title, row.date, row.at) != (title, date, at):
        row.title = title
        row.date = date
        row.at = _utc(at)
        row.updated_at = now
    session.flush()
    return _stored(row)


def delete_manual(session: Session, event_id: int, *, now: datetime) -> None:
    """Soft-delete a manual row. Vendor and seed rows are refused."""
    now = _utc_now(now)
    row = _live_manual_row(session, event_id)
    row.deleted_at = now
    row.updated_at = now
    session.flush()
