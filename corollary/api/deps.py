"""Dependency injection: the broker, the provider, the session, and the book.

Four things live here, and three of them are boundaries rather than plumbing.

**The account-mode boundary.** Every account-scoped route takes
``?account=paper|cash``, defaulting to ``paper`` because rule 5 says every
cold start comes up in Paper. If ``cash`` is asked for and the live keys are
absent, the request **fails with 409 and a stated reason**. It never falls
back. Serving paper's book while the header says Cash misreports real money,
and it does so in the one direction nobody checks -- the numbers look
plausible because they are somebody's real numbers.

**The broker type.** Every route signature depends on
:class:`~corollary.engine.execution.interface.BrokerAccount`, the read half of
the vendor surface. ``BrokerExecution`` does not exist until Phase 6, so
``submit_order`` is not a method any route can reach. That is what makes
CLAUDE.md rule 1 structural rather than disciplinary, and it is why
:func:`broker_for_account` is annotated with the ABC and not with
``AlpacaBroker``.

**The vendor SDK is not imported here, or anywhere outside the two files
CLAUDE.md names.** This module constructs ``AlpacaBroker`` and
``AlpacaProvider`` -- our classes -- and touches nothing from ``alpaca``.

**Construction is lazy, and that is deliberate rather than lazy.** The
registry is built and cached on ``app.state`` at startup, but the broker and
the provider are constructed on first use. A ``uvicorn --reload`` server that
refused to start because a credential was unset would take the whole terminal
offline -- including ``/api/health`` and the engine routes, neither of which
needs a broker -- for a condition that affects four endpoints. Missing
credentials surface where they are used, as a stated condition.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Final

from fastapi import Depends, Query, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.api.schemas import AccountMode
from corollary.data.providers.alpaca import (
    ALPACA_LIVE_KEY_ENV,
    ALPACA_LIVE_SECRET_ENV,
    AlpacaCredentials,
    AlpacaProvider,
)
from corollary.data.providers.finnhub import (
    FinnhubCredentialsError,
    FinnhubProvider,
)
from corollary.data.providers.fundamentals import (
    FundamentalsProvider,
    UnavailableFundamentals,
)
from corollary.data.providers.fred import FRED_API_KEY_ENV, FredCredentialsError, FredProvider
from corollary.data.providers.massive import MASSIVE_API_KEY_ENV, MassiveProvider
from corollary.data.news.pollers import MassiveNewsSource
from corollary.data.news.assets import AssetDirectoryHolder
from corollary.data.providers.interface import MarketDataProvider
from corollary.data.providers.openfigi import OPENFIGI_API_KEY_ENV, OpenFigiProvider
from corollary.data.providers.sec import SEC_USER_AGENT_ENV, SecProvider
from corollary.data.seeds import SeedError, SpdrSeed
from corollary.engine.execution.alpaca import AlpacaBroker
from corollary.engine.execution.interface import BrokerAccount
# Re-exported: the holder lives beside the jobs that read it, so the object a
# job holds a view of is one the jobs' import scan may walk.
from corollary.engine.scheduler import PositionUnderlyings
from corollary.instruments import parse_occ_symbol
from corollary.pricing.rates import RiskFreeRateSource
from corollary.wire import require_aware

__all__ = [
    "AccountMode",
    "AccountModeDep",
    "ApiError",
    "AssetDirectoryDep",
    "BrokerDep",
    "FundamentalsDep",
    "LIVE_CREDENTIAL_ENV_VARS",
    "POSITION_UNDERLYINGS_TTL",
    "PaperPositionsRefresher",
    "PositionUnderlyings",
    "PositionUnderlyingsDep",
    "ProviderDep",
    "ServiceRegistry",
    "SeedLoader",
    "SessionDep",
    "SpdrSeedDep",
    "account_mode",
    "asset_directory",
    "broker_for_account",
    "db_session",
    "fundamentals_data",
    "market_data",
    "missing_live_credentials",
    "position_underlying",
    "position_underlyings",
    "service_registry",
    "spdr_seed",
]

logger = logging.getLogger(__name__)

#: Both halves of the live pair. Named as a pair because the 409 must name
#: both: reporting only the one that happens to be checked first sends
#: somebody to set one variable and try again.
LIVE_CREDENTIAL_ENV_VARS: Final[tuple[str, str]] = (
    ALPACA_LIVE_KEY_ENV,
    ALPACA_LIVE_SECRET_ENV,
)


# --------------------------------------------------------------------------
# The stated-condition error
# --------------------------------------------------------------------------


class ApiError(Exception):
    """A condition the API states rather than crashing on.

    Rendered into the shared error envelope by the handler in ``api/app.py``,
    so ``api.ts`` has one shape to parse for every non-2xx.

    Not a ``fastapi.HTTPException``: that renders ``{"detail": ...}``, a
    second shape, and the whole value of the envelope is that there is only
    one. :attr:`code` is what a client branches on; :attr:`message` is for a
    human and is scrubbed before it is rendered.
    """

    def __init__(self, *, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def missing_live_credentials(env: Mapping[str, str]) -> tuple[str, ...]:
    """Which of the two live variables are unset or blank, in declared order.

    Blank counts as absent. ``ALPACA_LIVE_API_KEY=`` in a ``.env`` is a
    variable somebody meant to fill in, and treating an empty string as a
    credential produces a 401 from the vendor instead of a 409 from here --
    the same outcome, diagnosed three layers further away.
    """
    return tuple(
        name for name in LIVE_CREDENTIAL_ENV_VARS if not (env.get(name) or "").strip()
    )


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

BrokerFactory = Callable[[], BrokerAccount]
ProviderFactory = Callable[[], MarketDataProvider]
FundamentalsFactory = Callable[[], FundamentalsProvider]
FredFactory = Callable[[], FredProvider]
#: ``None`` is a stated absence: ``MassiveProvider.available_from_env``
#: answers it, logged, when ``MASSIVE_API_KEY`` is unset.
MassiveFactory = Callable[[], MassiveNewsSource | None]
#: How the registry builds SEC EDGAR. ``None`` is a stated absence:
#: ``SecProvider.from_env`` logs an unset ``SEC_USER_AGENT`` once, by name.
SecFactory = Callable[[], SecProvider | None]
#: How the registry builds OpenFIGI (spec Q17). Never absent for want of a
#: key: ``OPENFIGI_API_KEY`` unset is the keyless limits, not an error.
OpenFigiFactory = Callable[[], OpenFigiProvider]


def _fundamentals_from_env(env: Mapping[str, str]) -> FundamentalsProvider:
    """Finnhub if it is configured, and a stated absence if it is not.

    **A missing key degrades one column rather than failing the table.** The
    market-cap column is supplementary -- decision 7 designed its null path --
    and 503-ing a screener of prices and volumes over a reference-data key
    nobody set would be the larger error. What must not happen is silence:
    :class:`~corollary.data.providers.fundamentals.UnavailableFundamentals`
    logs the cause every time it is asked.

    Caught here rather than at the property, so the composition root is the
    only place that knows which vendor can be absent.
    """
    try:
        return FinnhubProvider.from_env(env)
    except FinnhubCredentialsError as exc:
        return UnavailableFundamentals(str(exc))


class RiskFreeRateSplitError(RuntimeError):
    """The market-data provider reads a different rate source from the registry's.

    The FRED job updates the registry's; the provider prices chains at its
    own. Nothing would fail -- every chain would just say ``default`` long
    after an observation was stored -- so the pair is refused outright.
    """


class ServiceRegistry:
    """The brokers and the provider, built at most once each and closed once.

    A mode with no factory is a mode with no credentials. That is the whole
    representation of "Cash is unavailable" -- there is no cash broker object
    holding blank keys, so there is nothing for a later change to accidentally
    call.

    :attr:`missing_live_credentials` is carried alongside so the 409 can name
    the variables. The constructor refuses the inconsistent combination rather
    than trusting two arguments to agree.
    """

    def __init__(
        self,
        *,
        brokers: Mapping[AccountMode, BrokerFactory],
        provider: ProviderFactory,
        fundamentals: FundamentalsFactory | None = None,
        fred: FredFactory | None = None,
        rates: RiskFreeRateSource | None = None,
        missing_live_credentials: Sequence[str] = (),
        massive: MassiveFactory | None = None,
        sec: SecFactory | None = None,
        openfigi: OpenFigiFactory | None = None,
    ) -> None:
        self._factories: dict[AccountMode, BrokerFactory] = dict(brokers)
        #: The process's one risk-free rate (Phase 3 decision 19): the
        #: market-data provider reads it per chain, the FRED job updates it,
        #: and the lifespan seeds it from ``fred_observation``. One per
        #: registry, so a test registry is never priced at another's rate.
        #: A provider that derives analytics must hold **this** source: pass
        #: the provider's own as ``rates``, or build the provider from this
        #: one (``from_env`` does). :attr:`provider` raises
        #: :class:`RiskFreeRateSplitError` on the first use of a split pair.
        self.rates: RiskFreeRateSource = rates if rates is not None else RiskFreeRateSource()
        #: Optional, like fundamentals: no factory -- a test registry, or no
        #: way to build one -- is FRED unavailable, and the rate stays the
        #: labelled default until an observation has been stored.
        self._fred_factory: FredFactory | None = fred
        self._fred: FredProvider | None = None
        self._fred_resolved = False
        #: Optional, like FRED: the news jobs' Massive feed (step 4). One
        #: client per process, so Massive's 5/min is counted once.
        self._massive_factory: MassiveFactory | None = massive
        self._massive: MassiveNewsSource | None = None
        self._massive_resolved = False
        #: Optional, like Massive: SEC EDGAR for the ``spdr_holdings`` job
        #: (unit 4SEC-B2). One client per process, so SEC's 10/s fair-access
        #: ceiling is counted by one limiter.
        self._sec_factory: SecFactory | None = sec
        self._sec: SecProvider | None = None
        self._sec_resolved = False
        #: Optional, like SEC: OpenFIGI for the ``spdr_holdings`` job's
        #: ISIN-only lines (spec Q17). One client per process, so the
        #: ``api.openfigi.com`` bucket is counted by one limiter.
        self._openfigi_factory: OpenFigiFactory | None = openfigi
        self._openfigi: OpenFigiProvider | None = None
        self._openfigi_resolved = False
        self._provider_factory = provider
        #: Optional because it is the one service whose absence is a designed
        #: state rather than a failure -- see :func:`_fundamentals_from_env`.
        #: A registry built without one serves a null market cap and says so.
        self._fundamentals_factory: FundamentalsFactory = (
            fundamentals
            if fundamentals is not None
            else lambda: UnavailableFundamentals(
                "this app was built with no fundamentals provider"
            )
        )
        self.missing_live_credentials: tuple[str, ...] = tuple(missing_live_credentials)
        if (AccountMode.CASH in self._factories) and self.missing_live_credentials:
            raise ValueError(
                "a cash broker was supplied alongside missing live credentials "
                f"{self.missing_live_credentials!r}; one of the two is wrong, and "
                "guessing which would mean guessing whose money is at stake"
            )
        self._brokers: dict[AccountMode, BrokerAccount] = {}
        self._provider: MarketDataProvider | None = None
        self._fundamentals: FundamentalsProvider | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ServiceRegistry":
        """Build from the process environment.

        Paper always exists as a factory -- whether its credentials are set is
        discovered when it is first used, which keeps a missing paper key from
        stopping the server booting. Cash exists only when both live variables
        are set.
        """
        import os

        source: Mapping[str, str] = os.environ if env is None else env
        missing = missing_live_credentials(source)

        brokers: dict[AccountMode, BrokerFactory] = {
            AccountMode.PAPER: lambda: AlpacaBroker.from_env(source),
        }
        if not missing:
            brokers[AccountMode.CASH] = lambda: AlpacaBroker(
                credentials=AlpacaCredentials.live_from_env(source)
            )
        rates = RiskFreeRateSource()
        return cls(
            brokers=brokers,
            provider=lambda: AlpacaProvider.from_env(source, risk_free_rate=rates),
            fundamentals=lambda: _fundamentals_from_env(source),
            fred=lambda: FredProvider.from_env(source),
            rates=rates,
            missing_live_credentials=missing,
            massive=lambda: MassiveProvider.available_from_env(source),
            sec=lambda: SecProvider.from_env(source),
            openfigi=lambda: OpenFigiProvider.from_env(source),
        )

    def broker(self, mode: AccountMode) -> BrokerAccount:
        """The broker for one book, built on first use and cached after.

        Raises :class:`ApiError` with 409 when the book has no credentials.
        **Never substitutes another book.**
        """
        factory = self._factories.get(mode)
        if factory is None:
            raise self._unavailable(mode)
        cached = self._brokers.get(mode)
        if cached is None:
            cached = self._brokers[mode] = factory()
        return cached

    def _unavailable(self, mode: AccountMode) -> ApiError:
        message = (
            f"The {mode.value} account is not configured: "
            f"{' and '.join(LIVE_CREDENTIAL_ENV_VARS)} are both required and "
            f"{self._absent_phrase()}. Paper was not substituted -- serving "
            "one book under the other's name misreports which money moved."
        )
        logger.warning(
            "refused an account-scoped request: %s",
            message,
            extra={
                "event": "account_unavailable",
                "rule": "a book with no credentials is refused, never substituted",
                "account": mode.value,
                "missing": list(self.missing_live_credentials),
            },
        )
        return ApiError(status_code=409, code="account_unavailable", message=message)

    def _absent_phrase(self) -> str:
        """Which of the pair is missing, said once.

        The message has to stay inside :data:`~corollary.wire.ERROR_BODY_MAX`,
        because it is scrubbed through ``vendor_detail`` on the way out like
        every other error body. Repeating both variable names -- once as
        "required", once as "missing" -- put the earlier wording at 279 of
        300, close enough that a reworded sentence would have truncated the
        half that says paper was *not* substituted, while the half a test
        asserts on sat safely at the front.
        """
        missing = self.missing_live_credentials
        if not missing:
            return "neither is configured for it"
        if set(missing) == set(LIVE_CREDENTIAL_ENV_VARS):
            return "neither is set"
        return f"{' and '.join(missing)} is not set"

    @property
    def provider(self) -> MarketDataProvider:
        """The one market-data provider, built on first use and cached after.

        One instance, so the 200/min budget for ``data.alpaca.markets`` is
        counted once. Two providers in one process would each believe they
        held the whole bucket.
        """
        if self._provider is None:
            built = self._provider_factory()
            # Read defensively: every real provider subclasses
            # ``MarketDataProvider`` and has the property, but the suite's
            # duck-typed doubles do not, and one that derives nothing has
            # nothing to split.
            theirs = getattr(built, "risk_free_rates", None)
            if isinstance(theirs, RiskFreeRateSource) and theirs is not self.rates:
                logger.error(
                    "the market-data provider reads a different risk-free rate "
                    "source from the one the FRED job updates; refusing it",
                    extra={
                        "event": "risk_free_rate_split",
                        "rule": (
                            "one risk-free rate source per process, shared by "
                            "the provider and the FRED job (decision 19)"
                        ),
                        "provider": type(built).__name__,
                    },
                )
                raise RiskFreeRateSplitError(
                    f"{type(built).__name__} was built with its own risk-free "
                    "rate source, not the registry's; pass its source as "
                    "ServiceRegistry(rates=...) or build it from registry.rates"
                )
            self._provider = built
        return self._provider

    @property
    def fundamentals(self) -> FundamentalsProvider:
        """The one fundamentals provider, built on first use and cached after.

        One instance, so Finnhub's 60/min is counted once. Never ``None``:
        an unconfigured vendor is a provider that answers "unavailable" with
        a reason, which is a thing every call site can handle.
        """
        if self._fundamentals is None:
            self._fundamentals = self._fundamentals_factory()
        return self._fundamentals

    def fred_provider(self) -> FredProvider | None:
        """The one FRED client, built on first call -- or ``None``, said once.

        **Never raises**: FRED is optional and the lifespan that calls this
        carries rule 9. A missing ``FRED_API_KEY`` is logged once, as a
        warning naming the variable (never a value), and every later call
        answers ``None`` quietly; so does anything else a constructor raises,
        logged by class name only, since its message could quote the key.
        """
        if self._fred_resolved:
            return self._fred
        self._fred_resolved = True
        if self._fred_factory is None:
            logger.debug(
                "this registry was built without a FRED provider",
                extra={"event": "fred_not_configured"},
            )
            return None
        try:
            self._fred = self._fred_factory()
        except FredCredentialsError:
            logger.warning(
                "FRED is unavailable: %s is not set. Derived greeks use the "
                "latest stored DGS3MO observation if one exists, else the "
                "quoted 4.25 percent default (0.0422764153 continuous), and say which",
                FRED_API_KEY_ENV,
                extra={
                    "event": "fred_unavailable",
                    "rule": (
                        "a missing optional vendor key leaves that vendor "
                        "unavailable and never stops the app booting"
                    ),
                    "variable": FRED_API_KEY_ENV,
                },
            )
        except Exception as exc:
            logger.error(
                "the FRED provider could not be built; FRED is unavailable",
                extra={
                    "event": "fred_unavailable",
                    "variable": FRED_API_KEY_ENV,
                    "error_type": type(exc).__name__,
                },
            )
        return self._fred

    def massive_provider(self) -> MassiveNewsSource | None:
        """The one Massive client, built on first call -- or ``None``, said once.

        **Never raises**, for :meth:`fred_provider`'s reason: the lifespan
        that calls this carries rule 9. An unset ``MASSIVE_API_KEY`` is
        logged inside ``MassiveProvider.available_from_env``; anything else a
        factory raises is logged here by class name only, since its message
        could quote the key.
        """
        if self._massive_resolved:
            return self._massive
        self._massive_resolved = True
        if self._massive_factory is None:
            return None
        try:
            self._massive = self._massive_factory()
        except Exception as exc:
            logger.error(
                "the Massive provider could not be built; Massive news is unavailable",
                extra={
                    "event": "massive_unavailable",
                    "variable": MASSIVE_API_KEY_ENV,
                    "error_type": type(exc).__name__,
                },
            )
        return self._massive

    def sec_provider(self) -> SecProvider | None:
        """The one SEC EDGAR client, built on first call -- or ``None``, said once.

        **Never raises**, for :meth:`massive_provider`'s reason. An unset
        ``SEC_USER_AGENT`` is logged inside ``SecProvider.from_env``, by name;
        anything else a factory raises is logged here by class name only,
        since its message could quote the User-Agent (rule 6).
        """
        if self._sec_resolved:
            return self._sec
        self._sec_resolved = True
        if self._sec_factory is None:
            return None
        try:
            self._sec = self._sec_factory()
        except Exception as exc:
            logger.error(
                "the SEC provider could not be built; the SPDR N-PORT snapshot is unavailable",
                extra={
                    "event": "sec_unavailable",
                    "variable": SEC_USER_AGENT_ENV,
                    "error_type": type(exc).__name__,
                },
            )
        return self._sec

    def openfigi_provider(self) -> OpenFigiProvider | None:
        """The one OpenFIGI client, built on first call -- or ``None``, said once.

        **Never raises**, for :meth:`sec_provider`'s reason. An unset
        ``OPENFIGI_API_KEY`` is keyless, not an absence. Anything a factory
        raises is logged by class name only, since its message could quote
        the key (rule 6).
        """
        if self._openfigi_resolved:
            return self._openfigi
        self._openfigi_resolved = True
        if self._openfigi_factory is None:
            return None
        try:
            self._openfigi = self._openfigi_factory()
        except Exception as exc:
            logger.error(
                "the OpenFIGI provider could not be built; the SPDR seed's ISIN-only "
                "lines are not resolved",
                extra={
                    "event": "openfigi_unavailable",
                    "variable": OPENFIGI_API_KEY_ENV,
                    "error_type": type(exc).__name__,
                },
            )
        return self._openfigi

    async def aclose(self) -> None:
        """Close whatever was actually built. Called from the lifespan.

        Every close is attempted even if one raises: an HTTP client left open
        on shutdown is a warning, and skipping the rest because of it turns
        one warning into several.
        """
        built: list[Any] = list(self._brokers.values())
        if self._provider is not None:
            built.append(self._provider)
        if self._fundamentals is not None:
            built.append(self._fundamentals)
        if self._fred is not None:
            built.append(self._fred)
        if self._massive is not None:
            built.append(self._massive)
        if self._sec is not None:
            built.append(self._sec)
        if self._openfigi is not None:
            built.append(self._openfigi)
        self._brokers.clear()
        self._provider = None
        self._fundamentals = None
        self._fred = None
        self._fred_resolved = False
        self._massive = None
        self._massive_resolved = False
        self._sec = None
        self._sec_resolved = False
        self._openfigi = None
        self._openfigi_resolved = False
        for service in built:
            close = getattr(service, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:
                logger.exception(
                    "failed to close %s on shutdown", type(service).__name__
                )


# --------------------------------------------------------------------------
# News state the routes read and never fetch (Phase 3 step 4)
# --------------------------------------------------------------------------

#: How the SPDR seed is loaded. A callable on ``app.state`` -- in production
#: a :class:`~corollary.data.seeds.nport.DatabaseSeedLoader` serving the latest
#: accepted N-PORT snapshot -- so a test can hand in a seed directly.
SeedLoader = Callable[[], SpdrSeed | None]


#: How long one read of the paper positions serves the watch universe. The
#: watch tier asks on every request (every ~14 s at W = 66); positions change
#: when an order fills, and Phase 3 places none. Five minutes keeps the
#: ``paper-api.`` cost at 12/hour against 200/min.
POSITION_UNDERLYINGS_TTL: Final = timedelta(minutes=5)


def position_underlying(symbol: str) -> str:
    """The symbol whose news a held position is about.

    An OCC contract maps to its root, with an adjusted root's numeric suffix
    stripped (``AAPL1`` is an AAPL deliverable after a corporate action, and
    the news is about Apple). **This is watch-universe membership only** --
    nothing sizes or prices from it; the multiplier stays per contract.
    Anything else -- an equity, or a symbol that is neither -- passes through
    unchanged, and :func:`~corollary.data.news.watchlist.watch_universe` skips
    what cannot be a member, with its reason.
    """
    try:
        root = parse_occ_symbol(symbol).root
    except ValueError:
        return symbol
    return root.rstrip("0123456789") or root


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PaperPositionsRefresher:
    """Reads the **paper** account's positions into the watch universe's holder.

    Owned by the lifespan, and **outside everything a context job is built
    from** (unit 4B2 audit): the jobs are handed
    :class:`~corollary.engine.scheduler.HeldPositionUnderlyings`, which only
    reads the holder, so no object a job holds can reach this refresher, its
    broker callable, or the registry that callable closes over.

    ``broker`` is ``registry.broker(PAPER)`` -- rule 5: Paper is the default,
    and a news cycle has no business reading Cash. The refresher calls exactly
    two things: ``positions()`` on that broker, which is read-only REST, and
    ``holder.replace``. It is handed no runtime, watchdog or socket
    supervisor, and every failure is caught here, so it can neither halt nor
    resume the engine (rule 9) -- a failed read is a stale watch list, never a
    lost connection.

    One read at start, then one per ``interval``. A failure (including a
    registry with no paper keys) is logged by class name only -- its message
    could carry a credential, rule 6 -- and leaves the holder as it was, whose
    ``as_of`` then says how stale it is. Before the first successful read the
    holder is empty with ``as_of`` ``None``: "never read", not "none held".
    """

    def __init__(
        self,
        *,
        broker: Callable[[], BrokerAccount],
        holder: PositionUnderlyings,
        clock: Callable[[], datetime] = _utc_now,
        interval: timedelta = POSITION_UNDERLYINGS_TTL,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._broker = broker
        self._holder = holder
        self._clock = clock
        self._interval = interval
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    @property
    def holder(self) -> PositionUnderlyings:
        return self._holder

    async def refresh(self) -> bool:
        """One read. ``True`` if the holder was replaced. Never raises an ``Exception``.

        The whole body is guarded, not only the broker read: a naive clock
        (``require_aware``), a row the mapping cannot take, or the holder's
        ``replace`` refusing its input would otherwise escape into the read
        loop and end it with nothing logged (unit 4B2 re-audit). Anything the
        broker read itself raises is the expected, specific warning below;
        anything else is logged here, by class name only (rule 6), and the
        holder is left as it was.
        """
        try:
            return await self._refresh_once()
        except Exception as exc:
            logger.error(
                "the paper positions refresh failed outside the broker read; "
                "the watch universe keeps the last position underlyings",
                extra={
                    "event": "position_underlyings_refresh_failed",
                    "account": AccountMode.PAPER.value,
                    "error_type": type(exc).__name__,
                    "retry_after_seconds": int(self._interval.total_seconds()),
                    "rule": (
                        "a failed position refresh keeps the last value and "
                        "is never a rule 9 input"
                    ),
                },
            )
            return False

    async def _refresh_once(self) -> bool:
        now = self._clock()
        require_aware(now, "now")
        try:
            held = await self._broker().positions()
        except Exception as exc:
            as_of = self._holder.as_of
            logger.warning(
                "the paper positions could not be read; the watch universe "
                "keeps the last position underlyings",
                extra={
                    "event": "position_underlyings_unavailable",
                    "account": AccountMode.PAPER.value,
                    "error_type": type(exc).__name__,
                    "kept": len(self._holder.current()),
                    "kept_as_of": None if as_of is None else as_of.isoformat(),
                    "retry_after_seconds": int(self._interval.total_seconds()),
                    "rule": (
                        "a failed position read keeps the last value and "
                        "is never a rule 9 input"
                    ),
                },
            )
            return False
        self._holder.replace(
            (position_underlying(row.symbol) for row in held), at=now
        )
        return True

    async def _run(self) -> None:
        while True:
            # ``refresh`` never raises an ``Exception``; this guard is the
            # loop's own, so that a regression there degrades to a logged,
            # stale watch list rather than a silently dead refresh task.
            try:
                await self.refresh()
            except Exception as exc:
                logger.error(
                    "the paper positions refresh raised; the read loop "
                    "continues on its interval",
                    extra={
                        "event": "position_underlyings_loop_error",
                        "account": AccountMode.PAPER.value,
                        "error_type": type(exc).__name__,
                        "retry_after_seconds": int(self._interval.total_seconds()),
                    },
                )
            await self._sleep(self._interval.total_seconds())

    def start(self) -> None:
        """Start the read loop. Call once, from the running event loop."""
        if self._task is not None:
            raise RuntimeError("the paper positions refresher is already running")
        self._task = asyncio.get_running_loop().create_task(
            self._run(), name="position-underlyings-refresh"
        )

    async def aclose(self) -> None:
        """Stop the loop and wait for it. Safe to call more than once. Never raises."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def service_registry(request: Request) -> ServiceRegistry:
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise ApiError(
            status_code=503,
            code="not_started",
            message=(
                "The service registry was never built. The app was started "
                "without its lifespan -- in production that is a process "
                "manager problem, in a test it is a missing "
                "`with TestClient(app)`."
            ),
        )
    assert isinstance(registry, ServiceRegistry)
    return registry


