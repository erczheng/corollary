"""``AlpacaProvider`` — the Alpaca implementation of ``MarketDataProvider``.

This is one of exactly two files in the codebase allowed to hold the Alpaca
vendor surface (the other is ``engine/execution/alpaca.py``, for order
placement). See CLAUDE.md, "Working with market data". Nothing outside these
two files should know an endpoint path, a feed name, or a JSON field
abbreviation.

Why raw ``httpx`` and not the ``alpaca-py`` SDK
-----------------------------------------------

CLAUDE.md's import rule names the ``alpaca`` package, which reads as an
instruction to use the SDK. It was considered and rejected, on one decisive
technical ground and three supporting ones.

**The decisive one: ``alpaca-py`` parses money into ``float``.** Its market
data models are annotated ``bid_price: float``, ``ask_price: float``,
``open: float`` and so on. CLAUDE.md's first convention is *"Money as
``Decimal``, never ``float``. Currency arithmetic in floats is a real bug
source"* — and ``db/types.Money`` goes to the length of storing TEXT on SQLite
and **raising** on a float bind to keep that true at the database boundary. A
vendor client that converts every price to an IEEE double on ingest defeats
that at the *entry* boundary, where the loss is unrecoverable: by the time a
``Decimal`` is reconstructed from ``4.15``, the precision is already gone.

That is not avoidable by careful use, because of a fact worth recording
separately: **Alpaca's market data API returns prices as JSON numbers, not
strings.** The trading API returns strings (``"avg_entry_price": "8.21"``),
which is what the design spec observed and what makes *its* Decimal path easy.
The market data API does not — ``option_quote.ap`` is documented ``type:
number, format: double``. So the only way to reach an exact ``Decimal`` from a
quote is to control the JSON decoder, and ``json.loads(text,
parse_float=Decimal)`` does exactly that: ``4.15`` becomes ``Decimal('4.15')``
having never existed as a float. ``httpx``'s ``.json()`` offers no hook for
this and neither does ``alpaca-py``. :func:`corollary.wire.decode_json` is the
whole reason this argument matters, and it is four lines.

The coercions themselves moved to :mod:`corollary.wire` in step 4, when
``engine/execution/alpaca.py`` turned out to need exactly the same decode
path -- including the nanosecond truncation in
:func:`~corollary.wire.as_datetime`, which is thirty lines that must not exist
twice. They are re-bound below under the private names this file already used,
so nothing here changed but where the source sits.

The supporting three:

* The two-bucket rate limiter is ours regardless — ``alpaca-py`` has no
  concept of a per-host budget, and the ``data.``/``paper-api.`` split is not
  optional.
* Feed names must be configuration read from three environment variables and
  never a literal. The SDK wants enum members, so every value would be
  round-tripped through a string-to-enum map anyway.
* The responses this app needs are flat JSON with two-letter keys. The probe
  that produced the Phase 2 design reached all of them with plain HTTP.

The cost, stated: we now own the mapping, and a field Alpaca renames breaks
here rather than in a dependency's release notes. The recorded fixtures under
``tests/fixtures/alpaca/`` are the mitigation — they are real responses, and
re-recording them is how a rename gets caught.

What this file will not do
--------------------------

It does not read the ``data_feed`` table. Step 2 seeded that table with the
same three feed values, and **which of ``.env`` and the table wins at runtime
is an open step-7 decision**. Consulting both here would settle it by
accident, in the direction nobody chose. The environment is the input;
``tests/data/providers/test_feed_config.py`` asserts no database import
appears in this module.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Final

import httpx

from corollary.calendars import nyse_close_at
from corollary.data.providers.interface import (
    AnalyticsSource,
    Bar,
    BarTimeframe,
    FeedAccessError,
    MarketDataProvider,
    OptionContract,
    OptionDeliverable,
    OptionSnapshot,
    OptionType,
    ProviderError,
    Quote,
    RateLimitedError,
    StockSnapshot,
    Trade,
)
from corollary.instruments import is_adjusted_root, parse_occ_symbol
from corollary.pricing.blackscholes import (
    DEFAULT_DIVIDEND_YIELD,
    DEFAULT_RISK_FREE_RATE,
    Analytics,
    AnalyticsUnavailable,
    CloseAt,
    derive_analytics,
    years_to_expiry,
)
from corollary.ratelimit import (
    ALPACA_DATA_HOST,
    ALPACA_LIVE_TRADING_HOST,
    ALPACA_PAPER_TRADING_HOST,
    HostRateLimiter,
    default_limiter,
)
from corollary.wire import (
    as_date,
    as_datetime,
    as_decimal,
    as_int,
    clean_params,
    decode_json,
    require_aware,
    rfc3339,
    translating,
    vendor_detail,
)

__all__ = [
    "ALPACA_OPTIONS_FEED_ENV",
    "ALPACA_STOCK_FEED_HISTORICAL_ENV",
    "ALPACA_STOCK_FEED_REALTIME_ENV",
    "AlpacaCredentials",
    "AlpacaProvider",
    "CredentialsError",
    "DATA_BASE_URL",
    "FeedConfig",
    "FeedConfigError",
    "OPTION_FEEDS",
    "REALTIME_DELAY",
    "STOCK_HISTORICAL_FEEDS",
    "STOCK_REALTIME_FEEDS",
]

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

ALPACA_OPTIONS_FEED_ENV: Final = "ALPACA_OPTIONS_FEED"
ALPACA_STOCK_FEED_HISTORICAL_ENV: Final = "ALPACA_STOCK_FEED_HISTORICAL"
ALPACA_STOCK_FEED_REALTIME_ENV: Final = "ALPACA_STOCK_FEED_REALTIME"

ALPACA_PAPER_KEY_ENV: Final = "ALPACA_PAPER_API_KEY"
ALPACA_PAPER_SECRET_ENV: Final = "ALPACA_PAPER_SECRET_KEY"
ALPACA_LIVE_KEY_ENV: Final = "ALPACA_LIVE_API_KEY"
ALPACA_LIVE_SECRET_ENV: Final = "ALPACA_LIVE_SECRET_KEY"

DATA_BASE_URL: Final = f"https://{ALPACA_DATA_HOST}"
PAPER_TRADING_BASE_URL: Final = f"https://{ALPACA_PAPER_TRADING_HOST}"
LIVE_TRADING_BASE_URL: Final = f"https://{ALPACA_LIVE_TRADING_HOST}"

#: Valid values per endpoint family, taken from Alpaca's OpenAPI enums. These
#: are *validation*, not selection — the value still comes from the
#: environment. A typo caught here is a startup error; the same typo passed
#: through is an HTTP 400 in the middle of a poll, several layers from its
#: cause.
OPTION_FEEDS: Final = frozenset({"opra", "indicative"})
#: ``stock_historical_feed``. Note ``delayed_sip`` is **absent** — it is valid
#: on the latest/snapshot endpoints only.
STOCK_HISTORICAL_FEEDS: Final = frozenset({"iex", "otc", "sip", "boats"})
#: ``stock_latest_feed``, which is the wider set.
STOCK_REALTIME_FEEDS: Final = frozenset(
    {"delayed_sip", "iex", "otc", "sip", "boats", "overnight"}
)

#: The Basic plan's real-time embargo. Anything whose ``end`` is older than
#: this may be requested on SIP for free; anything newer may not.
REALTIME_DELAY: Final = timedelta(minutes=15)

#: Slack on top of the embargo. Fifteen minutes exactly is a boundary the
#: server evaluates against *its* clock, and a request landing a second inside
#: it fails with an auth error rather than returning less data.
_DELAY_MARGIN: Final = timedelta(seconds=60)

#: How many pages one call may follow before it gives up.
#:
#: **Deliberately well under the per-host minute ceiling.** It used to be 200,
#: which is exactly :data:`~corollary.ratelimit.DEFAULT_REQUESTS_PER_MINUTE` —
#: so a single runaway call consumed 100% of the ``data.`` budget, stalled
#: every concurrent poll for the best part of a minute, and only then raised.
#: A quarter of the budget leaves the 2s market poll and the account trio
#: running while a bulk download is in flight. At 10,000 bars a page this is
#: still half a million bars, or ~1,300 trading days of one symbol's minute
#: bars, before a caller is told to narrow the request.
_MAX_PAGES: Final = 50

_OPTION_CHAIN_PAGE_LIMIT: Final = 1000
_BARS_PAGE_LIMIT: Final = 10_000
_CONTRACTS_PAGE_LIMIT: Final = 10_000


class FeedConfigError(RuntimeError):
    """A feed environment variable is missing or not a value Alpaca accepts.

    Deliberately fatal. CLAUDE.md's reasoning is specific and worth repeating
    at the raise site: defaulting the historical equity feed to IEX measures
    ``min_avg_volume`` against ~2.5% of real volume, so a 5,000,000 threshold
    filters on a fortieth of what the strategy author wrote — and nothing
    errors, the scanner just quietly returns a different universe.
    """


class CredentialsError(RuntimeError):
    """No API key pair in the environment for the requested account."""


@dataclass(frozen=True, slots=True)
class FeedConfig:
    """The three feed names, resolved from the environment exactly once.

    Read **only** here. CLAUDE.md: *"Feed names are configuration, never
    literals. Three env vars, read only inside ``data/providers/alpaca.py``."*
    Upgrading to Algo Trader Plus sets all three to ``opra``/``sip``/``sip``
    and changes nothing else in the codebase.
    """

    options: str
    stock_historical: str
    stock_realtime: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "FeedConfig":
        import os

        source: Mapping[str, str] = os.environ if env is None else env
        return cls(
            options=_require_feed(
                source,
                ALPACA_OPTIONS_FEED_ENV,
                OPTION_FEEDS,
                "the options feed. `indicative` is the free 15-minute-delayed "
                "derivative of OPRA; `opra` needs a paid subscription and a "
                "signed agreement",
            ),
            stock_historical=_require_feed(
                source,
                ALPACA_STOCK_FEED_HISTORICAL_ENV,
                STOCK_HISTORICAL_FEEDS,
                "the historical equity feed. This must be `sip` even on the "
                "Basic plan: SIP is 100% of US volume and IEX is ~2.5%, and "
                "any request whose `end` is more than 15 minutes old may use "
                "SIP for free. Defaulting it to IEX would silently measure "
                "every strategy's min_avg_volume against a fortieth of real "
                "volume",
            ),
            stock_realtime=_require_feed(
                source,
                ALPACA_STOCK_FEED_REALTIME_ENV,
                STOCK_REALTIME_FEEDS,
                "the real-time equity feed, used for snapshots and latest "
                "quotes. `iex` on the Basic plan",
            ),
        )


def _require_feed(
    env: Mapping[str, str], name: str, allowed: frozenset[str], purpose: str
) -> str:
    """Read one feed variable, or raise. Never returns a default."""
    raw = env.get(name)
    if raw is None or not raw.strip():
        raise FeedConfigError(
            f"{name} is not set. It configures {purpose}. "
            f"Accepted values: {', '.join(sorted(allowed))}. "
            "This raises rather than defaulting because a wrong feed produces "
            "plausible numbers rather than an error — see CLAUDE.md, "
            "'Working with market data'. Add it to your .env; the name is "
            "already in .env.example."
        )
    value = raw.strip().lower()
    if value not in allowed:
        raise FeedConfigError(
            f"{name}={raw.strip()!r} is not a feed this endpoint accepts. "
            f"It configures {purpose}. "
            f"Accepted values: {', '.join(sorted(allowed))}."
        )
    return value


@dataclass(frozen=True, slots=True)
class AlpacaCredentials:
    """An API key pair and the trading host it belongs to.

    ``__repr__`` is overridden and the class is not printable in full. Rule 6:
    *"No keys in code, in tests, in fixtures, or in log output."* A plain
    dataclass repr would put the secret into any exception traceback that
    happens to hold a provider — which is most of them.
    """

    key_id: str
    secret_key: str
    trading_base_url: str
    is_paper: bool

    def __repr__(self) -> str:
        masked = f"{self.key_id[:2]}…{self.key_id[-2:]}" if len(self.key_id) > 4 else "…"
        kind = "paper" if self.is_paper else "LIVE"
        return f"AlpacaCredentials({kind}, key_id={masked!r}, secret=<hidden>)"

    __str__ = __repr__

    def headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
        }

    @classmethod
    def paper_from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "AlpacaCredentials":
        """The paper pair. Rule 5: paper is the default, everywhere."""
        return cls._from_env(
            env, ALPACA_PAPER_KEY_ENV, ALPACA_PAPER_SECRET_ENV,
            PAPER_TRADING_BASE_URL, is_paper=True,
        )

    @classmethod
    def live_from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "AlpacaCredentials":
        """The live pair. Nothing in Phase 2 calls this — see rule 5.

        Present so the Settings page can report *presence* of live keys
        without a second code path, and so Phase 7 has one place to change.
        """
        return cls._from_env(
            env, ALPACA_LIVE_KEY_ENV, ALPACA_LIVE_SECRET_ENV,
            LIVE_TRADING_BASE_URL, is_paper=False,
        )

    @classmethod
    def _from_env(
        cls,
        env: Mapping[str, str] | None,
        key_name: str,
        secret_name: str,
        trading_base_url: str,
        *,
        is_paper: bool,
    ) -> "AlpacaCredentials":
        import os

        source: Mapping[str, str] = os.environ if env is None else env
        key_id = (source.get(key_name) or "").strip()
        secret_key = (source.get(secret_name) or "").strip()
        missing = [
            name
            for name, value in ((key_name, key_id), (secret_name, secret_key))
            if not value
        ]
        if missing:
            raise CredentialsError(
                f"{' and '.join(missing)} not set in the environment. "
                "The names are in .env.example; the values never appear in "
                "code, fixtures or logs."
            )
        return cls(
            key_id=key_id,
            secret_key=secret_key,
            trading_base_url=trading_base_url,
            is_paper=is_paper,
        )


# --------------------------------------------------------------------------
# Wire decoding
# --------------------------------------------------------------------------


#: The coercions themselves live in :mod:`corollary.wire`, vendor-neutral,
#: because ``engine/execution/alpaca.py`` needs the identical decode path and
#: ``as_datetime``'s nanosecond truncation must not exist twice. They are
#: re-bound here under the private names every call site below already uses,
#: and wrapped in :func:`corollary.wire.translating` so a malformed value
#: still surfaces as the ``ProviderError`` this module's callers handle. A
#: ``TypeError`` is deliberately not translated: a float reaching the money
#: path is a bug in this repository, not a vendor response.
_decode = translating(ProviderError, decode_json)
_as_decimal = translating(ProviderError, as_decimal)
_as_int = translating(ProviderError, as_int)
_as_datetime = translating(ProviderError, as_datetime)
_as_date = translating(ProviderError, as_date)


def _price_or_none(value: Any) -> Decimal | None:
    """A quote side, where Alpaca documents ``0`` as *"no active bid/ask"*.

    The distinction is the difference between "this is worth nothing" and "no
    one is quoting it", and collapsing them puts an invented mid into the
    input of a derived implied volatility.
    """
    parsed = _as_decimal(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _quote(symbol: str, payload: Mapping[str, Any] | None) -> Quote | None:
    if not payload:
        return None
    return Quote(
        symbol=symbol,
        bid=_price_or_none(payload.get("bp")),
        ask=_price_or_none(payload.get("ap")),
        bid_size=_as_int(payload.get("bs")) or 0,
        ask_size=_as_int(payload.get("as")) or 0,
        at=_as_datetime(payload["t"]),
    )


def _trade(symbol: str, payload: Mapping[str, Any] | None) -> Trade | None:
    if not payload:
        return None
    price = _as_decimal(payload.get("p"))
    if price is None:
        return None
    return Trade(
        symbol=symbol,
        price=price,
        size=_as_int(payload.get("s")) or 0,
        at=_as_datetime(payload["t"]),
    )


def _bar(symbol: str, payload: Mapping[str, Any] | None) -> Bar | None:
    if not payload:
        return None
    def need(key: str) -> Decimal:
        value = _as_decimal(payload.get(key))
        if value is None:
            # ``payload!r`` is unbounded and unredacted deliberately. This is
            # reached only after a 200 -- ``_get`` raises on >= 400 before
            # ``_decode`` -- so it is one decoded OHLCV row: no free text, no
            # credential, no account identifier. It is one of exactly two
            # interpolations on this vendor surface that do NOT pass through
            # ``wire.vendor_detail``. If a 200 payload ever gains free text,
            # that exemption stops holding and this must route through
            # ``_detail`` like the error branches already do.
            raise ProviderError(f"bar for {symbol} is missing {key!r}: {payload!r}")
        return value

    return Bar(
        symbol=symbol,
        at=_as_datetime(payload["t"]),
        open=need("o"),
        high=need("h"),
        low=need("l"),
        close=need("c"),
        volume=_as_int(payload.get("v")) or 0,
        trade_count=_as_int(payload.get("n")) or 0,
        vwap=_as_decimal(payload.get("vw")),
    )


def _option_contract(payload: Mapping[str, Any]) -> OptionContract:
    symbol = str(payload["symbol"])
    underlying = str(payload["underlying_symbol"])
    # `root_symbol` is NOT in the endpoint's `required` list, so it can be
    # absent even though every contract sampled carried it. Falling back to
    # the OCC root keeps adjusted-contract detection working rather than
    # defaulting it to the underlying, which would call every adjusted
    # contract standard — the failure direction that costs money.
    root = payload.get("root_symbol")
    root_symbol = str(root).strip() if root else parse_occ_symbol(symbol).root

    expiration = _as_date(payload["expiration_date"])
    if expiration is None:
        raise ProviderError(f"contract {symbol} has no expiration date")
    strike = _as_decimal(payload["strike_price"])
    multiplier = _as_decimal(payload["multiplier"])
    size = _as_decimal(payload["size"])
    if strike is None or multiplier is None or size is None:
        # Unbounded ``payload!r`` on purpose -- the second of the two sites
        # noted in ``_bar``. Bounding costs more here than anywhere: an
        # adjusted contract's ``deliverables`` runs to ~986 characters, and
        # that field is exactly what distinguishes a ``GME1`` delivering
        # 100 GME + 10 GME.WS from a standard contract. Truncating it would
        # remove the diagnostic that ``ledger.RejectionRule.
        # UNVERIFIED_DELIVERABLE`` exists to make answerable.
        raise ProviderError(
            f"contract {symbol} is missing strike, multiplier or size: {payload!r}"
        )
    return OptionContract(
        symbol=symbol,
        underlying_symbol=underlying,
        root_symbol=root_symbol,
        expiration=expiration,
        option_type=OptionType(str(payload["type"]).lower()),
        strike=strike,
        style=str(payload.get("style", "")),
        multiplier=multiplier,
        size=size,
        open_interest=_as_int(payload.get("open_interest")),
        open_interest_date=_as_date(payload.get("open_interest_date")),
        close_price=_as_decimal(payload.get("close_price")),
        close_price_date=_as_date(payload.get("close_price_date")),
        tradable=bool(payload.get("tradable", False)),
        status=str(payload.get("status", "")),
        name=str(payload.get("name", "")),
        deliverables=tuple(
            _deliverable(item) for item in payload.get("deliverables") or ()
        ),
    )


def _deliverable(payload: Mapping[str, Any]) -> OptionDeliverable:
    """One entry of a contract's deliverables array.

    ``amount`` is documented nullable -- *"can be null in case the deliverable
    settlement is delayed and the amount is yet to be determined"* -- so the
    ``None`` survives rather than becoming a zero.
    """
    return OptionDeliverable(
        type=str(payload.get("type", "")),
        symbol=str(payload.get("symbol", "")),
        amount=_as_decimal(payload.get("amount")),
        allocation_percentage=_as_decimal(payload.get("allocation_percentage")),
        settlement_type=str(payload.get("settlement_type", "")),
        settlement_method=str(payload.get("settlement_method", "")),
        delayed_settlement=bool(payload.get("delayed_settlement", False)),
    )


# --------------------------------------------------------------------------
# The provider
# --------------------------------------------------------------------------


class AlpacaProvider(MarketDataProvider):
    """Alpaca market data over REST, with a per-host request budget.

    Construct with :meth:`from_env` in production. The explicit constructor
    exists so tests can inject an ``httpx.MockTransport`` client, a frozen
    clock and a fake sleeper, and therefore make **no live calls at all**.
    """

    def __init__(
        self,
        *,
        credentials: AlpacaCredentials,
        feeds: FeedConfig,
        client: httpx.AsyncClient | None = None,
        limiter: HostRateLimiter | None = None,
        risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
        dividend_yield: float = DEFAULT_DIVIDEND_YIELD,
        now: Callable[[], datetime] | None = None,
        close_at: CloseAt | None = None,
    ) -> None:
        self._credentials = credentials
        self._feeds = feeds
        self._client = client if client is not None else httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None
        # Shared unless one is passed in. A private limiter per provider means
        # two providers in one process each believe they hold 200/min against
        # a single server-side ceiling — see `ratelimit.default_limiter`.
        self._limiter = limiter if limiter is not None else default_limiter()
        self._risk_free_rate = risk_free_rate
        self._dividend_yield = dividend_yield
        self._now = now if now is not None else lambda: datetime.now(timezone.utc)
        # The market calendar enters here and nowhere deeper. `blackscholes`
        # stays pure arithmetic; this is the composition point that gives it
        # the 13:00 ET close on the ~10 half-days a year.
        self._close_at: CloseAt = close_at if close_at is not None else nyse_close_at

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> "AlpacaProvider":
        """Build from the process environment. Paper credentials, per rule 5."""
        return cls(
            credentials=AlpacaCredentials.paper_from_env(env),
            feeds=FeedConfig.from_env(env),
            **kwargs,
        )

    @property
    def feeds(self) -> FeedConfig:
        return self._feeds

    @property
    def limiter(self) -> HostRateLimiter:
        """The budget this provider spends against. Shared by default."""
        return self._limiter

    async def aclose(self) -> None:
        """Close the transport, but only if we opened it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "AlpacaProvider":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- HTTP

    def _detail(self, response: httpx.Response) -> str:
        """This response's body, bounded and de-identified for an error message.

        **Both halves of the key pair** go in as literals, because both are
        sent: :meth:`AlpacaCredentials.headers` puts the key id in
        ``APCA-API-KEY-ID`` and the secret in ``APCA-API-SECRET-KEY`` on every
        request this provider makes. Alpaca echoes neither in an error body,
        and nothing about that is a guarantee — anything in front of it that
        reflects request headers into an error page (a WAF, a corporate proxy,
        a future error shape) writes a credential into a body this method then
        quotes into an exception message, and rule 6 covers log output.

        The 403 branch is the one that matters most. An entitlement or auth
        rejection is precisely the response most likely to quote back what it
        rejected, and this provider's 403 is not rare: ``feed=opra`` inside
        the 15-minute window answers with one on every call. A 40-character
        secret sits well inside :data:`ERROR_BODY_MAX`, so the bound is no
        protection here; the substitution is.
        """
        return vendor_detail(
            response.text,
            secrets=(self._credentials.key_id, self._credentials.secret_key),
        )

    async def _get(
        self, base_url: str, path: str, params: Mapping[str, Any] | None = None
    ) -> Any:
        """One GET, metered against the bucket for that host.

        The host is derived from ``base_url`` rather than passed in, so a new
        endpoint cannot accidentally be billed to the wrong budget.
        """
        url = f"{base_url}{path}"
        host = httpx.URL(base_url).host
        await self._limiter.acquire(host)

        try:
            response = await self._client.get(
                url, params=_clean_params(params), headers=self._credentials.headers()
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"GET {path} failed: {exc}") from exc

        if response.status_code == 403:
            raise FeedAccessError(
                f"GET {path} returned 403: {self._detail(response)}. This is an "
                "entitlement, not a bug — the Basic plan serves `indicative` "
                "options and IEX real-time equities, and `opra`/`sip` inside "
                "the last 15 minutes answers with an auth error rather than "
                "empty data. Check the plan before debugging the code."
            )
        if response.status_code == 429:
            # The body is deliberately not quoted here: `X-RateLimit-Reset` is
            # an epoch second, it is the only useful field in a 429, and it is
            # not credential-shaped. Anything added to this branch later goes
            # through `_detail` like the two above.
            raise RateLimitedError(
                f"GET {path} returned 429 despite the local budget. The server "
                "window and the local bucket disagree — another process may be "
                f"sharing this key. Reset header: "
                f"{response.headers.get('X-RateLimit-Reset', 'absent')}"
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"GET {path} returned {response.status_code}: "
                f"{self._detail(response)}"
            )
        return _decode(response.text)

    async def _paginate_bars(
        self, path: str, params: Mapping[str, Any], *, limit: int | None
    ) -> list[Any]:
        """Follow the page token, but stop once ``limit`` bars are in hand.

        Alpaca's ``limit`` is **per page**. Passing a caller's cap straight
        through and then paginating to exhaustion gives a parameter that reads
        like a bound and bounds nothing: ``stock_bars(["NVDA"], MINUTE,
        start=a year ago, limit=200)`` — a caller deliberately narrowing the
        request — used to spend a page per 200 bars until it hit the page
        ceiling, then raise and tell them to narrow a request they had already
        narrowed.

        Here ``limit`` is a cap on the total bars returned, as the ABC now
        says, and the page size is derived from what is left of it. The number
        of requests is therefore at most ``ceil(limit / _BARS_PAGE_LIMIT)``.
        """
        if limit is not None and limit <= 0:
            raise ValueError(
                f"limit must be a positive number of bars when given, got "
                f"{limit!r}. Pass None for 'as many as the window holds'."
            )
        pages: list[Any] = []
        token: str | None = None
        remaining = limit
        for _ in range(_MAX_PAGES):
            page_size = (
                _BARS_PAGE_LIMIT
                if remaining is None
                else min(remaining, _BARS_PAGE_LIMIT)
            )
            page = await self._get(
                DATA_BASE_URL,
                path,
                {**params, "limit": page_size, "page_token": token},
            )
            pages.append(page)
            if remaining is not None:
                remaining -= _count_bars(page)
                if remaining <= 0:
                    return pages
            token = page.get("next_page_token") if isinstance(page, dict) else None
            if not token:
                return pages
        raise ProviderError(
            f"GET {path} did not terminate within {_MAX_PAGES} pages. Refusing "
            "to return a partial result — narrow the window, or pass a `limit`."
        )

    async def _paginate(
        self, base_url: str, path: str, params: Mapping[str, Any]
    ) -> list[Any]:
        """Follow ``next_page_token`` to exhaustion, returning every page.

        Raises rather than truncating at the cap. A silently short chain is a
        chain missing strikes, and a scanner filtering on a partial universe
        gives a deterministic wrong answer — the worst kind.
        """
        pages: list[Any] = []
        token: str | None = None
        for _ in range(_MAX_PAGES):
            page = await self._get(
                base_url, path, {**params, "page_token": token}
            )
            pages.append(page)
            token = page.get("next_page_token") if isinstance(page, dict) else None
            if not token:
                return pages
        raise ProviderError(
            f"GET {path} did not terminate within {_MAX_PAGES} pages. Refusing "
            "to return a partial result — narrow the request instead."
        )

    # ------------------------------------------------------------- feeds

    def _historical_cutoff(self) -> datetime:
        """The newest ``end`` the historical feed will serve.

        Fifteen minutes plus a minute of slack. The boundary is evaluated
        against Alpaca's clock, not ours, and a request landing a second
        inside it answers with an auth error rather than less data.
        """
        return self._now() - REALTIME_DELAY - _DELAY_MARGIN

    def _historical_end(
        self, start: datetime | None, end: datetime | None
    ) -> datetime:
        """The ``end`` a bars request may actually ask for, or a refusal.

        **Bars are the historical surface and always use the historical
        feed.** CLAUDE.md draws the line at the endpoint rather than at the
        timestamp: *"Only the latest/snapshot endpoints and the live stream
        are IEX-limited."* So ``stock_realtime`` has no call site here at all.

        The bug this replaces looked only at ``end``, and an *explicit* one
        was used verbatim. ``stock_bars(u, start=T-20d, end=datetime.now(tz))``
        therefore put the **entire twenty-day window** on IEX — ~2.5% of US
        volume — so the scanner's 20-day average volume came out at a fortieth
        of the real figure and a ``min_avg_volume`` of 5,000,000 filtered on
        125,000. Nothing errored and nothing logged.

        Splitting the request was considered and rejected as both too clever
        for the engine and wrong: on any timeframe coarser than the embargo
        the two halves overlap in the same bar, and merging them would print
        one bar whose volume came from IEX beside nineteen whose volume came
        from SIP. A series that disagrees with itself about what "volume"
        means is worse than a series that is sixteen minutes short.

        So the window is shortened, out loud. And when *nothing* of it
        survives the shortening, the request is refused rather than answered
        from the thin feed under the same name.
        """
        # Both bounds are checked for awareness *before* either is compared
        # against the cutoff. Comparing a naive datetime to an aware one is a
        # bare TypeError from deep inside a comparison, and the caller has no
        # way to tell which of the two arguments was at fault.
        _require_aware(start, "start")
        _require_aware(end, "end")
        cutoff = self._historical_cutoff()
        if start is not None and start >= cutoff:
            raise ProviderError(
                f"stock_bars: the whole window {_rfc3339(start)}.."
                f"{_rfc3339(end) if end else 'now'} lies inside the "
                f"{int(REALTIME_DELAY.total_seconds() // 60)}-minute real-time "
                f"embargo, so the historical feed "
                f"({self._feeds.stock_historical}) cannot serve any of it. "
                "Refusing rather than falling back to the real-time feed: it "
                "is IEX on this plan, ~2.5% of US volume, and a request that "
                "silently answers with a fortieth of the volume under the same "
                "name is the min_avg_volume failure CLAUDE.md is written "
                "against. For the live tail ask stock_snapshots() or "
                "latest_stock_quotes(), which are real-time by design and say "
                "so."
            )
        if end is None:
            # Not a clamp — nobody asked for anything beyond it. Defaulting to
            # "now" instead is the same failure in its likeliest disguise,
            # since nobody writes `end=` when they mean "up to the present".
            return cutoff
        if end <= cutoff:
            return end
        logger.warning(
            "stock_bars: end shortened from %s to %s — the last %s of the "
            "window is inside the real-time embargo and only the %s feed "
            "serves it, which is ~2.5%% of US volume. Mixing it into a %s "
            "series would make the final bar's volume a fortieth of the rest, "
            "so it is dropped instead.",
            _rfc3339(end),
            _rfc3339(cutoff),
            end - cutoff,
            self._feeds.stock_realtime,
            self._feeds.stock_historical,
            extra={
                "requested_end": _rfc3339(end),
                "effective_end": _rfc3339(cutoff),
                "feed": self._feeds.stock_historical,
            },
        )
        return cutoff

    # ------------------------------------------------------------- stocks

    async def latest_stock_quotes(
        self, symbols: Sequence[str]
    ) -> dict[str, Quote]:
        if not symbols:
            return {}
        payload = await self._get(
            DATA_BASE_URL,
            "/v2/stocks/quotes/latest",
            {"symbols": ",".join(symbols), "feed": self._feeds.stock_realtime},
        )
        quotes = payload.get("quotes") or {}
        result: dict[str, Quote] = {}
        for symbol, raw in quotes.items():
            quote = _quote(symbol, raw)
            if quote is not None:
                result[symbol] = quote
        return result

    async def stock_snapshots(
        self, symbols: Sequence[str]
    ) -> dict[str, StockSnapshot]:
        """Latest trade, quote and bars per symbol.

        Note the response shape asymmetry against the option chain: this
        endpoint returns a **bare map** of symbol to snapshot, while
        ``/v1beta1/options/snapshots/{underlying}`` wraps its map in a
        ``snapshots`` key alongside ``next_page_token``. Same word, two
        envelopes.
        """
        if not symbols:
            return {}
        payload = await self._get(
            DATA_BASE_URL,
            "/v2/stocks/snapshots",
            {"symbols": ",".join(symbols), "feed": self._feeds.stock_realtime},
        )
        return {
            symbol: StockSnapshot(
                symbol=symbol,
                latest_quote=_quote(symbol, raw.get("latestQuote")),
                latest_trade=_trade(symbol, raw.get("latestTrade")),
                minute_bar=_bar(symbol, raw.get("minuteBar")),
                daily_bar=_bar(symbol, raw.get("dailyBar")),
                previous_daily_bar=_bar(symbol, raw.get("prevDailyBar")),
            )
            for symbol, raw in payload.items()
            if isinstance(raw, dict)
        }

    async def stock_bars(
        self,
        symbols: Sequence[str],
        *,
        timeframe: BarTimeframe = BarTimeframe.DAY,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, list[Bar]]:
        if not symbols:
            return {}
        # Resolved first: `_historical_end` validates both bounds by name, and
        # a dict literal would otherwise evaluate `_rfc3339(start)` before it.
        resolved_end = self._historical_end(start, end)
        pages = await self._paginate_bars(
            "/v2/stocks/bars",
            {
                "symbols": ",".join(symbols),
                "timeframe": timeframe.value,
                "start": _rfc3339(start),
                "end": _rfc3339(resolved_end),
                # Always the historical feed. See `_historical_end`.
                "feed": self._feeds.stock_historical,
                # Split adjustment is not optional for anything that compares
                # volume: a pre-split bar reports a quarter of the share count
                # at four times the price, so an unadjusted average volume is
                # wrong by the split ratio across the boundary. Dividend
                # adjustment is deliberately *not* applied — it rewrites
                # historical prices so a chart disagrees with what printed on
                # the day.
                "adjustment": "split",
                "sort": "asc",
            },
            limit=limit,
        )
        return _collect_bars(pages)

    # ------------------------------------------------------------ options

    async def latest_option_quotes(
        self, symbols: Sequence[str]
    ) -> dict[str, Quote]:
        if not symbols:
            return {}
        payload = await self._get(
            DATA_BASE_URL,
            "/v1beta1/options/quotes/latest",
            {"symbols": ",".join(symbols), "feed": self._feeds.options},
        )
        quotes = payload.get("quotes") or {}
        result: dict[str, Quote] = {}
        for symbol, raw in quotes.items():
            quote = _quote(symbol, raw)
            if quote is not None:
                result[symbol] = quote
        return result

    async def option_bars(
        self,
        symbols: Sequence[str],
        *,
        timeframe: BarTimeframe = BarTimeframe.DAY,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, list[Bar]]:
        if not symbols:
            return {}
        _require_aware(start, "start")
        _require_aware(end, "end")
        pages = await self._paginate_bars(
            "/v1beta1/options/bars",
            {
                "symbols": ",".join(symbols),
                "timeframe": timeframe.value,
                "start": _rfc3339(start),
                # No clamp and no refusal here, unlike `stock_bars`: this
                # endpoint takes no `feed`, so there is no thin feed to be
                # silently downgraded to. An `end` inside the embargo simply
                # returns what the plan may serve.
                "end": _rfc3339(end if end is not None else self._historical_cutoff()),
                # No `feed` parameter. Verified against the live API on
                # 2026-09-10: this endpoint answers HTTP 400 "unexpected query
                # parameter(s): feed" -- unlike every other options endpoint,
                # which requires one. Historical option bars are the same on
                # either feed because everything older than 15 minutes is
                # available to every plan, so there is nothing to select.
                "sort": "asc",
            },
            limit=limit,
        )
        return _collect_bars(pages)

    async def option_snapshots(
        self,
        underlying: str,
        *,
        expiration_lte: date | None = None,
        expiration_gte: date | None = None,
        strike_gte: Decimal | None = None,
        strike_lte: Decimal | None = None,
        option_type: OptionType | None = None,
    ) -> dict[str, OptionSnapshot]:
        """The raw chain: quotes, trades and bars, no derived analytics.

        Kept separate from :meth:`option_chain` so the vendor mapping stays
        honest — what Alpaca sent, and nothing computed. Every snapshot here
        reports ``AnalyticsSource.UNAVAILABLE`` unless the feed itself
        supplied IV and greeks, which only OPRA does.
        """
        pages = await self._paginate(
            DATA_BASE_URL,
            f"/v1beta1/options/snapshots/{underlying}",
            {
                "feed": self._feeds.options,
                "limit": _OPTION_CHAIN_PAGE_LIMIT,
                "type": option_type.value if option_type is not None else None,
                "strike_price_gte": _plain(strike_gte),
                "strike_price_lte": _plain(strike_lte),
                "expiration_date_gte": expiration_gte,
                "expiration_date_lte": expiration_lte,
                # A server-side root filter is the cheap half of adjusted
                # rejection: it stops AAPL1 contracts being sent at all. The
                # symbol-level check in option_chain is the half that does not
                # depend on the vendor honouring it.
                "root_symbol": underlying,
            },
        )
        snapshots: dict[str, OptionSnapshot] = {}
        for page in pages:
            for symbol, raw in (page.get("snapshots") or {}).items():
                if not isinstance(raw, dict):
                    continue
                snapshots[symbol] = _option_snapshot(symbol, raw)
        return snapshots

    async def option_chain(
        self,
        underlying: str,
        *,
        expiration_lte: date | None = None,
        expiration_gte: date | None = None,
        strike_gte: Decimal | None = None,
        strike_lte: Decimal | None = None,
        option_type: OptionType | None = None,
    ) -> dict[str, OptionSnapshot]:
        """The chain with IV and greeks derived per decision 10.

        One extra request, against the same ``data.`` bucket, to get the
        underlying's spot — Black-Scholes needs it and the chain endpoint does
        not carry it.
        """
        snapshots = await self.option_snapshots(
            underlying,
            expiration_lte=expiration_lte,
            expiration_gte=expiration_gte,
            strike_gte=strike_gte,
            strike_lte=strike_lte,
            option_type=option_type,
        )
        if not snapshots:
            return {}

        spot = (await self.stock_snapshots([underlying])).get(underlying)
        spot_price = spot.price if spot is not None else None
        now = self._now()

        enriched: dict[str, OptionSnapshot] = {}
        dropped: list[str] = []
        for symbol, snapshot in snapshots.items():
            try:
                occ = parse_occ_symbol(symbol)
            except ValueError:
                dropped.append(symbol)
                continue
            if is_adjusted_root(occ.root, underlying):
                dropped.append(symbol)
                continue
            enriched[symbol] = self._with_analytics(snapshot, occ_strike=occ.strike,
                                                    expiration=occ.expiration,
                                                    is_call=occ.option_type is OptionType.CALL,
                                                    spot=spot_price, now=now)
        if dropped:
            # Never silently. An adjusted contract vanishing without a word is
            # how a chain quietly stops matching the broker's position list.
            logger.warning(
                "dropped %d non-standard contract(s) from the %s chain: %s",
                len(dropped), underlying, ", ".join(sorted(dropped)),
                extra={"underlying": underlying, "dropped_symbols": sorted(dropped)},
            )
        return enriched

    def _with_analytics(
        self,
        snapshot: OptionSnapshot,
        *,
        occ_strike: Decimal,
        expiration: date,
        is_call: bool,
        spot: Decimal | None,
        now: datetime,
    ) -> OptionSnapshot:
        if snapshot.analytics_source is AnalyticsSource.VENDOR:
            return snapshot
        if spot is None:
            return _unavailable(snapshot, "no price for the underlying")
        mid = snapshot.latest_quote.mid if snapshot.latest_quote else None
        if mid is None:
            return _unavailable(
                snapshot, "no two-sided quote on the indicative feed"
            )
        result = derive_analytics(
            mid=mid,
            spot=spot,
            strike=occ_strike,
            years=years_to_expiry(expiration, now, close_at=self._close_at),
            is_call=is_call,
            rate=self._risk_free_rate,
            dividend_yield=self._dividend_yield,
        )
        if isinstance(result, AnalyticsUnavailable):
            return _unavailable(snapshot, result.reason)
        return _derived(snapshot, result)

    async def option_contracts(
        self,
        underlying: str,
        *,
        expiration_lte: date | None = None,
        expiration_gte: date | None = None,
        strike_gte: Decimal | None = None,
        strike_lte: Decimal | None = None,
        option_type: OptionType | None = None,
        include_adjusted: bool = False,
        show_deliverables: bool = False,
    ) -> list[OptionContract]:
        """Reference data — the only source of ``multiplier`` and open interest.

        **On the trading host, not the data host.** ``/v2/options/contracts``
        is part of the Trading API, so this request is metered against the
        ``paper-api.alpaca.markets`` bucket while the chain above is metered
        against ``data.alpaca.markets``. One logical operation, two budgets —
        which is why :class:`~corollary.ratelimit.HostRateLimiter` keys on the
        host rather than holding a single counter.
        """
        pages = await self._paginate(
            self._credentials.trading_base_url,
            "/v2/options/contracts",
            {
                "underlying_symbols": underlying,
                "status": "active",
                "limit": _CONTRACTS_PAGE_LIMIT,
                "type": option_type.value if option_type is not None else None,
                "strike_price_gte": _plain(strike_gte),
                "strike_price_lte": _plain(strike_lte),
                "expiration_date_gte": expiration_gte,
                "expiration_date_lte": expiration_lte,
                "show_deliverables": show_deliverables or None,
            },
        )
        contracts: list[OptionContract] = []
        adjusted: list[str] = []
        for page in pages:
            for raw in page.get("option_contracts") or []:
                contract = _option_contract(raw)
                if contract.is_adjusted and not include_adjusted:
                    adjusted.append(contract.symbol)
                    continue
                contracts.append(contract)
        if adjusted:
            logger.warning(
                "excluded %d adjusted contract(s) for %s (root_symbol differs "
                "from underlying_symbol; note these report multiplier=100 like "
                "any standard contract, so root_symbol is the only detector "
                "and the real deliverable is in `deliverables`): %s",
                len(adjusted), underlying, ", ".join(sorted(adjusted)),
                extra={"underlying": underlying, "adjusted_symbols": sorted(adjusted)},
            )
        return contracts


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _option_snapshot(symbol: str, raw: Mapping[str, Any]) -> OptionSnapshot:
    """Map one chain entry. Vendor analytics pass through; nothing is derived.

    ``impliedVolatility`` and ``greeks`` are both optional in Alpaca's schema
    — neither appears in any ``required`` list — and on ``feed=indicative``
    they arrive **per contract**, not per feed: 16 of the 100 snapshots in
    ``tests/fixtures/alpaca/option_chain_nvda_page1.json`` carry both, 28 of
    100 in ``page2``, and all 26 in ``option_chain_nvda_dated``. The design's
    "the feed serves none" came from a sample of the deep-ITM tail, which is
    where a vendor derivation has least to work with.

    That matters beyond accuracy of comment: :attr:`analytics_note` is what
    §8.5 renders as the reason an absence is an absence, so it is a sentence
    the user reads. It says what is true of *this* contract.
    """
    vendor_iv = _as_decimal(raw.get("impliedVolatility"))
    vendor_greeks_raw = raw.get("greeks")
    greeks_value = None
    source = AnalyticsSource.UNAVAILABLE
    note = "the feed supplied no implied volatility or greeks for this contract"

    if vendor_iv is not None and isinstance(vendor_greeks_raw, dict):
        from corollary.pricing.blackscholes import Greeks

        def leg(key: str) -> Decimal:
            value = _as_decimal(vendor_greeks_raw.get(key))
            if value is None:
                raise ProviderError(f"greeks for {symbol} are missing {key!r}")
            return value

        greeks_value = Greeks(
            delta=leg("delta"), gamma=leg("gamma"), theta=leg("theta"),
            vega=leg("vega"), rho=leg("rho"),
        )
        source = AnalyticsSource.VENDOR
        note = ""

    return OptionSnapshot(
        symbol=symbol,
        latest_quote=_quote(symbol, raw.get("latestQuote")),
        latest_trade=_trade(symbol, raw.get("latestTrade")),
        minute_bar=_bar(symbol, raw.get("minuteBar")),
        daily_bar=_bar(symbol, raw.get("dailyBar")),
        previous_daily_bar=_bar(symbol, raw.get("prevDailyBar")),
        implied_volatility=vendor_iv if source is AnalyticsSource.VENDOR else None,
        greeks=greeks_value,
        analytics_source=source,
        analytics_note=note,
    )


