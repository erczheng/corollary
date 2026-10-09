"""Phase 3 step 4 -- the news-volume measurements the step's *Done when* records.

    Step 4 records each feed's real articles per day, the share of Finnhub
    market-news rows with a non-empty ``related``, and every watch symbol that
    never returns Finnhub rows (*Not verified*).

Run by hand, never by the test suite, and **only through the launcher** so no
agent ever reads ``.env`` (rule 6)::

    uv run --env-file <path-to>/.env python scripts/measure_phase3_news.py
    uv run --env-file <path-to>/.env python scripts/measure_phase3_news.py --day 2026-09-25
    uv run --env-file <path-to>/.env python scripts/measure_phase3_news.py --no-positions

Safety properties, enforced by construction rather than intended:

* **The committed providers only.** Every request goes through
  ``FinnhubProvider``, ``AlpacaProvider``, ``MassiveProvider`` and
  ``AlpacaBroker.positions`` -- GET-only, paced by the shared per-host
  limiter, credentials scrubbed by the provider. No hand-rolled HTTP, no MCP
  (rule 3), no ``alpaca`` SDK import.
* **Read-only (rule 1).** The one broker call is ``positions()``.
* **Counts only.** No headline, summary, URL or key is printed or written.
  Error texts are the providers' (already scrubbed) messages, passed once
  more through :func:`_safe`, which blanks the value of every credential
  variable in the environment, and :func:`write_summary` refuses to write a
  file that still contains one.

Output: counts on stdout, and a JSON summary at
``.claude/scratch/4m/summary.json`` (gitignored).

**What "a day" means here.** A UTC calendar day, ``[00:00Z, 24:00Z)``. The
default measures the last complete UTC day *and*, if that was a weekend, the
last complete UTC weekday too: a Sunday's volume is not the number a
retention window is sized against.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Protocol

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from corollary.data.news.article import (  # noqa: E402
    NewsAccessDenied,
    NewsArticle,
    NewsProviderError,
)
from corollary.data.providers.alpaca import AlpacaNews  # noqa: E402
from corollary.data.providers.finnhub import (  # noqa: E402
    COMPANY_NEWS_CAP_THRESHOLD,
    CompanyNews,
    MarketNews,
)
from corollary.data.providers.massive import MassiveNews  # noqa: E402
from corollary.data.providers.sec import parse_nport_document  # noqa: E402
from corollary.instruments import parse_occ_symbol  # noqa: E402

SEC_FIXTURES: Final = REPO_ROOT / "tests" / "fixtures" / "sec"
DEFAULT_OUT: Final = REPO_ROOT / ".claude" / "scratch" / "4m" / "summary.json"

#: The eleven SPDR sector funds whose N-PORT filings are committed fixtures.
#: Spelled out here rather than imported from ``corollary.data.seeds``, which
#: is being rewritten concurrently; the fixture files are the source.
SPDR_FUNDS: Final[tuple[str, ...]] = (
    "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
)  # fmt: skip
LEADERS_PER_FUND: Final = 5

#: **Hand-mapped, not surveyed.** ``cusip_survey.json`` covers every EC line
#: that *has* a CUSIP; these 29 foreign-domiciled lines carry an ISIN and no
#: CUSIP in the committed N-PORT fixtures, so the survey never saw them. One
#: of them is Linde, 14% of XLB -- dropping them would drop a leader. Keyed by
#: ISIN, so a changed line fails loudly instead of inheriting a symbol. The
#: seed proper (step 4's ``corollary/data/seeds``) needs a surveyed answer for
#: these; this table is only good enough to size a news measurement.
ISIN_SYMBOLS: Final[Mapping[str, str]] = {
    "JE00BV7DQ550": "AMCR",  # Amcor PLC
    "IE0001827041": "CRH",  # CRH PLC
    "IE000S9YS762": "LIN",  # Linde PLC
    "IE00028FXN24": "SW",  # Smurfit Westrock PLC
    "NL0009434992": "LYB",  # LyondellBasell Industries NV
    "IE00BLP1HW54": "AON",  # Aon PLC
    "BMG0450A1053": "ACGL",  # Arch Capital Group Ltd
    "BMG3223R1088": "EG",  # Everest Group Ltd
    "BMG491BT1088": "IVZ",  # Invesco Ltd
    "IE00BDB6Q211": "WTW",  # Willis Towers Watson PLC
    "CH0044328745": "CB",  # Chubb Ltd
    "IE00BFRT3W74": "ALLE",  # Allegion plc
    "IE00B8KQN827": "ETN",  # Eaton Corp PLC
    "IE00BY7QL619": "JCI",  # Johnson Controls International plc
    "IE00BLS09M33": "PNR",  # Pentair PLC
    "IE00BK9ZQ967": "TT",  # Trane Technologies PLC
    "IE00B4BNMY34": "ACN",  # Accenture PLC
    "IE00BKVD2N49": "STX",  # Seagate Technology Holdings PLC
    "IE000IVNQZ81": "TEL",  # TE Connectivity PLC
    "NL0009538784": "NXPI",  # NXP Semiconductors NV
    "SG9999000020": "FLEX",  # Flex Ltd
    "CH1300646267": "BG",  # Bunge Global SA
    "IE00BTN1Y115": "MDT",  # Medtronic PLC
    "IE00BFY8C754": "STE",  # STERIS PLC
    "BMG2004J1036": "CCL",  # Carnival Corp Ltd
    "JE00BTDN8H13": "APTV",  # Aptiv PLC
    "BMG667211046": "NCLH",  # Norwegian Cruise Line Holdings Ltd
    "CH0114405324": "GRMN",  # Garmin Ltd
    "LR0008862868": "RCL",  # Royal Caribbean Cruises Ltd
}

#: Environment variables whose values must never leave this process.
SECRET_ENV: Final[tuple[str, ...]] = (
    "FINNHUB_API_KEY",
    "MASSIVE_API_KEY",
    "ALPACA_PAPER_API_KEY",
    "ALPACA_PAPER_SECRET_KEY",
    "ALPACA_LIVE_API_KEY",
    "ALPACA_LIVE_SECRET_KEY",
    "FRED_API_KEY",
    "SEC_USER_AGENT",
    "DISCORD_WEBHOOK_URL",
)

#: Seconds between two Finnhub calls, on top of the shared 60/min limiter:
#: the limiter's bucket starts full, so without this the first sixty calls
#: could land inside a second, and Finnhub also caps at 30/s.
FINNHUB_SPACING: Final = 1.1
#: A 429 from a *second* process sharing the key is waited out once.
RETRY_AFTER_429: Final = 65.0
#: Enough pages for ~5,000 articles at Alpaca's 50 per page.
ALPACA_MAX_PAGES: Final = 100
#: Massive calls to reach past the last measured day (each is up to 3 pages
#: of 1,000 at 5/min).
MASSIVE_MAX_CALLS: Final = 5
WINDOW_DAYS: Final = 30

#: The spec's per-day estimates (upper end of any stated range). A measured
#: day more than twice one of these is flagged: the spec revisits the
#: retention window if so.
ESTIMATES: Final[Mapping[str, int]] = {
    "finnhub_watch": 1000,
    "alpaca": 500,
    "massive": 190,
    "finnhub_market": 100,
    "total": 2000,
}
ESTIMATE_NOTES: Final[Mapping[str, str]] = {
    "finnhub_watch": "~1,000/day",
    "alpaca": "200-500/day, whole untickered feed",
    "massive": "~190/day",
    "finnhub_market": "~100/day",
    "total": "~1,500-2,000/day",
}


# --------------------------------------------------------------------------
# Output hygiene
# --------------------------------------------------------------------------


def _secret_values() -> list[str]:
    return [v for name in SECRET_ENV if len(v := os.environ.get(name, "").strip()) >= 6]


def _safe(text: str, limit: int = 300) -> str:
    """Blank every credential value held in the environment, then bound it."""
    for secret in _secret_values():
        text = text.replace(secret, "<redacted>")
    return text[:limit]


def say(message: str) -> None:
    """The only print in this file. Callers pass counts and symbols, never article text."""
    print(_safe(message, limit=10_000), flush=True)


def write_summary(summary: Mapping[str, Any], path: Path) -> None:
    """Write the summary as JSON, refusing if a credential value survived."""
    text = json.dumps(summary, indent=2, sort_keys=True)
    if any(secret in text for secret in _secret_values()):
        raise RuntimeError("the summary contains a credential value; refusing to write it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def _error(exc: BaseException) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": _safe(str(exc))}


# --------------------------------------------------------------------------
# The watch universe
# --------------------------------------------------------------------------


def sector_leaders(
    sec_dir: Path = SEC_FIXTURES, per_fund: int = LEADERS_PER_FUND
) -> dict[str, tuple[str, ...]]:
    """Each SPDR fund's top ``per_fund`` common-equity holdings, heaviest first.

    From the committed N-PORT ``primary_doc.xml`` fixtures (``EC`` lines
    only), with CUSIPs mapped to symbols by ``cusip_survey.json`` and
    CUSIP-less lines by :data:`ISIN_SYMBOLS`. A symbol carried on two lines of
    one fund has the weights summed. Ties break by symbol. An ``EC`` line
    neither maps raises: a silently dropped holding could be a leader.
    """
    survey = json.loads((sec_dir / "cusip_survey.json").read_text(encoding="utf-8"))
    symbol_of: dict[str, str] = {
        row["cusip"]: row["symbol"]
        for row in survey["rows"]
        if row.get("status_code") == 200 and row.get("symbol")
    }
    out: dict[str, tuple[str, ...]] = {}
    for etf in SPDR_FUNDS:
        document = parse_nport_document((sec_dir / f"nport_{etf}_primary_doc.xml").read_bytes())
        weights: dict[str, Decimal] = {}
        for holding in document.equity:
            if holding.cusip is not None:
                symbol = symbol_of.get(holding.cusip)
            else:
                symbol = ISIN_SYMBOLS.get(holding.isin or "")
            if symbol is None:
                raise ValueError(
                    f"{etf}: EC holding {holding.name!r} (CUSIP {holding.cusip}, "
                    f"ISIN {holding.isin}) maps to no symbol"
                )
            weights[symbol] = weights.get(symbol, Decimal(0)) + holding.pct_val
        ranked = sorted(weights.items(), key=lambda kv: (kv[1].copy_negate(), kv[0]))
        out[etf] = tuple(symbol for symbol, _ in ranked[:per_fund])
    return out


def position_underlyings(held: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    """``(symbol, asset_class)`` pairs to the distinct underlyings, sorted.

    An option is read by its OCC root. An adjusted root (``AAPL1``) is kept
    as the root: its news is filed under the unadjusted name, which the
    report flags rather than guesses at.
    """
    names: set[str] = set()
    for symbol, asset_class in held:
        if asset_class == "us_option":
            names.add(parse_occ_symbol(symbol).root)
        else:
            names.add(symbol.strip().upper())
    return tuple(sorted(names))


@dataclass(frozen=True, slots=True)
class WatchUniverse:
    markets: tuple[str, ...]
    leaders: Mapping[str, tuple[str, ...]]
    #: ``None`` when the positions were not read.
    positions: tuple[str, ...] | None
    positions_note: str

    def _leader_set(self) -> set[str]:
        return {s for top in self.leaders.values() for s in top}

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.markets) | self._leader_set() | set(self.positions or ())))

    def breakdown(self) -> dict[str, int]:
        markets = set(self.markets)
        leaders = self._leader_set()
        positions = set(self.positions or ())
        return {
            "markets": len(markets),
            "leaders_distinct": len(leaders),
            "positions": len(positions),
            "leaders_not_in_markets": len(leaders - markets),
            "positions_not_elsewhere": len(positions - markets - leaders),
            "W": len(self.symbols()),
        }

    def as_json(self) -> dict[str, Any]:
        return {
            "breakdown": self.breakdown(),
            "markets": list(self.markets),
            "leaders": {etf: list(top) for etf, top in self.leaders.items()},
            "leaders_hand_mapped_by_isin": sorted(
                self._leader_set() & set(ISIN_SYMBOLS.values())
            ),
            "positions": None if self.positions is None else list(self.positions),
            "positions_note": self.positions_note,
            "symbols": list(self.symbols()),
        }


# --------------------------------------------------------------------------
# Days
# --------------------------------------------------------------------------


def default_days(now: datetime) -> tuple[date, ...]:
    """The last complete UTC day, then the last complete UTC weekday if different."""
    yesterday = now.astimezone(timezone.utc).date() - timedelta(days=1)
    days = [yesterday]
    if yesterday.weekday() >= 5:
        weekday = yesterday
        while weekday.weekday() >= 5:
            weekday -= timedelta(days=1)
        days.append(weekday)
    return tuple(days)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _in_day(article: NewsArticle, day: date) -> bool:
    start, end = day_bounds(day)
    return start <= article.published_at < end


def over_estimate(per_feed: Mapping[str, int]) -> list[str]:
    """Feeds whose count is more than twice the spec's estimate, in estimate order."""
    return [
        feed
        for feed, estimate in ESTIMATES.items()
        if feed in per_feed and per_feed[feed] > 2 * estimate
    ]


