"""The Phase 3 step 4 news-volume measurement, offline.

The script is run by hand against live vendors; these tests make no network
call. They pin the three things its numbers rest on: the sector leaders come
from the committed N-PORT fixtures (not from memory, the probe's
approximation), the summary has the shape the spec's *Done when* reads, and
nothing the script prints or writes carries a headline, a URL or a key.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from corollary.data.news.article import NewsAccessDenied, NewsArticle, NewsFeed
from corollary.data.providers.alpaca import AlpacaNews
from corollary.data.providers.finnhub import CompanyNews, MarketNews
from corollary.data.providers.massive import MassiveNews

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "measure_phase3_news.py"
SEC_FIXTURES = REPO / "tests" / "fixtures" / "sec"
FAKE_KEY = "Fk3yMeasureAbCdEf1234567890"
HEADLINE = "Zyzzyva headline that must never be printed"
URL_PATH = "never-printed-path-7731"

DAY = date(2026, 9, 25)
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _load(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("measure_phase3_news", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "measure_phase3_news", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def m(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    for name in ("FINNHUB_API_KEY", "MASSIVE_API_KEY", "ALPACA_PAPER_API_KEY"):
        monkeypatch.setenv(name, FAKE_KEY)
    return _load(monkeypatch)


def _article(feed: NewsFeed, n: int, at: datetime, tickers: tuple[str, ...] = ()) -> NewsArticle:
    vendor = {"alpaca_news": "alpaca", "massive_news": "massive"}.get(feed.value, "finnhub")
    return NewsArticle(
        vendor=vendor,
        vendor_id=f"{feed.value}-{n}",
        feed=feed,
        url=f"https://example.test/{URL_PATH}/{n}",
        headline=f"{HEADLINE} {n}",
        summary=f"{HEADLINE} summary",
        publisher="Example",
        published_at=at,
        tickers=tickers,
    )


def _noon(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)


class FakeFinnhub:
    """AAPL has three articles a day (one shared with MSFT); ZZZZ never has any."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, date, date]] = []

    async def company_news(self, symbol: str, from_date: date, to_date: date) -> CompanyNews:
        self.calls.append((symbol, from_date, to_date))
        rows: tuple[NewsArticle, ...] = ()
        if symbol == "AAPL":
            rows = tuple(
                _article(NewsFeed.FINNHUB_COMPANY, i, _noon(to_date), (symbol,)) for i in range(3)
            )
        elif symbol == "MSFT":
            rows = (_article(NewsFeed.FINNHUB_COMPANY, 0, _noon(to_date), (symbol,)),)
        return CompanyNews(symbol, from_date, to_date, rows, False, None, 0)

    async def market_news(self, min_id: int | None) -> MarketNews:
        # 48 rows, one every hour, newest at NOW - 1h: spans all of 09-26 and 09-27.
        rows = tuple(
            _article(
                NewsFeed.FINNHUB_MARKET,
                i,
                NOW - timedelta(hours=1 + i),
                ("AAPL",) if i % 4 == 0 else (),
            )
            for i in range(48)
        )
        return MarketNews(rows, 48, 0)


class FakeAlpaca:
    async def news(self, *, start: datetime, end: datetime | None = None, max_pages: int = 10) -> AlpacaNews:
        assert end is not None
        rows = tuple(_article(NewsFeed.ALPACA_NEWS, i, start + timedelta(minutes=i)) for i in range(7))
        return AlpacaNews(rows, end, True, 1, 0)


class DeniedAlpaca:
    async def news(self, *, start: datetime, end: datetime | None = None, max_pages: int = 10) -> AlpacaNews:
        raise NewsAccessDenied(f"GET /v1beta1/news returned 403 for key {FAKE_KEY}")


class FakeMassive:
    async def news_since(self, published_after: datetime) -> MassiveNews:
        rows = tuple(
            _article(NewsFeed.MASSIVE_NEWS, i, published_after + timedelta(hours=i + 1))
            for i in range(30)
        )
        return MassiveNews(rows, rows[-1].published_at, True, 1, 0)


async def _nothing(_: float) -> None:
    return None


def _watch(m: ModuleType) -> Any:
    return m.WatchUniverse(
        markets=("AAPL", "MSFT"),
        leaders={"XLK": ("AAPL", "ZZZZ")},
        positions=("MSFT",),
        positions_note="read from the paper account",
    )


# --------------------------------------------------------------------------
# The watch universe
# --------------------------------------------------------------------------


def test_sector_leaders_come_from_the_committed_nport_fixtures(m: ModuleType) -> None:
    leaders = m.sector_leaders(SEC_FIXTURES)
    assert leaders["XLK"] == ("NVDA", "AAPL", "MSFT", "MU", "AVGO")
    assert set(leaders) == {
        "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
    }  # fmt: skip
    assert all(len(top) == 5 for top in leaders.values())


def test_a_cusip_less_leader_is_mapped_by_isin_not_dropped(m: ModuleType) -> None:
    # Linde carries an ISIN and no CUSIP, and is 14% of XLB.
    leaders = m.sector_leaders(SEC_FIXTURES)
    assert leaders["XLB"][0] == "LIN"
    watch = m.WatchUniverse(markets=(), leaders=leaders, positions=None, positions_note="")
    assert "LIN" in watch.as_json()["leaders_hand_mapped_by_isin"]


def test_sector_leaders_are_deterministic(m: ModuleType) -> None:
    assert m.sector_leaders(SEC_FIXTURES) == m.sector_leaders(SEC_FIXTURES)


