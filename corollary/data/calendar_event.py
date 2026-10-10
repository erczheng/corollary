"""The vendor-neutral calendar event every calendar producer hands the store.

Phase 3 step 7. Decisions 7 (earnings from Finnhub, dividends from Alpaca),
8 (central-bank dates from a committed seed), 9 (geopolitical rows entered by
hand), Q3 (economic releases from FRED, consensus unavailable) and Q15 (IPOs
from Finnhub). This module is the record that crosses from a fetcher into
:mod:`corollary.data.calendar`, the storage layer. It imports nothing from
``corollary/db/`` -- the models import *it*, for the CHECK sets -- and
nothing from ``corollary/data/providers/``, so the store never learns a
vendor's wire shape, only ``source`` as data.

What this record is deliberate about
------------------------------------

**Kind and source are closed sets, and paired.** :class:`CalendarKind` uses
the frontend's ``CalendarEventType`` spellings (``web/src/lib/types.ts``)
plus ``ipo`` (Q15). :data:`KIND_SOURCE` names the one producer the spec
gives each kind, so a fetcher that writes earnings under ``alpaca`` is
refused at construction, and the ``calendar_event`` CHECK refuses it again at
the write boundary. Widening a pairing is a migration, on purpose.

**``date`` and ``at`` are separate, and agree.** ``date`` is the Eastern
session the event falls on; ``at`` is the instant, or ``None`` for an event
that is a property of a day (an ex-date, a BoJ decision, an earnings report
whose vendor gives only "before open"). A timed event's ``date`` must be its
instant's **Eastern** date: written as the UTC day, a 20:30 ET release would
group under the next morning. That is the invariant the frontend's
``CalendarEvent.date`` documents, refused here rather than repaired.

**No manual rows.** ``source='manual'`` rows -- decision 9's geopolitical
notes -- are created only through
:func:`corollary.data.calendar.create_manual`, which gives them no vendor
key. This record is for producers that re-fetch, and every one of those
carries the ``vendor_id`` its re-fetch upserts on.

**An earnings session is not a time.** Finnhub gives an earnings report an
``hour`` of ``bmo``/``amc``/``dmh`` and no instant; decision 7 renders it
"Before open" / "After close". It is stored as :class:`EarningsSession` in
``session`` -- only on an earnings row -- and ``at`` stays ``None``, because a
placeholder time is how a calendar row reads "7:00 PM" for something that
never had one. An empty or unknown ``hour`` is ``None``; mapping the vendor's
string to the enum is the fetcher's job, so a raw string is refused here.

**Money is ``Decimal``; a float is refused.** ``estimate``, ``prior`` and
``actual`` land in ``Money`` columns. ``estimate`` is ``None`` for an
economic release (Q3: consensus is unavailable on the free plan), and the
record refuses one rather than storing a number nobody supplied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping
from zoneinfo import ZoneInfo

__all__ = [
    "CalendarEventInput",
    "CalendarKind",
    "CalendarSource",
    "ET_ZONE",
    "EarningsSession",
    "KIND_SOURCE",
    "TICKER_REQUIRED_KINDS",
    "check_title",
    "check_when",
]

#: Display zone, and the zone ``date`` is stated in.
ET_ZONE: Final = ZoneInfo("America/New_York")

#: Widths the ``calendar_event`` columns carry. Repeated in migration 0013.
TITLE_MAX: Final = 256
TICKER_MAX: Final = 16
UNIT_MAX: Final = 32
VENDOR_ID_MAX: Final = 128


class CalendarKind(StrEnum):
    """What the event is. The frontend's ``CalendarEventType`` spellings, plus ``ipo``."""

    EARNINGS = "earnings"
    ECONOMIC = "economic"
    CENTRAL_BANK = "central-bank"
    DIVIDEND = "dividend"
    GEOPOLITICAL = "geopolitical"
    IPO = "ipo"


class CalendarSource(StrEnum):
    """Who produced the row."""

    FINNHUB = "finnhub"
    ALPACA = "alpaca"
    FRED = "fred"
    SEED = "seed"
    MANUAL = "manual"


class EarningsSession(StrEnum):
    """When in the session an earnings report lands (decision 7). Finnhub's ``hour`` spellings."""

    BMO = "bmo"  # before market open
    AMC = "amc"  # after market close
    DMH = "dmh"  # during market hours


#: The one producer the spec gives each kind. Decision 7: earnings from
#: Finnhub, dividends from Alpaca corporate actions. Q15: IPOs from Finnhub.
#: Q3: economic releases from FRED's release dates (the times table is a
#: lookup, not a producer). Decision 8: central banks from the committed seed.
#: Decision 9: geopolitical rows by hand.
KIND_SOURCE: Final[Mapping[CalendarKind, CalendarSource]] = MappingProxyType(
    {
        CalendarKind.EARNINGS: CalendarSource.FINNHUB,
        CalendarKind.ECONOMIC: CalendarSource.FRED,
        CalendarKind.CENTRAL_BANK: CalendarSource.SEED,
        CalendarKind.DIVIDEND: CalendarSource.ALPACA,
        CalendarKind.GEOPOLITICAL: CalendarSource.MANUAL,
        CalendarKind.IPO: CalendarSource.FINNHUB,
    }
)