# --------------------------------------------------------------------------
# The sources, as the measurement sees them
# --------------------------------------------------------------------------


class FinnhubNewsSource(Protocol):
    async def company_news(self, symbol: str, from_date: date, to_date: date) -> CompanyNews: ...

    async def market_news(self, min_id: int | None) -> MarketNews: ...


class AlpacaNewsSource(Protocol):
    async def news(
        self, *, start: datetime, end: datetime | None = ..., max_pages: int = ...
    ) -> AlpacaNews: ...


class MassiveNewsSource(Protocol):
    async def news_since(self, published_after: datetime) -> MassiveNews: ...


Pause = Callable[[float], Awaitable[None]]


async def _finnhub_call(
    finnhub: FinnhubNewsSource, symbol: str, from_date: date, to_date: date, pause: Pause
) -> CompanyNews:
    """One paced ``company_news`` call; a 429 is waited out once, then re-raised."""
    await pause(FINNHUB_SPACING)
    try:
        return await finnhub.company_news(symbol, from_date, to_date)
    except NewsAccessDenied:
        raise
    except NewsProviderError as exc:
        if "429" not in str(exc):
            raise
        say(f"  finnhub 429 -- waiting {RETRY_AFTER_429:.0f}s and retrying once")
        await pause(RETRY_AFTER_429)
        return await finnhub.company_news(symbol, from_date, to_date)