def account_mode(
    account: Annotated[
        AccountMode,
        Query(description="Which book to read. Defaults to paper (rule 5)."),
    ] = AccountMode.PAPER,
) -> AccountMode:
    """Which book this request is about. **Paper unless told otherwise.**

    Rule 5: every cold start comes up in Paper, and nothing persists Cash
    across a restart. A missing parameter is therefore Paper, never "whatever
    was selected last".
    """
    return account


def broker_for_account(
    mode: Annotated[AccountMode, Depends(account_mode)],
    registry: Annotated[ServiceRegistry, Depends(service_registry)],
) -> BrokerAccount:
    """The broker for the requested book, or a 409 naming what is missing.

    Typed :class:`BrokerAccount`, never ``AlpacaBroker``: the read half of the
    vendor surface is the only half that exists, so there is no order path in
    a type a route can name.
    """
    return registry.broker(mode)


def market_data(
    registry: Annotated[ServiceRegistry, Depends(service_registry)],
) -> MarketDataProvider:
    """The market-data provider. Account-independent -- quotes are quotes."""
    return registry.provider


def fundamentals_data(
    registry: Annotated[ServiceRegistry, Depends(service_registry)],
) -> FundamentalsProvider:
    """The fundamentals provider. Account-independent, and never ``None``.

    A second vendor behind a second interface, because the two fail
    independently: Alpaca going down is a table with no prices, Finnhub going
    down is one column of nulls.
    """
    return registry.fundamentals


