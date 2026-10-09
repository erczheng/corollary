"""The in-memory asset directory: decision 21's daily ``has_options`` list.

One :class:`~corollary.data.providers.interface.AssetDirectory` -- every active
US equity the broker lists, with its name and whether it has options -- held
between daily refreshes (07:30 ET, by the scheduler; this module owns no
schedule). Three readers:

* the ingest's **tag filter** -- is this vendor tag an active US equity at all;
* the **watch routes** -- validating a manual watch before it is stored;
* the **tradeability refresh** -- the ``has_options`` check.

**Nothing before the first success.** :meth:`AssetDirectoryHolder.current` is
``None`` until a fetch has succeeded, and every reader must treat ``None`` as
"unknown", never as "empty". An empty directory would fail every tag and every
``has_options`` check in silence, which is why an empty *answer* is refused
too: the vendor never lists zero active equities, so zero is a fault. So is a
list with zero *optionable* names: that is what a vendor schema change dropping
``attributes`` looks like (every asset reads ``has_options=False``), and held,
it would cache every candidate as "no options" for the session.

**A failed refresh keeps the previous directory.** Yesterday's list is a far
better answer to "is AAPL optionable" than no list, and the staleness is
exposed (:attr:`~AssetDirectoryHolder.fetched_at`,
:meth:`~AssetDirectoryHolder.age`, :meth:`~AssetDirectoryHolder.is_stale`) so a
caller can say *asset list stale since 07:30 yesterday* rather than pretending.
The failure is recorded (:attr:`~AssetDirectoryHolder.last_failure`), logged
with its rule, and **re-raised**, so the scheduler states it the way it states
every failed job -- the same contract as ``refresh_dgs3mo``.

Decision 1: nothing here imports the engine runtime, its state or its sockets.
Vendor HTTP happens only inside the provider, through the shared limiter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Protocol

from corollary.data.providers.interface import AssetDirectory, ProviderError
from corollary.wire import require_aware

__all__ = [
    "MAX_DIRECTORY_AGE",
    "AssetDirectoryHolder",
    "AssetRefreshFailure",
    "AssetSource",
]

logger = logging.getLogger(__name__)

#: How old a directory may be before it reads stale. The list is refreshed
#: once a day, so 24 hours is the ordinary age just before a refresh; the two
#: hours beyond that are slack for a slow or retried job, not a tolerance for a
#: missed one. Inclusive: a directory exactly this old is not yet stale.
MAX_DIRECTORY_AGE: Final = timedelta(hours=26)

UtcClock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AssetSource(Protocol):
    """What the holder needs from a provider: the active equity list."""

    async def active_equities(self) -> AssetDirectory: ...


@dataclass(frozen=True, slots=True)
class AssetRefreshFailure:
    """The most recent refresh that failed: when, and the error as text."""

    at: datetime
    error: str


class AssetDirectoryHolder:
    """The latest good :class:`AssetDirectory` and when it was fetched.

    Single-process, single event loop: a refresh replaces the reference in one
    assignment, so a reader sees the old directory or the new one, never half
    of either.
    """

    def __init__(self, *, now: UtcClock = _utc_now) -> None:
        self._now = now
        self._directory: AssetDirectory | None = None
        self._fetched_at: datetime | None = None
        self._last_failure: AssetRefreshFailure | None = None

    def current(self) -> AssetDirectory | None:
        """The latest good directory, or ``None`` before the first success."""
        return self._directory

    @property
    def fetched_at(self) -> datetime | None:
        """When :meth:`current` was fetched (UTC), or ``None`` before the first success."""
        return self._fetched_at

    @property
    def last_failure(self) -> AssetRefreshFailure | None:
        """The latest failed refresh, cleared by the next success."""
        return self._last_failure

    def age(self) -> timedelta | None:
        """How long ago :meth:`current` was fetched, or ``None`` if it never was."""
        if self._fetched_at is None:
            return None
        now = self._now()
        require_aware(now, "now")
        return now - self._fetched_at

    def is_stale(self, *, max_age: timedelta = MAX_DIRECTORY_AGE) -> bool:
        """True with no directory at all, or one older than ``max_age``."""
        age = self.age()
        return age is None or age > max_age

    async def refresh(self, source: AssetSource) -> AssetDirectory:
        """Fetch the directory and hold it. Returns what is now held.

        On any failure -- the fetch raising, an empty list, a list with no
        optionable name, a naive clock --
        the previous directory and its ``fetched_at`` are kept, the failure is
        recorded and logged, and the exception is re-raised.
        """
        try:
            directory = await source.active_equities()
            if len(directory) == 0:
                raise ProviderError(
                    "the asset list came back empty; the vendor never lists zero "
                    "active equities, so the previous directory is kept"
                )
            if not directory.optionable():
                raise ProviderError(
                    f"the asset list named {len(directory)} active equities and none "
                    f"with options ({directory.missing_attributes} carried no "
                    "attributes); a schema change reads as every name losing its "
                    "options, so the previous directory is kept"
                )
            fetched_at = self._now()
            require_aware(fetched_at, "now")
        except Exception as exc:
            self._record_failure(exc)
            raise
        self._directory = directory
        self._fetched_at = fetched_at.astimezone(timezone.utc)
        self._last_failure = None
        logger.info(
            "asset directory refreshed: %d active equities, %d with options",
            len(directory),
            len(directory.optionable()),
            extra={
                "event": "asset_directory_refreshed",
                "assets": len(directory),
                "optionable": len(directory.optionable()),
                "skipped": directory.skipped,
                "missing_attributes": directory.missing_attributes,
                "fetched_at": self._fetched_at.isoformat(),
            },
        )
        return directory

    def _record_failure(self, exc: Exception) -> None:
        # The clock may be the thing that failed; the failure is still recorded,
        # stamped by the real UTC clock rather than dropped.
        try:
            at = self._now()
            require_aware(at, "now")
        except Exception:
            at = _utc_now()
        error = f"{type(exc).__name__}: {exc}"
        self._last_failure = AssetRefreshFailure(at=at.astimezone(timezone.utc), error=error)
        logger.warning(
            "asset directory refresh failed; %s",
            "keeping the previous directory" if self._directory is not None
            else "there is no directory yet",
            extra={
                "event": "asset_directory_refresh_failed",
                "rule": (
                    "a failed refresh never replaces a good directory; readers treat "
                    "no directory as unknown, never as empty"
                ),
                "error": error,
                "at": self._last_failure.at.isoformat(),
                "previous_fetched_at": (
                    self._fetched_at.isoformat() if self._fetched_at is not None else None
                ),
            },
        )