# --------------------------------------------------------------------------
# The measurement
# --------------------------------------------------------------------------


async def _measure_finnhub_watch(
    finnhub: FinnhubNewsSource,
    symbols: Sequence[str],
    days: Sequence[date],
    pause: Pause,
) -> dict[str, Any]:
    result: dict[str, Any] = {"days": {}, "window_30d": None, "error": None}
    seen_rows: set[str] = set()
    try:
        for day in days:
            per_symbol: dict[str, int] = {}
            ids: set[str] = set()
            at_cap: list[str] = []
            errors: dict[str, dict[str, str]] = {}
            published_in_day = 0
            for symbol in symbols:
                try:
                    answer = await _finnhub_call(finnhub, symbol, day, day, pause)
                except NewsAccessDenied:
                    raise
                except NewsProviderError as exc:
                    errors[symbol] = _error(exc)
                    continue
                per_symbol[symbol] = len(answer.articles)
                ids.update(a.vendor_id for a in answer.articles)
                published_in_day += sum(1 for a in answer.articles if _in_day(a, day))
                if answer.articles:
                    seen_rows.add(symbol)
                if (
                    len(answer.articles) >= COMPANY_NEWS_CAP_THRESHOLD
                    or answer.overflow_date is not None
                ):
                    at_cap.append(symbol)
            total = sum(per_symbol.values())
            result["days"][day.isoformat()] = {
                "sum_over_symbols": total,
                "distinct_articles": len(ids),
                "published_in_day": published_in_day,
                "per_symbol": per_symbol,
                "at_cap": at_cap,
                "cap_threshold": COMPANY_NEWS_CAP_THRESHOLD,
                "errors": errors,
            }
            say(
                f"finnhub watch {day}: {total} rows over {len(per_symbol)} symbols "
                f"({len(ids)} distinct, {published_in_day} published in the UTC day); "
                f"at cap: {at_cap or 'none'}; errors: {sorted(errors) or 'none'}"
            )

        # Only a symbol with nothing on any measured day needs the 30-day call.
        latest = max(days)
        window_from = latest - timedelta(days=WINDOW_DAYS - 1)
        window: dict[str, int] = {}
        window_errors: dict[str, dict[str, str]] = {}
        for symbol in symbols:
            if symbol in seen_rows:
                continue
            try:
                answer = await _finnhub_call(finnhub, symbol, window_from, latest, pause)
            except NewsAccessDenied:
                raise
            except NewsProviderError as exc:
                window_errors[symbol] = _error(exc)
                continue
            window[symbol] = len(answer.articles)
        zero = sorted(s for s, n in window.items() if n == 0)
        result["window_30d"] = {
            "from": window_from.isoformat(),
            "to": latest.isoformat(),
            "queried": sorted(window),
            "rows": window,
            "zero_rows": zero,
            "errors": window_errors,
        }
        say(
            f"finnhub watch {window_from}..{latest}: {len(window)} symbols had no rows on "
            f"the measured days; zero rows in {WINDOW_DAYS} days: {zero or 'none'}"
        )
    except NewsAccessDenied as exc:
        result["error"] = _error(exc)
        say(f"finnhub company news refused ({type(exc).__name__}); watch tier stopped")
    return result