#: Kinds that are about one company and must name it. ``ipo`` is absent on
#: purpose: an upcoming IPO can be listed before its symbol is assigned.
TICKER_REQUIRED_KINDS: Final = frozenset({CalendarKind.EARNINGS, CalendarKind.DIVIDEND})


def check_title(title: str) -> str:
    """The title, refused if blank or too long for its column."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("a calendar event needs a title")
    if len(title) > TITLE_MAX:
        raise ValueError(f"title is {len(title)} characters; the column holds {TITLE_MAX}")
    return title


def check_when(day: date, at: datetime | None) -> None:
    """Refuse a ``date``/``at`` pair that disagrees, or an instant with no zone.

    ``datetime`` is a subclass of ``date``, so a ``datetime`` passed as
    ``day`` would type-check and then be stored as whatever SQLAlchemy makes
    of it. Refused.
    """
    if isinstance(day, datetime) or not isinstance(day, date):
        raise ValueError(f"date must be a calendar date, got {type(day).__name__} ({day!r})")
    if at is None:
        return
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError(f"at must be a timezone-aware datetime, got {at!r}")
    shown = at.astimezone(ET_ZONE).date()
    if shown != day:
        raise ValueError(
            f"date {day.isoformat()} is not the Eastern date of at ({at.isoformat()} is "
            f"{shown.isoformat()} in America/New_York); date is the Eastern session, "
            "never the UTC day"
        )


def _check_money(name: str, value: Decimal | None) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal):
        raise ValueError(
            f"{name} must be a Decimal, got {type(value).__name__} ({value!r}) -- "
            "see CLAUDE.md Conventions"
        )
    if not value.is_finite():
        raise ValueError(f"{name} must be a finite number, got {value!r}")


@dataclass(frozen=True)
class CalendarEventInput:
    """One vendor or seed event, keyed for upsert by ``(source, kind, vendor_id)``.

    ``vendor_id`` is the producer's stable identity for the event -- the
    vendor's own id where it has one (an Alpaca corporate-action id), else a
    key the fetcher composes from what does not change when the event is
    rescheduled (``NVDA:2026Q3`` for an earnings report, ``FOMC:2027-01-27``
    for a seeded decision). It must not contain the date of an event whose
    date can move, or a reschedule becomes a second row.
    """

    kind: CalendarKind
    source: CalendarSource
    vendor_id: str
    title: str
    date: date
    at: datetime | None = None
    ticker: str | None = None
    estimate: Decimal | None = None
    prior: Decimal | None = None
    actual: Decimal | None = None
    unit: str | None = None
    #: Earnings only; ``None`` when the vendor gave no (or no known) session.
    session: EarningsSession | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CalendarKind):
            raise ValueError(f"kind must be a CalendarKind, got {self.kind!r}")
        if not isinstance(self.source, CalendarSource):
            raise ValueError(f"source must be a CalendarSource, got {self.source!r}")
        if self.source is CalendarSource.MANUAL:
            raise ValueError(
                "manual rows are not upserted: create them with "
                "corollary.data.calendar.create_manual"
            )
        if KIND_SOURCE[self.kind] is not self.source:
            raise ValueError(
                f"{self.kind.value} events come from {KIND_SOURCE[self.kind].value}, "
                f"not {self.source.value}"
            )
        if not isinstance(self.vendor_id, str) or not self.vendor_id.strip():
            raise ValueError("a vendor or seed event needs a vendor_id to upsert on")
        if len(self.vendor_id) > VENDOR_ID_MAX:
            raise ValueError(f"vendor_id is longer than {VENDOR_ID_MAX} characters")
        check_title(self.title)
        check_when(self.date, self.at)
        if self.ticker is not None:
            if not self.ticker or self.ticker != self.ticker.upper():
                raise ValueError(f"ticker {self.ticker!r} must be non-empty and upper case")
            if len(self.ticker) > TICKER_MAX:
                raise ValueError(f"ticker {self.ticker!r} is longer than {TICKER_MAX}")
        elif self.kind in TICKER_REQUIRED_KINDS:
            raise ValueError(f"a {self.kind.value} event must name its ticker")
        for name in ("estimate", "prior", "actual"):
            _check_money(name, getattr(self, name))
        if self.kind is CalendarKind.ECONOMIC and self.estimate is not None:
            raise ValueError(
                "an economic release carries no estimate: consensus is unavailable (Q3)"
            )
        if self.unit is not None and (not self.unit.strip() or len(self.unit) > UNIT_MAX):
            raise ValueError(f"unit {self.unit!r} must be non-empty and at most {UNIT_MAX}")
        if self.session is not None:
            if not isinstance(self.session, EarningsSession):
                raise ValueError(
                    f"session must be an EarningsSession or None, got {self.session!r}; "
                    "an empty or unknown vendor hour is None"
                )
            if self.kind is not CalendarKind.EARNINGS:
                raise ValueError(
                    f"a {self.kind.value} event has no earnings session; only earnings do"
                )
