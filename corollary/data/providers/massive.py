"""Massive over REST -- the vendor-scored news feed, untickered.

Phase 3 design, decision 21's discovery tier: *"Massive
``/v2/reference/news``, untickered, ``limit=1000``, every 15 minutes."* And
*Feeds and budgets*: *"from the last ``published_utc`` seen"*. One call holds
up to 5.3 days of articles (step 0: 1,000 articles, 1,246 distinct tickers),
so a 15-minute cadence cannot leave a gap on its own account.

**This is the entire Massive surface area.** Everything else receives the
vendor-neutral :class:`~corollary.data.news.article.NewsArticle`; each
article's ``insights[]`` -- Massive's own per-ticker ``{ticker, sentiment,
sentiment_reasoning}`` -- rides along verbatim for step 5's vendor tier and
is not interpreted here.

The request
-----------

``order=asc&sort=published_utc&published_utc.gt=<cursor>&limit=1000``, then
``next_url`` until it runs out or :data:`MASSIVE_MAX_PAGES` is reached.
**Ascending is the point**: everything read is older than everything not yet
read, so a stop at the page cap leaves no hole in the middle. Descending
would read the newest page first and a stop would leave a hole that no
cursor describes. Verified against the key on 2026-09-24: ``asc`` and
``published_utc.gt`` are accepted, and the ``next_url`` of an ascending
query pages forward and carries no key.

**Where an incomplete call's cursor sits.** Not *at* the newest article
read, T, but one second below it (never below where the call started).
Timestamps are often whole minutes -- four articles shared ``12:55:00Z`` on
the probe's first page -- so the unread page after the cap can open with
more articles stamped T, and ``.gt=T`` would never return them. From
``T - 1s`` the next call re-reads the articles at T it already holds, which
is harmless: the store keys on ``(vendor, vendor_id)``. A *complete* call's
cursor is T itself, because nothing was left pending. (The degenerate case
-- more than ``MASSIVE_MAX_PAGES`` pages all stamped the second after the
start -- makes no progress rather than skipping; at 1,000 rows a page that
is 3,000 articles in one second. So does a capped call on which every row
failed to decode, since the cursor never moves below where it started.)

Two things a ``.gt`` cursor still cannot see, stated rather than hidden --
the poller that owns the cursor decides what to do about them (see the
report for unit 4P):

* **Late ties.** An article stamped with a complete call's cursor second
  that arrives *after* that call is skipped by ``.gt``.
* **Late arrivals.** The docs say news is *"updated hourly"*, so an article
  can appear with a ``published_utc`` older than the cursor. A caller that
  passes ``cursor - overlap`` re-reads that window; the store's
  ``(vendor, vendor_id)`` key makes the re-read idempotent.

The key
-------

``MASSIVE_API_KEY``, sent as ``Authorization: Bearer``. Massive also accepts
``?apiKey=``, and that form is not used: a query string is what turns up in
access logs and in ``httpx`` exception messages, and rule 6 covers log
output. Should a ``next_url`` ever carry ``apiKey``, the parameter is
stripped before the URL is followed (the header authenticates), and every
message this module composes is scrubbed of the literal key and of any
``apiKey=`` value -- a key this process was never given included.

A ``next_url`` on any other origin than the configured base URL -- another
scheme, host or port, or one carrying userinfo -- is refused outright:
following it would send the bearer token to whoever the URL names.

A missing key leaves the feed **unavailable**, never the app unbootable:
:meth:`MassiveProvider.available_from_env` returns ``None`` and logs why.

The budget
----------

5 requests a minute (PRD section 9's probe), metered by the **shared**
:class:`~corollary.ratelimit.HostRateLimiter` against ``api.massive.com``.
Four calls an hour is 1.3% of it.
"""

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

import httpx

from corollary.data.news.article import (
    NewsAccessDenied,
    NewsArticle,
    NewsFeed,
    NewsProviderError,
    VendorInsight,
)
from corollary.ratelimit import MASSIVE_HOST, HostRateLimiter, default_limiter
from corollary.wire import (
    WireFormatError,
    as_datetime,
    clean_params,
    decode_json,
    require_aware,
    vendor_detail,
)