async def _measure_alpaca(alpaca: AlpacaNewsSource, days: Sequence[date]) -> dict[str, Any]:
    result: dict[str, Any] = {"days": {}, "error": None}
    for day in days:
        start, end = day_bounds(day)
        try:
            answer = await alpaca.news(
                start=start, end=end - timedelta(seconds=1), max_pages=ALPACA_MAX_PAGES
            )
        except NewsProviderError as exc:
            result["error"] = _error(exc)
            say(f"alpaca news refused or failed ({type(exc).__name__}); stopped")
            break
        in_day = sum(1 for a in answer.articles if _in_day(a, day))
        untagged = sum(1 for a in answer.articles if a.is_market)
        result["days"][day.isoformat()] = {
            "returned": len(answer.articles),
            "published_in_day": in_day,
            "tagged_to_nothing": untagged,
            "complete": answer.complete,
            "pages": answer.pages,
            "skipped": answer.skipped,
        }
        say(
            f"alpaca {day}: {len(answer.articles)} articles updated in the day "
            f"({in_day} first published in it, {untagged} untagged), "
            f"{answer.pages} pages, complete={answer.complete}"
        )
    return result


async def _measure_massive(massive: MassiveNewsSource, days: Sequence[date]) -> dict[str, Any]:
    result: dict[str, Any] = {"days": {}, "error": None, "calls": 0, "pages": 0, "complete": False}
    after = day_bounds(min(days))[0] - timedelta(seconds=1)
    horizon = day_bounds(max(days))[1]
    articles: dict[str, NewsArticle] = {}
    try:
        for _ in range(MASSIVE_MAX_CALLS):
            answer = await massive.news_since(after)
            result["calls"] += 1
            result["pages"] += answer.pages
            for article in answer.articles:
                articles.setdefault(article.vendor_id, article)
            newest = max((a.published_at for a in answer.articles), default=None)
            if answer.complete or (newest is not None and newest >= horizon):
                result["complete"] = True
                break
            if answer.cursor <= after:
                break
            after = answer.cursor
    except NewsProviderError as exc:
        result["error"] = _error(exc)
        say(f"massive news refused or failed ({type(exc).__name__}); stopped")
        return result
    for day in days:
        in_day = sum(1 for a in articles.values() if _in_day(a, day))
        result["days"][day.isoformat()] = {"published_in_day": in_day}
        say(f"massive {day}: {in_day} articles published in the day")
    say(f"massive: {result['calls']} calls, {result['pages']} pages, reached past the last day={result['complete']}")
    return result