def test_watch_universe_is_the_sorted_union_with_a_breakdown(m: ModuleType) -> None:
    watch = _watch(m)
    assert watch.symbols() == ("AAPL", "MSFT", "ZZZZ")
    assert watch.breakdown() == {
        "markets": 2,
        "leaders_distinct": 2,
        "positions": 1,
        "leaders_not_in_markets": 1,
        "positions_not_elsewhere": 0,
        "W": 3,
    }


def test_position_underlyings_read_an_option_by_its_root(m: ModuleType) -> None:
    held = [("AAPL261218C00250000", "us_option"), ("SPY", "us_equity")]
    assert m.position_underlyings(held) == ("AAPL", "SPY")


# --------------------------------------------------------------------------
# The measurement
# --------------------------------------------------------------------------


async def _run(m: ModuleType, tmp_path: Path, alpaca: Any = None) -> dict[str, Any]:
    out = tmp_path / "summary.json"
    summary: dict[str, Any] = await m.measure(
        finnhub=FakeFinnhub(),
        alpaca=alpaca if alpaca is not None else FakeAlpaca(),
        massive=FakeMassive(),
        watch=_watch(m),
        days=(DAY,),
        now=NOW,
        pause=_nothing,
    )
    m.write_summary(summary, out)
    return summary


@pytest.mark.asyncio
async def test_summary_carries_every_number_the_spec_asks_for(m: ModuleType, tmp_path: Path) -> None:
    summary = await _run(m, tmp_path)
    assert summary["watch"]["breakdown"]["W"] == 3

    day = summary["finnhub_watch"]["days"][DAY.isoformat()]
    assert day["sum_over_symbols"] == 4
    assert day["distinct_articles"] == 3
    assert day["per_symbol"] == {"AAPL": 3, "MSFT": 1, "ZZZZ": 0}
    assert day["at_cap"] == []
    # ZZZZ had nothing on the measured day, so only it costs a 30-day call.
    assert summary["finnhub_watch"]["window_30d"]["zero_rows"] == ["ZZZZ"]

    assert summary["alpaca"]["days"][DAY.isoformat()]["returned"] == 7
    assert summary["massive"]["days"][DAY.isoformat()]["published_in_day"] == 24

    market = summary["finnhub_market"]
    assert market["rows"] == 48
    assert market["related_nonempty"] == 12
    assert market["related_share_pct"] == 25.0
    assert market["by_utc_date"]["2026-09-27"] == 24
    assert market["full_days"] == ["2026-09-27"]

    assert set(summary["estimates"]) == {
        "finnhub_watch", "alpaca", "massive", "finnhub_market", "total",
    }  # fmt: skip
    assert summary["flags"] == []


@pytest.mark.asyncio
async def test_summary_file_round_trips_as_json(m: ModuleType, tmp_path: Path) -> None:
    summary = await _run(m, tmp_path)
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8")) == summary


@pytest.mark.asyncio
async def test_nothing_printed_or_written_carries_a_headline_url_or_key(
    m: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await _run(m, tmp_path, alpaca=DeniedAlpaca())
    written = (tmp_path / "summary.json").read_text(encoding="utf-8")
    printed = capsys.readouterr()
    for text in (printed.out, printed.err, written):
        assert "Zyzzyva" not in text
        assert URL_PATH not in text
        assert "example.test" not in text
        assert FAKE_KEY not in text


@pytest.mark.asyncio
async def test_a_refused_feed_stops_that_part_and_the_rest_is_measured(
    m: ModuleType, tmp_path: Path
) -> None:
    summary = await _run(m, tmp_path, alpaca=DeniedAlpaca())
    assert summary["alpaca"]["error"]["type"] == "NewsAccessDenied"
    assert summary["alpaca"]["days"] == {}
    assert summary["massive"]["days"][DAY.isoformat()]["published_in_day"] == 24


def test_a_feed_over_twice_its_estimate_is_flagged(m: ModuleType) -> None:
    flags = m.over_estimate({"finnhub_watch": 2001, "alpaca": 1000, "massive": 381, "finnhub_market": 50})
    assert flags == ["finnhub_watch", "massive"]


def test_default_days_are_the_last_complete_utc_day_and_weekday(m: ModuleType) -> None:
    # Monday 2026-09-28: yesterday is a Sunday, so Friday is measured as well.
    assert m.default_days(NOW) == (date(2026, 9, 27), date(2026, 9, 25))
    # Wednesday: yesterday is itself a weekday.
    assert m.default_days(datetime(2026, 9, 30, 1, tzinfo=timezone.utc)) == (date(2026, 9, 29),)


def test_the_summary_writer_refuses_a_surviving_credential(
    m: ModuleType, tmp_path: Path
) -> None:
    """The last guard: a field that bypassed the redactor is refused whole,
    and nothing reaches the disk."""
    path = tmp_path / "summary.json"
    with pytest.raises(RuntimeError, match="credential"):
        m.write_summary({"x": FAKE_KEY}, path)
    assert not path.exists()


@pytest.mark.parametrize("name", [
    "ALPACA_PAPER_SECRET_KEY", "FRED_API_KEY", "SEC_USER_AGENT", "DISCORD_WEBHOOK_URL",
])
def test_every_secret_variable_is_redacted(
    m: ModuleType, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    value = f"not-a-real-{name.lower()}-value"
    monkeypatch.setenv(name, value)
    assert name in m.SECRET_ENV
    assert value not in m._safe(f"error text quoting {value} verbatim")
