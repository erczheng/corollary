"""Finnhub over REST -- the one field the Markets stock table cannot get from
Alpaca.

Phase 2 design, decision 7: *"One field from ``/stock/profile2``, cached
daily. ``FINNHUB_API_KEY`` is already in ``.env.example`` and Finnhub is
already section 7's source for calendar and consensus, so this is a planned
dependency arriving one phase early rather than a new one."* Decision 12
re-examined the vendor against yfinance, FMP, Alpha Vantage and Massive and
kept it.

**This is the entire Finnhub surface area**, the way
``data/providers/alpaca.py`` and ``engine/execution/alpaca.py`` are Alpaca's.
Everything else reaches it through
:class:`~corollary.data.providers.fundamentals.FundamentalsProvider`, so the
day SEC EDGAR's XBRL ``companyfacts`` replaces it -- decision 12's recorded
vendor-free alternative -- the change is this file and a constructor.

Four things this file is deliberate about
-----------------------------------------

**The unit is millions, and the conversion happens here.** Finnhub reports
``marketCapitalization`` as a JSON number *in millions of the reporting
currency*: the recorded AAPL profile carries ``4849208.193522442``, meaning
~$4.85 trillion, beside a ``shareOutstanding`` of ``14687.36`` -- millions
again, and the two multiply out to the quoted price. Recorded and
pinned in ``tests/data/providers/test_finnhub_provider.py``, which asserts the
answer lands inside a sane band for a mega cap -- a units error here is wrong
by a factor of a million and would sort the whole screener by nothing at all.
The interface hands callers whole USD so that no caller has to remember.

**No float touches it.** The body goes through
:func:`corollary.wire.decode_json`, so ``4849208.193522442`` is a ``Decimal``
before
any of our code sees it, and :func:`corollary.wire.as_decimal` *raises* on a
``float`` if the decoder is ever bypassed. Millions become units by
multiplying by an exact ``Decimal``, not by ``1e6``.

**A fund's market cap is null, and that is an answer.** An ETF files no share
count, and Finnhub returns a profile with no ``marketCapitalization`` -- the
same shape it returns for a symbol it does not cover. Both are
:attr:`~corollary.data.providers.fundamentals.MarketCapStatus.NOT_FILED`:
absent on purpose, never zero. CLAUDE.md: *"coerced to zero it would sort SPY
to the top of an ascending list and state, in a column of dollars, that a
fund is worth nothing."*

**A failure is never a fund.** Every per-symbol failure comes back as
:attr:`~corollary.data.providers.fundamentals.MarketCapStatus.UNAVAILABLE`
with its cause, and is logged as a warning naming the rule, the symbol and
the reason. The two absences are identical on the wire and must never be
identical in the log -- rule 8, and the difference between "SPY has no market
cap" and "nobody could ask".

*Every* failure, including a shape nobody predicted: :meth:`market_caps`
does not raise, and a symbol whose coroutine somehow does is still an
``UNAVAILABLE`` entry rather than the loss of the other twenty-five. Market
cap is a display-only column on a table of **prices**, and a display column
that can 500 the price path is the one outcome this file is arranged to make
impossible.

**A vendor sending nonsense and our decoder being bypassed are two different
events, and they stay two.** :func:`corollary.wire.as_decimal` raises
``TypeError`` -- untranslated, on purpose -- when it is handed a ``float``,
because a finite float can only reach it if :func:`corollary.wire.decode_json`
was skipped, which is *our* bug on the money path and is meant to be loud.
A vendor answering ``"marketCapitalization": true`` or ``[1, 2]`` is not that
event: it is untrusted input arriving in exactly the shape the decoder exists
to reject. So :func:`_vendor_number` refuses the untrusted shapes **before**
they reach ``as_decimal``, as a :class:`FundamentalsError` naming the type,
and the tripwire is left holding only the case it was written for. The one
subtlety is that ``json.loads`` accepts the ``NaN`` and ``Infinity``
extensions and ``parse_float`` does **not** intercept them, so a *non-finite*
float is a vendor shape while a finite one is our bug -- the two are split on
exactly that line.

The token
---------

Sent as the ``X-Finnhub-Token`` **header**. Finnhub also accepts ``?token=``,
and that form is not used here: a query string is the part of a request that
turns up in access logs, proxy logs and exception messages, and rule 6 covers
log output. :func:`corollary.wire.vendor_detail` scrubs the token out of any
error body this module quotes, for the same reason the Alpaca provider scrubs
its key pair.

The budget
----------

60 requests a minute on the free tier, metered by the **shared**
:class:`~corollary.ratelimit.HostRateLimiter` against ``finnhub.io``. Not a
private limiter: two limiters against one server-side ceiling over-spend by
double and look fine locally, which is the failure ``ratelimit`` is written
against. The per-host table there carries the 60.

At 26 symbols in the universe and one request per symbol per trading date, a
cold start costs 26 requests once a day, inside a bucket that starts full at
60. The caller's daily cache is what keeps it to once -- see
``api/routes/markets.py``.
"""

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import httpx