async def _measure_finnhub_market(finnhub: FinnhubNewsSource, days: Sequence[date]) -> dict[str, Any]:
    try:
        answer = await finnhub.market_news(None)
    except NewsProviderError as exc:
        say(f"finnhub market news refused or failed ({type(exc).__name__})")
        return {"error": _error(exc)}
    rows = answer.articles
    if not rows:
        return {"error": None, "rows": 0}
    oldest = min(a.published_at for a in rows)
    newest = max(a.published_at for a in rows)
    span_hours = (newest - oldest).total_seconds() / 3600
    by_date = Counter(a.published_at.date().isoformat() for a in rows)
    full_days = sorted(
        d.isoformat()
        for d in {a.published_at.date() for a in rows}
        | set(days)
        if oldest <= day_bounds(d)[0] and day_bounds(d)[1] <= newest
    )
    related = sum(1 for a in rows if a.tickers)
    per_hour = len(rows) / span_hours if span_hours > 0 else None
    result: dict[str, Any] = {
        "error": None,
        "rows": len(rows),
        "skipped": answer.skipped,
        "oldest": oldest.isoformat(),
        "newest": newest.isoformat(),
        "span_hours": round(span_hours, 2),
        "by_utc_date": dict(sorted(by_date.items())),
        "full_days": full_days,
        "rows_per_hour": None if per_hour is None else round(per_hour, 2),
        "extrapolated_per_day": None if per_hour is None else round(per_hour * 24),
        "related_nonempty": related,
        "related_share_pct": round(100 * related / len(rows), 1),
        "related_note": (
            "non-empty after the provider's normalisation (split on ',', "
            "stripped, blanks dropped)"
        ),
    }
    say(
        f"finnhub market news: {len(rows)} rows spanning {span_hours:.1f}h "
        f"({oldest:%Y-%m-%d %H:%M}Z..{newest:%Y-%m-%d %H:%M}Z); full UTC days: "
        f"{full_days or 'none'}; related non-empty {related}/{len(rows)} "
        f"({result['related_share_pct']}%)"
    )
    return result


