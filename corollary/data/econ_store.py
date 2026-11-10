"""The economic first-print store and its read side (spec R6 and R8, unit U6d).

``corollary.data.econ_values`` is the pure half -- the transform, the attempt
mapping and the display-state derivation -- and imports no database code.
This module is the half that does: it lives beside it rather than inside it
so that stays true.

Three tables are involved, and only two are written here:

* ``econ_release_value`` -- the first print. :func:`store_first_print` is
  **insert-only**: if the key already holds a row, it is returned unchanged,
  whatever figure the second call carried (Q23c keeps the first print).
* ``econ_release_fetch`` -- the last attempt. :func:`record_attempt` upserts
  it on every attempt.
* ``calendar_event`` -- **read only**. Its economic rows' ``prior`` and
  ``actual`` stay NULL: the daily release-dates refresh rewrites them
  through ``corollary.data.calendar._apply``, which is why the figure has a
  table of its own (R6).

:func:`read_economic_releases` joins the three for a date window by
``calendar_event.vendor_id = "<release_id>:<release_date>"`` and adds the
seed's ``unit_label`` and ``decimals``. The display state is derived from the
result by :meth:`EconomicRelease.display` (pure, ``econ_values.value_display``)
and never stored.

Every function flushes and none commits: the caller owns the transaction, as
in :mod:`corollary.data.calendar`. Every instant is passed in and must be
aware; it is stored as UTC.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from corollary.data.calendar import StoredCalendarEvent, read_range
from corollary.data.calendar_event import CalendarKind
from corollary.data.econ_values import (
    FETCH_OUTCOMES,
    Attempt,
    Figure,
    ValueDisplay,
    value_display,
)
from corollary.data.seeds.calendar_seeds import (
    EconReleaseSeeds,
    EconReleaseSeries,
    load_econ_release_seeds,
)
from corollary.db.models import EconReleaseFetch, EconReleaseValue

logger = logging.getLogger(__name__)

__all__ = [
    "EconomicRelease",
    "StoredEconValue",
    "StoredFetchAttempt",
    "parse_release_vendor_id",
    "read_attempt",
    "read_economic_releases",
    "read_value",
    "record_attempt",
    "store_first_print",
]

#: ``calendar_releases.release_vendor_id``'s shape, ``10:2026-10-14``.
_VENDOR_ID: Final = re.compile(r"([1-9][0-9]*):([0-9]{4}-[0-9]{2}-[0-9]{2})")


@dataclass(frozen=True)
class StoredEconValue:
    """One ``econ_release_value`` row, detached from its session."""

    release_id: int
    release_date: date
    series_id: str
    period: date
    actual: Decimal
    prior: Decimal
    unit_label: str
    vintage_date: date
    fetched_at: datetime


@dataclass(frozen=True)
class StoredFetchAttempt:
    """One ``econ_release_fetch`` row -- the last attempt -- detached from its session."""

    release_id: int
    release_date: date
    attempted_at: datetime
    attempt: Attempt


@dataclass(frozen=True)
class EconomicRelease:
    """One economic calendar row with its first print, seed row and last attempt.

    ``release_at`` is the calendar row's ``at``: the seed's ET release time on
    the release date (``econ_release_times.csv``, through
    ``calendar_releases.release_events``), as UTC. ``series`` is the release's
    ``econ_release_series.csv`` row, ``None`` only for a calendar row whose
    release has since left the seed.
    """

    event: StoredCalendarEvent
    release_id: int
    release_date: date
    release_at: datetime
    series: EconReleaseSeries | None
    value: StoredEconValue | None
    last_attempt: StoredFetchAttempt | None

    @property
    def unit_label(self) -> str | None:
        """The stored figure's own unit if there is one, else the seed's.

        The value row keeps the unit it was computed in, so a later seed edit
        cannot relabel a number already stored.
        """
        if self.value is not None:
            return self.value.unit_label
        return None if self.series is None else self.series.unit_label

    @property
    def decimals(self) -> int | None:
        """The seed's display precision (display rounds ``ROUND_HALF_UP``, R7)."""
        return None if self.series is None else self.series.decimals

    def display(self, now: datetime) -> ValueDisplay:
        """R8's display state at ``now``. Pure; see ``econ_values.value_display``."""
        return value_display(
            self.release_at,
            value_series_id=None if self.value is None else self.value.series_id,
            last_attempt=None if self.last_attempt is None else self.last_attempt.attempt,
            now=now,
        )


def _utc(at: datetime, name: str) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime, got {at!r}")
    return at.astimezone(UTC)


def _stored_value(row: EconReleaseValue) -> StoredEconValue:
    return StoredEconValue(
        release_id=row.release_id,
        release_date=row.release_date,
        series_id=row.series_id,
        period=row.period,
        actual=row.actual,
        prior=row.prior,
        unit_label=row.unit_label,
        vintage_date=row.vintage_date,
        fetched_at=row.fetched_at.astimezone(UTC),
    )


