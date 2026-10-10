"""Announced cash dividends into ``calendar_event`` rows (Phase 3 step 7, unit 7.2b-A).

Decision 7: dividends come from Alpaca corporate actions, and *"a dividend is
date-only (``at: null``)"*. This module is the vendor-neutral half: the
records a provider reads a dividend into (:class:`CashDividend`,
:class:`SkippedDividend`, :class:`CashDividendRead`), and the mapping from
those to :class:`~corollary.data.calendar_event.CalendarEventInput`. It
imports nothing vendor-shaped; ``AlpacaProvider.cash_dividends`` in
``data/providers/alpaca.py`` produces the records and is the only code that
knows the wire format. Nothing here is scheduled -- a later unit wires the
job.

What the probe established, and what this module does about each
-----------------------------------------------------------------

Recorded 2026-10-10 in ``tests/fixtures/alpaca/p7_corporate_actions_*``:

* **The request window filters on process/payable date, not ``ex_date``.** A
  2026 window returns 2025 ex-dates. So ``ex_date`` is filtered here, on
  ``today <= ex_date <= today + DIVIDEND_EX_DATE_HORIZON``, both ends
  inclusive.
* **The request reaches further than the kept window** --
  ``end = today + DIVIDEND_REQUEST_HORIZON`` -- because a row is found by its
  process date, which trails its ex-date (``process_date - ex_date``: median
  16 days, p99 126). The arithmetic is not airtight and is stated so: a row
  is missed when its ex-date is inside the kept window but its process date
  is past ``end``, i.e. when ``process - ex > 180 - (ex - today)``. Near the
  far edge (ex-date ~90 days out) that is a gap over ~90 days, which the
  probe puts between p90 (39) and p99 (126) of rows. Such a row is not lost,
  only late: it appears on the day its process date enters the window.
* **The start reaches back a week** (:data:`DIVIDEND_REQUEST_LOOKBACK`). The
  probe saw ``process_date`` one day *before* ``ex_date`` on 6 rows; with
  ``start = today`` such a row whose ex-date is today would be outside the
  request. A week covers the observed -1 with margin, at the cost of a few
  already-processed rows the ex-date filter then drops.
* **``rate`` is an unquoted JSON number.** The provider reads it from the
  literal text to an exact ``Decimal``; this module refuses anything else at
  :class:`CashDividend` construction.

Outcomes, kept apart
--------------------

The calendar panel has three different things to say, and conflating any two
of them misinforms: *"no dividends announced in the window"*, *"dividends
were announced"* and *"the dividend fetch failed"*. :class:`DividendOutcome`
names which one happened. ``NONE_ANNOUNCED`` is claimed only when the read
succeeded and **no** row -- readable or not -- fell in the window for the
watch universe: a row that was announced but could not be read is
``ANNOUNCED`` with the row in :attr:`DividendFetch.skipped`, because "none
announced" would then be false.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol

from corollary.data.calendar_event import CalendarEventInput, CalendarKind, CalendarSource
from corollary.data.news.watchlist import MARKET_TICKER
from corollary.data.providers.interface import ProviderError
from corollary.data.seeds import normalize_symbol

__all__ = [
    "DIVIDEND_EX_DATE_HORIZON",
    "DIVIDEND_REQUEST_HORIZON",
    "DIVIDEND_REQUEST_LOOKBACK",
    "DIVIDEND_TITLE",
    "DIVIDEND_UNIT",
    "CashDividend",
    "CashDividendRead",
    "CashDividendSource",
    "DividendFetch",
    "DividendOutcome",
    "SkippedDividend",
    "dividend_events",
    "fetch_dividends",
]

logger = logging.getLogger(__name__)

#: How far past today the request's ``end`` reaches. The window filters on
#: process/payable date, which trails the ex-date; see the module docstring.
DIVIDEND_REQUEST_HORIZON: Final = timedelta(days=180)

#: The furthest ex-date kept, from today, inclusive.
DIVIDEND_EX_DATE_HORIZON: Final = timedelta(days=90)

#: How far before today the request's ``start`` reaches. The probe saw
#: ``process_date`` at most one day before ``ex_date``; a week is the margin.
DIVIDEND_REQUEST_LOOKBACK: Final = timedelta(days=7)

#: What ``actual`` is measured in. Alpaca's ``rate`` is cash per share; the
#: reference gives it no currency field, and every row the probe saw is a
#: US-listed security quoted in dollars.
DIVIDEND_UNIT: Final = "USD/share"

#: The frontend's own wording for an ex-date row (``mockData.ts``'s fixtures).
DIVIDEND_TITLE: Final = "Ex-dividend date"


@dataclass(frozen=True)
class CashDividend:
    """One announced cash dividend, as read from the vendor. Exact, never a float.

    ``sub_type`` is the vendor's own qualifier (Alpaca documents
    ``interest`` and ``return_of_capital``), ``None`` on an ordinary
    dividend. ``foreign`` is carried as read and deliberately **not** used:
    Alpaca documents it as a required boolean with no description, and a
    title asserting what it means would be a claim the data does not make.
    """

    vendor_id: str
    symbol: str
    ex_date: date
    rate: Decimal
    special: bool
    sub_type: str | None = None
    foreign: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.vendor_id, str) or not self.vendor_id.strip():
            raise ValueError("a dividend needs the vendor's id")
        if not isinstance(self.symbol, str) or self.symbol != normalize_symbol(self.symbol):
            raise ValueError(f"symbol {self.symbol!r} is not a normalised symbol")
        if not self.symbol:
            raise ValueError("a dividend needs a symbol")
        if isinstance(self.ex_date, datetime) or not isinstance(self.ex_date, date):
            raise ValueError(f"ex_date must be a calendar date, got {self.ex_date!r}")
        if not isinstance(self.rate, Decimal):
            raise ValueError(
                f"rate must be a Decimal, got {type(self.rate).__name__} ({self.rate!r}) -- "
                "see CLAUDE.md Conventions"
            )
        if not self.rate.is_finite() or self.rate <= 0:
            raise ValueError(f"rate must be a finite positive amount, got {self.rate!r}")
        if not isinstance(self.special, bool):
            raise ValueError(f"special must be a bool, got {self.special!r}")
        if self.sub_type is not None and (
            not isinstance(self.sub_type, str) or not self.sub_type.strip()
        ):
            raise ValueError(f"sub_type must be a non-blank string or None, got {self.sub_type!r}")


@dataclass(frozen=True)
class SkippedDividend:
    """A row the provider or the mapping refused, with whatever of it was readable.

    ``vendor_id``, ``symbol`` and ``ex_date`` are ``None`` where they were
    themselves the unreadable part. ``reason`` arrives already scrubbed of
    credentials by the provider.
    """

    vendor_id: str | None
    symbol: str | None
    ex_date: date | None
    reason: str


@dataclass(frozen=True)
class CashDividendRead:
    """Everything one corporate-actions read returned, in vendor order."""

    dividends: tuple[CashDividend, ...]
    skipped: tuple[SkippedDividend, ...]


class CashDividendSource(Protocol):
    """What :func:`fetch_dividends` needs from a provider. ``AlpacaProvider`` is one."""

    async def cash_dividends(
        self, *, symbols: Sequence[str], start: date, end: date
    ) -> CashDividendRead: ...


class DividendOutcome(StrEnum):
    """Which of the three answers a fetch gave. See the module docstring."""

    ANNOUNCED = "announced"
    NONE_ANNOUNCED = "none_announced"
    FAILED = "failed"


@dataclass(frozen=True)
class DividendFetch:
    """The calendar rows one fetch produced, and which answer it was.

    ``first_ex_date``..``last_ex_date`` is the kept window, both inclusive,
    so the panel can say *which* window had no dividends. ``error`` is set
    only on ``FAILED``.
    """

    outcome: DividendOutcome
    events: tuple[CalendarEventInput, ...]
    skipped: tuple[SkippedDividend, ...]
    first_ex_date: date
    last_ex_date: date
    error: str | None = None


def _watch_symbols(watch: Iterable[str]) -> tuple[str, ...]:
    """The watch universe as sorted, normalised security symbols; ``MARKET`` dropped."""
    symbols = {normalize_symbol(raw) for raw in watch}
    symbols.discard(MARKET_TICKER)
    symbols.discard("")
    if not symbols:
        raise ValueError(
            "the watch universe has no securities to read dividends for; an empty "
            "universe would report 'no dividends announced' about nothing"
        )
    return tuple(sorted(symbols))


def _title(dividend: CashDividend) -> str:
    qualifiers: list[str] = []
    if dividend.special:
        qualifiers.append("special")
    if dividend.sub_type is not None:
        qualifiers.append(dividend.sub_type.strip().replace("_", " "))
    if not qualifiers:
        return DIVIDEND_TITLE
    return f"{DIVIDEND_TITLE} ({', '.join(qualifiers)})"


def _relevant(
    symbol: str | None, ex_date: date | None, watch: frozenset[str], first: date, last: date
) -> bool:
    """Whether a skipped row could have been a calendar row. Unknown counts as yes."""
    if symbol is not None and symbol not in watch:
        return False
    if ex_date is not None and not first <= ex_date <= last:
        return False
    return True


def _log_skipped(skipped: SkippedDividend) -> None:
    logger.warning(
        "dividend row skipped (id %s, symbol %s): %s",
        skipped.vendor_id,
        skipped.symbol,
        skipped.reason,
        extra={
            "event": "calendar_dividend_skipped",
            "rule": (
                "a dividend row whose rate or identity cannot be read exactly is "
                "skipped and reported, never coerced"
            ),
            "vendor_id": skipped.vendor_id,
            "symbol": skipped.symbol,
            "ex_date": None if skipped.ex_date is None else skipped.ex_date.isoformat(),
            "cause": skipped.reason,
        },
    )


def dividend_events(
    read: CashDividendRead, *, today: date, watch: Iterable[str]
) -> tuple[tuple[CalendarEventInput, ...], tuple[SkippedDividend, ...]]:
    """The calendar rows ``read`` holds for ``watch`` in the window, and the skips that matter.

    Pure. Keeps a dividend when its symbol is watched and
    ``today <= ex_date <= today + DIVIDEND_EX_DATE_HORIZON``. A vendor id
    listed twice keeps its first row; the second is skipped, since the store
    refuses a batch that names one key twice. Skipped rows are returned only
    if they could have been calendar rows (watched, in the window, or with
    those parts unreadable); the rest are not this calendar's concern.

    Events are sorted by ``(date, ticker, vendor_id)`` so identical inputs give
    identical output whatever order the vendor paged in.
    """
    symbols = frozenset(_watch_symbols(watch))
    first, last = today, today + DIVIDEND_EX_DATE_HORIZON
    events: list[CalendarEventInput] = []
    skipped: list[SkippedDividend] = [
        row for row in read.skipped if _relevant(row.symbol, row.ex_date, symbols, first, last)
    ]
    seen: set[str] = set()
    for dividend in read.dividends:
        if dividend.vendor_id in seen:
            duplicate = SkippedDividend(
                vendor_id=dividend.vendor_id,
                symbol=dividend.symbol,
                ex_date=dividend.ex_date,
                reason="the vendor id is listed twice; the first row is kept",
            )
            if _relevant(dividend.symbol, dividend.ex_date, symbols, first, last):
                skipped.append(duplicate)
            continue
        seen.add(dividend.vendor_id)
        if dividend.symbol not in symbols or not first <= dividend.ex_date <= last:
            continue
        events.append(
            CalendarEventInput(
                kind=CalendarKind.DIVIDEND,
                source=CalendarSource.ALPACA,
                vendor_id=dividend.vendor_id,
                title=_title(dividend),
                date=dividend.ex_date,
                at=None,
                ticker=dividend.symbol,
                actual=dividend.rate,
                unit=DIVIDEND_UNIT,
            )
        )
    events.sort(key=lambda e: (e.date, e.ticker or "", e.vendor_id))
    skipped.sort(
        key=lambda s: (s.ex_date or date.min, s.symbol or "", s.vendor_id or "", s.reason)
    )
    return tuple(events), tuple(skipped)


async def fetch_dividends(
    source: CashDividendSource, *, today: date, watch: Iterable[str]
) -> DividendFetch:
    """Read the announced dividends for ``watch`` and map them; say which answer it was.

    ``today`` is the Eastern session date -- the caller's to state, so the
    window does not move with the machine's clock. One request window,
    ``[today - DIVIDEND_REQUEST_LOOKBACK, today + DIVIDEND_REQUEST_HORIZON]``,
    paginated by the provider. A :class:`ProviderError` (HTTP failure, plan
    refusal, 429, malformed body) is ``FAILED``, logged, and carries no rows;
    it is never reported as an empty window. An empty watch universe raises
    ``ValueError`` before any request.
    """
    symbols = _watch_symbols(watch)
    first, last = today, today + DIVIDEND_EX_DATE_HORIZON
    try:
        read = await source.cash_dividends(
            symbols=symbols,
            start=today - DIVIDEND_REQUEST_LOOKBACK,
            end=today + DIVIDEND_REQUEST_HORIZON,
        )
    except ProviderError as exc:
        logger.error(
            "dividend fetch failed: %s",
            exc,
            extra={
                "event": "calendar_dividend_fetch_failed",
                "rule": "a failed fetch is reported as failed, never as an empty window",
                "today": today.isoformat(),
                "symbols": len(symbols),
                "cause": str(exc),
            },
        )
        return DividendFetch(
            outcome=DividendOutcome.FAILED,
            events=(),
            skipped=(),
            first_ex_date=first,
            last_ex_date=last,
            error=str(exc),
        )
    events, skipped = dividend_events(read, today=today, watch=symbols)
    for row in skipped:
        _log_skipped(row)
    outcome = (
        DividendOutcome.ANNOUNCED if events or skipped else DividendOutcome.NONE_ANNOUNCED
    )
    return DividendFetch(
        outcome=outcome,
        events=events,
        skipped=skipped,
        first_ex_date=first,
        last_ex_date=last,
    )
