"""The OpenFIGI ISIN resolver for the SPDR seed's ISIN-only lines (spec Q17).

The 2026-06-30 SPDR N-PORT filings identify 29 equity holdings by ISIN alone
(N-PORT's CUSIP is ``000000000``) -- Linde is 14.06% of XLB. The builder in
:mod:`corollary.data.seeds.nport` asks an
:class:`~corollary.data.seeds.nport.IsinResolver` about each one;
:class:`OpenFigiIsinResolver` is that resolver, over
:class:`~corollary.data.providers.openfigi.OpenFigiProvider`.

This module is **outside the vendor surface**. It makes no HTTP request of
its own -- every request goes through the provider's one exempt mapping call
(spec Q21) -- and it imports nothing from ``corollary.engine.execution`` or
any broker module. It reads the broker's asset list only as the
vendor-neutral :class:`~corollary.data.providers.interface.AssetDirectory`.

The acceptance rule (owner's Q17 rule; the orchestrator's concrete reading)
---------------------------------------------------------------------------

Given an ISIN's OpenFIGI :class:`~corollary.data.providers.openfigi.MappingResult`,
take the records with ``exchCode == "US"`` (Bloomberg's US composite) **and**
``marketSector == "Equity"``. The ISIN resolves only if

1. a directory is available -- **fail closed**: the builder only
   shape-checks an ISIN answer when it has no directory, so this resolver
   refuses everything without one, and asks OpenFIGI nothing;
2. OpenFIGI answered ``data`` (a ``warning`` or ``error`` refuses);
3. at least one record is a US composite equity record;
4. every such record carries a ticker (a blank or missing one refuses --
   an unknown is not counted as agreement);
5. those records carry **exactly one** distinct ticker after normalising
   class-share separators to the dot form with
   :func:`~corollary.data.seeds.normalize_symbol` (``BRK/B`` -> ``BRK.B``);
6. and that ticker is in today's directory (every active US equity the
   broker lists).

Otherwise the ISIN is unresolved: logged at WARNING (event
``isin_unresolved``) with a :class:`IsinRefusal` reason code, the ISIN and
the fund, and answered ``None`` -- the builder then logs and skips the line,
never guessing. ``securityType`` is recorded in every log line and **never
filtered on**. The holding's name is never passed here (the
:class:`~corollary.data.seeds.nport.IsinResolver` protocol takes identifiers
only), so it cannot be used to guess.

Batching
--------

The builder asks :meth:`OpenFigiIsinResolver.resolve` once per ISIN, but
calls :meth:`OpenFigiIsinResolver.prefetch` first with every ISIN-only ISIN
of the build (the optional
:class:`~corollary.data.seeds.nport.BatchIsinResolver` hook). ``prefetch``
maps every *uncached* ISIN in one
:meth:`~corollary.data.providers.openfigi.OpenFigiProvider.map_isins` call,
which batches at the provider's ``jobs_per_request`` -- 29 keyless ISINs are
three requests. A :class:`~corollary.data.providers.interface.ProviderError`
(``RateLimitedError`` included) propagates, and the builder aborts with no
snapshot stored and no cache row written: the cache is written only by
:meth:`resolve`, after every OpenFIGI request has succeeded. **The cache is
not part of the snapshot's transaction**, though. A build that aborts
*later* -- a CUSIP lookup failing after some ISIN lines were resolved --
stores no snapshot but keeps the accepted ``isin_ticker`` rows (and any
eviction) already written. That is harmless by construction: every row is an
accepted answer, and is re-checked against the day's directory on every use
here and again by the builder. A ``resolve``
for an ISIN nobody prefetched maps that ISIN alone (one request) rather
than failing -- correct, merely slower.

The cache (``isin_ticker``, migration 0012)
-------------------------------------------

* **Only accepted answers are stored.** A refused ISIN is never written,
  so it is asked again on every run.
* **A cached ticker is re-checked against the directory on every use.** A
  ticker the broker no longer lists makes the line unresolved *for this
  run* (reason :attr:`IsinRefusal.CACHED_NOT_LISTED`) and its row is
  deleted, so the next run asks OpenFIGI afresh.
* A cached ISIN is not asked of OpenFIGI at all.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final, Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from corollary.data.providers.interface import AssetDirectory
from corollary.data.providers.openfigi import MappingResult, validate_isin
from corollary.data.seeds import normalize_symbol
from corollary.db.models import IsinTicker

__all__ = [
    "IsinMappingSource",
    "IsinRefusal",
    "IsinVerdict",
    "OpenFigiIsinResolver",
    "SOURCE_OPENFIGI",
    "US_COMPOSITE_EXCH_CODE",
    "EQUITY_MARKET_SECTOR",
    "accept_mapping",
]

logger = logging.getLogger(__name__)

#: Bloomberg's exchange code for the US composite listing.
US_COMPOSITE_EXCH_CODE: Final = "US"

#: OpenFIGI's market sector for equities.
EQUITY_MARKET_SECTOR: Final = "Equity"

#: ``isin_ticker.source`` for an answer this resolver accepted.
SOURCE_OPENFIGI: Final = "openfigi"

#: A FIGI is twelve characters; anything else is not stored as one.
_FIGI_LENGTH: Final = 12


class IsinRefusal(StrEnum):
    """Why an ISIN did not resolve. Logged as ``reason`` on ``isin_unresolved``."""

    NO_DIRECTORY = "no_directory"  # no asset directory to check against: fail closed
    WARNING = "openfigi_warning"  # OpenFIGI found nothing ("No identifier found.")
    ERROR = "openfigi_error"  # OpenFIGI refused the job
    NO_US_COMPOSITE_EQUITY = "no_us_composite_equity"  # no exchCode US / Equity record
    MISSING_TICKER = "missing_ticker"  # a US composite equity record with no ticker
    AMBIGUOUS_TICKER = "ambiguous_ticker"  # more than one distinct ticker
    NOT_LISTED = "not_listed"  # the one ticker is not in the broker's directory
    CACHED_NOT_LISTED = "cached_not_listed"  # a cached ticker the directory dropped


class IsinMappingSource(Protocol):
    """What the resolver needs from OpenFIGI: :class:`~corollary.data.providers.openfigi.OpenFigiProvider`."""

    @property
    def jobs_per_request(self) -> int: ...

    async def map_isins(self, isins: Sequence[str]) -> dict[str, MappingResult]: ...


@dataclass(frozen=True, slots=True)
class IsinVerdict:
    """The acceptance rule's answer for one ISIN.

    Exactly one of ``ticker`` (accepted, dot form) and ``refusal`` is set.
    ``candidates`` are the distinct normalised tickers of the US composite
    equity records; ``security_types`` their distinct ``securityType``
    values -- recorded, never filtered on. ``detail`` is OpenFIGI's own
    warning or error text, already bounded and scrubbed by the provider.
    """

    ticker: str | None
    refusal: IsinRefusal | None
    composite_figi: str | None = None
    candidates: tuple[str, ...] = ()
    security_types: tuple[str, ...] = ()
    records: int = 0
    detail: str | None = None


def accept_mapping(result: MappingResult, directory: AssetDirectory | None) -> IsinVerdict:
    """Apply the Q17 acceptance rule (see the module docstring) to one result. Pure."""
    if directory is None:
        return IsinVerdict(ticker=None, refusal=IsinRefusal.NO_DIRECTORY)
    if result.error is not None:
        return IsinVerdict(ticker=None, refusal=IsinRefusal.ERROR, detail=result.error)
    if result.warning is not None or not result.records:
        return IsinVerdict(ticker=None, refusal=IsinRefusal.WARNING, detail=result.warning)

    us_equity = [
        record
        for record in result.records
        if record.exch_code == US_COMPOSITE_EXCH_CODE
        and record.market_sector == EQUITY_MARKET_SECTOR
    ]
    security_types = tuple(
        sorted({record.security_type for record in us_equity if record.security_type})
    )
    records = len(result.records)
    if not us_equity:
        return IsinVerdict(
            ticker=None, refusal=IsinRefusal.NO_US_COMPOSITE_EQUITY, records=records
        )

    tickers: list[str] = []
    for record in us_equity:
        raw = (record.ticker or "").strip()
        if not raw:
            return IsinVerdict(
                ticker=None,
                refusal=IsinRefusal.MISSING_TICKER,
                security_types=security_types,
                records=records,
            )
        tickers.append(normalize_symbol(raw))
    candidates = tuple(sorted(set(tickers)))
    if len(candidates) != 1:
        return IsinVerdict(
            ticker=None,
            refusal=IsinRefusal.AMBIGUOUS_TICKER,
            candidates=candidates,
            security_types=security_types,
            records=records,
        )
    (ticker,) = candidates
    if ticker not in directory:
        return IsinVerdict(
            ticker=None,
            refusal=IsinRefusal.NOT_LISTED,
            candidates=candidates,
            security_types=security_types,
            records=records,
        )
    figis = {
        record.composite_figi
        for record in us_equity
        if record.composite_figi is not None and len(record.composite_figi) == _FIGI_LENGTH
    }
    composite_figi = next(iter(figis)) if len(figis) == 1 else None
    return IsinVerdict(
        ticker=ticker,
        refusal=None,
        composite_figi=composite_figi,
        candidates=candidates,
        security_types=security_types,
        records=records,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class _Cached:
    """A cache row, read out of its session."""

    ticker: str
    composite_figi: str | None


class OpenFigiIsinResolver:
    """An :class:`~corollary.data.seeds.nport.IsinResolver` over OpenFIGI, with a cache.

    Built once per snapshot build: ``directory`` is that day's asset
    directory (``None`` refuses every ISIN -- fail closed). Implements the
    builder's optional ``prefetch`` hook so a build is a handful of batched
    requests, never one per ISIN.
    """

    def __init__(
        self,
        source: IsinMappingSource,
        session_factory: Callable[[], Session],
        *,
        directory: AssetDirectory | None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._source = source
        self._session_factory = session_factory
        self._directory = directory
        self._clock = clock
        #: ISINs whose cache row (or its absence) has been read this build.
        self._looked_up: set[str] = set()
        self._cached: dict[str, _Cached] = {}
        #: OpenFIGI's answers this build, for ISINs with no cache row.
        self._mapped: dict[str, MappingResult] = {}

    # ------------------------------------------------------------- cache

    def _load_cache(self, isins: Sequence[str]) -> None:
        wanted = [isin for isin in isins if isin not in self._looked_up]
        if not wanted:
            return
        with self._session_factory() as session:
            rows = session.scalars(select(IsinTicker).where(IsinTicker.isin.in_(wanted))).all()
            for row in rows:
                self._cached[row.isin] = _Cached(row.ticker, row.composite_figi)
        self._looked_up.update(wanted)

    def _evict(self, isin: str) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(delete(IsinTicker).where(IsinTicker.isin == isin))
        self._cached.pop(isin, None)

    def _store(self, isin: str, verdict: IsinVerdict) -> None:
        assert verdict.ticker is not None
        resolved_at = self._clock()
        if resolved_at.tzinfo is None:
            raise ValueError("the resolver's clock must be timezone-aware UTC")
        with self._session_factory() as session, session.begin():
            session.merge(
                IsinTicker(
                    isin=isin,
                    ticker=verdict.ticker,
                    composite_figi=verdict.composite_figi,
                    source=SOURCE_OPENFIGI,
                    resolved_at=resolved_at.astimezone(timezone.utc),
                )
            )
        self._cached[isin] = _Cached(verdict.ticker, verdict.composite_figi)

    # ----------------------------------------------------------- logging

    def _refused(self, isin: str, fund: str, verdict: IsinVerdict, source: str) -> None:
        assert verdict.refusal is not None
        logger.warning(
            "isin %s (%s) unresolved: %s",
            isin,
            fund,
            verdict.refusal.value,
            extra={
                "event": "isin_unresolved",
                "isin": isin,
                "fund": fund,
                "reason": verdict.refusal.value,
                "source": source,
                "candidates": list(verdict.candidates),
                "security_types": list(verdict.security_types),
                "records": verdict.records,
                "detail": verdict.detail,
                "at": self._clock().isoformat(),
            },
        )

    def _accepted(self, isin: str, fund: str, verdict: IsinVerdict, source: str) -> None:
        logger.info(
            "isin %s (%s) resolved to %s from %s",
            isin,
            fund,
            verdict.ticker,
            source,
            extra={
                "event": "isin_resolved",
                "isin": isin,
                "fund": fund,
                "ticker": verdict.ticker,
                "composite_figi": verdict.composite_figi,
                "source": source,
                "security_types": list(verdict.security_types),
                "at": self._clock().isoformat(),
            },
        )

    # ------------------------------------------------------------ seam

    async def prefetch(self, isins: Sequence[str]) -> None:
        """Map every uncached ISIN of the build in batched requests.

        Without a directory nothing could be accepted, so nothing is asked.
        A ``ProviderError`` propagates (the builder aborts, nothing stored).
        """
        if isinstance(isins, str):
            raise TypeError("prefetch takes a sequence of ISINs, not one string")
        unique = list(dict.fromkeys(validate_isin(isin) for isin in isins))
        if self._directory is None or not unique:
            return
        self._load_cache(unique)
        ask = [isin for isin in unique if isin not in self._cached and isin not in self._mapped]
        if ask:
            self._mapped.update(await self._source.map_isins(ask))

    async def resolve(self, isin: str, *, fund: str) -> str | None:
        """The accepted ticker for ``isin`` (dot form), or ``None`` -- logged with its reason."""
        validate_isin(isin)
        if self._directory is None:
            self._refused(isin, fund, IsinVerdict(None, IsinRefusal.NO_DIRECTORY), "none")
            return None

        self._load_cache([isin])
        cached = self._cached.get(isin)
        if cached is not None:
            if cached.ticker in self._directory:
                self._accepted(
                    isin, fund, IsinVerdict(cached.ticker, None, cached.composite_figi), "cache"
                )
                return cached.ticker
            self._evict(isin)
            self._refused(
                isin,
                fund,
                IsinVerdict(None, IsinRefusal.CACHED_NOT_LISTED, candidates=(cached.ticker,)),
                "cache",
            )
            return None

        if isin not in self._mapped:
            self._mapped.update(await self._source.map_isins([isin]))
        verdict = accept_mapping(self._mapped[isin], self._directory)
        if verdict.ticker is None:
            self._refused(isin, fund, verdict, SOURCE_OPENFIGI)
            return None
        self._store(isin, verdict)
        self._accepted(isin, fund, verdict, SOURCE_OPENFIGI)
        return verdict.ticker
