"""Phase 3 step 5, unit 5.4 -- the rules tier's label volume, re-measured with the real patterns.

Step 5's *Done when*: "The rules tier's per-session volume is re-measured with
the real patterns, on the watch universe and in total. If the watch-universe
figure alone is below 1.5, decision 13's second risk goes back to the owner."
Q22 gates step 6's transitions on that figure.

Same method as step 0's ``scripts/probe_phase3.py labels --sessions 10`` (which
stays as it was, so step 0's draft-pattern figures remain reproducible), with
four things made production-equivalent:

* **Fetching** goes through the committed providers -- ``FinnhubProvider``
  (``/company-news`` for every polled watch symbol), ``AlpacaProvider.news``
  (no ``symbols`` parameter: the whole feed, decision 21's discovery tier) and
  ``MassiveProvider.news_since`` -- paced by the shared per-host limiter. GET
  only, no MCP (rule 3), no ``alpaca`` SDK import. The asset list comes from
  ``AlpacaProvider.active_equities`` and the positions, read-only, from
  ``AlpacaBroker.positions`` (``--no-positions`` skips them).
* **The watch universe** is :func:`corollary.data.news.watchlist.watch_universe`
  over the Markets universe, decision 6's real sector leaders (the committed
  2026-06-30 N-PORT filings, via ``measure_phase3_news.sector_leaders``) and
  the paper account's position underlyings. Manual watches live in the app's
  database and are not read: none are counted.
* **Storing and labelling** are the production functions, run against a
  scratch SQLite file: ``ingest.store_articles`` (decision 3's cross-vendor
  dedup, decision 21's tag filter) and ``labelling.label_stored`` (the rules
  tier with a production-equivalent ``NameBook``, and ``vendor_labels`` on the
  Massive copy). The label rows are then counted.
* **Sessions** are step 0's: an article belongs to the first session whose
  close is at or after its publication (``calendars.py``), over the last N
  completed sessions.

Run by hand, never by the test suite, and **only through the launcher** so no
agent ever reads ``.env`` (rule 6)::

    uv run --env-file <path-to>/.env python scripts/measure_phase3_rules.py
    uv run --env-file <path-to>/.env python scripts/measure_phase3_rules.py --sessions 10
    uv run python scripts/measure_phase3_rules.py --from-cache   # relabel, no requests

Output: counts on stdout; the summary, the fetched-article cache and the
labelled database under ``.claude/scratch/5.4/`` (gitignored). The cache and
the examples file carry headlines; neither is ever printed.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from corollary.data.news.article import (  # noqa: E402
    NewsAccessDenied,
    NewsArticle,
    NewsFeed,
    NewsProviderError,
    VendorInsight,
)
from corollary.data.news.ingest import store_articles  # noqa: E402
from corollary.data.news.labelling import LabelWrite, label_stored  # noqa: E402
from corollary.data.news.rules import WATCH_COMPANY_NAMES, NameBook  # noqa: E402
from corollary.data.news.watchlist import MARKET_TICKER, watch_universe  # noqa: E402
from corollary.data.providers.finnhub import (  # noqa: E402
    PUBLISHER_CLOCK_FUTURE_TOLERANCE,
    correct_publisher_clock,
)
from corollary.data.providers.interface import AssetDirectory, EquityAsset  # noqa: E402
from corollary.db.models import Base, SentimentLabelRow  # noqa: E402
from corollary.db.models import NewsArticle as ArticleRow  # noqa: E402
from corollary.db.session import create_db_engine, sqlite_url  # noqa: E402

import measure_phase3_news as m4  # noqa: E402
import probe_phase3 as p0  # noqa: E402

SCRATCH: Final = REPO_ROOT / ".claude" / "scratch" / "5.4"
CACHE: Final = SCRATCH / "articles.json"
DB_PATH: Final = SCRATCH / "labels.db"
SUMMARY: Final = SCRATCH / "summary.json"
EXAMPLES: Final = SCRATCH / "examples.json"

#: The Finnhub window is fetched in chunks of this many UTC days; a chunk the
#: provider reports capped is halved until it is one day (step 0's rule).
FINNHUB_CHUNK_DAYS: Final = 5
#: Alpaca and Massive are read in calls of this many pages, cursor to cursor.
ALPACA_PAGES_PER_CALL: Final = 100
ALPACA_MAX_CALLS: Final = 20
MASSIVE_MAX_CALLS: Final = 80
#: One Massive page per call, this far apart: the free tier is 5/min, and a
#: second process on the key (a running app) shares that budget -- a
#: three-page burst was answered 429 on the first live run.
MASSIVE_SPACING: Final = 15.0
#: A 429 from a vendor shared with another process is waited out this long,
#: this many times, before the run gives up.
RETRY_AFTER_429: Final = 65.0
RETRIES_429: Final = 4

#: The cache payload's record of whether its Finnhub stamps went through
#: ``correct_publisher_clock`` (07e27fa). A fetch through the fixed provider
#: writes :data:`FINNHUB_CLOCK_CORRECTED`; a cache without the key predates the
#: fix, holds raw stamps, and is corrected on load -- once.
FINNHUB_CLOCK_KEY: Final = "finnhub_clock"
FINNHUB_CLOCK_CORRECTED: Final = "corrected"

DIRECTIONAL: Final = frozenset({"bullish", "bearish"})
GATE: Final = 1.5

#: Step 0's figures (spec, *Done when* label-volume section, measured
#: 2026-09-24 over 2026-09-10 -> 2026-09-23 with the draft regexes).
STEP0: Final[Mapping[str, Any]] = {
    "rules_watch_strict_dedup_mean": 4.2, "rules_watch_strict_dedup_range": [1, 7],
    "rules_watch_strict_events_mean": 2.9, "rules_watch_strict_events_range": [1, 4],
    "rules_watch_strict_per_vendor_mean": {"finnhub": 3.7, "alpaca": 1.2, "massive": 0.2},
    "rules_watch_tagged_pairs_mean": 8.4, "rules_watch_tagged_articles_mean": 7.0,
    "rules_total": "unmeasured (draft patterns applied to watch tickers only)",
    "massive_watch_directional_mean": 71.4, "massive_watch_directional_range": [37, 132],
    "massive_watch_neutral_share": 0.471, "massive_neutral_share_note": "mixed excluded",
    "massive_total_directional_approx": 400,
    "watch_universe": 66,
}  # fmt: skip


def say(message: str) -> None:
    m4.say(message)


async def _patient(call: Any, label: str) -> Any:
    """Await ``call()``, waiting out a vendor 429 up to :data:`RETRIES_429` times."""
    for attempt in range(RETRIES_429 + 1):
        try:
            return await call()
        except NewsAccessDenied:
            raise
        except NewsProviderError as exc:
            if "429" not in str(exc) or attempt == RETRIES_429:
                raise
            say(f"  {label}: 429, waiting {RETRY_AFTER_429:.0f}s (attempt {attempt + 1})")
            await asyncio.sleep(RETRY_AFTER_429)
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------
# The article cache (so a relabel needs no requests)
# --------------------------------------------------------------------------


def _article_json(a: NewsArticle) -> dict[str, Any]:
    return {
        "vendor": a.vendor, "vendor_id": a.vendor_id, "feed": a.feed.value, "url": a.url,
        "headline": a.headline, "publisher": a.publisher, "published_at": a.published_at.isoformat(),
        "tickers": list(a.tickers),
        "insights": [{"ticker": i.ticker, "sentiment": i.sentiment, "reasoning": i.reasoning} for i in a.insights],
    }  # fmt: skip


def _article_from(raw: Mapping[str, Any]) -> NewsArticle:
    return NewsArticle(
        vendor=raw["vendor"], vendor_id=raw["vendor_id"], feed=NewsFeed(raw["feed"]), url=raw["url"],
        headline=raw["headline"], summary=None, publisher=raw["publisher"],
        published_at=datetime.fromisoformat(raw["published_at"]), tickers=tuple(raw["tickers"]),
        insights=tuple(VendorInsight(i["ticker"], i["sentiment"], i["reasoning"]) for i in raw["insights"]),
    )  # fmt: skip


def cache_payload(**parts: Any) -> dict[str, Any]:
    """The cache payload of a fresh fetch. Its Finnhub rows came through the
    fixed decoder, so it says so and is never corrected again on load."""
    return {**parts, FINNHUB_CLOCK_KEY: FINNHUB_CLOCK_CORRECTED}


def correct_finnhub_clock(payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """``payload`` with its Finnhub-Benzinga stamps corrected, unless it already is.

    Applies :func:`correct_publisher_clock` exactly where the decoder now does:
    vendor ``finnhub``, feed ``finnhub_company``, keyed on the row's publisher
    (the function itself matches ``"Benzinga"`` exactly). A payload carrying
    ``finnhub_clock: corrected`` is returned unchanged, and the result carries
    it, so a stamp is never corrected twice. The provider's future guard is
    mirrored with the cache's ``fetched_at`` standing in for the response time
    -- a lower bound on it, so this disbelieves at least as often. Returns a
    copy; the loaded payload is not mutated.
    """
    counts = {"corrected": 0, "disbelieved_future": 0}
    out = dict(payload)
    if payload.get(FINNHUB_CLOCK_KEY) == FINNHUB_CLOCK_CORRECTED:
        return out, counts
    ceiling = datetime.fromisoformat(payload["fetched_at"]) + PUBLISHER_CLOCK_FUTURE_TOLERANCE
    articles: list[dict[str, Any]] = []
    for raw in payload["articles"]:
        row = dict(raw)
        if row["vendor"] == "finnhub" and row["feed"] == NewsFeed.FINNHUB_COMPANY.value:
            stamp = datetime.fromisoformat(row["published_at"])
            fixed = correct_publisher_clock(stamp, row["publisher"])
            if fixed != stamp:
                if fixed > ceiling:
                    counts["disbelieved_future"] += 1
                else:
                    row["published_at"] = fixed.isoformat()
                    counts["corrected"] += 1
        articles.append(row)
    out["articles"] = articles
    out[FINNHUB_CLOCK_KEY] = FINNHUB_CLOCK_CORRECTED
    return out, counts


def save_cache(payload: Mapping[str, Any]) -> None:
    m4.write_summary(payload, CACHE)


def load_cache() -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(CACHE.read_text(encoding="utf-8"))
    return raw


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


async def _finnhub_symbol(
    finnhub: m4.FinnhubNewsSource, symbol: str, lo: date, hi: date, meta: dict[str, Any]
) -> list[NewsArticle]:
    answer = await m4._finnhub_call(finnhub, symbol, lo, hi, asyncio.sleep)
    meta["calls"] += 1
    capped = answer.retried_today or answer.overflow_date is not None
    if capped and lo < hi:
        meta["splits"] += 1
        mid = lo + (hi - lo) // 2
        left = await _finnhub_symbol(finnhub, symbol, lo, mid, meta)
        return left + await _finnhub_symbol(finnhub, symbol, mid + timedelta(days=1), hi, meta)
    if answer.overflow_date is not None:
        meta["overflow"].append(f"{symbol} {answer.overflow_date.isoformat()}")
    return list(answer.articles)


async def fetch_finnhub(
    finnhub: m4.FinnhubNewsSource, symbols: Sequence[str], lo: date, hi: date
) -> tuple[list[NewsArticle], dict[str, Any]]:
    """Every sighting -- one article returned under two symbols is two sightings,
    each tagged with its queried symbol, exactly as the watch-tier poller stores
    and labels them."""
    meta: dict[str, Any] = {"calls": 0, "splits": 0, "overflow": [], "errors": {}, "symbols": len(symbols)}
    out: list[NewsArticle] = []
    for n, symbol in enumerate(symbols, 1):
        chunk_lo = lo
        while chunk_lo <= hi:
            chunk_hi = min(hi, chunk_lo + timedelta(days=FINNHUB_CHUNK_DAYS - 1))
            try:
                out.extend(await _finnhub_symbol(finnhub, symbol, chunk_lo, chunk_hi, meta))
            except NewsAccessDenied:
                raise
            except NewsProviderError as exc:
                meta["errors"][f"{symbol} {chunk_lo}..{chunk_hi}"] = m4._error(exc)
            chunk_lo = chunk_hi + timedelta(days=1)
        if n % 10 == 0 or n == len(symbols):
            say(f"  finnhub {n}/{len(symbols)} symbols, {meta['calls']} calls, {len(out)} sightings")
    return out, meta


async def fetch_alpaca(alpaca: Any, start: datetime) -> tuple[list[NewsArticle], dict[str, Any]]:
    """The whole feed updated since ``start``, up to now (an article edited after
    the window still counts by its first publication)."""
    articles: dict[str, NewsArticle] = {}
    meta: dict[str, Any] = {"calls": 0, "pages": 0, "complete": False, "skipped": 0}
    cursor = start
    for _ in range(ALPACA_MAX_CALLS):
        answer = await _patient(lambda: alpaca.news(start=cursor, max_pages=ALPACA_PAGES_PER_CALL), "alpaca")
        meta["calls"] += 1
        meta["pages"] += answer.pages
        meta["skipped"] += answer.skipped
        for a in answer.articles:
            articles[a.vendor_id] = a  # the latest copy: what the store ends up holding
        say(f"  alpaca call {meta['calls']}: {answer.pages} pages, {len(articles)} articles, complete={answer.complete}")
        if answer.complete:
            meta["complete"] = True
            break
        if answer.cursor <= cursor:
            break
        cursor = answer.cursor
    meta["articles"] = len(articles)
    return list(articles.values()), meta


async def fetch_massive(massive: Any, start: datetime, end: datetime) -> tuple[list[NewsArticle], dict[str, Any]]:
    articles: dict[str, NewsArticle] = {}
    meta: dict[str, Any] = {"calls": 0, "pages": 0, "complete": False, "skipped": 0}
    after = start - timedelta(seconds=1)
    for n in range(MASSIVE_MAX_CALLS):
        if n:
            await asyncio.sleep(MASSIVE_SPACING)
        answer = await _patient(lambda: massive.news_since(after), "massive")
        meta["calls"] += 1
        meta["pages"] += answer.pages
        meta["skipped"] += answer.skipped
        for a in answer.articles:
            articles.setdefault(a.vendor_id, a)
        newest = max((a.published_at for a in answer.articles), default=None)
        if meta["calls"] % 5 == 0 or answer.complete:
            say(f"  massive call {meta['calls']}: {meta['pages']} pages, {len(articles)} articles")
        if answer.complete or (newest is not None and newest > end):
            meta["complete"] = True
            break
        if answer.cursor <= after:
            break
        after = answer.cursor
    meta["articles"] = len(articles)
    return list(articles.values()), meta


async def fetch_all(args: argparse.Namespace) -> dict[str, Any]:
    from corollary.api.routes.markets import UNIVERSE_FUND_SYMBOLS, UNIVERSE_SYMBOLS
    from corollary.data.providers.alpaca import AlpacaProvider
    from corollary.data.providers.finnhub import FinnhubProvider
    from corollary.data.providers.massive import MassiveProvider

    now = datetime.now(timezone.utc)
    sessions = p0.completed_sessions(args.sessions, now)
    start, end = sessions[0][1], sessions[-1][1]
    if args.no_positions:
        positions: tuple[str, ...] = ()
        positions_note = "positions skipped: --no-positions"
    else:
        read, positions_note = await m4._read_positions()
        positions = read or ()
    leaders = m4.sector_leaders()
    say(f"window {start.isoformat()} -> {end.isoformat()}; positions: {positions_note}")

    alpaca = AlpacaProvider.from_env()
    finnhub = FinnhubProvider.from_env()
    massive = MassiveProvider.from_env(max_pages=1)
    try:
        directory = await alpaca.active_equities()
        say(f"asset list: {len(directory.assets)} active US equities")
        universe = watch_universe(
            markets=UNIVERSE_SYMBOLS,
            leaders=sorted({s for top in leaders.values() for s in top}),
            positions=positions,
            manual=(),
        )
        polled = universe.polled_symbols
        say(f"watch universe: {len(polled)} polled symbols (+ MARKET)")
        say("massive:")
        massive_articles, massive_meta = await fetch_massive(massive, start, end)
        say("alpaca:")
        alpaca_articles, alpaca_meta = await fetch_alpaca(alpaca, start)
        say("finnhub /company-news:")
        finnhub_articles, finnhub_meta = await fetch_finnhub(
            finnhub, polled, start.date(), end.date()
        )
    finally:
        await alpaca.aclose()
        await finnhub.aclose()
        await massive.aclose()

    payload = cache_payload(**{
        "fetched_at": now.isoformat(timespec="seconds"),
        "sessions": [[d.isoformat(), c.isoformat()] for d, c in sessions],
        "watch": {
            "symbols": list(universe.symbols),
            "markets": list(UNIVERSE_SYMBOLS),
            "leaders": {k: list(v) for k, v in leaders.items()},
            "positions": list(positions),
            "positions_note": positions_note,
            "manual": "not read (the app database's watch_symbol rows); none counted",
        },
        "funds": list(UNIVERSE_FUND_SYMBOLS),
        "assets": [{"symbol": a.symbol, "name": a.name, "tradable": a.tradable, "has_options": a.has_options,
                    "exchange": a.exchange} for a in directory.assets],
        "fetch": {"massive": massive_meta, "alpaca": alpaca_meta, "finnhub": finnhub_meta},
        "articles": [_article_json(a) for a in (*massive_articles, *alpaca_articles, *finnhub_articles)],
    })
    save_cache(payload)
    say(f"cache written: {len(payload['articles'])} articles/sightings")
    return payload


# --------------------------------------------------------------------------
# Storing and labelling -- the production functions
# --------------------------------------------------------------------------


def _directory(raw: Iterable[Mapping[str, Any]]) -> AssetDirectory:
    return AssetDirectory(
        assets=tuple(
            EquityAsset(symbol=a["symbol"], name=a["name"], tradable=a["tradable"], has_options=a["has_options"],
                        exchange=a["exchange"])
            for a in raw
        )
    )


def store_and_label(payload: Mapping[str, Any]) -> tuple[LabelWrite, dict[str, int]]:
    directory = _directory(payload["assets"])
    book = NameBook(
        watch=payload["watch"]["symbols"],
        watch_names=WATCH_COMPANY_NAMES,
        asset_names={a.symbol: a.name for a in directory.assets},
        funds=payload["funds"],
    )
    articles = [_article_from(a) for a in payload["articles"]]
    if DB_PATH.exists():
        DB_PATH.unlink()
    engine = create_db_engine(sqlite_url(DB_PATH))
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        result = store_articles(session, articles, assets=directory, now=now)
        written = label_stored(session, result.stored, book, assets=directory, now=now)
        session.commit()
    engine.dispose()
    ingest = {
        "inserted": result.inserted, "updated": result.updated, "unchanged": result.unchanged,
        "duplicates_linked": result.duplicates_linked, "tags_dropped": result.tags_dropped,
    }  # fmt: skip
    return written, ingest


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------


def stats(values: Sequence[int]) -> dict[str, Any]:
    if not values:
        return {"mean": None, "median": None, "min": None, "max": None}
    return {
        "mean": round(statistics.mean(values), 2),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def tally(payload: Mapping[str, Any]) -> dict[str, Any]:
    sessions = [(date.fromisoformat(d), datetime.fromisoformat(c)) for d, c in payload["sessions"]]
    binner = p0.SessionBinner(sessions)
    days = [d.isoformat() for d in binner.days]
    watch = frozenset(payload["watch"]["symbols"]) - {MARKET_TICKER}
    step0_set = p0.WATCH_TICKERS
    scopes: dict[str, Any] = {
        "watch": lambda t: t in watch,
        "step0_set": lambda t: t in step0_set,
        "off_watch": lambda t: t not in watch and t != MARKET_TICKER,
        "total": lambda t: True,
    }
    engine = create_db_engine(sqlite_url(DB_PATH))
    with Session(engine) as session:
        rows = session.execute(
            select(
                SentimentLabelRow.ticker, SentimentLabelRow.source, SentimentLabelRow.direction,
                SentimentLabelRow.rule_id, ArticleRow.id, ArticleRow.canonical_id, ArticleRow.vendor,
                ArticleRow.published_at, ArticleRow.headline, SentimentLabelRow.reasoning,
            ).join(ArticleRow, ArticleRow.id == SentimentLabelRow.article_id)
        ).all()  # fmt: skip
    engine.dispose()

    # rules[scope][metric][day] -> count
    rules: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    dedup: dict[str, dict[str, set[tuple[int, str, str]]]] = defaultdict(lambda: defaultdict(set))
    events: dict[str, dict[str, set[tuple[str, str]]]] = defaultdict(lambda: defaultdict(set))
    massive: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    rule_hits: dict[str, Counter[str]] = defaultdict(Counter)
    examples: list[dict[str, Any]] = []
    outside = 0
    for ticker, source, direction, rule_id, art_id, canonical, vendor, published, headline, reasoning in rows:
        at = published if published.tzinfo else published.replace(tzinfo=timezone.utc)
        s = binner.session(at)
        if s is None:
            outside += 1
            continue
        day = s.isoformat()
        for scope, member in scopes.items():
            if not member(ticker):
                continue
            if source == "rules":
                if direction not in DIRECTIONAL:
                    continue
                rules[scope][vendor][day] += 1
                rules[scope]["all_rows"][day] += 1
                dedup[scope][day].add((canonical if canonical is not None else art_id, ticker, direction))
                events[scope][day].add((ticker, direction))
                rule_hits[scope][str(rule_id)] += 1
            else:
                massive[scope][direction][day] += 1
        if source == "rules" and direction in DIRECTIONAL:
            examples.append({
                "session": day, "vendor": vendor, "group": canonical if canonical is not None else art_id,
                "ticker": ticker, "watch": ticker in watch, "direction": direction, "rule_id": rule_id,
                "reasoning": reasoning, "headline": headline,
            })  # fmt: skip

    def series(counter: Mapping[str, int]) -> dict[str, Any]:
        per = {d: counter.get(d, 0) for d in days}
        return {"per_session": per, **stats(list(per.values()))}

    out: dict[str, Any] = {"sessions": days, "label_rows_outside_window": outside, "rules": {}, "massive": {}}
    for scope in scopes:
        r = rules[scope]
        out["rules"][scope] = {
            **{v: series(r[v]) for v in ("finnhub", "alpaca", "massive")},
            "as_written_rows": series(r["all_rows"]),
            "deduplicated": series({d: len(dedup[scope][d]) for d in days}),
            "distinct_events": series({d: len(events[scope][d]) for d in days}),
            "rule_hits": dict(rule_hits[scope].most_common()),
        }
        mz = massive[scope]
        directional = {d: mz["bullish"][d] + mz["bearish"][d] for d in days}
        total_dir = sum(directional.values())
        total_neu = sum(mz["neutral"].values())
        out["massive"][scope] = {
            "directional": series(directional),
            "neutral": series(mz["neutral"]),
            "bullish_total": sum(mz["bullish"].values()),
            "bearish_total": sum(mz["bearish"].values()),
            "neutral_share_mixed_as_neutral": round(total_neu / (total_dir + total_neu), 3)
            if total_dir + total_neu else None,
        }
    # Step 0 excluded ``mixed``; the production mapping files it as neutral.
    # Recount from the raw insights so both definitions are on the record.
    raw: dict[str, Counter[str]] = defaultdict(Counter)
    for a in payload["articles"]:
        if a["feed"] != NewsFeed.MASSIVE_NEWS.value:
            continue
        s = binner.session(datetime.fromisoformat(a["published_at"]))
        if s is None:
            continue
        for ins in a["insights"]:
            raw["watch" if ins["ticker"] in watch else "off_watch"][ins["sentiment"]] += 1
            raw["total"][ins["sentiment"]] += 1
    for scope, c in raw.items():
        directional_raw = c["positive"] + c["negative"]
        out["massive"].setdefault(scope, {})["raw_insights"] = dict(c)
        out["massive"][scope]["neutral_share_mixed_excluded"] = (
            round(c["neutral"] / (directional_raw + c["neutral"]), 3) if directional_raw + c["neutral"] else None
        )
    m4.write_summary({"examples": examples}, EXAMPLES)
    return out


def report(out: Mapping[str, Any], written: LabelWrite, ingest: Mapping[str, int]) -> None:
    w = out["rules"]["watch"]
    t = out["rules"]["total"]
    say("")
    say("session     | watch: finnhub alpaca massive dedup events | total: dedup | off-watch dedup | massive watch dir/neu | massive total dir")
    for d in out["sessions"]:
        say(
            f"{d}  |        {w['finnhub']['per_session'][d]:>7} {w['alpaca']['per_session'][d]:>6} "
            f"{w['massive']['per_session'][d]:>7} {w['deduplicated']['per_session'][d]:>5} "
            f"{w['distinct_events']['per_session'][d]:>6} | {t['deduplicated']['per_session'][d]:>12} | "
            f"{out['rules']['off_watch']['deduplicated']['per_session'][d]:>15} | "
            f"{out['massive']['watch']['directional']['per_session'][d]:>9}/"
            f"{out['massive']['watch']['neutral']['per_session'][d]:<11} | "
            f"{out['massive']['total']['directional']['per_session'][d]:>6}"
        )
    for scope in ("watch", "step0_set", "off_watch", "total"):
        r = out["rules"][scope]
        say(f"RULES[{scope}] " + json.dumps({k: {x: r[k][x] for x in ("mean", "median", "min", "max")}
                                             for k in ("finnhub", "alpaca", "massive", "as_written_rows",
                                                       "deduplicated", "distinct_events")}))
        say(f"RULE_HITS[{scope}] " + json.dumps(r["rule_hits"]))
    for scope in ("watch", "total"):
        mz = out["massive"][scope]
        say(f"MASSIVE[{scope}] directional {json.dumps({x: mz['directional'][x] for x in ('mean', 'median', 'min', 'max')})} "
            f"bull/bear {mz['bullish_total']}/{mz['bearish_total']} neutral share (mixed as neutral) "
            f"{mz['neutral_share_mixed_as_neutral']} (mixed excluded) {mz.get('neutral_share_mixed_excluded')}")
    say("INGEST " + json.dumps(ingest))
    say("LABELWRITE " + json.dumps(dataclasses.asdict(written)))
    mean = w["deduplicated"]["mean"] or 0
    verdict = "AT OR ABOVE" if mean >= GATE else "BELOW"
    say(f"GATE: watch-universe rules rate (deduplicated) mean {mean} per session is {verdict} {GATE}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--sessions", type=int, default=10)
    parser.add_argument("--no-positions", action="store_true", help="do not read the paper account")
    parser.add_argument("--from-cache", action="store_true", help="relabel the cached articles; no requests")
    args = parser.parse_args(argv)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    if args.from_cache:
        loaded = load_cache()
        clock_source = (
            "cache written by the fixed decoder"
            if loaded.get(FINNHUB_CLOCK_KEY) == FINNHUB_CLOCK_CORRECTED
            else "raw cache (predates 07e27fa), corrected on load"
        )
        payload, clock = correct_finnhub_clock(loaded)
    else:
        payload = asyncio.run(fetch_all(args))
        clock_source, clock = "fetched through the fixed decoder", {"corrected": 0, "disbelieved_future": 0}
    say(f"finnhub clock: {clock_source}; corrected {clock['corrected']}, "
        f"disbelieved (future) {clock['disbelieved_future']}")
    written, ingest = store_and_label(payload)
    out = tally(payload)
    summary = {
        "fetched_at": payload["fetched_at"],
        "sessions": payload["sessions"],
        "watch": {**payload["watch"], "size": len(payload["watch"]["symbols"]) - 1},
        "fetch": payload["fetch"],
        "finnhub_clock": {"source": clock_source, **clock},
        "ingest": ingest,
        "label_write": dataclasses.asdict(written),
        "measured": out,
        "step0": STEP0,
        "gate": {"threshold": GATE, "watch_dedup_mean": out["rules"]["watch"]["deduplicated"]["mean"]},
    }
    m4.write_summary(summary, SUMMARY)
    report(out, written, ingest)
    say(f"summary: {SUMMARY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