from corollary.data.providers.fundamentals import (
    FundamentalsError,
    FundamentalsProvider,
    MarketCap,
)
from corollary.ratelimit import FINNHUB_HOST, HostRateLimiter, default_limiter
from corollary.wire import (
    as_decimal,
    clean_params,
    decode_json,
    translating,
    vendor_detail,
)

__all__ = [
    "FINNHUB_API_KEY_ENV",
    "FINNHUB_BASE_URL",
    "FINNHUB_TOKEN_HEADER",
    "FinnhubCredentials",
    "FinnhubCredentialsError",
    "FinnhubProvider",
    "MILLION",
]

logger = logging.getLogger(__name__)

#: The REST root. ``finnhub.io`` is the host the rate limiter meters.
FINNHUB_BASE_URL: Final = "https://finnhub.io/api/v1"

#: The header the token travels in. Never the query string -- see the module
#: docstring.
FINNHUB_TOKEN_HEADER: Final = "X-Finnhub-Token"

#: Already present in ``.env.example``; decision 7 depends on that fact.
FINNHUB_API_KEY_ENV: Final = "FINNHUB_API_KEY"

#: The vendor's unit, as an exact ``Decimal``. Never ``1e6``: that is a float,
#: and multiplying a ``Decimal`` by one raises.
MILLION: Final = Decimal(1_000_000)

#: Currencies whose figures may be served as dollars -- one of them.
#:
#: Finnhub reports a foreign listing's market cap in its own currency, and
#: this column is headed in dollars. Converting would need an FX rate nobody
#: here has; serving the number anyway would state, in a dollar column, a
#: figure that is not dollars. So a non-USD profile is ``UNAVAILABLE`` with
#: the currency named, which is the honest third answer. Every symbol in the
#: current universe is a US listing, so nothing hits this today.
_USD: Final = "USD"

_decode = translating(FundamentalsError, decode_json)
_as_decimal = translating(FundamentalsError, as_decimal)


class FinnhubCredentialsError(RuntimeError):
    """No Finnhub key in the environment.

    Parallel to ``alpaca.CredentialsError`` rather than shared with it: the
    two vendors have two keys and fail independently, and a caller that
    degrades one column on a missing Finnhub key must not swallow a missing
    Alpaca pair by accident.
    """


@dataclass(frozen=True, slots=True)
class FinnhubCredentials:
    """The API token.

    ``__repr__`` is overridden for the reason ``AlpacaCredentials``'s is. Rule
    6: *"No keys in code, in tests, in fixtures, or in log output."* A plain
    dataclass repr would put the token into any traceback holding a provider.
    """

    token: str

    def __repr__(self) -> str:
        return "FinnhubCredentials(token=<hidden>)"

    __str__ = __repr__

    def headers(self) -> dict[str, str]:
        return {FINNHUB_TOKEN_HEADER: self.token, "Accept": "application/json"}

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "FinnhubCredentials":
        import os

        source: Mapping[str, str] = os.environ if env is None else env
        token = (source.get(FINNHUB_API_KEY_ENV) or "").strip()
        if not token:
            raise FinnhubCredentialsError(
                f"{FINNHUB_API_KEY_ENV} is not set in the environment. It is "
                "the market-cap column's only source; the name is already in "
                ".env.example and the value never appears in code, fixtures "
                "or logs."
            )
        return cls(token=token)