__all__ = [
    "MASSIVE_API_KEY_ENV",
    "MASSIVE_BASE_URL",
    "MASSIVE_MAX_PAGES",
    "MASSIVE_NEWS_PATH",
    "MASSIVE_PAGE_LIMIT",
    "MassiveCredentials",
    "MassiveCredentialsError",
    "MassiveNews",
    "MassiveProvider",
]

logger = logging.getLogger(__name__)

#: The REST root. ``api.massive.com`` is the host the rate limiter meters.
MASSIVE_BASE_URL: Final = "https://api.massive.com"

MASSIVE_NEWS_PATH: Final = "/v2/reference/news"

#: Already present in ``.env.example``.
MASSIVE_API_KEY_ENV: Final = "MASSIVE_API_KEY"

#: The vendor's maximum page size; one page covered 5.3 days in step 0.
MASSIVE_PAGE_LIMIT: Final = 1000

#: Pages per call before stopping with ``complete=False``. Only a cold start
#: or a long outage needs more than one page, and each page is a fifth of the
#: minute's budget; the ascending order makes stopping early safe.
MASSIVE_MAX_PAGES: Final = 3

#: ``apiKey=<value>`` in any text, whoever's key it is. Scrubbed from every
#: message this module composes.
_API_KEY_PARAM: Final = re.compile(r"(?i)api_?key=[^&\s\"'<>]+")


class MassiveCredentialsError(RuntimeError):
    """No Massive key in the environment. The feed is optional; see
    :meth:`MassiveProvider.available_from_env`."""


@dataclass(frozen=True, slots=True)
class MassiveCredentials:
    """The API key. ``__repr__`` hides it, for the reason Finnhub's does."""

    token: str

    def __repr__(self) -> str:
        return "MassiveCredentials(token=<hidden>)"

    __str__ = __repr__

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MassiveCredentials":
        import os

        source: Mapping[str, str] = os.environ if env is None else env
        token = (source.get(MASSIVE_API_KEY_ENV) or "").strip()
        if not token:
            raise MassiveCredentialsError(
                f"{MASSIVE_API_KEY_ENV} is not set in the environment. It is the "
                "vendor-scored news feed's only credential; the name is in "
                ".env.example and the value never appears in code, fixtures or logs."
            )
        return cls(token=token)


@dataclass(frozen=True, slots=True)
class MassiveNews:
    """One call's articles and the cursor for the next."""

    #: Newest first, then by ``vendor_id``; unique by ``vendor_id``.
    articles: tuple[NewsArticle, ...]
    #: What to pass as the next call's ``published_after``. A complete call:
    #: the latest ``published_utc`` read, or the value passed in when nothing
    #: was. An incomplete call: one second below the latest read (truncated
    #: to the second), never below the value passed in -- so articles sharing
    #: that second on the unread page are not lost to ``.gt``.
    cursor: datetime
    #: ``False`` when the page cap stopped the call with a ``next_url``
    #: still pending -- the next call resumes from ``cursor``, re-reading the
    #: newest second it already holds.
    complete: bool
    pages: int
    #: Rows refused as malformed, each logged as ``massive_news_row_skipped``.
    skipped: int