def _market_per_day(market: Mapping[str, Any], day: date) -> tuple[int | None, str]:
    key = day.isoformat()
    if key in market.get("full_days", []):
        return int(market["by_utc_date"].get(key, 0)), "counted over a full UTC day"
    extrapolated = market.get("extrapolated_per_day")
    if extrapolated is None:
        return None, "unmeasured"
    return int(extrapolated), "extrapolated from rows/hour (the page does not span this day)"


async def measure(
    *,
    finnhub: FinnhubNewsSource,
    alpaca: AlpacaNewsSource | None,
    massive: MassiveNewsSource | None,
    watch: WatchUniverse,
    days: Sequence[date],
    now: datetime,
    pause: Pause = asyncio.sleep,
) -> dict[str, Any]:
    """Every measurement, as one JSON-ready summary. Prints counts as it goes."""
    symbols = watch.symbols()
    say(f"watch universe W={len(symbols)}: {watch.breakdown()}; {watch.positions_note}")
    say(f"days measured (UTC): {[d.isoformat() for d in days]}")

    summary: dict[str, Any] = {
        "measured_at": now.astimezone(timezone.utc).isoformat(),
        "days": [d.isoformat() for d in days],
        "watch": watch.as_json(),
    }
    summary["finnhub_market"] = await _measure_finnhub_market(finnhub, days)
    summary["alpaca"] = (
        await _measure_alpaca(alpaca, days)
        if alpaca is not None
        else {"days": {}, "error": {"type": "Unavailable", "message": "no Alpaca provider"}}
    )
    summary["massive"] = (
        await _measure_massive(massive, days)
        if massive is not None
        else {"days": {}, "error": {"type": "Unavailable", "message": "MASSIVE_API_KEY not set"}}
    )
    summary["finnhub_watch"] = await _measure_finnhub_watch(finnhub, symbols, days, pause)

    per_day: dict[str, dict[str, Any]] = {}
    flags: list[str] = []
    for day in days:
        key = day.isoformat()
        counts: dict[str, int] = {}
        fw = summary["finnhub_watch"]["days"].get(key)
        if fw is not None:
            counts["finnhub_watch"] = fw["sum_over_symbols"]
        al = summary["alpaca"]["days"].get(key)
        if al is not None:
            counts["alpaca"] = al["returned"]
        ms = summary["massive"]["days"].get(key)
        if ms is not None:
            counts["massive"] = ms["published_in_day"]
        market_note = "unmeasured"
        if summary["finnhub_market"].get("error") is None and summary["finnhub_market"].get("rows"):
            market, market_note = _market_per_day(summary["finnhub_market"], day)
            if market is not None:
                counts["finnhub_market"] = market
        counts["total"] = sum(counts.values())
        day_flags = over_estimate(counts)
        flags.extend(f for f in day_flags if f not in flags)
        per_day[key] = {"counts": counts, "finnhub_market_basis": market_note, "over_2x": day_flags}
        say(f"per-day {key}: {counts}; over 2x estimate: {day_flags or 'none'}")

    summary["per_day"] = per_day
    summary["estimates"] = {
        feed: {"estimate": est, "note": ESTIMATE_NOTES[feed]} for feed, est in ESTIMATES.items()
    }
    summary["flags"] = [f for f in ESTIMATES if f in flags]
    return summary