class FinnhubProvider(FundamentalsProvider):
    """Company reference data from Finnhub, with a per-host request budget.

    Construct with :meth:`from_env` in production. The explicit constructor
    exists so tests can inject an ``httpx.MockTransport`` client and therefore
    make **no live calls at all**.
    """

    def __init__(
        self,
        *,
        credentials: FinnhubCredentials,
        client: httpx.AsyncClient | None = None,
        limiter: HostRateLimiter | None = None,
        base_url: str = FINNHUB_BASE_URL,
    ) -> None:
        self._credentials = credentials
        self._client = client if client is not None else httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None
        self._limiter = limiter if limiter is not None else default_limiter()
        self._base_url = base_url.rstrip("/")

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, **kwargs: Any
    ) -> "FinnhubProvider":
        """Build from the process environment."""
        return cls(credentials=FinnhubCredentials.from_env(env), **kwargs)

    @property
    def limiter(self) -> HostRateLimiter:
        """The budget this provider spends against. Shared by default."""
        return self._limiter

    async def aclose(self) -> None:
        """Close the transport, but only if we opened it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "FinnhubProvider":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- HTTP

    def _scrub(self, text: str) -> str:
        """Third-party text, bounded and de-identified, for a message or a log.

        The token goes in as a literal because it is sent on every request:
        anything in front of the vendor that reflects a request header into
        an error page -- a WAF, a corporate proxy, a future error shape --
        writes the credential into text this module then quotes into an
        exception message, and from there into a log. Rule 6 covers log
        output.

        **Every quotation of text this process did not compose goes through
        here**, response bodies and exception messages alike. The exception
        reach is the exotic one -- an ``httpx``/``h11`` error that embeds a
        rejected header value, which is the token -- but it is one call, and
        closing the channel by construction is cheaper than re-arguing each
        new error shape's contents.
        """
        return vendor_detail(text, secrets=(self._credentials.token,))

    def _detail(self, response: httpx.Response) -> str:
        """This response's body, bounded and de-identified for a message."""
        return self._scrub(response.text)

    def _fault(self, symbol: str, exc: BaseException) -> MarketCap:
        """:func:`_log_internal_fault`, with this provider's scrub applied.

        Every residual catch reports through here rather than composing its
        own cause text. There are three of them and they were not equal: two
        passed the exception bare, so the function below re-derived
        ``str(exc)`` itself and the scrub was one arm wide. ``detail`` is a
        required argument there for that reason, and this is the only place
        in the module that holds the token, so a fourth catch either calls
        this or does not type-check.
        """
        return _log_internal_fault(
            symbol, exc, detail=self._scrub(f"unexpected {type(exc).__name__}: {exc}")
        )

    async def _get(self, path: str, params: Mapping[str, Any]) -> Any:
        """One GET, metered against the ``finnhub.io`` bucket.

        Raises :class:`FundamentalsError` on anything that is not a 2xx.
        Per-symbol callers catch it and turn it into an ``UNAVAILABLE``
        entry, so one failure never costs another symbol its answer.
        """
        await self._limiter.acquire(FINNHUB_HOST)
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                params=clean_params(params),
                headers=self._credentials.headers(),
            )
        except httpx.HTTPError as exc:
            # Scrubbed for the reason a response body is: ``httpx`` builds
            # this message out of the request it could not send, so a header
            # it rejected can land inside it, and the message becomes one
            # symbol's ``note`` and a log line.
            raise FundamentalsError(
                f"GET {path} failed: {self._scrub(str(exc))}"
            ) from exc

        if response.status_code in (401, 403):
            raise FundamentalsError(
                f"GET {path} returned {response.status_code}: "
                f"{self._detail(response)}. The token was not accepted -- "
                f"check {FINNHUB_API_KEY_ENV}, not the code."
            )
        if response.status_code == 429:
            raise FundamentalsError(
                f"GET {path} returned 429 despite the local budget of "
                "60/min. The server window and the local bucket disagree -- "
                "another process may be sharing this key."
            )
        if response.status_code >= 400:
            raise FundamentalsError(
                f"GET {path} returned {response.status_code}: "
                f"{self._detail(response)}"
            )
        return _decode(response.text)

    # ------------------------------------------------------------ the field

    async def market_caps(self, symbols: Sequence[str]) -> dict[str, MarketCap]:
        """Market capitalisation per symbol, in whole USD.

        One request per symbol -- ``/stock/profile2`` takes a single
        ``symbol`` and there is no batch form. They are issued concurrently
        and the shared rate limiter is what bounds them, so the wall time for
        the universe is one round trip rather than twenty-six.

        Every requested symbol appears in the result, including the ones that
        failed. Duplicates collapse to one request; blanks are dropped.
        """
        wanted = tuple(dict.fromkeys(s.strip() for s in symbols if s.strip()))
        if not wanted:
            return {}
        #: ``return_exceptions=True`` because the alternative discards
        #: twenty-five good answers to report one bad one: a bare ``gather``
        #: propagates the first exception and throws the rest of the results
        #: away. :meth:`_market_cap` already catches everything, so nothing
        #: should arrive here as an exception -- which is precisely why the
        #: day one does, it must not also cost the other symbols their
        #: figures.
        results = await asyncio.gather(
            *(self._market_cap(symbol) for symbol in wanted),
            return_exceptions=True,
        )
        return {
            symbol: (
                outcome
                if isinstance(outcome, MarketCap)
                else self._fault(symbol, outcome)
            )
            for symbol, outcome in zip(wanted, results)
        }

    async def _market_cap(self, symbol: str) -> MarketCap:
        """One symbol, with every failure turned into a stated absence."""
        try:
            payload = await self._get("/stock/profile2", {"symbol": symbol})
        except FundamentalsError as exc:
            return _log_unavailable(symbol, str(exc))
        except Exception as exc:  # noqa: BLE001 - one symbol must not sink 26
            # ``_log_internal_fault``, not ``_log_unavailable``, and the
            # sibling catch below already did this. Every *vendor* failure
            # from ``_get`` is a ``FundamentalsError`` or an
            # ``httpx.HTTPError`` it already translated, so what is left here
            # is dominated by faults in this repository: ``httpx`` raises a
            # bare ``RuntimeError`` if a lifecycle change closes the cached
            # provider's client while the app keeps serving, and
            # ``ratelimit`` raises ``ValueError`` on a misedited per-host
            # table. Reported as ``market_cap_unavailable`` those became 26
            # WARNINGs a poll, re-fired every ``MARKET_CAP_RETRY_TTL``, with
            # no traceback and the vendor named as the culprit.
            return self._fault(symbol, exc)
        if not isinstance(payload, Mapping):
            return _log_unavailable(
                symbol,
                f"/stock/profile2 answered with a {type(payload).__name__}, "
                "not an object",
            )
        try:
            return market_cap_from_profile(symbol, payload)
        except FundamentalsError as exc:
            return _log_unavailable(symbol, str(exc))
        except Exception as exc:  # noqa: BLE001 - see _log_internal_fault
            return self._fault(symbol, exc)