def _stored_attempt(row: EconReleaseFetch) -> StoredFetchAttempt:
    if row.outcome not in FETCH_OUTCOMES:  # pragma: no cover - ck_econ_release_fetch_outcome
        raise ValueError(f"econ_release_fetch holds an unknown outcome {row.outcome!r}")
    return StoredFetchAttempt(
        release_id=row.release_id,
        release_date=row.release_date,
        attempted_at=row.attempted_at.astimezone(UTC),
        attempt=Attempt(row.outcome, row.detail),
    )


# --- writes -----------------------------------------------------------------


def store_first_print(
    session: Session,
    release_id: int,
    release_date: date,
    series_row: EconReleaseSeries,
    figure: Figure,
    *,
    fetched_at: datetime,
) -> StoredEconValue:
    """Insert the release's first print, or return the one already stored.

    **Insert-only.** If ``(release_id, release_date)`` already has a row,
    nothing is written and that row is returned -- never updated, never
    overwritten, whatever ``figure`` says. A differing second figure is
    logged as a warning; the first stands.

    Raises (nothing written) for a caller's bug: a ``figure`` that is not a
    :class:`~corollary.data.econ_values.Figure`, a seed row for another
    release, a figure for another series than the seed row names, a figure
    read from another day's vintage than ``release_date``, or a naive
    ``fetched_at``. Flushes, never commits.
    """
    if not isinstance(figure, Figure):
        raise TypeError(f"only a Figure is stored, got {type(figure).__name__}")
    if series_row.release_id != release_id:
        raise ValueError(
            f"the seed row is release {series_row.release_id}'s, not release {release_id}'s"
        )
    if figure.series_id != series_row.series_id:
        raise ValueError(
            f"the figure is for {figure.series_id}, the seed row for {series_row.series_id}"
        )
    if figure.vintage_date != release_date:
        raise ValueError(
            f"the figure was read from the {figure.vintage_date} vintage, not the release "
            f"day {release_date}"
        )
    fetched_at = _utc(fetched_at, "fetched_at")

    existing = session.get(EconReleaseValue, (release_id, release_date))
    if existing is not None:
        kept = _stored_value(existing)
        if (kept.period, kept.actual, kept.prior) != (figure.period, figure.actual, figure.prior):
            logger.warning(
                "a second first print for release %s on %s differed; the first is kept",
                release_id,
                release_date.isoformat(),
                extra={
                    "event": "econ_first_print_kept",
                    "rule": "spec R6 / Q23c: a release's first print is written once",
                    "release_id": release_id,
                    "release_date": release_date.isoformat(),
                    "series_id": kept.series_id,
                    "kept": {
                        "period": kept.period.isoformat(),
                        "actual": str(kept.actual),
                        "prior": str(kept.prior),
                    },
                    "offered": {
                        "period": figure.period.isoformat(),
                        "actual": str(figure.actual),
                        "prior": str(figure.prior),
                    },
                    "at": fetched_at.isoformat(),
                },
            )
        return kept

    row = EconReleaseValue(
        release_id=release_id,
        release_date=release_date,
        series_id=figure.series_id,
        period=figure.period,
        actual=figure.actual,
        prior=figure.prior,
        unit_label=series_row.unit_label,
        vintage_date=figure.vintage_date,
        fetched_at=fetched_at,
    )
    session.add(row)
    session.flush()
    logger.info(
        "stored the first print for release %s on %s",
        release_id,
        release_date.isoformat(),
        extra={
            "event": "econ_first_print_stored",
            "release_id": release_id,
            "release_date": release_date.isoformat(),
            "series_id": figure.series_id,
            "period": figure.period.isoformat(),
            "actual": str(figure.actual),
            "prior": str(figure.prior),
            "at": fetched_at.isoformat(),
        },
    )
    return _stored_value(row)


def record_attempt(
    session: Session,
    release_id: int,
    release_date: date,
    attempt: Attempt,
    *,
    at: datetime,
) -> StoredFetchAttempt:
    """Make this attempt the release's last attempt (upsert).

    ``attempt`` is an :class:`~corollary.data.econ_values.Attempt`, taken
    whole and never as a raw ``(outcome, detail)`` pair (U6d audit P4).
    ``TypeError``, nothing written, for anything else. Flushes, never commits.

    **The type check guarantees the shape, not the scrub.** ``Attempt``
    refuses what the table's CHECKs refuse, but ``Attempt("error", str(exc))``
    is still an ``Attempt`` and would be stored unscrubbed. Scrubbing is the
    caller's duty: an ``error`` attempt must come from
    ``econ_values.attempt_of_error``, which scrubs the provider's secrets, and
    a fetch's result from ``attempt_of`` (U6e audit L6b). That is enforced by
    convention and by tests, not by this function:
    ``tests/data/test_econ_value_writers.py`` fails on any ``Attempt(...)``
    built outside ``econ_values`` (and this module's read-back of a stored
    row), and ``test_an_error_is_recorded_scrubbed_and_the_others_proceed``
    shows the job's error path stores no key.
    """
    if not isinstance(attempt, Attempt):
        raise TypeError(f"record_attempt takes an Attempt, got {type(attempt).__name__}")
    at = _utc(at, "at")
    row = session.get(EconReleaseFetch, (release_id, release_date))
    if row is None:
        row = EconReleaseFetch(release_id=release_id, release_date=release_date)
        session.add(row)
    row.attempted_at = at
    row.outcome = attempt.outcome
    row.detail = attempt.detail
    session.flush()
    return StoredFetchAttempt(
        release_id=release_id, release_date=release_date, attempted_at=at, attempt=attempt
    )