def _unavailable(snapshot: OptionSnapshot, reason: str) -> OptionSnapshot:
    from dataclasses import replace

    return replace(
        snapshot,
        implied_volatility=None,
        greeks=None,
        analytics_source=AnalyticsSource.UNAVAILABLE,
        analytics_note=reason,
    )


def _derived(snapshot: OptionSnapshot, analytics: Analytics) -> OptionSnapshot:
    from dataclasses import replace

    return replace(
        snapshot,
        implied_volatility=analytics.implied_volatility,
        greeks=analytics.greeks,
        analytics_source=AnalyticsSource.DERIVED,
        analytics_note="",
    )


def _count_bars(page: Any) -> int:
    """How many bar rows one page carried, across every symbol in it.

    Counted from the wire rather than from the parsed result so the page
    budget is spent against what Alpaca actually returned, including rows a
    later mapping step might drop.
    """
    if not isinstance(page, dict):
        return 0
    return sum(len(rows or ()) for rows in (page.get("bars") or {}).values())


def _collect_bars(pages: Sequence[Any]) -> dict[str, list[Bar]]:
    collected: dict[str, list[Bar]] = {}
    for page in pages:
        for symbol, rows in (page.get("bars") or {}).items():
            bucket = collected.setdefault(symbol, [])
            for row in rows or []:
                bar = _bar(symbol, row)
                if bar is not None:
                    bucket.append(bar)
    return collected


#: Request-side encoding, also from :mod:`corollary.wire` -- the trading host
#: builds the same ``after``/``until`` parameters from aware datetimes.
_require_aware = require_aware
_rfc3339 = rfc3339


def _plain(value: Decimal | None) -> str | None:
    """A ``Decimal`` as a query string without scientific notation.

    ``str(Decimal('1E+2'))`` is ``'1E+2'``, which a strike filter would read
    as anything but 100.
    """
    return None if value is None else format(value, "f")


_clean_params = clean_params