def market_cap_from_profile(symbol: str, payload: Mapping[str, Any]) -> MarketCap:
    """Read one ``/stock/profile2`` body into a :class:`MarketCap`.

    Module-level and public so the unit conversion can be tested against a
    recorded body without a transport, which is where a factor-of-a-million
    error would be caught.

    Raises :class:`FundamentalsError` for a body that cannot be read as a USD
    figure; the caller turns that into an ``UNAVAILABLE`` entry. A body that
    simply has no market cap is **not** an error -- it is the fund answer.
    """
    raw = payload.get("marketCapitalization")
    if raw is None:
        return _log_not_filed(
            symbol,
            "/stock/profile2 carries no marketCapitalization. A fund files no "
            "share count, and a symbol the vendor does not cover answers the "
            "same way -- both recorded as an empty object, HTTP 200",
        )

    # Before the number, because a number in the wrong currency is worse than
    # no number: it renders, it sorts, and it is wrong by an FX rate.
    #
    # An **absent** currency is read as USD, and that premise is stated rather
    # than demonstrated: all three recorded bodies that carry a market cap
    # carry a currency beside it, and the one body with no `currency` field --
    # the unknown symbol -- has no `marketCapitalization` either and returns
    # above. So "a figure with no currency" is unconstructable from anything
    # observed. The fallback is the safe direction anyway for the universe as
    # it stands, which is US listings only; the day a foreign listing appears
    # with a bare figure, this is the line that decided to trust it.
    currency = str(payload.get("currency") or "").strip().upper()
    if currency and currency != _USD:
        raise FundamentalsError(
            f"marketCapitalization is reported in {currency}, not {_USD}. "
            "This column is dollars and there is no FX rate in this process, "
            "so the figure is withheld rather than relabelled."
        )

    millions = _as_decimal(_vendor_number("marketCapitalization", raw))
    if millions is not None and not millions.is_finite():
        # ``Decimal("NaN")`` and ``Decimal("Infinity")`` both construct
        # happily out of a quoted vendor value, and the comparison below
        # would then raise ``InvalidOperation`` rather than answering.
        raise FundamentalsError(
            f"marketCapitalization is {millions}, which is not a finite number"
        )
    if millions is None or millions <= 0:
        # Zero is the vendor's other way of saying nothing. Serving it would
        # put a live company at the bottom of a dollar column and claim it is
        # worth nothing -- the fund error, one row lower down.
        return _log_not_filed(
            symbol, f"marketCapitalization is {millions}, which is not a value"
        )
    try:
        # Exact: ``MILLION`` is a ``Decimal``, so the product carries every
        # digit the vendor sent. ``MarketCap.reported`` then rounds it to
        # whole dollars -- Finnhub computes this in a double and emits up to
        # seventeen significant digits, which the API boundary's
        # ``money_serialization_lossy`` alarm would fire on every single
        # response. The rounding lives on the constructor rather than here so
        # a second fundamentals vendor cannot reintroduce the problem by
        # forgetting; see that docstring for why it is the right answer
        # semantically and not a way around the check.
        return MarketCap.reported(symbol, millions * MILLION)
    except ArithmeticError as exc:
        # A finite but absurd exponent -- ``1E+999999999`` is a legal JSON
        # number and a legal ``Decimal`` -- overflows the multiply or the
        # rounding to whole dollars. Both are trapped by the default context
        # and both are ``ArithmeticError``; neither is worth a 500 on a table
        # of prices.
        raise FundamentalsError(
            f"marketCapitalization {millions} million does not scale to a "
            f"whole-dollar figure: {type(exc).__name__}"
        ) from exc