class MassiveProvider:
    """Massive's news feed with the shared per-host budget.

    Construct with :meth:`available_from_env` in production. The explicit
    constructor exists so tests inject an ``httpx.MockTransport`` client and
    make **no live calls at all**.
    """

    def __init__(
        self,
        *,
        credentials: MassiveCredentials,
        client: httpx.AsyncClient | None = None,
        limiter: HostRateLimiter | None = None,
        base_url: str = MASSIVE_BASE_URL,
        max_pages: int = MASSIVE_MAX_PAGES,
    ) -> None:
        if max_pages < 1:
            raise ValueError(f"max_pages must be at least 1, got {max_pages}")
        self._credentials = credentials
        self._client = client if client is not None else httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None
        self._limiter = limiter if limiter is not None else default_limiter()
        self._base_url = base_url.rstrip("/")
        self._base = httpx.URL(self._base_url)
        self._max_pages = max_pages

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, **kwargs: Any
    ) -> "MassiveProvider":
        """Build from the process environment; raises if the key is absent."""
        return cls(credentials=MassiveCredentials.from_env(env), **kwargs)

    @classmethod
    def available_from_env(
        cls, env: Mapping[str, str] | None = None, **kwargs: Any
    ) -> "MassiveProvider | None":
        """Build from the environment, or ``None`` -- logged -- with no key.

        The boot path's constructor: a missing optional feed must never stop
        the app starting.
        """
        try:
            return cls.from_env(env, **kwargs)
        except MassiveCredentialsError:
            logger.warning(
                "massive news unavailable: %s is not set",
                MASSIVE_API_KEY_ENV,
                extra={
                    "event": "massive_unavailable",
                    "rule": "a missing optional news key disables that feed and never the app",
                    "variable": MASSIVE_API_KEY_ENV,
                    "vendor": MASSIVE_HOST,
                },
            )
            return None

    @property
    def limiter(self) -> HostRateLimiter:
        return self._limiter

    async def aclose(self) -> None:
        """Close the transport, but only if we opened it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "MassiveProvider":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- HTTP

    def _scrub(self, text: str) -> str:
        """Third-party text, bounded and de-identified. Every quotation goes here."""
        return vendor_detail(
            text, secrets=(self._credentials.token,), patterns=(_API_KEY_PARAM,)
        )

    def _follow(self, next_url: object) -> httpx.URL:
        """The ``next_url`` to request, keyless, or a refusal.

        Refused unless it is on the configured origin exactly -- scheme,
        host and port, with no userinfo -- because the bearer header goes
        wherever this URL points. Same host on another port is another
        server, and userinfo would re-author the request's credentials.
        ``httpx`` normalises a scheme's default port to ``None``, so an
        explicit ``:443`` on https compares equal to the configured base.
        """
        if not isinstance(next_url, str) or not next_url.strip():
            raise NewsProviderError(f"next_url is {type(next_url).__name__}, not a URL")
        try:
            url = httpx.URL(next_url.strip())
        except httpx.InvalidURL as exc:
            raise NewsProviderError(
                f"next_url is not a URL: {self._scrub(next_url)}"
            ) from exc
        if (
            url.scheme != self._base.scheme
            or url.host != self._base.host
            or url.port != self._base.port
            or url.userinfo
        ):
            port = f":{url.port}" if url.port is not None else ""
            userinfo = "userinfo@" if url.userinfo else ""
            raise NewsProviderError(
                f"next_url points at {url.scheme}://{userinfo}"
                f"{self._scrub(url.host)}{port}, not the configured origin "
                f"{self._base.scheme}://{self._base.host}; refusing to send the "
                "key there"
            )
        params = [(k, v) for k, v in url.params.multi_items() if k.lower() not in ("apikey", "api_key")]
        return url.copy_with(params=params)

    async def _get(self, url: httpx.URL | str, params: Mapping[str, Any] | None) -> Any:
        """One GET against the ``api.massive.com`` bucket: a JSON object or an error."""
        await self._limiter.acquire(MASSIVE_HOST)
        try:
            response = await self._client.get(
                url,
                params=clean_params(params) if params is not None else None,
                headers=self._credentials.headers(),
            )
        except httpx.HTTPError as exc:
            raise NewsProviderError(
                f"GET {MASSIVE_NEWS_PATH} failed: {self._scrub(str(exc))}"
            ) from exc
        # The body is scrubbed only where it is about to be quoted -- the
        # error paths below -- never on an ordinary 200.
        if response.status_code in (401, 403):
            raise NewsAccessDenied(
                f"GET {MASSIVE_NEWS_PATH} returned {response.status_code}: "
                f"{self._scrub(response.text)}. "
                f"The key was not accepted -- check {MASSIVE_API_KEY_ENV}, not the code."
            )
        if response.status_code == 429:
            raise NewsProviderError(
                f"GET {MASSIVE_NEWS_PATH} returned 429 despite the local budget of "
                "5/min. Another process may be sharing this key."
            )
        if response.status_code >= 400:
            raise NewsProviderError(
                f"GET {MASSIVE_NEWS_PATH} returned {response.status_code}: "
                f"{self._scrub(response.text)}"
            )
        try:
            payload = decode_json(response.text)
        except WireFormatError as exc:
            raise NewsProviderError(
                f"GET {MASSIVE_NEWS_PATH}: {self._scrub(str(exc))}"
            ) from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
            raise NewsProviderError(
                f"GET {MASSIVE_NEWS_PATH} answered without a results list: "
                f"{self._scrub(response.text)}"
            )
        return payload

    # ----------------------------------------------------------------- news

    async def news_since(self, published_after: datetime) -> MassiveNews:
        """Every article published strictly after ``published_after``, ascending.

        ``published_after`` must be timezone-aware; it is sent to the second,
        in UTC. There is deliberately no default: an ascending query with no
        lower bound starts two years back, at the start of the plan's history.
        Any aware instant is accepted, including one earlier than the last
        cursor -- the poller passes ``cursor - overlap`` to catch late
        arrivals.

        The returned :attr:`MassiveNews.cursor` is the next call's argument;
        see its field comment for why an incomplete call's cursor sits one
        second below the newest article read.

        Raises :class:`NewsProviderError` on any failed page, including a
        later one -- a partial read is not returned as if it were whole.
        """
        require_aware(published_after, "published_after")
        start = published_after.astimezone(timezone.utc)
        params: Mapping[str, Any] | None = {
            "limit": MASSIVE_PAGE_LIMIT,
            "order": "asc",
            "sort": "published_utc",
            "published_utc.gt": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        url: httpx.URL | str = f"{self._base_url}{MASSIVE_NEWS_PATH}"

        articles: dict[str, NewsArticle] = {}
        skipped = 0
        pages = 0
        pending: object = None
        while True:
            payload = await self._get(url, params)
            pages += 1
            for row in payload["results"]:
                article = _news_article(row, self._scrub)
                if article is None:
                    skipped += 1
                    continue
                articles.setdefault(article.vendor_id, article)
            pending = payload.get("next_url")
            if not pending:
                break
            if pages >= self._max_pages:
                _log_page_cap(pages, len(articles))
                break
            url = self._follow(pending)
            params = None

        complete = not pending
        newest = max((a.published_at for a in articles.values()), default=start)
        if not complete:
            # A tie at the newest second may continue on the unread page;
            # ``.gt=newest`` would never return it. Back off one whole second
            # (the request is sent to the second) and re-read -- idempotent
            # downstream. Never below ``start``: that would move backwards.
            newest = newest.replace(microsecond=0) - timedelta(seconds=1)
        return MassiveNews(
            articles=_newest_first(articles.values()),
            cursor=max(newest, start),
            complete=complete,
            pages=pages,
            skipped=skipped,
        )


def _newest_first(articles: Iterable[NewsArticle]) -> tuple[NewsArticle, ...]:
    """Newest first, ties by ``vendor_id``: the same body, the same order."""
    return tuple(sorted(articles, key=lambda a: (-a.published_at.timestamp(), a.vendor_id)))


def _text(row: Mapping[str, Any], name: str, *, required: bool) -> str | None:
    value = row.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} is a {type(value).__name__}, not a string")
    return value


Scrub = Callable[[str], str]


def _news_article(row: Any, scrub: Scrub) -> NewsArticle | None:
    """One Massive result as a :class:`NewsArticle`, or ``None`` -- logged -- if malformed.

    ``scrub`` is the provider's redactor. The skip log quotes vendor text (the
    row's id, and a bad timestamp inside the cause), so both go through it:
    bounded, and with the key and any ``apiKey=`` blanked.
    """
    raw_id = row.get("id") if isinstance(row, Mapping) else None
    try:
        if not isinstance(row, Mapping):
            raise ValueError(f"row is a {type(row).__name__}, not an object")
        vendor_id = _text(row, "id", required=True)
        headline = _text(row, "title", required=True)
        url = _text(row, "article_url", required=True)
        summary = _text(row, "description", required=False)
        publisher_obj = row.get("publisher")
        publisher = (
            _text(publisher_obj, "name", required=False)
            if isinstance(publisher_obj, Mapping)
            else None
        )
        try:
            published_at = as_datetime(row.get("published_utc"))
        except (WireFormatError, OverflowError) as exc:
            # OverflowError: an extreme offset (``0001-01-01T00:30:00+01:00``)
            # overflows the UTC conversion. It is neither a ValueError nor a
            # WireFormatError, and escaping here would fail the whole call on
            # every poll from the same cursor -- a permanent silent outage.
            raise ValueError(f"published_utc: {exc}") from exc
        raw_tickers = row.get("tickers") or []
        if not isinstance(raw_tickers, list) or not all(
            isinstance(t, str) for t in raw_tickers
        ):
            raise ValueError("tickers is not a list of strings")
        return NewsArticle(
            vendor="massive",
            vendor_id=vendor_id or "",
            feed=NewsFeed.MASSIVE_NEWS,
            url=url or "",
            headline=headline or "",
            summary=summary,
            publisher=publisher,
            published_at=published_at,
            tickers=tuple(raw_tickers),
            insights=_insights(vendor_id or "", row.get("insights"), scrub),
        )
    except ValueError as exc:
        quoted_id = scrub(repr(raw_id))
        cause = scrub(str(exc))
        logger.warning(
            "massive news row skipped (id %s): %s",
            quoted_id,
            cause,
            extra={
                "event": "massive_news_row_skipped",
                "rule": "a news row that cannot be read is skipped and logged, never stored half-read",
                "feed": NewsFeed.MASSIVE_NEWS.value,
                "vendor_id": quoted_id,
                "cause": cause,
                "vendor": MASSIVE_HOST,
            },
        )
        return None


def _insights(vendor_id: str, raw: Any, scrub: Scrub) -> tuple[VendorInsight, ...]:
    """The row's insights; a malformed one is dropped and logged, the rest kept.

    Dropped rather than failing the article: one bad insight must not cost
    the article, or its other tickers, their labels.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        _log_insight_skipped(
            scrub(vendor_id), f"insights is a {type(raw).__name__}, not a list"
        )
        return ()
    kept: list[VendorInsight] = []
    for item in raw:
        try:
            if not isinstance(item, Mapping):
                raise ValueError(f"insight is a {type(item).__name__}, not an object")
            ticker = _text(item, "ticker", required=True)
            sentiment = _text(item, "sentiment", required=True)
            reasoning = _text(item, "sentiment_reasoning", required=False)
            kept.append(
                VendorInsight(ticker=ticker or "", sentiment=sentiment or "", reasoning=reasoning)
            )
        except ValueError as exc:
            _log_insight_skipped(scrub(vendor_id), scrub(str(exc)))
    return tuple(kept)


def _log_insight_skipped(vendor_id: str, cause: str) -> None:
    """Both arguments arrive already scrubbed by the caller."""
    logger.warning(
        "massive insight skipped on %s: %s",
        vendor_id,
        cause,
        extra={
            "event": "massive_insight_skipped",
            "rule": "a malformed insight is dropped and logged; the article and its other insights are kept",
            "vendor_id": vendor_id,
            "cause": cause,
            "vendor": MASSIVE_HOST,
        },
    )


def _log_page_cap(pages: int, articles: int) -> None:
    logger.warning(
        "massive news stopped at the %d-page cap with more pending; the next "
        "call resumes one second below the newest article read",
        pages,
        extra={
            "event": "massive_news_page_cap",
            "rule": (
                "a call reads at most MASSIVE_MAX_PAGES pages; ascending order "
                "leaves no hole, and the cursor backs off one second so a tie "
                "at the newest second is re-read rather than skipped"
            ),
            "pages": pages,
            "articles": articles,
            "vendor": MASSIVE_HOST,
        },
    )
