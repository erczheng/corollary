"""Shared builders for the news and watch route tests. No network, no broker.

A test SPDR seed, a direct ``news_article`` writer (the ingest has its own
suite; these tests are about what the routes serve from whatever is stored),
and an asset directory put into an :class:`AssetDirectoryHolder` through its
real ``refresh`` -- so the holder's own refusals (empty list, nothing
optionable) still apply to what a test hands it.
"""

import asyncio
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.data.news.assets import AssetDirectoryHolder
from corollary.data.providers.interface import AssetDirectory, EquityAsset
from corollary.data.seeds import SPDR_SECTORS, SpdrHolding, SpdrSeed
from corollary.db.models import (
    NewsArticle,
    NewsArticleTicker,
    SentimentLabelRow,
    TickerTradeability,
    WatchSymbol,
)

#: Two holdings in XLK, one in every other fund -- every fund present, as
#: ``SpdrSeed`` requires. All twelve are leaders (fewer than five per fund).
_HOLDINGS: dict[str, tuple[tuple[str, str], ...]] = {
    "XLB": (("LIN", "5"),),
    "XLC": (("META", "5"),),
    "XLE": (("XOM", "5"),),
    "XLF": (("JPM", "5"),),
    "XLI": (("GE", "5"),),
    "XLK": (("NVDA", "10"), ("MSFT", "9")),
    "XLP": (("PG", "5"),),
    "XLRE": (("PLD", "5"),),
    "XLU": (("NEE", "5"),),
    "XLV": (("LLY", "5"),),
    "XLY": (("HD", "5"),),
}

SEED_AS_OF = date(2026, 9, 22)


def make_seed() -> SpdrSeed:
    rows = tuple(
        SpdrHolding(etf, SPDR_SECTORS[etf], symbol, Decimal(weight))
        for etf, held in _HOLDINGS.items()
        for symbol, weight in held
    )
    return SpdrSeed(as_of=SEED_AS_OF, rows=rows)


SEED_LEADERS: frozenset[str] = frozenset(
    symbol for held in _HOLDINGS.values() for symbol, _ in held
)


class _Source:
    def __init__(self, directory: AssetDirectory) -> None:
        self.directory = directory

    async def active_equities(self) -> AssetDirectory:
        return self.directory


def fill_directory(holder: AssetDirectoryHolder, symbols: Iterable[str]) -> None:
    """Hold a directory listing ``symbols``, every one optionable and tradable."""
    assets = tuple(
        EquityAsset(
            symbol=symbol,
            name=f"{symbol} Inc.",
            tradable=True,
            has_options=True,
            exchange="NASDAQ",
        )
        for symbol in sorted(set(symbols))
    )
    asyncio.run(holder.refresh(_Source(AssetDirectory(assets=assets))))


_FEED_FOR_VENDOR = {
    "alpaca": "alpaca_news",
    "finnhub": "finnhub_company",
    "massive": "massive_news",
}


def add_article(
    engine: Engine,
    *,
    vendor: str,
    vendor_id: str,
    headline: str,
    published_at: datetime,
    tickers: Sequence[str],
    publisher: str | None = "Benzinga",
    canonical_id: int | None = None,
    url: str | None = None,
) -> int:
    """Write one article and its tags exactly as given. Returns its id."""
    link = url or f"https://example.com/{vendor}/{vendor_id}"
    with Session(engine) as session:
        row = NewsArticle(
            vendor=vendor,
            vendor_id=vendor_id,
            feed=_FEED_FOR_VENDOR[vendor],
            canonical_id=canonical_id,
            url=link,
            url_key=link.lower(),
            headline=headline,
            headline_key=headline.lower(),
            summary=None,
            publisher=publisher,
            published_at=published_at,
            ingested_at=published_at,
        )
        session.add(row)
        session.flush()
        for ticker in tickers:
            session.add(NewsArticleTicker(article_id=row.id, ticker=ticker))
        session.commit()
        return row.id


def add_manual_watches(engine: Engine, tickers: Iterable[str], *, at: datetime) -> None:
    """Active manual watches written directly -- the cap tests' filler."""
    with Session(engine) as session:
        for ticker in tickers:
            session.add(WatchSymbol(ticker=ticker, added_at=at))
        session.commit()


def filler_symbols(count: int, *, exclude: Iterable[str] = ()) -> list[str]:
    """``count`` distinct well-formed symbols, none of them in ``exclude``."""
    taken = set(exclude)
    out: list[str] = []
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for first in letters:
        for second in letters:
            symbol = f"QZ{first}{second}"
            if symbol not in taken:
                out.append(symbol)
            if len(out) == count:
                return out
    raise AssertionError("ran out of filler symbols")


def add_label(
    engine: Engine,
    *,
    article_id: int,
    ticker: str,
    source: str,
    direction: str,
    rule_id: str | None = None,
    reasoning: str | None = None,
    labeled_at: datetime | None = None,
) -> None:
    """One ``sentiment_label`` row, written directly (the labeller has its own suite)."""
    tier = {"rules": "rules", "massive": "vendor"}[source]
    with Session(engine) as session:
        session.add(
            SentimentLabelRow(
                article_id=article_id,
                ticker=ticker,
                source=source,
                tier=tier,
                direction=direction,
                reasoning=reasoning,
                rule_id=rule_id if source == "rules" else None,
                labeled_at=labeled_at or datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc),
            )
        )
        session.commit()


def add_verdict(
    engine: Engine,
    *,
    ticker: str,
    session_date: date,
    passes: bool = True,
    avg_volume_20d: int | None = 2_500_000,
    last_close: Decimal | None = Decimal("41.20"),
    sessions_available: int = 20,
    failures: str = "",
    checked_at: datetime | None = None,
) -> None:
    """One ``ticker_tradeability`` row, as the cache would hold it."""
    with Session(engine) as session:
        session.add(
            TickerTradeability(
                ticker=ticker,
                session_date=session_date,
                has_options=True,
                standard_root=True,
                avg_volume_20d=avg_volume_20d,
                last_close=last_close,
                sessions_available=sessions_available,
                passes=passes,
                failures=failures if not passes else "",
                checked_at=checked_at or datetime(2026, 9, 26, 11, 30, tzinfo=timezone.utc),
            )
        )
        session.commit()
