"""The article store: normalise, dedupe, upsert (Phase 3 decision 3, decision 21).

This is the storage half of news ingest. The pollers that fetch articles from
the vendors call :func:`store_articles` with what they fetched; nothing here
talks to a vendor.

Five pieces:

* :func:`url_key` and :func:`headline_key` -- the two normalisers whose output
  is stored in ``news_article.url_key`` / ``headline_key``. A stored key must
  be exactly what these return for the row's ``url`` / ``headline``; changing
  either function means re-deriving every stored key.
* :func:`filter_tags` -- decision 21: *"Tags that are not active US equities in
  Alpaca's asset list (crypto pairs, for one) are dropped at ingest. An article
  left with no tag at all is ``MARKET``."*
* :func:`store_articles` -- the upsert. Idempotent on ``(vendor, vendor_id)``,
  merges tags on a re-seen article, and links a new cross-vendor copy to its
  group's canonical row.
* :func:`relink_stored` -- R2: the same matcher, run once over rows already
  stored, for duplicates that arrived before a matcher fix
  (``scripts/relink_articles.py``).

Decisions this module makes, stated so they can be disagreed with
------------------------------------------------------------------

**Dedupe links across vendors only.** Decision 3 is about *cross-vendor*
duplicates, and a vendor that assigns two ids is asserting two stories. That
is not academic: ``tests/fixtures/alpaca/p4_news_untickered_page2.json``
carries two different Benzinga PMI briefs (Manufacturing and Services) filed
under one URL, ``https://www.benzinga.com/quote/SPY``, and Benzinga's
trading-halt notices repeat one headline verbatim across symbols. Linking
same-vendor rows would hide real stories from the feed. The rule is applied at
group level: a new article never joins a group that already holds a row from
its own vendor.

**A URL identifies a story only sometimes.** The same-vendor rule does nothing
once a *second* vendor serves a generic URL: two Benzinga briefs on
``/quote/SPY`` two days apart, then a Massive copy of the second, would join
the first by URL (the oldest group wins) before the headline was ever looked
at. So decision 3's "normalised URL" is read with three implementation bounds,
and when any one of them rules the URL out the headline check decides alone:

* a generic *shape* -- a bare host, or ``/quote/<SYMBOL>`` -- never identifies
  a story (:func:`_is_generic_url_shape`);
* a URL that already spans more than one canonical group, or on which one
  vendor already holds two ids, is a page carrying several stories and no
  longer identifies any of them (:func:`_url_not_identifying`);
* a URL match counts only within :data:`URL_MATCH_WINDOW` (+/-48 hours,
  inclusive) of the stored row's ``published_at``; a real cross-vendor copy
  is published near its original.

Each bound can only turn a URL link into a headline check, so the cost of
being wrong is a missed link, never a wrong one.

**One measured publisher pair gets a skewed headline window.** Finnhub serves
Motley Fool stories syndicated through Yahoo 20 minutes after the time fool.com
itself states, which Massive reproduces exactly. That puts the pair outside
decision 3's +/-10 minutes, so every such story showed twice. Finnhub labels
the row only ``"Yahoo"``, the same label it gives Zacks and GlobeNewswire
copies, which it stamps correctly, so its decoder has nothing to key a
correction on. The allowance therefore lives here: for that exact pair, the
headline match also looks +/-10 minutes around the shifted stamp. See
:data:`SYNDICATION_SKEWS` for the evidence. No stamp is rewritten.

**Link, then never re-link.** ``canonical_id`` is decided once, when a row is
inserted. A later edit (a new headline or URL on a re-poll) updates the row's
keys, so *future* articles match against the new text, but never re-points
the row or its group -- re-pointing is how a chain or an orphan gets made.
The one exception is :func:`relink_stored`, and it only fills a NULL: it links
an unlinked row to a row stored before it, never a row other rows point at,
and never touches a row that is already linked.

**No chains, enforced here.** The schema cannot say "a canonical row is itself
canonical" (SQLite admits no subquery in a CHECK). Every match is resolved to
its group's canonical row before linking, so a new row always names a row
whose ``canonical_id`` is NULL. A resolved row that is *not* canonical means
the invariant was already broken elsewhere; that group is skipped and logged
at ERROR rather than extended.

**Deterministic choice.** Several matching groups resolve to the canonical row
with the oldest ``published_at``, then the lowest ``id``. A batch is stored in
``(published_at, vendor, vendor_id, ...)`` order, so the same batch in any
order stores the same links.

**An edited article updates headline, URL and summary.** Alpaca re-serves an
article under the same id with ``updated_at`` moved when Benzinga edits it; the
row carries the vendor's latest text, and ``headline_key`` / ``url_key`` are
recomputed with it. A re-served copy whose summary is empty does not erase a
stored summary. ``publisher``, ``published_at``, ``feed`` (provenance of the
*first* fetch) and ``ingested_at`` are fixed at first sight.

**Tags merge, they are never replaced.** Finnhub ``/company-news`` returns one
article, same id and URL, under NVDA and again under TSLA; the second sighting
adds TSLA. ``MARKET`` means *no tag*, so an article stored as ``MARKET`` that
later gains a real tag loses ``MARKET``; a later copy with fewer tags removes
nothing.

**When the asset directory is unavailable** (the day's fetch failed), dropping
every tag would file the whole market under ``MARKET``. Instead a tag is kept
when it is shaped like an equity symbol (:data:`~corollary.data.seeds.EQUITY_SYMBOL_RE`),
which still drops ``BTC/USD`` but keeps ``BTCUSD``; each call says so at
WARNING and :attr:`IngestResult.degraded_tag_filter` reports it.

Blocking: these are synchronous ``Session`` functions and do not commit. A
caller on the event loop runs them in ``asyncio.to_thread`` with its own
session, as ``data/macro/risk_free.py`` does.
"""