# --- reads ------------------------------------------------------------------


def read_value(session: Session, release_id: int, release_date: date) -> StoredEconValue | None:
    """The stored first print, or ``None``."""
    row = session.get(EconReleaseValue, (release_id, release_date))
    return None if row is None else _stored_value(row)


def read_attempt(
    session: Session, release_id: int, release_date: date
) -> StoredFetchAttempt | None:
    """The last attempt, or ``None`` if none was ever recorded."""
    row = session.get(EconReleaseFetch, (release_id, release_date))
    return None if row is None else _stored_attempt(row)


def parse_release_vendor_id(vendor_id: str) -> tuple[int, date]:
    """``"10:2026-10-14"`` -> ``(10, date(2026, 10, 14))``; ``ValueError`` otherwise.

    The inverse of ``calendar_releases.release_vendor_id``.
    """
    match = _VENDOR_ID.fullmatch(vendor_id)
    if match is None:
        raise ValueError(
            f"economic calendar row has a malformed vendor_id {vendor_id!r}; "
            "expected <release_id>:<YYYY-MM-DD>"
        )
    try:
        day = date.fromisoformat(match.group(2))
    except ValueError as exc:
        raise ValueError(
            f"economic calendar row's vendor_id {vendor_id!r} names no calendar date"
        ) from exc
    return int(match.group(1)), day


def read_economic_releases(
    session: Session,
    start: date,
    end: date,
    *,
    seeds: EconReleaseSeeds | None = None,
) -> tuple[EconomicRelease, ...]:
    """Every live economic calendar row dated ``[start, end]``, with its value and attempt.

    In :func:`~corollary.data.calendar.read_range`'s order and by its rules
    (the Eastern ``date``, soft-deleted rows excluded). ``seeds`` defaults to
    the committed econ seeds.

    Raises ``ValueError`` for a row that cannot be joined honestly: a
    malformed ``vendor_id``, one naming a different date from the row's own,
    or a row with no release instant. Each is a corrupted calendar row, and
    guessing its release would show another release's figure beside it.
    """
    if seeds is None:
        seeds = load_econ_release_seeds()
    events = [e for e in read_range(session, start, end) if e.kind is CalendarKind.ECONOMIC]

    keyed: list[tuple[StoredCalendarEvent, int, date, datetime]] = []
    for event in events:
        if event.vendor_id is None:  # pragma: no cover - ck_calendar_event_vendor_id
            raise ValueError(f"economic calendar row {event.id} has no vendor_id")
        release_id, release_date = parse_release_vendor_id(event.vendor_id)
        if release_date != event.date:
            raise ValueError(
                f"economic calendar row {event.id}'s vendor_id {event.vendor_id!r} names "
                f"{release_date}, but the row is dated {event.date}"
            )
        if event.at is None:
            raise ValueError(
                f"economic calendar row {event.id} ({event.vendor_id}) has no release instant"
            )
        keyed.append((event, release_id, release_date, event.at.astimezone(UTC)))

    ids = sorted({release_id for _, release_id, _, _ in keyed})
    values: dict[tuple[int, date], StoredEconValue] = {}
    attempts: dict[tuple[int, date], StoredFetchAttempt] = {}
    if ids:
        for value_row in session.scalars(
            select(EconReleaseValue).where(EconReleaseValue.release_id.in_(ids))
        ):
            values[(value_row.release_id, value_row.release_date)] = _stored_value(value_row)
        for fetch_row in session.scalars(
            select(EconReleaseFetch).where(EconReleaseFetch.release_id.in_(ids))
        ):
            attempts[(fetch_row.release_id, fetch_row.release_date)] = _stored_attempt(
                fetch_row
            )

    return tuple(
        EconomicRelease(
            event=event,
            release_id=release_id,
            release_date=release_date,
            release_at=release_at,
            series=seeds.series.by_id(release_id),
            value=values.get((release_id, release_date)),
            last_attempt=attempts.get((release_id, release_date)),
        )
        for event, release_id, release_date, release_at in keyed
    )
