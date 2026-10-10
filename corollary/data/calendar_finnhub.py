"""Finnhub's earnings and IPO calendars as ``calendar_event`` inputs.

Phase 3 step 7, unit 7.2b-F. Decision 7 (earnings for the watch universe,
with estimate and actual; ``hour`` is a session, not a time) and Q15
(upcoming IPOs as a new kind, ``ipo``, refreshed daily). This module turns
the rows :class:`~corollary.data.providers.finnhub.FinnhubProvider` returns
into :class:`~corollary.data.calendar_event.CalendarEventInput` records for
:func:`corollary.data.calendar.upsert_events`. It knows Finnhub's field
names, and nothing else in ``corollary/data/`` does; it writes nothing and
schedules nothing -- the job that runs it daily at 07:00 ET is a later unit.

Decisions this module takes
---------------------------

**Earnings: EPS in ``estimate``/``actual``, revenue not stored.** A row
carries both an EPS and a revenue estimate/actual, and ``calendar_event`` has
one ``estimate`` and one ``actual``. EPS goes there, ``unit='EPS'``; revenue
is dropped. EPS is what a beat or miss is reported against, and two numbers
in different units cannot share a column. ``unit`` says ``EPS`` rather than
``USD`` because Finnhub states no currency, and a foreign filer's EPS is not
dollars. A missing EPS stays ``None`` -- revenue never stands in for it.

**Earnings: ``at`` is always ``None``.** ``hour`` maps to
:class:`~corollary.data.calendar_event.EarningsSession` (``bmo``/``amc``/
``dmh``); empty, absent or unrecognised is ``None``, the unrecognised case
logged. No instant is ever composed from a session.

**Earnings ``vendor_id`` is ``SYMBOL:YYYYQn``** -- the fiscal year and
quarter Finnhub reports, never the date, so a rescheduled report moves its
row instead of leaving a phantom on the old date.

**IPO ``vendor_id`` is ``symbol:SYM``, else ``name:<normalised name>``**
(orchestrator decision). Never the date, so a moved IPO date updates the row.
9 of the 58 recorded past rows have no symbol (8 null, 1 ``""``); they are
kept with ``ticker=None`` and the company name as the title. The name is
normalised -- case-folded, punctuation and runs of space collapsed -- so
``"Holtec Nuclear Corp"`` and ``"HOLTEC Nuclear Corp."`` are one company.
A row first listed by name and later given a symbol changes key; the
name-keyed row is withdrawn by :func:`corollary.data.calendar.replace_window`
when the scheduler hands it a complete fetch of the window (unit 7.2c-1), and
stays behind under plain upsert. The recorded past window shows exactly this
pair (``New Iceland Arctic Acquisition Corp.``, once without a symbol, once
as ``NIAAU``) -- as two rows with different statuses, which is what Finnhub
sent.

**IPO price is text.** ``"14.00-16.00"`` is a range, ``"10.00"`` a single
price (low == high), null or ``""`` none; parsed with an ASCII-only pattern
straight to ``Decimal``. Anything else skips the row.

**What skips a row, and what only nulls a field.** A value that cannot be
read *exactly* -- an EPS that is a bool, NaN or text that is not a number;
an offer price that does not parse; a share count that is not a whole
number; no date; no identity; a number too wide for its column (an EPS of
``1e50``, a share count past 64 bits) -- **skips the row**, reported in
:attr:`FinnhubCalendarBatch.skipped` and logged with its rule. A *label*
the vendor spelled in a way not recognised (``hour``, ``status``) nulls that
field and keeps the row, logged, per decision 7's "empty or unknown ->
NULL". A share count of ``0`` is treated as absent, logged: the recorded
``totalSharesValue`` uses 0 for "unknown", and 0 shares offered is not an
offering.

**One key twice in one response.** Identical rows collapse to one.
Conflicting rows under one key are **all** skipped and reported: nothing
says which is current, and picking by position would hide the conflict.
:func:`~corollary.data.calendar.upsert_events` would refuse the batch
anyway; this keeps one bad pair from costing every other row.

**A finite ``float`` is our bug, not a skip.** The provider decodes with
``parse_float=Decimal``; a finite float means the decoder was bypassed, and
:func:`corollary.wire.as_decimal` raises ``TypeError`` for it, deliberately
uncaught. NaN and Infinity can come off the wire (``json.loads`` accepts the
extensions and ``parse_float`` does not intercept them), so those are the
vendor's and skip the row.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from corollary.data.calendar_event import (
    TITLE_MAX,
    VENDOR_ID_MAX,
    CalendarEventInput,
    CalendarKind,
    CalendarSource,
    EarningsSession,
    IpoStatus,
)
from corollary.data.providers.finnhub import FinnhubProvider
from corollary.wire import WireFormatError, as_decimal

__all__ = [
    "EARNINGS_WINDOW",
    "EARNINGS_UNIT",
    "FinnhubCalendarBatch",
    "IPO_WINDOW",
    "SkippedRow",
    "earnings_events",
    "earnings_vendor_id",
    "fetch_earnings",
    "fetch_ipos",
    "ipo_events",
    "ipo_vendor_id",
    "parse_offer_price",
]

logger = logging.getLogger(__name__)

#: The spec's earnings window: today -> +21 days (*Feeds and budgets*).
EARNINGS_WINDOW: Final = timedelta(days=21)

#: The IPO window the Q15 probe used: today -> +30 days.
IPO_WINDOW: Final = timedelta(days=30)

#: ``unit`` on an earnings row: the columns hold EPS, in a currency Finnhub
#: does not state.
EARNINGS_UNIT: Final = "EPS"

_SESSIONS: Final = {session.value: session for session in EarningsSession}
_STATUSES: Final = {status.value: status for status in IpoStatus}

#: ``YYYY-MM-DD`` only. ``date.fromisoformat`` also takes ``20261015`` and ISO
#: week dates, which the vendor has never sent; one arriving is a change.
_DATE_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

#: An offer price: one ASCII decimal, or two joined by a hyphen.
_PRICE_RE: Final = re.compile(
    r"\s*([0-9]+(?:\.[0-9]+)?)\s*(?:-\s*([0-9]+(?:\.[0-9]+)?)\s*)?"
)

_NAME_PREFIX: Final = "name:"
_SYMBOL_PREFIX: Final = "symbol:"


class _RowError(ValueError):
    """One vendor row cannot be read exactly; it is skipped and reported."""


@dataclass(frozen=True)
class SkippedRow:
    """A row left out of the batch: its position in the response, and why."""

    index: int
    reason: str


@dataclass(frozen=True)
class FinnhubCalendarBatch:
    """What one calendar response became.

    ``events`` go to :func:`~corollary.data.calendar.upsert_events`.
    ``skipped`` are rows that could not be read, each logged.
    ``outside_watch`` counts earnings rows for symbols outside the watch
    universe -- filtered on purpose, so not reported as skips. Always 0 for
    IPOs, which are market-wide.
    """

    events: tuple[CalendarEventInput, ...]
    skipped: tuple[SkippedRow, ...]
    outside_watch: int = 0


# --- field readers ------------------------------------------------------------


def _text(row: Mapping[str, object], field: str) -> str | None:
    """A text field stripped, ``None`` if absent or blank; a non-string is a row error."""
    raw = row.get(field)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _RowError(f"{field} is a {type(raw).__name__} ({raw!r}), not text")
    stripped = raw.strip()
    return stripped or None


def _date(row: Mapping[str, object]) -> date:
    raw = row.get("date")
    if not isinstance(raw, str) or not _DATE_RE.fullmatch(raw):
        raise _RowError(f"date {raw!r} is not a YYYY-MM-DD calendar date")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise _RowError(f"date {raw!r} is not a calendar date: {exc}") from exc


def _whole(row: Mapping[str, object], field: str) -> int | None:
    """An integer field. ``bool`` and a ``Decimal`` are refused, even ``Decimal('2027')``."""
    raw = row.get(field)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _RowError(f"{field} is {raw!r}, not a whole number")
    return raw


def _number(row: Mapping[str, object], field: str) -> Decimal | None:
    """A money-path number as an exact ``Decimal``, or ``None``.

    The vendor shapes (bool, NaN, Infinity, a list, text that is not a
    number) are row errors. A *finite* float passes through to
    :func:`corollary.wire.as_decimal`, which raises ``TypeError``: that is a
    bypassed decoder, our bug, and is meant to be loud.
    """
    raw = row.get(field)
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise _RowError(f"{field} is a bool ({raw!r}), not a number")
    if isinstance(raw, float):
        if not math.isfinite(raw):
            raise _RowError(f"{field} is {raw!r}, which no arithmetic here can use")
        return as_decimal(raw)  # raises TypeError: the decoder was bypassed
    if not isinstance(raw, (Decimal, int, str)):
        raise _RowError(f"{field} is a {type(raw).__name__}, not a number")
    try:
        value = as_decimal(raw)
    except (WireFormatError, ArithmeticError) as exc:
        raise _RowError(f"{field} {raw!r} is not a number: {exc}") from exc
    if value is not None and not value.is_finite():
        raise _RowError(f"{field} is {raw!r}, which no arithmetic here can use")
    return value


def _ticker(symbol: str) -> str:
    return symbol.strip().upper()


# --- earnings ---------------------------------------------------------------------


def earnings_vendor_id(symbol: str, year: int, quarter: int) -> str:
    """``NVDA:2027Q3`` -- the fiscal period, never the date."""
    return f"{_ticker(symbol)}:{year}Q{quarter}"


def _session(row: Mapping[str, object], symbol: str) -> EarningsSession | None:
    raw = row.get("hour")
    if raw is None or raw == "":
        return None
    session = _SESSIONS.get(raw) if isinstance(raw, str) else None
    if session is None:
        logger.warning(
            "earnings row for %s has an unrecognised hour %r; stored with no session",
            symbol,
            raw,
            extra={
                "event": "finnhub_earnings_unknown_hour",
                "rule": (
                    "decision 7: hour is a session (bmo/amc/dmh), never a time; "
                    "an empty or unknown hour is NULL"
                ),
                "symbol": symbol,
                "hour": raw,
            },
        )
    return session


def _earnings_input(row: Mapping[str, object], symbol: str) -> CalendarEventInput:
    year = _whole(row, "year")
    quarter = _whole(row, "quarter")
    if year is None:
        raise _RowError("year is missing; the fiscal period is the row's key")
    if quarter is None or not 1 <= quarter <= 4:
        raise _RowError(f"quarter {quarter!r} is not 1-4; the fiscal period is the row's key")
    return CalendarEventInput(
        kind=CalendarKind.EARNINGS,
        source=CalendarSource.FINNHUB,
        vendor_id=earnings_vendor_id(symbol, year, quarter),
        title=f"{symbol} earnings",
        date=_date(row),
        at=None,
        ticker=symbol,
        estimate=_number(row, "epsEstimate"),
        actual=_number(row, "epsActual"),
        unit=EARNINGS_UNIT,
        session=_session(row, symbol),
    )


def earnings_events(rows: Sequence[object], watch: Iterable[str]) -> FinnhubCalendarBatch:
    """``/calendar/earnings`` rows as inputs, filtered to the ``watch`` universe."""
    watched = {_ticker(symbol) for symbol in watch if symbol.strip()}
    built: list[tuple[int, CalendarEventInput]] = []
    skipped: list[SkippedRow] = []
    outside = 0
    for index, row in enumerate(rows):
        try:
            if not isinstance(row, Mapping):
                raise _RowError(f"row is a {type(row).__name__}, not an object")
            raw_symbol = _text(row, "symbol")
            if raw_symbol is None:
                raise _RowError("symbol is missing or empty")
            symbol = _ticker(raw_symbol)
            if symbol not in watched:
                outside += 1
                continue
            built.append((index, _earnings_input(row, symbol)))
        except ValueError as exc:  # _RowError, and CalendarEventInput's refusals
            skipped.append(_skip(CalendarKind.EARNINGS, index, str(exc)))
    events, conflicts = _dedupe(CalendarKind.EARNINGS, built)
    return FinnhubCalendarBatch(
        events=events,
        skipped=tuple(sorted([*skipped, *conflicts], key=lambda s: s.index)),
        outside_watch=outside,
    )


# --- IPOs ---------------------------------------------------------------------------


def _normalised_name(name: str) -> str:
    words = re.sub(r"[^0-9a-z]+", " ", name.casefold()).split()
    return " ".join(words)


def ipo_vendor_id(symbol: str | None, name: str | None) -> str | None:
    """``symbol:SYM``, else ``name:<normalised>``, else ``None``. Never the date.

    A normalised name longer than the column is cut to fit; two names that
    agree for the first ~120 characters are one company here.
    """
    if symbol is not None and symbol.strip():
        return f"{_SYMBOL_PREFIX}{_ticker(symbol)}"
    if name is None:
        return None
    normalised = _normalised_name(name)
    if not normalised:
        return None
    return f"{_NAME_PREFIX}{normalised}"[:VENDOR_ID_MAX].rstrip()


def parse_offer_price(text: str | None) -> tuple[Decimal, Decimal] | None:
    """``"14.00-16.00"`` -> ``(14.00, 16.00)``; ``"10.00"`` -> ``(10.00, 10.00)``.

    ``None`` or blank is no price. Anything else, or an inverted range,
    raises ``ValueError``.
    """
    if text is None or not text.strip():
        return None
    match = _PRICE_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"price {text!r} is neither a price nor a low-high range")
    low = Decimal(match.group(1))
    high = Decimal(match.group(2)) if match.group(2) is not None else low
    if low > high:
        raise ValueError(f"price {text!r} is an inverted range")
    return low, high


def _status(row: Mapping[str, object], label: str) -> IpoStatus | None:
    raw = row.get("status")
    if raw is None or raw == "":
        return None
    status = _STATUSES.get(raw) if isinstance(raw, str) else None
    if status is None:
        logger.warning(
            "IPO row for %s has an unrecognised status %r; stored with no status",
            label,
            raw,
            extra={
                "event": "finnhub_ipo_unknown_status",
                "rule": "Q15: status is one of expected/filed/priced/withdrawn; unknown is NULL",
                "ipo": label,
                "status": raw,
            },
        )
    return status


def _shares(row: Mapping[str, object], label: str) -> int | None:
    shares = _whole(row, "numberOfShares")
    if shares is None:
        return None
    if shares < 0:
        raise _RowError(f"numberOfShares is {shares}, a negative share count")
    if shares == 0:
        logger.info(
            "IPO row for %s gives 0 shares offered; stored with no share count",
            label,
            extra={
                "event": "finnhub_ipo_zero_shares",
                "rule": "Q15: 0 shares offered is not an offering; the count is unknown",
                "ipo": label,
            },
        )
        return None
    return shares


def _ipo_input(row: Mapping[str, object]) -> CalendarEventInput:
    symbol = _text(row, "symbol")
    name = _text(row, "name")
    vendor_id = ipo_vendor_id(symbol, name)
    if vendor_id is None:
        raise _RowError("neither symbol nor name is present; the row has no identity")
    ticker = None if symbol is None else _ticker(symbol)
    title = name if name is not None else f"{ticker} IPO"
    if len(title) > TITLE_MAX:
        raise _RowError(f"name is {len(title)} characters; the title column holds {TITLE_MAX}")
    label = ticker or name or vendor_id
    raw_price = row.get("price")
    if raw_price is not None and not isinstance(raw_price, str):
        raise _RowError(f"price is a {type(raw_price).__name__} ({raw_price!r}), not text")
    try:
        price = parse_offer_price(raw_price)
    except ValueError as exc:
        raise _RowError(str(exc)) from exc
    return CalendarEventInput(
        kind=CalendarKind.IPO,
        source=CalendarSource.FINNHUB,
        vendor_id=vendor_id,
        title=title,
        date=_date(row),
        at=None,
        ticker=ticker,
        exchange=_text(row, "exchange"),
        shares=_shares(row, label),
        price_low=None if price is None else price[0],
        price_high=None if price is None else price[1],
        ipo_status=_status(row, label),
    )


def ipo_events(rows: Sequence[object]) -> FinnhubCalendarBatch:
    """``/calendar/ipo`` rows as inputs. Market-wide: no watch filter."""
    built: list[tuple[int, CalendarEventInput]] = []
    skipped: list[SkippedRow] = []
    for index, row in enumerate(rows):
        try:
            if not isinstance(row, Mapping):
                raise _RowError(f"row is a {type(row).__name__}, not an object")
            built.append((index, _ipo_input(row)))
        except ValueError as exc:
            skipped.append(_skip(CalendarKind.IPO, index, str(exc)))
    events, conflicts = _dedupe(CalendarKind.IPO, built)
    return FinnhubCalendarBatch(
        events=events, skipped=tuple(sorted([*skipped, *conflicts], key=lambda s: s.index))
    )


# --- shared ----------------------------------------------------------------------------


def _skip(kind: CalendarKind, index: int, reason: str) -> SkippedRow:
    logger.warning(
        "skipped Finnhub %s row %d: %s",
        kind.value,
        index,
        reason,
        extra={
            "event": "finnhub_calendar_row_skipped",
            "rule": (
                "unit 7.2b-F: a calendar row that cannot be read exactly is skipped "
                "and reported, never stored with a guessed value"
            ),
            "kind": kind.value,
            "index": index,
            "reason": reason,
        },
    )
    return SkippedRow(index=index, reason=reason)


def _dedupe(
    kind: CalendarKind, built: list[tuple[int, CalendarEventInput]]
) -> tuple[tuple[CalendarEventInput, ...], list[SkippedRow]]:
    """Identical rows under one key collapse; conflicting ones are all skipped."""
    by_key: dict[str, list[tuple[int, CalendarEventInput]]] = {}
    for index, event in built:
        by_key.setdefault(event.vendor_id, []).append((index, event))
    events: list[CalendarEventInput] = []
    conflicts: list[SkippedRow] = []
    for vendor_id, group in by_key.items():
        first = group[0][1]
        if all(event == first for _, event in group):
            events.append(first)
            continue
        indexes = [index for index, _ in group]
        for index in indexes:
            conflicts.append(
                _skip(
                    kind,
                    index,
                    f"rows {indexes} share the key {vendor_id!r} and disagree; "
                    "none is taken as current",
                )
            )
    return tuple(events), conflicts


async def fetch_earnings(
    provider: FinnhubProvider, watch: Iterable[str], *, start: date, end: date
) -> FinnhubCalendarBatch:
    """One ``/calendar/earnings`` request for ``[start, end]``, mapped and filtered."""
    return earnings_events(await provider.earnings_calendar(start, end), watch)


async def fetch_ipos(provider: FinnhubProvider, *, start: date, end: date) -> FinnhubCalendarBatch:
    """One ``/calendar/ipo`` request for ``[start, end]``, mapped."""
    return ipo_events(await provider.ipo_calendar(start, end))