import logging
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final, TypeVar
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from sqlalchemy import Select, delete, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from corollary.data.news.article import NewsArticle, NewsFeed
from corollary.data.providers.interface import AssetDirectory
from corollary.data.seeds import EQUITY_SYMBOL_RE, normalize_symbol
from corollary.db.models import NewsArticle as ArticleRow
from corollary.db.models import NewsArticleTicker
from corollary.wire import require_aware

__all__ = [
    "HEADLINE_MATCH_WINDOW",
    "MARKET_TICKER",
    "SYNDICATION_SKEWS",
    "URL_MATCH_WINDOW",
    "IngestResult",
    "Relink",
    "RelinkResult",
    "StoredArticle",
    "SyndicationSide",
    "SyndicationSkew",
    "filter_tags",
    "headline_key",
    "relink_stored",
    "store_articles",
    "url_key",
]

logger = logging.getLogger(__name__)

#: The ticker value an article tagged to no active equity is stored under
#: (``types.ts``'s ``MARKET``).
MARKET_TICKER: Final = "MARKET"

#: Decision 3: the headline fallback matches within +/-10 minutes, inclusive.
HEADLINE_MATCH_WINDOW: Final = timedelta(minutes=10)


@dataclass(frozen=True)
class SyndicationSide:
    """One side of a :class:`SyndicationSkew`: a feed and its exact publisher text."""

    vendor: str
    feed: str
    publisher: str


@dataclass(frozen=True)
class SyndicationSkew:
    """A measured, constant lag between two vendors' copies of one publisher's stories.

    ``late`` stamps the story ``offset`` after ``early`` does. When an incoming
    article is exactly one side, the headline fallback *also* matches the
    other side within :data:`HEADLINE_MATCH_WINDOW` of the stamp shifted by
    ``offset``, in the measured direction only. The ordinary window still
    applies alongside it, so the pair keeps linking if the lag is ever fixed
    upstream. Publisher text is compared exactly: variants that were never
    observed are not guessed at.
    """

    late: SyndicationSide
    early: SyndicationSide
    offset: timedelta


#: Measured 2026-09-25 -> 2026-10-09 (phase-3 step 5 re-measurement, two weeks
#: of real news): 575 headline twins of Finnhub ``/company-news`` rows with
#: ``source`` ``"Yahoo"`` and Massive rows with publisher ``"The Motley Fool"``
#: sat **exactly 1200 s apart, Finnhub later**. Four more pairs were recycled
#: generic headlines matched to a different day's article, so not
#: counter-examples. On 12 of 12 sampled pairs, fool.com's own
#: ``article:published_time`` and JSON-LD ``datePublished`` equal Massive's
#: stamp to the second, and Finnhub's ``url`` redirects to that same fool.com
#: page. So **Finnhub is 20 minutes late and Massive is right.**
#:
#: Why this is not a decoder correction like ``correct_publisher_clock``:
#: Finnhub gives the Fool rows the same ``source`` as its Zacks (466 twins) and
#: GlobeNewswire (28 twins) Yahoo rows, which match Massive at offset 0. The
#: image is a generic Yahoo placeholder and the url an opaque redirect id, so
#: nothing in the payload separates them. Shifting every ``"Yahoo"`` row would
#: break those 494 correct pairs. Only the counterpart's publisher identifies
#: the story, and that is known only here, at match time.
#:
#: Residuals, accepted: a Finnhub Fool row with no Massive twin keeps its late
#: stamp, which is the no-look-ahead direction. And if the Finnhub copy is
#: stored first it stays canonical (links are never re-rooted), so the group
#: shows the stamp 20 minutes late.
#:
#: Scoped to ``finnhub_company``. The general feed was not measured.
SYNDICATION_SKEWS: Final[tuple[SyndicationSkew, ...]] = (
    SyndicationSkew(
        late=SyndicationSide(vendor="finnhub", feed=NewsFeed.FINNHUB_COMPANY.value, publisher="Yahoo"),
        early=SyndicationSide(
            vendor="massive", feed=NewsFeed.MASSIVE_NEWS.value, publisher="The Motley Fool"
        ),
        offset=timedelta(minutes=20),
    ),
)

#: An implementation bound on decision 3's "normalised URL": a URL match
#: counts only between rows published within +/-48 hours of each other,
#: inclusive. A real cross-vendor copy is published near its original; a URL
#: reused days later is a page (a quote page, a section front), not a story.
URL_MATCH_WINDOW: Final = timedelta(hours=48)