def _vendor_number(field: str, raw: object) -> Any:
    """``raw`` if it is a shape a JSON number arrives in, refused otherwise.

    The guard that keeps a vendor type change out of
    :func:`corollary.wire.as_decimal`, and it runs *before* that function
    rather than catching after it, because the two failures it separates are
    not the same event and must not share a catch site:

    * **The vendor sent something that is not a number.** ``true``,
      ``[1, 2]``, ``{"value": 1}``, ``NaN``, ``Infinity``. Untrusted input in
      exactly the shape the decoder exists to reject. It becomes a
      :class:`FundamentalsError` naming the type, which the caller records as
      one symbol's ``UNAVAILABLE`` -- not a 500 on a table of prices.
    * **Our decoder was bypassed.** A *finite* ``float`` is unreachable
      through :func:`corollary.wire.decode_json`, which parses every JSON
      number with ``parse_float=Decimal``; getting one here means somebody
      called ``json.loads`` directly and put an IEEE double on the money
      path. That is a programming error, ``as_decimal`` raises ``TypeError``
      for it, :func:`corollary.wire.translating` deliberately does not
      translate it, and this function deliberately does not intercept it.

    ``NaN`` and ``Infinity`` are the line between the two, and they are why
    "a float means our bug" is not quite the whole rule: ``json.loads``
    accepts both as an extension, ``parse_float`` does not intercept either,
    so a non-finite float *can* come off the wire. Non-finite is the vendor;
    finite is us.
    """
    if isinstance(raw, bool):
        # Before the ``int`` branch: ``bool`` is a subclass of ``int``.
        raise FundamentalsError(
            f"{field} is a bool ({raw!r}), not a number. The vendor changed a "
            "field's type, or something in front of it rewrote the body."
        )
    if isinstance(raw, float):
        if math.isfinite(raw):
            return raw  # the tripwire's case; as_decimal raises TypeError
        raise FundamentalsError(
            f"{field} is {raw!r}. JSON has no such literal in the standard, "
            "and no arithmetic here can use it."
        )
    if isinstance(raw, (Decimal, int, str)):
        return raw
    raise FundamentalsError(
        f"{field} is a {type(raw).__name__}, not a number. The vendor changed "
        "a field's type, or something in front of it rewrote the body."
    )


