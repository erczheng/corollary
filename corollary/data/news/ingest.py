"""The article store: normalise, dedupe, upsert (Phase 3 decision 3, decision 21).

This is the storage half of news ingest. The pollers that fetch articles from
the vendors call :func:`store_articles` with what they fetched; nothing here
talks to a vendor.

Four pieces:

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

**Link, then never re-link.** ``canonical_id`` is decided once, when a row is
inserted. A later edit (a new headline or URL on a re-poll) updates the row's
keys, so *future* articles match against the new text, but never re-points
the row or its group -- re-pointing is how a chain or an orphan gets made.

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
from typing import Final
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.orm import Session

from corollary.data.news.article import NewsArticle
from corollary.data.providers.interface import AssetDirectory
from corollary.data.seeds import EQUITY_SYMBOL_RE, normalize_symbol
from corollary.db.models import NewsArticle as ArticleRow
from corollary.db.models import NewsArticleTicker
from corollary.wire import require_aware

__all__ = [
    "HEADLINE_MATCH_WINDOW",
    "MARKET_TICKER",
    "URL_MATCH_WINDOW",
    "IngestResult",
    "StoredArticle",
    "filter_tags",
    "headline_key",
    "store_articles",
    "url_key",
]

logger = logging.getLogger(__name__)

#: The ticker value an article tagged to no active equity is stored under
#: (``types.ts``'s ``MARKET``).
MARKET_TICKER: Final = "MARKET"

#: Decision 3: the headline fallback matches within +/-10 minutes, inclusive.
HEADLINE_MATCH_WINDOW: Final = timedelta(minutes=10)

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


def _insert(session: Session, article: NewsArticle, stamp: datetime) -> ArticleRow:
    ukey = url_key(article.url)
    hkey = headline_key(article.headline)
    canonical_id = _find_canonical(
        session,
        vendor=article.vendor,
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
                    "else normalised headline within +/-10 minutes"
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


def _url_not_identifying(session: Session, ukey: str) -> str | None:
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
    """
    if _is_generic_url_shape(ukey):
        return "generic URL shape (bare host or /quote/<SYMBOL>)"
    on_url = session.execute(
        select(ArticleRow.id, ArticleRow.canonical_id, ArticleRow.vendor).where(
            ArticleRow.url_key == ukey
        )
    ).all()
    groups = {canonical_id if canonical_id is not None else row_id for row_id, canonical_id, _ in on_url}
    if len(groups) > 1:
        return "URL already spans more than one canonical group"
    vendors = [row_vendor for _, _, row_vendor in on_url]
    if len(vendors) != len(set(vendors)):
        return "one vendor already holds two ids on this URL"
    return None


def _find_canonical(
    session: Session,
    *,
    vendor: str,
    ukey: str,
    hkey: str,
    published_at: datetime,
) -> int | None:
    """The canonical row a new article joins, or ``None`` if it starts its own group.

    Decision 3: by normalised URL, falling back to normalised headline within
    :data:`HEADLINE_MATCH_WINDOW`. The URL is tried only when it identifies a
    story (:func:`_url_not_identifying`), and matches only rows published
    within :data:`URL_MATCH_WINDOW`. Only other vendors' rows match, and only
    groups holding no row of ``vendor`` are joined. The headline fallback is
    tried when the URL finds no joinable group, not only when it finds no row.
    """
    reason = _url_not_identifying(session, ukey)
    if reason is None:
        by_url = session.scalars(
            select(ArticleRow).where(
                ArticleRow.url_key == ukey,
                ArticleRow.vendor != vendor,
                ArticleRow.published_at >= published_at - URL_MATCH_WINDOW,
                ArticleRow.published_at <= published_at + URL_MATCH_WINDOW,
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
    by_headline = session.scalars(
        select(ArticleRow).where(
            ArticleRow.headline_key == hkey,
            ArticleRow.vendor != vendor,
            ArticleRow.published_at >= published_at - HEADLINE_MATCH_WINDOW,
            ArticleRow.published_at <= published_at + HEADLINE_MATCH_WINDOW,
        )
    ).all()
    return _choose_canonical(session, by_headline, vendor)


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
    """Whether the group rooted at ``canonical_id`` already has a row from ``vendor``."""
    statement = select(
        exists().where(
            or_(ArticleRow.id == canonical_id, ArticleRow.canonical_id == canonical_id),
            ArticleRow.vendor == vendor,
        )
    )
    return bool(session.scalar(statement))