#: Query parameters that identify a click, not a story. Compared casefolded.
#: Deliberately short: an unknown parameter is kept, because dropping one that
#: identifies the story (Finnhub's ``?id=``) would merge different articles.
_TRACKING_PARAMS: Final = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "mkt_tok",
        "cmpid",
        "ncid",
    }
)
_TRACKING_PREFIXES: Final = ("utm_",)

#: Characters stripped from a headline beyond Unicode's punctuation categories.
#: The backtick and acute accent are "modifier symbols" to Unicode but stand
#: in for an apostrophe in vendor text.
_EXTRA_HEADLINE_PUNCTUATION: Final = frozenset({"`", "´"})

#: How many dropped tags one log line quotes, and how long each may be. Tags
#: are vendor text; a bound keeps one odd article from flooding the log.
_LOG_TAG_LIMIT: Final = 20
_LOG_TAG_WIDTH: Final = 32
#: How much of a URL key one log line quotes.
_LOG_URL_WIDTH: Final = 200

#: A ``/quote/<SYMBOL>`` path, as :func:`url_key` leaves it (no trailing
#: slash, case kept). A page about a symbol, not a story; see
#: :func:`_is_generic_url_shape`.
_QUOTE_PAGE_PATH_RE: Final = re.compile(r"/quote/[A-Za-z0-9.\-]{1,12}", re.IGNORECASE)


# ---------------------------------------------------------------- normalisers


def _is_tracking(name: str) -> bool:
    folded = name.casefold()
    return folded in _TRACKING_PARAMS or folded.startswith(_TRACKING_PREFIXES)


def url_key(url: str) -> str:
    """The URL as dedupe compares it. Stored in ``news_article.url_key``.

    * scheme and host lowercased, ``http`` folded into ``https``;
    * a leading ``www.`` and a default port (80, 443) dropped;
    * the fragment dropped;
    * tracking parameters (``utm_*``, ``fbclid``, ``gclid`` ...) dropped, the
      rest kept and sorted -- Finnhub's ``/api/news?id=<hash>`` *is* its story;
    * trailing slashes dropped from the path; the path's case is kept.

    Text that does not parse as an absolute URL is returned trimmed and
    otherwise untouched. Idempotent: ``url_key(url_key(u)) == url_key(u)``.
    """
    text = url.strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return text
    host = parts.hostname
    if not parts.scheme or not host:
        return text
    scheme = parts.scheme.lower()
    if scheme == "http":
        scheme = "https"
    host = host.lower()
    if host.startswith("www."):
        host = host[len("www.") :]
    if port is not None and port not in (80, 443):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/")
    pairs = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(name)
    ]
    pairs.sort()
    key = f"{scheme}://{host}{path}"
    if pairs:
        key = f"{key}?{urlencode(pairs, quote_via=quote)}"
    return key


def headline_key(headline: str) -> str:
    """The headline as dedupe compares it. Stored in ``news_article.headline_key``.

    NFKC-normalised and casefolded; every punctuation character (Unicode
    category ``P*``, which covers straight and curly quotes, dashes, colons)
    becomes a space; whitespace collapses to single spaces and is trimmed.
    Symbols and digits stay: ``$120`` and ``5`` carry meaning. A headline of
    nothing but punctuation keys to ``""``, which never matches anything.
    """
    text = unicodedata.normalize("NFKC", headline).casefold()
    spaced = "".join(
        " "
        if unicodedata.category(char).startswith("P") or char in _EXTRA_HEADLINE_PUNCTUATION
        else char
        for char in text
    )
    return " ".join(spaced.split())


# ---------------------------------------------------------------- tags