# --------------------------------------------------------------------------
# The live run
# --------------------------------------------------------------------------


async def _read_positions() -> tuple[tuple[str, ...] | None, str]:
    from corollary.engine.execution.alpaca import AlpacaBroker

    try:
        async with AlpacaBroker.from_env() as broker:
            if not broker.is_paper:
                return None, "positions skipped: the broker is not paper"
            held = await broker.positions()
    except Exception as exc:  # noqa: BLE001 -- a measurement, not a decision
        return None, f"positions skipped: {type(exc).__name__}: {_safe(str(exc), 160)}"
    underlyings = position_underlyings((p.symbol, p.asset_class) for p in held)
    return underlyings, f"read read-only from the paper account ({len(held)} positions)"


async def _main(args: argparse.Namespace) -> int:
    from corollary.api.routes.markets import UNIVERSE_SYMBOLS
    from corollary.data.providers.alpaca import AlpacaProvider, CredentialsError, FeedConfigError
    from corollary.data.providers.finnhub import FinnhubProvider
    from corollary.data.providers.massive import MassiveProvider

    now = datetime.now(timezone.utc)
    days = tuple(date.fromisoformat(d) for d in args.day) if args.day else default_days(now)
    if args.no_positions:
        positions: tuple[str, ...] | None = None
        note = "positions skipped: --no-positions"
    else:
        positions, note = await _read_positions()
    watch = WatchUniverse(
        markets=tuple(UNIVERSE_SYMBOLS),
        leaders=sector_leaders(),
        positions=positions,
        positions_note=note,
    )

    alpaca: AlpacaProvider | None
    try:
        alpaca = AlpacaProvider.from_env()
    except (CredentialsError, FeedConfigError) as exc:
        say(f"alpaca provider unavailable: {type(exc).__name__}")
        alpaca = None
    massive = MassiveProvider.available_from_env()
    finnhub = FinnhubProvider.from_env()
    try:
        summary = await measure(
            finnhub=finnhub, alpaca=alpaca, massive=massive, watch=watch, days=days, now=now
        )
    finally:
        await finnhub.aclose()
        if alpaca is not None:
            await alpaca.aclose()
        if massive is not None:
            await massive.aclose()
    write_summary(summary, args.out)
    say(f"summary written to {args.out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--day", action="append", help="a UTC day to measure (repeatable)")
    parser.add_argument("--no-positions", action="store_true", help="do not read the paper account")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
