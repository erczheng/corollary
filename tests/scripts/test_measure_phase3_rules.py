"""The unit-5.4 rules re-measurement's counting, offline: no request is made.

Pins what the gate figure means: a story two vendors carry is one
deduplicated label (decision 3), a label on a name outside the watch universe
counts in total and off-watch but never toward the watch figure, Massive's
vendor labels never count as rules labels, and a label outside the measured
sessions is not counted at all.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import measure_phase3_rules as m  # noqa: E402
from probe_phase3 import completed_sessions  # noqa: E402

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def _article(vendor: str, vid: str, feed: str, url: str, headline: str, at: datetime,
             tickers: list[str], insights: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"vendor": vendor, "vendor_id": vid, "feed": feed, "url": url, "headline": headline,
            "publisher": None, "published_at": at.isoformat(), "tickers": tickers, "insights": insights or []}


def _asset(symbol: str, name: str) -> dict[str, Any]:
    return {"symbol": symbol, "name": name, "tradable": True, "has_options": True, "exchange": "NYSE"}


@pytest.fixture
def payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(m, "DB_PATH", tmp_path / "labels.db")
    monkeypatch.setattr(m, "EXAMPLES", tmp_path / "examples.json")
    sessions = completed_sessions(2, NOW)
    inside = sessions[-1][1] - timedelta(hours=3)
    before = sessions[0][1] - timedelta(hours=3)
    return {
        "fetched_at": NOW.isoformat(),
        "sessions": [[d.isoformat(), c.isoformat()] for d, c in sessions],
        "watch": {"symbols": ["MARKET", "NVDA", "SPY"]},
        "funds": ["SPY"],
        "assets": [_asset("NVDA", "NVIDIA Corporation Common Stock"),
                   _asset("ACME", "Acme Widgets Inc. Common Stock")],
        "fetch": {},
        "articles": [
            # One story, two vendors: two label rows, one deduplicated label.
            _article("alpaca", "1", "alpaca_news", "https://x.example/a", "Nvidia Beats Q3 Estimates",
                     inside, ["NVDA"]),
            _article("finnhub", "9", "finnhub_company", "https://x.example/a", "Nvidia Beats Q3 Estimates",
                     inside, ["NVDA"]),
            # Off-watch rules label, and two Massive vendor labels.
            _article("massive", "m", "massive_news", "https://y.example/b", "Acme Widgets Cuts Guidance",
                     inside, ["ACME", "NVDA"],
                     [{"ticker": "ACME", "sentiment": "negative", "reasoning": "r"},
                      {"ticker": "NVDA", "sentiment": "mixed", "reasoning": "r"}]),
            # Published before the first measured session opened: not counted.
            _article("alpaca", "2", "alpaca_news", "https://x.example/c", "Nvidia Raises Guidance",
                     before, ["NVDA"]),
        ],
    }


def test_the_watch_figure_counts_one_story_once_and_only_watch_names(payload: dict[str, Any]) -> None:
    m.store_and_label(payload)
    out = m.tally(payload)
    last = out["sessions"][-1]
    watch = out["rules"]["watch"]
    assert watch["as_written_rows"]["per_session"][last] == 2
    assert watch["deduplicated"]["per_session"][last] == 1
    assert watch["finnhub"]["per_session"][last] == 1
    assert watch["alpaca"]["per_session"][last] == 1
    assert out["rules"]["off_watch"]["deduplicated"]["per_session"][last] == 1
    assert out["rules"]["total"]["deduplicated"]["per_session"][last] == 2
    assert out["label_rows_outside_window"] == 1
    assert sum(watch["deduplicated"]["per_session"].values()) == 1


def test_massive_vendor_labels_are_counted_apart_from_rules(payload: dict[str, Any]) -> None:
    m.store_and_label(payload)
    out = m.tally(payload)
    last = out["sessions"][-1]
    assert out["massive"]["total"]["directional"]["per_session"][last] == 1
    assert out["massive"]["watch"]["neutral"]["per_session"][last] == 1  # mixed files as neutral
    assert out["massive"]["watch"]["raw_insights"] == {"mixed": 1}


def test_the_tally_is_deterministic(payload: dict[str, Any]) -> None:
    m.store_and_label(payload)
    first = m.tally(payload)
    m.store_and_label(payload)
    assert m.tally(payload) == first


# --------------------------------------------------------------------------
# Finnhub's Benzinga clock (07e27fa): a cache written before the decoder fix
# holds raw stamps and is corrected on load; one written after is not touched.
# --------------------------------------------------------------------------

#: A Benzinga story published 15:00 EDT (19:00Z) that Finnhub stamps 15:00Z.
RAW_BENZINGA = datetime(2026, 10, 8, 15, tzinfo=timezone.utc)
TRUE_BENZINGA = datetime(2026, 10, 8, 19, tzinfo=timezone.utc)


def _clock_payload(**extra: Any) -> dict[str, Any]:
    def row(vendor: str, vid: str, feed: str, publisher: str | None, at: datetime) -> dict[str, Any]:
        a = _article(vendor, vid, feed, f"https://x.example/{vid}", f"Headline {vid}", at, ["NVDA"])
        a["publisher"] = publisher
        return a

    return {
        "fetched_at": NOW.isoformat(),
        "articles": [
            row("finnhub", "fb", "finnhub_company", "Benzinga", RAW_BENZINGA),
            row("finnhub", "fy", "finnhub_company", "Yahoo", RAW_BENZINGA),
            row("finnhub", "fm", "finnhub_market", "Benzinga", RAW_BENZINGA),
            row("alpaca", "ab", "alpaca_news", "Benzinga", RAW_BENZINGA),
        ],
        **extra,
    }


def _stamps(payload: dict[str, Any]) -> dict[str, datetime]:
    return {a["vendor_id"]: datetime.fromisoformat(a["published_at"]) for a in payload["articles"]}


def test_a_cache_without_the_clock_key_is_raw_and_corrected_on_load() -> None:
    raw = _clock_payload()
    fixed, counts = m.correct_finnhub_clock(raw)
    stamps = _stamps(fixed)
    assert stamps["fb"] == TRUE_BENZINGA  # finnhub, company feed, exactly "Benzinga"
    assert stamps["fy"] == RAW_BENZINGA  # another publisher: untouched
    assert stamps["fm"] == RAW_BENZINGA  # general feed: unmeasured, untouched
    assert stamps["ab"] == RAW_BENZINGA  # Alpaca's clock was never wrong
    assert fixed[m.FINNHUB_CLOCK_KEY] == m.FINNHUB_CLOCK_CORRECTED
    assert counts == {"corrected": 1, "disbelieved_future": 0}
    assert _stamps(raw)["fb"] == RAW_BENZINGA  # the loaded payload is not mutated


def test_a_corrected_payload_is_never_corrected_twice() -> None:
    once, _ = m.correct_finnhub_clock(_clock_payload())
    twice, counts = m.correct_finnhub_clock(once)
    assert _stamps(twice) == _stamps(once)
    assert counts == {"corrected": 0, "disbelieved_future": 0}


def test_a_fresh_fetch_writes_the_clock_key_and_is_left_alone() -> None:
    fresh = m.cache_payload(**_clock_payload())
    assert fresh[m.FINNHUB_CLOCK_KEY] == m.FINNHUB_CLOCK_CORRECTED
    left, counts = m.correct_finnhub_clock(fresh)
    assert _stamps(left)["fb"] == RAW_BENZINGA  # already through the fixed decoder
    assert counts == {"corrected": 0, "disbelieved_future": 0}


def test_a_correction_past_the_fetch_time_is_disbelieved_as_the_provider_does() -> None:
    late = _clock_payload()
    late["fetched_at"] = (TRUE_BENZINGA - timedelta(hours=1)).isoformat()
    fixed, counts = m.correct_finnhub_clock(late)
    assert _stamps(fixed)["fb"] == RAW_BENZINGA
    assert counts == {"corrected": 0, "disbelieved_future": 1}


def test_the_correction_lets_a_benzinga_twin_deduplicate(payload: dict[str, Any]) -> None:
    """The defect itself: a raw Finnhub-Benzinga stamp 4h early sits outside
    decision 3's +/-10 minute window, so the twin counted twice."""
    at = datetime.fromisoformat(payload["articles"][0]["published_at"])
    for a in payload["articles"][:2]:
        a["publisher"] = "Benzinga"
        a["url"] = f"https://{a['vendor']}.example/nvda"  # no URL match: headline only
    payload["articles"][1]["published_at"] = (at - timedelta(hours=4)).isoformat()
    m.store_and_label(payload)
    assert m.tally(payload)["rules"]["watch"]["deduplicated"]["mean"] > 0.5  # raw: two groups
    fixed, counts = m.correct_finnhub_clock(payload)
    assert counts["corrected"] == 1
    m.store_and_label(fixed)
    out = m.tally(fixed)
    assert sum(out["rules"]["watch"]["deduplicated"]["per_session"].values()) == 1