def filter_tags(
    tags: Iterable[str], assets: AssetDirectory | None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split vendor tags into ``(kept, dropped)``. Decision 21.

    With a directory, a tag is kept when it is an active US equity in it, and
    kept in the directory's spelling (``BRK/B`` -> ``BRK.B``). Without one --
    the daily fetch failed -- a tag is kept when it is shaped like an equity
    symbol. A vendor's own ``MARKET`` tag is always dropped: ``MARKET`` is the
    store's word for *no tag*, not a symbol. ``kept`` is de-duplicated in
    vendor order; ``dropped`` carries the vendor's text as given.
    """
    kept: dict[str, None] = {}
    dropped: list[str] = []
    for raw in tags:
        symbol = normalize_symbol(raw)
        keep: str | None = None
        if symbol and symbol != MARKET_TICKER:
            if assets is not None:
                asset = assets.get(symbol)
                if asset is not None:
                    keep = normalize_symbol(asset.symbol)
            elif EQUITY_SYMBOL_RE.fullmatch(symbol):
                keep = symbol
        if keep is None:
            dropped.append(raw)
        else:
            kept[keep] = None
    return tuple(kept), tuple(dropped)


# ---------------------------------------------------------------- store


@dataclass(frozen=True, slots=True)
class StoredArticle:
    """One article as stored: its ``news_article.id`` and the article as fetched.

    The fetched article, not the row, because Massive's ``insights`` are not
    persisted -- the labeller reads them from here, at store time, or never.
    """

    article_id: int
    article: NewsArticle


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What one :func:`store_articles` call did.

    ``inserted + updated + unchanged`` is the number of articles passed in:
    every article is a new row, a re-seen row that changed (text or tags), or
    a re-seen row that did not.
    """

    #: New rows written.
    inserted: int
    #: Re-seen rows whose headline, URL, summary or tags changed.
    updated: int
    #: Re-seen rows where nothing changed -- an idempotent re-poll.
    unchanged: int
    #: Of ``inserted``, the rows linked to an existing canonical row.
    duplicates_linked: int
    #: ``news_article_ticker`` rows written, ``MARKET`` included.
    tags_added: int
    #: Vendor tags discarded as not active US equities.
    tags_dropped: int
    #: True when no asset directory was available and tags were kept by shape.
    degraded_tag_filter: bool
    #: Every article passed in, new or re-seen, with the row it landed on --
    #: in storing order. What the labeller labels.
    stored: tuple[StoredArticle, ...] = ()


@dataclass(frozen=True, slots=True)
class Relink:
    """One link :func:`relink_stored` made: ``article_id`` now names ``canonical_id``.

    Carries both rows' vendor, publisher and ``published_at`` so a caller can
    report the link without reading the rows again. No headline: it is vendor
    text, and the scripts that print these never print headlines.
    """

    article_id: int
    vendor: str
    publisher: str | None
    published_at: datetime
    canonical_id: int
    canonical_vendor: str
    canonical_publisher: str | None
    canonical_published_at: datetime


@dataclass(frozen=True, slots=True)
class RelinkResult:
    """What one :func:`relink_stored` pass did, and what its ``since`` covered.

    ``examined`` counts the unlinked rows published at or after ``since`` that
    share a key with an earlier row -- the only rows that can match -- and
    ``earliest_examined`` is the earliest ``published_at`` among them, ``None``
    when there were none. ``before_since`` counts the rows that would have been
    examined but were published before ``since``: zero means the bound left no
    candidate out. A candidate is not a duplicate; a recycled headline is one too.
    """

    links: tuple[Relink, ...]
    #: Matches not linked because other rows already name the row as their
    #: canonical row; each is also logged as ``news_relink_skipped``.
    skipped: int
    examined: int
    earliest_examined: datetime | None
    before_since: int


def _batch_order(article: NewsArticle) -> tuple[datetime, str, str, str, str, str, str, tuple[str, ...]]:
    """A total order, so one batch stores the same way whatever order it came in."""
    return (
        article.published_at,
        article.vendor,
        article.vendor_id,
        article.feed.value,
        article.url,
        article.headline,
        article.summary or "",
        article.tickers,
    )


def store_articles(
    session: Session,
    articles: Iterable[NewsArticle],
    *,
    assets: AssetDirectory | None,
    now: datetime,
) -> IngestResult:
    """Upsert ``articles`` into ``news_article`` / ``news_article_ticker``. Does not commit.

    ``assets`` is the day's :class:`AssetDirectory`, or ``None`` when its fetch
    failed (tags are then kept by shape; see the module docstring). ``now``
    must be timezone-aware and becomes ``ingested_at`` on every new row.

    **Callers must be serialised** -- a requirement on the poller that calls
    this, which must hold one lock around every call. Two unserialised calls
    cannot see each other's unflushed rows: a copy can miss its link, a URL
    can look identifying to one call while the other is adding the second
    group that would make it ambiguous (a *wrong* link), and two inserts of
    one ``(vendor, vendor_id)`` collide on the unique constraint.

    Even serialised, the first match on a generic page URL not yet on the
    denylist links by URL, and links are never revisited; the denylist covers
    the shapes seen in recorded data.
    """
    require_aware(now, "now")
    stamp = now.astimezone(timezone.utc)
    ordered = sorted(articles, key=_batch_order)

    degraded = assets is None
    if degraded and ordered:
        logger.warning(
            "news tag filtering degraded: no asset directory, keeping tags shaped "
            "like equity symbols for %d article(s)",
            len(ordered),
            extra={
                "event": "news_tag_filter_degraded",
                "rule": (
                    "decision 21 drops tags that are not active US equities; without "
                    "the day's asset list a tag is kept when it matches EQUITY_SYMBOL_RE"
                ),
                "articles": len(ordered),
            },
        )

    inserted = updated = unchanged = linked = tags_added = 0
    dropped_all: list[str] = []
    stored: list[StoredArticle] = []
    for article in ordered:
        kept, dropped = filter_tags(article.tickers, assets)
        dropped_all.extend(dropped)
        existing = session.scalars(
            select(ArticleRow).where(
                ArticleRow.vendor == article.vendor,
                ArticleRow.vendor_id == article.vendor_id,
            )
        ).one_or_none()

        if existing is None:
            row = _insert(session, article, stamp)
            inserted += 1
            if row.canonical_id is not None:
                linked += 1
            added, _ = _merge_tags(session, row.id, kept)
            tags_added += added
            stored.append(StoredArticle(row.id, article))
            continue

        text_changed = _apply_edit(existing, article)
        added, market_removed = _merge_tags(session, existing.id, kept)
        tags_added += added
        if text_changed or added or market_removed:
            updated += 1
        else:
            unchanged += 1
        stored.append(StoredArticle(existing.id, article))

    session.flush()
    if dropped_all:
        distinct = sorted(set(dropped_all))
        logger.info(
            "news ingest dropped %d tag(s) that are not active US equities",
            len(dropped_all),
            extra={
                "event": "news_tags_dropped",
                "rule": "decision 21: tags that are not active US equities are dropped at ingest",
                "count": len(dropped_all),
                "tags": [tag[:_LOG_TAG_WIDTH] for tag in distinct[:_LOG_TAG_LIMIT]],
                "degraded": degraded,
            },
        )
    return IngestResult(
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        duplicates_linked=linked,
        tags_added=tags_added,
        tags_dropped=len(dropped_all),
        degraded_tag_filter=degraded,
        stored=tuple(stored),
    )


def relink_stored(session: Session, *, since: datetime) -> RelinkResult:
    """Link stored duplicates that ingest missed (R2). Sets ``canonical_id`` only. Does not commit.

    Runs decision 3's matcher, :func:`_find_canonical` itself and so
    :data:`SYNDICATION_SKEWS` with it, over every stored row published at or
    after ``since`` (inclusive, timezone-aware) whose ``canonical_id`` is NULL,
    in ``id`` order. Each row is matched only against rows stored before it
    (``before_id=row.id``), so the result is the link ingest would have made
    had today's matcher been live when the row arrived. The earlier-stored
    copy stays canonical, as at ingest; the reverse would make the copy
    stored second canonical, and for the Fool pair stored Massive-first that
    is the stamp Finnhub serves 20 minutes late. Each link is flushed before
    the next row is matched, as ingest flushes each insert, so a row stored
    later sees it.

    ``since`` bounds the rows being linked, not their twins. A row published
    after ``since`` can join a twin published before it.

    What it never does: delete a row, change a row that is already linked,
    re-point a group, or write any column but the one ``canonical_id``. A row
    that other rows already name as their canonical row is not linked even
    when it matches: linking it would chain its members, and re-pointing them
    would change linked rows. That missed link is logged at WARNING as
    ``news_relink_skipped``. A match whose group already holds a row of the
    row's vendor is refused, as at ingest (:func:`_choose_canonical`), even
    when that row was stored after this one.

    Returns a :class:`RelinkResult`: the links made, the matches skipped, and
    what ``since`` covered. Nothing is written for the last part; it is a count.

    Idempotent: a second run makes no link.
    Every row it linked is no longer NULL. Every row it left alone is matched
    against the same earlier rows again, and in the meantime a group can only
    have gained rows, which can refuse a link but never permit one.

    Only rows sharing a ``url_key`` or ``headline_key`` with an earlier row
    are handed to the matcher. That is not a second matcher: every path in
    :func:`_find_canonical` matches on equality of one of those two keys with
    a row stored before, so a row sharing neither cannot match. It is there
    for the write lock. The pass holds SQLite's lock from its first link to
    the caller's commit, and the matcher costs about 1.7 ms a row (52 s over
    30,600 synthetic rows, 2026-11-09). A pass over every row would outlast
    the 5 s that the live app's writers wait.
    """
    require_aware(since, "since")
    bound = since.astimezone(timezone.utc)
    earlier = aliased(ArticleRow)
    shares_a_key = or_(
        exists().where(earlier.id < ArticleRow.id, earlier.url_key == ArticleRow.url_key),
        exists().where(earlier.id < ArticleRow.id, earlier.headline_key == ArticleRow.headline_key),
    )
    unlinked = session.scalars(
        select(ArticleRow)
        .where(
            ArticleRow.canonical_id.is_(None),
            ArticleRow.published_at >= bound,
            shares_a_key,
        )
        .order_by(ArticleRow.id)
    ).all()
    # Read before the first link is flushed, so it costs the write lock nothing.
    before_since = session.scalar(
        select(func.count())
        .select_from(ArticleRow)
        .where(
            ArticleRow.canonical_id.is_(None),
            ArticleRow.published_at < bound,
            shares_a_key,
        )
    )

    links: list[Relink] = []
    skipped = 0
    for row in unlinked:
        canonical_id = _find_canonical(
            session,
            vendor=row.vendor,
            feed=row.feed,
            publisher=row.publisher,
            ukey=row.url_key,
            hkey=row.headline_key,
            published_at=row.published_at,
            before_id=row.id,
        )
        if canonical_id is None:
            continue
        members = _member_count(session, row.id)
        if members:
            skipped += 1
            logger.warning(
                "news_article %d not re-linked to canonical row %d: %d other row(s) "
                "already name it as their canonical row, and linking it would chain them",
                row.id,
                canonical_id,
                members,
                extra={
                    "event": "news_relink_skipped",
                    "rule": (
                        "R2: a re-link sets canonical_id on an unlinked row only. A row "
                        "other rows already point at is left alone: linking it would make "
                        "a chain, and re-pointing its members would change linked rows"
                    ),
                    "article_id": row.id,
                    "canonical_id": canonical_id,
                    "members": members,
                },
            )
            continue
        row.canonical_id = canonical_id
        # Flushed now, so the next row matched sees this link, as at ingest.
        session.flush()
        canonical = session.get_one(ArticleRow, canonical_id)
        link = Relink(
            article_id=row.id,
            vendor=row.vendor,
            publisher=row.publisher,
            published_at=row.published_at,
            canonical_id=canonical.id,
            canonical_vendor=canonical.vendor,
            canonical_publisher=canonical.publisher,
            canonical_published_at=canonical.published_at,
        )
        links.append(link)
        logger.info(
            "news article %d re-linked to canonical row %d",
            row.id,
            canonical_id,
            extra={
                "event": "news_duplicate_relinked",
                "rule": (
                    "R2: decision 3's matcher, SYNDICATION_SKEWS included, run over a "
                    "stored unlinked row against the rows stored before it"
                ),
                "article_id": row.id,
                "canonical_id": canonical_id,
            },
        )

    earliest = min((row.published_at for row in unlinked), default=None)
    result = RelinkResult(
        links=tuple(links),
        skipped=skipped,
        examined=len(unlinked),
        earliest_examined=earliest,
        before_since=int(before_since or 0),
    )
    logger.info(
        "news re-link matched %d unlinked row(s) published since %s that share a key "
        "with an earlier row: %d linked, %d skipped; %d such row(s) published before it",
        result.examined,
        bound.isoformat(),
        len(result.links),
        result.skipped,
        result.before_since,
        extra={
            "event": "news_relink_done",
            "since": bound.isoformat(),
            "examined": result.examined,
            "earliest_examined": None if earliest is None else earliest.isoformat(),
            "before_since": result.before_since,
            "linked": len(result.links),
            "skipped": result.skipped,
        },
    )
    return result


def _member_count(session: Session, canonical_id: int) -> int:
    """How many rows name ``canonical_id`` as their canonical row."""
    count = session.scalar(
        select(func.count()).select_from(ArticleRow).where(ArticleRow.canonical_id == canonical_id)
    )
    return int(count or 0)


def _insert(session: Session, article: NewsArticle, stamp: datetime) -> ArticleRow:
    ukey = url_key(article.url)
    hkey = headline_key(article.headline)
    canonical_id = _find_canonical(
        session,
        vendor=article.vendor,
        feed=article.feed.value,
        publisher=article.publisher,
        ukey=ukey,
        hkey=hkey,
        published_at=article.published_at,
    )
    row = ArticleRow(
        vendor=article.vendor,
        vendor_id=article.vendor_id,
        feed=article.feed.value,
        canonical_id=canonical_id,
        url=article.url,
        url_key=ukey,
        headline=article.headline,
        headline_key=hkey,
        summary=article.summary,
        publisher=article.publisher,
        published_at=article.published_at,
        ingested_at=stamp,
    )
    session.add(row)
    # Flushed now, so the next article in the batch can match against it.
    session.flush()
    if canonical_id is not None:
        logger.info(
            "news article %s/%s linked to canonical row %d",
            article.vendor,
            article.vendor_id,
            canonical_id,
            extra={
                "event": "news_duplicate_linked",
                "rule": (
                    "decision 3: a cross-vendor copy links to one canonical row by "
                    "normalised URL (a story-identifying URL, within +/-48 hours), "
                    "else normalised headline within +/-10 minutes (also around a "
                    "measured syndication lag, for the publisher pairs in "
                    "SYNDICATION_SKEWS)"
                ),
                "article_id": row.id,
                "canonical_id": canonical_id,
            },
        )
    return row


def _apply_edit(row: ArticleRow, article: NewsArticle) -> bool:
    """Carry a re-served article's text onto its row. True when anything changed."""
    changed = False
    if article.headline != row.headline:
        row.headline = article.headline
        row.headline_key = headline_key(article.headline)
        changed = True
    if article.url != row.url:
        row.url = article.url
        row.url_key = url_key(article.url)
        changed = True
    if article.summary is not None and article.summary != row.summary:
        row.summary = article.summary
        changed = True
    return changed


def _merge_tags(session: Session, article_id: int, kept: Sequence[str]) -> tuple[int, bool]:
    """Union ``kept`` into the article's tags. Returns ``(rows added, MARKET removed)``."""
    current = set(
        session.scalars(
            select(NewsArticleTicker.ticker).where(NewsArticleTicker.article_id == article_id)
        )
    )
    if not kept:
        if current:
            return 0, False
        session.add(NewsArticleTicker(article_id=article_id, ticker=MARKET_TICKER))
        session.flush()
        return 1, False

    market_removed = False
    if MARKET_TICKER in current:
        session.execute(
            delete(NewsArticleTicker).where(
                NewsArticleTicker.article_id == article_id,
                NewsArticleTicker.ticker == MARKET_TICKER,
            )
        )
        market_removed = True
    new = [ticker for ticker in kept if ticker not in current]
    for ticker in new:
        session.add(NewsArticleTicker(article_id=article_id, ticker=ticker))
    session.flush()
    return len(new), market_removed


def _is_generic_url_shape(ukey: str) -> bool:
    """Whether a :func:`url_key` has a shape that names a page, never a story.

    A bare host (no path, no query once tracking parameters are gone) and a
    ``/quote/<SYMBOL>`` page -- the shapes the recorded fixtures show: Benzinga
    files briefs under ``https://www.benzinga.com/quote/SPY``, and Stocktwits
    links carry ``https://stocktwits.com``. A root path *with* a query
    (``/?p=123``) is kept: that query can be the story's id. Text that is not
    an absolute URL is not a recognised shape and is left to the other rules;
    an empty key is generic.
    """
    if not ukey:
        return True
    try:
        parts = urlsplit(ukey)
    except ValueError:
        return False
    if not parts.scheme or not parts.hostname:
        return False
    if parts.path == "" and parts.query == "":
        return True
    return _QUOTE_PAGE_PATH_RE.fullmatch(parts.path) is not None


def _url_not_identifying(
    session: Session, ukey: str, *, before_id: int | None = None
) -> str | None:
    """Why ``ukey`` cannot identify a story, or ``None`` when it can.

    Checked over *every* stored row on the URL, with no time bound: a URL that
    has ever been shared by two groups, or by two ids of one vendor, is a page
    that carries several stories, and matching on it would pick one of them by
    age rather than by content. The incoming article's own vendor needs no
    separate count: if it already holds a row on the URL, either that row's
    group is the only group (and the group-holds-vendor rule refuses it) or
    there are two groups. Likewise, while no group holds two rows of one
    vendor, one vendor's two ids on a URL are always two groups; the two-ids
    check is the guard for a group that already breaks that invariant.

    ``before_id`` is :func:`_find_canonical`'s bound: only rows stored before
    it are counted.
    """
    if _is_generic_url_shape(ukey):
        return "generic URL shape (bare host or /quote/<SYMBOL>)"
    on_url = session.execute(
        _stored_before(
            select(ArticleRow.id, ArticleRow.canonical_id, ArticleRow.vendor).where(
                ArticleRow.url_key == ukey
            ),
            before_id,
        )
    ).all()
    groups = {canonical_id if canonical_id is not None else row_id for row_id, canonical_id, _ in on_url}
    if len(groups) > 1:
        return "URL already spans more than one canonical group"
    vendors = [row_vendor for _, _, row_vendor in on_url]
    if len(vendors) != len(set(vendors)):
        return "one vendor already holds two ids on this URL"
    return None


_Statement = TypeVar("_Statement", bound=Select[Any])


def _stored_before(statement: _Statement, before_id: int | None) -> _Statement:
    """``statement`` limited to rows with ``id < before_id``; unchanged when ``None``.

    Unchanged means unchanged: no clause is added, so the matching queries
    ingest runs are exactly what they were before :func:`relink_stored` existed.
    """
    if before_id is None:
        return statement
    return statement.where(ArticleRow.id < before_id)


def _find_canonical(
    session: Session,
    *,
    vendor: str,
    feed: str,
    publisher: str | None,
    ukey: str,
    hkey: str,
    published_at: datetime,
    before_id: int | None = None,
) -> int | None:
    """The canonical row a new article joins, or ``None`` if it starts its own group.

    Decision 3: by normalised URL, falling back to normalised headline within
    :data:`HEADLINE_MATCH_WINDOW`. The URL is tried only when it identifies a
    story (:func:`_url_not_identifying`), and matches only rows published
    within :data:`URL_MATCH_WINDOW`. Only other vendors' rows match, and only
    groups holding no row of ``vendor`` are joined. The headline fallback is
    tried when the URL finds no joinable group, not only when it finds no row.
    It also takes the rows :func:`_skewed_headline_matches` finds for a
    publisher pair in :data:`SYNDICATION_SKEWS`.

    ``before_id``, when given, limits every query that finds *matching* rows
    to rows stored before it (``id < before_id``): the URL-identifying count,
    the URL match, the headline match and the skewed match. That is the table
    as the article stored as row ``before_id`` saw it on arrival, which is how
    :func:`relink_stored` runs this matcher on a row already stored. Ingest
    passes nothing: at insert, every stored row is already older. The bound
    never limits what a group *holds*: :func:`_group_holds_vendor` and the
    canonical lookup in :func:`_choose_canonical` read each group as it
    stands, so a link can never put two rows of one vendor in one group.

    Every path here matches on equality of ``url_key`` or ``headline_key``.
    :func:`relink_stored` selects its rows on exactly that, so a new path
    keyed on anything else must widen that selection too.
    """
    reason = _url_not_identifying(session, ukey, before_id=before_id)
    if reason is None:
        by_url = session.scalars(
            _stored_before(
                select(ArticleRow).where(
                    ArticleRow.url_key == ukey,
                    ArticleRow.vendor != vendor,
                    ArticleRow.published_at >= published_at - URL_MATCH_WINDOW,
                    ArticleRow.published_at <= published_at + URL_MATCH_WINDOW,
                ),
                before_id,
            )
        ).all()
        chosen = _choose_canonical(session, by_url, vendor)
        if chosen is not None:
            return chosen
    else:
        logger.debug(
            "news URL does not identify a story; headline check only",
            extra={
                "event": "news_url_not_identifying",
                "rule": reason,
                "url_key": ukey[:_LOG_URL_WIDTH],
                "vendor": vendor,
            },
        )
    if not hkey:
        return None
    by_headline = list(
        session.scalars(
            _stored_before(
                select(ArticleRow).where(
                    ArticleRow.headline_key == hkey,
                    ArticleRow.vendor != vendor,
                    ArticleRow.published_at >= published_at - HEADLINE_MATCH_WINDOW,
                    ArticleRow.published_at <= published_at + HEADLINE_MATCH_WINDOW,
                ),
                before_id,
            )
        ).all()
    )
    if publisher is not None:
        by_headline.extend(
            _skewed_headline_matches(
                session,
                incoming=SyndicationSide(vendor=vendor, feed=feed, publisher=publisher),
                hkey=hkey,
                published_at=published_at,
                before_id=before_id,
            )
        )
    return _choose_canonical(session, by_headline, vendor)


def _skewed_headline_matches(
    session: Session,
    *,
    incoming: SyndicationSide,
    hkey: str,
    published_at: datetime,
    before_id: int | None = None,
) -> list[ArticleRow]:
    """Rows on ``hkey`` that a :data:`SYNDICATION_SKEWS` entry puts in reach of ``incoming``.

    The incoming article must be one side of a skew exactly (vendor, feed and
    publisher text). If it is the late side, the early side is searched
    around ``published_at - offset``. If it is the early side, the late side is
    searched around ``published_at + offset``. Both use
    :data:`HEADLINE_MATCH_WINDOW`, inclusive. Each skew that finds rows is
    logged as ``news_syndication_skew_match``. ``before_id`` is
    :func:`_find_canonical`'s bound.
    """
    found: list[ArticleRow] = []
    for skew in SYNDICATION_SKEWS:
        if incoming == skew.late:
            other = skew.early
            centre = published_at - skew.offset
        elif incoming == skew.early:
            other = skew.late
            centre = published_at + skew.offset
        else:
            continue
        rows = session.scalars(
            _stored_before(
                select(ArticleRow).where(
                    ArticleRow.headline_key == hkey,
                    ArticleRow.vendor == other.vendor,
                    ArticleRow.feed == other.feed,
                    ArticleRow.publisher == other.publisher,
                    ArticleRow.published_at >= centre - HEADLINE_MATCH_WINDOW,
                    ArticleRow.published_at <= centre + HEADLINE_MATCH_WINDOW,
                ),
                before_id,
            )
        ).all()
        if not rows:
            continue
        logger.info(
            "news headline matched %d %s/%s row(s) across a measured %ds syndication lag",
            len(rows),
            other.vendor,
            other.publisher,
            int(skew.offset.total_seconds()),
            extra={
                "event": "news_syndication_skew_match",
                "rule": (
                    "a publisher pair in SYNDICATION_SKEWS also matches by headline "
                    "within +/-10 minutes of the stamp shifted by its measured lag"
                ),
                "vendor": incoming.vendor,
                "feed": incoming.feed,
                "publisher": incoming.publisher,
                "counterpart_vendor": other.vendor,
                "counterpart_publisher": other.publisher,
                "offset_seconds": int(skew.offset.total_seconds()),
                "matches": len(rows),
            },
        )
        found.extend(rows)
    return found


def _choose_canonical(
    session: Session, matches: Sequence[ArticleRow], vendor: str
) -> int | None:
    """Resolve matches to their groups' canonical rows; pick oldest, then lowest id."""
    if not matches:
        return None
    group_ids = sorted(
        {match.canonical_id if match.canonical_id is not None else match.id for match in matches}
    )
    candidates = session.scalars(select(ArticleRow).where(ArticleRow.id.in_(group_ids))).all()
    eligible: list[ArticleRow] = []
    for candidate in candidates:
        if candidate.canonical_id is not None:
            # A duplicate named as a canonical row: the no-chain invariant is
            # already broken. Never extend it; leave the group alone.
            logger.error(
                "news_article %d is named as canonical but itself points at %d; "
                "not linking to it",
                candidate.id,
                candidate.canonical_id,
                extra={
                    "event": "news_canonical_chain_found",
                    "rule": "a canonical row is itself canonical -- no chains",
                    "article_id": candidate.id,
                    "canonical_id": candidate.canonical_id,
                },
            )
            continue
        if _group_holds_vendor(session, candidate.id, vendor):
            continue
        eligible.append(candidate)
    if not eligible:
        return None
    best = min(eligible, key=lambda candidate: (candidate.published_at, candidate.id))
    return best.id


def _group_holds_vendor(session: Session, canonical_id: int, vendor: str) -> bool:
    """Whether the group rooted at ``canonical_id`` already has a row from ``vendor``.

    Two ``EXISTS``, the root and then its members, rather than one over an
    ``OR``: the answer is the same, since ``EXISTS`` distributes over ``OR``,
    but SQLite planned the single form as a search on ``vendor`` through the
    ``(vendor, vendor_id)`` unique index. That reads every row of the vendor,
    about 12 ms a call at 30,600 rows and growing with the table. Split, the
    two use the primary key and ``ix_news_article_canonical_id`` (EXPLAIN
    QUERY PLAN, 2026-11-09).
    """
    statement = select(
        or_(
            exists().where(ArticleRow.id == canonical_id, ArticleRow.vendor == vendor),
            exists().where(ArticleRow.canonical_id == canonical_id, ArticleRow.vendor == vendor),
        )
    )
    return bool(session.scalar(statement))
