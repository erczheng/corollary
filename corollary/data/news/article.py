"""The vendor-neutral news article every news provider returns.

Phase 3 design, decision 3: *"Every vendor's article lands in
``news_article``, keyed ``(vendor, vendor_id)``, so re-polling is
idempotent."* This module is the record that crosses from a provider file
into the rest of the engine. It imports nothing from
``corollary/data/providers/`` -- the providers import it -- so the pipeline
downstream of a poll never learns which vendor's wire shape an article came
from, only its ``vendor`` and ``feed`` as data.

Four things this record is deliberate about
-------------------------------------------

**The feed is provenance, and it is a closed set.** Decision 3: a vendor has
more than one feed after Q9, and *"Finnhub"* alone no longer says whether a
story came from the watch-tier poll or the market-wide one. :class:`NewsFeed`
carries exactly the four values the ``news_article.feed`` CHECK constraint
will carry, and :data:`FEED_VENDOR` pins which vendor each feed belongs to --
an article that claims ``vendor="massive"`` on the ``finnhub_market`` feed is
refused at construction rather than stored as a contradiction.

**No tickers means ``MARKET``.** ``tickers`` is empty for an article the
vendor tagged to nothing; the store writes that as the ``MARKET`` ticker
value (``types.ts``). It is not written as ``MARKET`` here, because whether a
tag survives is the ingest's call: decision 21 drops tags that are not active
US equities (crypto pairs among them) *at ingest*, and an article left with
nothing after that is ``MARKET`` too. A provider passes every tag through
untouched apart from case, so that decision is made once, in one place.

**The timestamp is aware UTC, always.** Refused naive at construction and
normalised to UTC otherwise -- CLAUDE.md: timestamps stored UTC.

**Massive's insights ride along, unlabelled.** Each is the vendor's own
per-ticker ``{ticker, sentiment, sentiment_reasoning}``, kept verbatim so
step 5's vendor tier can label from it. ``sentiment`` stays the vendor's word
(``positive | negative | neutral | mixed``, per the step-0 probe) rather than
becoming a direction here: ``mixed`` is neither directional value, and the
mapping that says so belongs to the labeller, not the transport.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final

__all__ = [
    "FEED_VENDOR",
    "NewsArticle",
    "NewsFeed",
    "NewsAccessDenied",
    "NewsProviderError",
    "VendorInsight",
]


class NewsFeed(StrEnum):
    """Which poll fetched an article. Exactly the ``news_article.feed`` CHECK set."""

    ALPACA_NEWS = "alpaca_news"
    FINNHUB_COMPANY = "finnhub_company"
    FINNHUB_MARKET = "finnhub_market"
    MASSIVE_NEWS = "massive_news"


#: The vendor each feed belongs to. ``vendor`` is half of the idempotency key
#: ``(vendor, vendor_id)``, so it must agree with the feed or two copies of
#: one article could land under two keys.
FEED_VENDOR: Final[dict[NewsFeed, str]] = {
    NewsFeed.ALPACA_NEWS: "alpaca",
    NewsFeed.FINNHUB_COMPANY: "finnhub",
    NewsFeed.FINNHUB_MARKET: "finnhub",
    NewsFeed.MASSIVE_NEWS: "massive",
}


class NewsProviderError(RuntimeError):
    """A news fetch failed: transport, status, or a body that is not the shape.

    Vendor-neutral so the poller catches one type per feed. The message has
    already been scrubbed of credentials by the provider that raised it.
    """


class NewsAccessDenied(NewsProviderError):
    """The vendor answered 401 or 403: the key, or the plan, refused the call.

    Separate from a transport failure because the remedy is different -- a
    retry never fixes it. Finnhub answers a premium endpoint with a JSON 403
    (step 0), so on that vendor this can mean "not on this plan" as well as
    "bad key".
    """


def _clean_ticker(raw: str) -> str:
    return raw.strip().upper()


@dataclass(frozen=True, slots=True)
class VendorInsight:
    """One vendor-supplied per-ticker sentiment, verbatim. Massive only today."""

    ticker: str
    sentiment: str
    reasoning: str | None

    def __post_init__(self) -> None:
        ticker = _clean_ticker(self.ticker)
        if not ticker:
            raise ValueError("an insight must name a ticker")
        object.__setattr__(self, "ticker", ticker)
        sentiment = self.sentiment.strip().lower()
        if not sentiment:
            raise ValueError(f"the insight for {ticker} carries no sentiment")
        object.__setattr__(self, "sentiment", sentiment)
        reasoning = (self.reasoning or "").strip()
        object.__setattr__(self, "reasoning", reasoning or None)


@dataclass(frozen=True, slots=True)
class NewsArticle:
    """One article as a provider fetched it. Construction validates and normalises.

    Raises ``ValueError`` on a contradiction: a vendor that does not own the
    feed, a blank id, headline or URL, or a naive timestamp. Providers catch
    that per row, log it and skip the row, so one malformed article never
    costs the rest of the batch.
    """

    vendor: str
    vendor_id: str
    feed: NewsFeed
    url: str
    headline: str
    summary: str | None
    publisher: str | None
    published_at: datetime
    #: Uppercase, de-duplicated, vendor order kept. Empty means ``MARKET``.
    tickers: tuple[str, ...] = ()
    #: Massive's per-ticker insights, unlabelled. Empty for every other feed.
    insights: tuple[VendorInsight, ...] = ()

    def __post_init__(self) -> None:
        feed = NewsFeed(self.feed)
        object.__setattr__(self, "feed", feed)
        expected = FEED_VENDOR[feed]
        if self.vendor != expected:
            raise ValueError(
                f"feed {feed.value} belongs to vendor {expected!r}, not {self.vendor!r}"
            )
        for name in ("vendor_id", "url", "headline"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"a news article needs a non-blank {name}")
            object.__setattr__(self, name, value.strip())
        for name in ("summary", "publisher"):
            value = getattr(self, name)
            object.__setattr__(self, name, (value or "").strip() or None)
        moment = self.published_at
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError(
                f"published_at must be timezone-aware; got a naive {moment!r}"
            )
        object.__setattr__(self, "published_at", moment.astimezone(timezone.utc))
        cleaned = (_clean_ticker(t) for t in self.tickers)
        object.__setattr__(
            self, "tickers", tuple(dict.fromkeys(t for t in cleaned if t))
        )
        object.__setattr__(self, "insights", tuple(self.insights))

    @property
    def is_market(self) -> bool:
        """Tagged to no ticker by the vendor -- a ``MARKET`` item."""
        return not self.tickers
