"""The risk-free rate from FRED ``DGS3MO``: stored, read back, refreshed.

Phase 3 decision 19. Three pieces, each small:

* :func:`store_observations` upserts FRED observations into
  ``fred_observation`` -- one row per ``(series_id, date)``, a missing
  observation (``"."``) stored as ``NULL`` so a retraction overwrites.
* :func:`latest_dgs3mo_rate` is **the** definition of the rate in use: the
  latest stored ``DGS3MO`` row whose value is not ``NULL``, as a
  :class:`~corollary.pricing.rates.RiskFreeRate` dated by its observation.
  The table holds FRED's quoted percent as published; the continuous rate
  pricing uses is derived from it on the way out, by
  :func:`~corollary.pricing.rates.rate_from_dgs3mo` (owner decision Q16).
* :func:`refresh_dgs3mo` / :func:`catch_up_dgs3mo` are the context job's
  bodies: fetch, store, then adopt the table's answer into the process's
  :class:`~corollary.pricing.rates.RiskFreeRateSource`. A call that completes
  without fault but adopts nothing -- no key, a current table, a response of
  only gaps -- returns :class:`NotRefreshed` with the reason, never a bare
  ``None``: the scheduler records it as *skipped*, so a feed that fetched
  nothing is never reported fresh.

Rule 9 does not reach here
--------------------------

These run as context jobs (decision 1). A FRED failure raises out of the job
body into the scheduler, which logs it with its rule and inputs and runs the
job again at its next slot; nothing here imports the engine runtime, its state
row, the sockets or the API, and ``tests/engine/test_scheduler.py`` scans this
module's imports to keep it that way. A stale rate is a stated input to a
display column, never a lost connection.

Blocking SQLite work runs in ``asyncio.to_thread``: the scheduler shares the
event loop with rule 9's socket readers (``engine/scheduler.py``).
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from corollary.calendars import NYSE_TZ, nyse_session_close
from corollary.data.providers.fred import FredObservation
from corollary.db.models import FredObservationRecord
from corollary.pricing.rates import (
    DGS3MO_SERIES,
    FALLBACK_RISK_FREE_RATE,
    RiskFreeRate,
    RiskFreeRateSource,
    rate_from_dgs3mo,
)
from corollary.wire import require_aware

__all__ = [
    "DGS3MO_RECENT_OBSERVATIONS",
    "DGS3MO_STALE_AFTER_SESSIONS",
    "NotRefreshed",
    "ObservationSource",
    "catch_up_dgs3mo",
    "dgs3mo_catch_up_threshold",
    "latest_dgs3mo_rate",
    "refresh_dgs3mo",
    "seed_rate_source",
    "store_observations",
]

logger = logging.getLogger(__name__)

#: Observations fetched per refresh. Ten sessions back-fills a fortnight of
#: missed refreshes (a laptop asleep over a holiday week) in one request.
DGS3MO_RECENT_OBSERVATIONS = 10

#: How many NYSE sessions before today the newest stored ``DGS3MO`` row may
#: be dated and still count as current at start-up. Two, not one: FRED
#: publishes a close the next morning, so before that lands the newest
#: observation that exists is two sessions old. With one, every restart on
#: such a morning -- and every ``--reload`` save -- would re-fetch.
DGS3MO_STALE_AFTER_SESSIONS = 2

#: How far back :func:`dgs3mo_catch_up_threshold` walks looking for sessions.
#: The longest published NYSE closure is under a fortnight; past this the
#: calendar has nothing to say and the catch-up fetches rather than guesses.
_SESSION_SEARCH_DAYS = 21

UtcClock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class NotRefreshed:
    """A ``DGS3MO`` run that completed without fault and adopted nothing, and why.

    Distinct from a failure (which raises) and from a refresh (which returns
    the adopted :class:`~corollary.pricing.rates.RiskFreeRate`). The scheduler
    records it as a *skipped* run with this reason -- never as a success, since
    a "last refreshed" time that moved when nothing was fetched would call a
    FRED feed fresh that was never read.
    """

    reason: str


_NO_FRED = NotRefreshed(
    reason="FRED is unavailable (FRED_API_KEY is unset); nothing was fetched"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ObservationSource(Protocol):
    """What the refresh needs from FRED: recent observations of one series."""

    async def observations(
        self, series_id: str, *, limit: int = ...
    ) -> Sequence[FredObservation]: ...


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def store_observations(
    session: Session,
    observations: Sequence[FredObservation],
    *,
    fetched_at: datetime,
) -> int:
    """Upsert ``observations`` into ``fred_observation``. Does not commit.

    ``INSERT ... ON CONFLICT DO UPDATE`` rather than read-then-write, so two
    writers racing on one ``(series_id, date)`` cannot collide on the primary
    key: the second simply overwrites. A ``None`` value (FRED's ``"."``) is
    written as ``NULL`` and overwrites a stored number -- that is a retraction,
    and the number must stop being read as current.

    Returns how many observations were written.
    """
    require_aware(fetched_at, "fetched_at")
    stamp = fetched_at.astimezone(timezone.utc)
    rows = [
        {
            "series_id": observation.series_id,
            "date": observation.date,
            "value": observation.value,
            "fetched_at": stamp,
        }
        for observation in observations
    ]
    if not rows:
        return 0
    statement = sqlite_insert(FredObservationRecord).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[FredObservationRecord.series_id, FredObservationRecord.date],
        set_={
            "value": statement.excluded.value,
            "fetched_at": statement.excluded.fetched_at,
        },
    )
    session.execute(statement)
    return len(rows)


def latest_dgs3mo_rate(session: Session) -> RiskFreeRate | None:
    """The latest stored non-missing ``DGS3MO`` observation, as a rate. ``None`` if none.

    Ordered by ``date``, which is a SQL ``DATE`` (ISO text on SQLite, so
    lexicographic order is chronological); ``value`` is only tested for
    presence, never compared.
    """
    row = session.scalars(
        select(FredObservationRecord)
        .where(
            FredObservationRecord.series_id == DGS3MO_SERIES,
            FredObservationRecord.value.is_not(None),
        )
        .order_by(FredObservationRecord.date.desc())
        .limit(1)
    ).first()
    if row is None or row.value is None:
        return None
    return rate_from_dgs3mo(row.value, row.date)


def _describe(rate: RiskFreeRate) -> dict[str, str | None]:
    return {
        "rate": str(rate.rate),
        "source": rate.provenance.value,
        "observation_date": (
            None if rate.observation_date is None else rate.observation_date.isoformat()
        ),
    }


# --------------------------------------------------------------------------
# The rate source
# --------------------------------------------------------------------------


def seed_rate_source(
    session_factory: Callable[[], Session], rates: RiskFreeRateSource
) -> RiskFreeRate:
    """Adopt the stored rate at startup, if there is one. Returns the rate in use.

    Synchronous: one indexed read, run once from the lifespan before any
    socket opens. Logs which rate the chain will be derived at, and why.
    """
    with session_factory() as session:
        stored = latest_dgs3mo_rate(session)
    if stored is not None:
        rates.adopt(stored)
    current = rates.current()
    logger.info(
        "risk-free rate at startup: %s (%s)",
        current.rate,
        current.provenance.value,
        extra={
            "event": "risk_free_rate_seeded",
            "rule": (
                "derived greeks use the latest stored FRED DGS3MO observation, "
                "and the 4.25% default only when none was ever obtained; either "
                "quoted yield is converted to a continuous rate, "
                "r = ln(1 + y*91/365) / (91/365)"
            ),
            **_describe(current),
        },
    )
    return current


def _store_and_read(
    session_factory: Callable[[], Session],
    observations: Sequence[FredObservation],
    fetched_at: datetime,
) -> RiskFreeRate | None:
    """The blocking half of a refresh. Only ever called through ``to_thread``."""
    with session_factory() as session:
        store_observations(session, observations, fetched_at=fetched_at)
        session.commit()
        return latest_dgs3mo_rate(session)


def dgs3mo_catch_up_threshold(today: date) -> date | None:
    """The oldest newest-row date that still counts as current on ``today`` (ET).

    The :data:`DGS3MO_STALE_AFTER_SESSIONS`-th NYSE session strictly before
    ``today``, per :mod:`corollary.calendars` -- sessions, not calendar days,
    so a weekend or a holiday does not make Monday's table look stale.
    ``None`` when the calendar cannot answer that far back; the catch-up then
    fetches rather than guessing.
    """
    found = 0
    day = today
    for _ in range(_SESSION_SEARCH_DAYS):
        day -= timedelta(days=1)
        if nyse_session_close(day) is not None:
            found += 1
            if found == DGS3MO_STALE_AFTER_SESSIONS:
                return day
    return None


def _latest_dgs3mo_row_date(session: Session) -> date | None:
    """The newest stored ``DGS3MO`` row's date, **gaps included**.

    A ``"."`` row (a bond-market holiday the stock market kept) is still
    proof FRED was read that recently; judged from non-missing rows only,
    the morning after such a day would look stale and re-fetch on every save.
    """
    return session.scalars(
        select(FredObservationRecord.date)
        .where(FredObservationRecord.series_id == DGS3MO_SERIES)
        .order_by(FredObservationRecord.date.desc())
        .limit(1)
    ).first()


def _catch_up_skip_reason(
    session_factory: Callable[[], Session], now: datetime
) -> str | None:
    """Why start-up need not fetch, or ``None`` when it must. Runs in ``to_thread``.

    It must fetch when no ``DGS3MO`` value has ever been stored (the fallback
    is in use), or when the newest row is older than
    :func:`dgs3mo_catch_up_threshold` allows (a long downtime). Otherwise --
    the ``--reload`` loop on a current table -- it must not.
    """
    with session_factory() as session:
        stored = latest_dgs3mo_rate(session)
        newest = _latest_dgs3mo_row_date(session)
    if stored is None or newest is None:
        return None
    threshold = dgs3mo_catch_up_threshold(now.astimezone(NYSE_TZ).date())
    if threshold is None or newest < threshold:
        return None
    return (
        f"the newest stored DGS3MO row is dated {newest.isoformat()}, on or after "
        f"{threshold.isoformat()} ({DGS3MO_STALE_AFTER_SESSIONS} sessions back); "
        "nothing to catch up"
    )


async def refresh_dgs3mo(
    *,
    fred: ObservationSource | None,
    session_factory: Callable[[], Session],
    rates: RiskFreeRateSource,
    now: UtcClock = _utc_now,
) -> RiskFreeRate | NotRefreshed:
    """Fetch recent ``DGS3MO``, store it, adopt the table's latest. Returns what was adopted.

    ``fred=None`` means FRED is unavailable (no key); the lifespan said so once
    at startup, so this returns :class:`NotRefreshed` rather than failing every
    day -- and rather than ``None``, which read as "it worked". A fetch
    failure **raises** -- the scheduler states it -- and leaves the rate in use
    untouched: the last observation if there was one, never a switch back to
    the default.
    """
    if fred is None:
        return _NO_FRED
    observations = await fred.observations(
        DGS3MO_SERIES, limit=DGS3MO_RECENT_OBSERVATIONS
    )
    latest = await asyncio.to_thread(_store_and_read, session_factory, observations, now())
    if latest is None:
        logger.warning(
            "FRED answered with no DGS3MO value in its recent observations; the "
            "risk-free rate in use is unchanged",
            extra={
                "event": "risk_free_rate_not_refreshed",
                "series_id": DGS3MO_SERIES,
                "observations": len(observations),
                **_describe(rates.current()),
            },
        )
        return NotRefreshed(
            reason=(
                f"FRED answered with no DGS3MO value in its {len(observations)} "
                "most recent observations; the rate in use is unchanged"
            )
        )
    previous = rates.current()
    rates.adopt(latest)
    logger.info(
        "risk-free rate refreshed from FRED DGS3MO: %s observed %s",
        latest.rate,
        latest.observation_date,
        extra={
            "event": "risk_free_rate_refreshed",
            "series_id": DGS3MO_SERIES,
            "previous": _describe(previous),
            "was_default": previous == FALLBACK_RISK_FREE_RATE,
            **_describe(latest),
        },
    )
    return latest


async def catch_up_dgs3mo(
    *,
    fred: ObservationSource | None,
    session_factory: Callable[[], Session],
    rates: RiskFreeRateSource,
    now: UtcClock = _utc_now,
) -> RiskFreeRate | NotRefreshed:
    """At startup: refresh if nothing was ever stored, or what is stored is stale.

    Decided from the rows (see :func:`_catch_up_skip_reason`), so a
    ``--reload`` loop on a current table never re-fetches; a first run, a
    table that never held a value, or a restart after a long downtime spends
    one request before the next 10:00 ET slot.
    """
    if fred is None:
        return _NO_FRED
    skip = await asyncio.to_thread(_catch_up_skip_reason, session_factory, now())
    if skip is not None:
        return NotRefreshed(reason=skip)
    return await refresh_dgs3mo(
        fred=fred, session_factory=session_factory, rates=rates, now=now
    )