def _log_unavailable(symbol: str, cause: str) -> MarketCap:
    """Record a failure as a failure. Rule 8: the rule, the inputs, the time.

    The warning level is the point. A fund with no market cap is not logged
    at this level at all, so a log reader can tell "nobody could ask" from
    "there is nothing to ask for" -- the distinction the wire's single
    ``null`` cannot carry.
    """
    logger.warning(
        "market cap unavailable for %s: %s",
        symbol,
        cause,
        extra={
            "event": "market_cap_unavailable",
            "rule": (
                "a market cap that could not be fetched is served as null and "
                "logged as a failure; a symbol that genuinely has none is "
                "served as the same null and is not"
            ),
            "symbol": symbol,
            "cause": cause,
            "vendor": FINNHUB_HOST,
        },
    )
    return MarketCap.unavailable(symbol, cause)


def _log_not_filed(symbol: str, note: str) -> MarketCap:
    """A legitimate absence. Debug, because it is not a fault."""
    logger.debug(
        "no market cap filed for %s: %s",
        symbol,
        note,
        extra={
            "event": "market_cap_not_filed",
            "symbol": symbol,
            "cause": note,
            "vendor": FINNHUB_HOST,
        },
    )
    return MarketCap.not_filed(symbol, note)


def _log_internal_fault(
    symbol: str, exc: BaseException, *, detail: str
) -> MarketCap:
    """A fault that is *ours*, recorded as ours, without taking prices down.

    The residual catch, and the two halves of its job pull in opposite
    directions. A display-only column may never 500 a table of prices, so
    this degrades rather than propagates. But the thing most likely to arrive
    here is ``as_decimal``'s ``TypeError`` tripwire -- "a float reached the
    Decimal boundary" -- and that tripwire is what protects *money is*
    ``Decimal``, *never* ``float`` at the vendor boundary. Swallowing it
    silently would destroy it.

    So the two stay distinguishable, at two levels and under two event names:
    a vendor's bad shape is ``market_cap_unavailable`` at WARNING with a
    stated cause, and this is ``market_cap_internal_fault`` at ERROR with the
    traceback attached and the word *bug* in the message. The loudness moved
    from the HTTP status into the log, which is the only place it can live
    once "a display column cannot fail the table" is an invariant.

    Nothing *should* reach here: every vendor shape is refused as a
    :class:`FundamentalsError` upstream, and every transport failure as an
    ``httpx.HTTPError`` translated into one. If something does, the line to
    read is the one in the traceback, not this one.

    ``detail`` is the cause text, and it is **required** rather than
    defaulting to ``str(exc)``: an exception's message is third-party text
    like any other, and rule 6 makes no exception for the path that reports
    a bug. The default was the hole -- two of the three residual catches took
    it, so the scrub covered the arm somebody remembered and no other. Every
    caller now goes through :meth:`FinnhubProvider._fault`, which holds the
    token and composes this. One honest
    residual: a formatter rendering ``exc_info`` re-derives the exception's
    own ``str``, which no call here can rewrite, so the scrub covers the
    fields this module composes and not the traceback. Withholding the
    traceback conditionally would take it away from exactly the faults that
    are hardest to diagnose, which is the worse trade.
    """
    cause = detail
    logger.error(
        "market cap for %s hit a fault in our own code: %s",
        symbol,
        cause,
        exc_info=exc if isinstance(exc, Exception) else None,
        extra={
            "event": "market_cap_internal_fault",
            "rule": (
                "a display-only column never fails the price table, so an "
                "unexpected exception degrades one cell -- but it is a bug, "
                "not a vendor outage, and is recorded at error level with "
                "its traceback so the two are never read as the same event"
            ),
            "symbol": symbol,
            "cause": cause,
            "vendor": FINNHUB_HOST,
        },
    )
    # The note carries the type and not the text: it is served to a caller
    # and stored in a cache, where "internal fault: RuntimeError" is the
    # whole of what a reader can act on. The text is in the log.
    return MarketCap.unavailable(symbol, f"internal fault: {type(exc).__name__}")