def db_session(request: Request) -> Iterator[Session]:
    """A session on the app's engine.

    No pooling cleverness and no retry loop: the design spec chose one process
    with one writer precisely so there is nothing to contend with. The route
    commits; this only opens and closes.
    """
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        raise ApiError(
            status_code=503,
            code="not_started",
            message=(
                "No database engine is configured. The app was started without "
                "its lifespan."
            ),
        )
    assert isinstance(engine, Engine)
    with Session(engine) as session:
        yield session


def asset_directory(request: Request) -> AssetDirectoryHolder:
    """The cached daily asset list. Read-only here: the scheduler refreshes it."""
    holder = request.app.state.asset_directory
    assert isinstance(holder, AssetDirectoryHolder)
    return holder


def position_underlyings(request: Request) -> PositionUnderlyings:
    """The last-known position underlyings (see :class:`PositionUnderlyings`)."""
    holder = request.app.state.position_underlyings
    assert isinstance(holder, PositionUnderlyings)
    return holder


def spdr_seed(request: Request) -> SpdrSeed | None:
    """The SPDR seed, ``None`` when none was accepted, 503 when it cannot be read.

    Absent -- no accepted N-PORT snapshot yet, which is today's real state
    while XLB is refused -- is an ordinary, stated state. Unreadable
    (malformed, or a loader not yet bound to its database) is refused rather
    than degraded: a half-read seed would misfile sectors and shrink the
    watch universe's count under the cap in silence.
    """
    loader: SeedLoader | None = getattr(request.app.state, "spdr_seed_loader", None)
    if loader is None:
        raise ApiError(
            status_code=503,
            code="spdr_seed_invalid",
            message="The SPDR holdings seed cannot be read: this app has no seed loader.",
        )
    try:
        return loader()
    except SeedError as exc:
        logger.error(
            "the SPDR seed is malformed; refusing rather than reading half of it",
            extra={
                "event": "spdr_seed_invalid",
                "rule": "a malformed seed raises; a half-read seed is worse than none",
                "error": str(exc),
            },
        )
        raise ApiError(
            status_code=503,
            code="spdr_seed_invalid",
            message=f"The SPDR holdings seed cannot be read: {exc}",
        ) from exc



AccountModeDep = Annotated[AccountMode, Depends(account_mode)]
BrokerDep = Annotated[BrokerAccount, Depends(broker_for_account)]
ProviderDep = Annotated[MarketDataProvider, Depends(market_data)]
FundamentalsDep = Annotated[FundamentalsProvider, Depends(fundamentals_data)]
SessionDep = Annotated[Session, Depends(db_session)]
AssetDirectoryDep = Annotated[AssetDirectoryHolder, Depends(asset_directory)]
PositionUnderlyingsDep = Annotated[PositionUnderlyings, Depends(position_underlyings)]
SpdrSeedDep = Annotated[SpdrSeed | None, Depends(spdr_seed)]
