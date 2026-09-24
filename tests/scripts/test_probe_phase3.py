"""The Phase 3 probe script's redaction, and the two pure helpers its numbers rest on.

The script itself talks to live vendors and is run by hand; these tests make
no network call. What they pin is the part that must never be wrong: a key
held in the environment cannot reach stdout, a fixture or an exception text.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probe_phase3.py"
FAKE_KEY = "Fk3yAbCdEfGh1234567890"


def _load(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the script by path, undoing its side effects at teardown.

    The script puts the repo root on ``sys.path`` when it is imported, and
    its dataclasses need the module registered in ``sys.modules`` while they
    are built. Both go through ``monkeypatch`` so neither outlives the test.
    """
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("probe_phase3", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "probe_phase3", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def probe(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("FRED_API_KEY", FAKE_KEY)
    return _load(monkeypatch)


def test_a_key_in_a_query_string_is_redacted(probe: ModuleType) -> None:
    url = f"https://api.stlouisfed.org/fred/series?series_id=VIXCLS&api_key={FAKE_KEY}"
    out = probe.redact(url)
    assert FAKE_KEY not in out
    assert "api_key=<redacted>" in out


def test_a_recorded_url_carries_no_credential_parameter_at_all(probe: ModuleType) -> None:
    url = f"https://x.test/fred/series?api_key={FAKE_KEY}&series_id=VIXCLS"
    assert probe.safe_url(url) == "https://x.test/fred/series?series_id=VIXCLS"
    assert probe.safe_url(f"https://x.test/n?cursor=abc&apiKey={FAKE_KEY}") == "https://x.test/n?cursor=abc"


def test_a_key_quoted_in_prose_is_redacted_case_insensitively(probe: ModuleType) -> None:
    text = f"Client error '400' for url 'x?token={FAKE_KEY.lower()}' -- see {FAKE_KEY}"
    out = probe.redact(text)
    assert FAKE_KEY.lower() not in out.lower()


def test_a_credential_shaped_value_the_process_does_not_hold_is_redacted(
    probe: ModuleType,
) -> None:
    out = probe.redact("next_url=https://api.massive.com/v2?cursor=abc&apiKey=ZZZZzzzz9999")
    assert "ZZZZzzzz9999" not in out
    assert "cursor=abc" in out
    assert "sk-" + "a" * 24 not in probe.redact("key sk-" + "a" * 24)


def test_save_fixture_refuses_a_name_that_could_overwrite_an_existing_fixture(
    probe: ModuleType,
) -> None:
    with pytest.raises(SystemExit):
        probe.save_fixture("finnhub", "profile2_aapl", {"body": {}})


def test_save_fixture_writes_nothing_the_scan_can_find(
    probe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "FIXTURES", tmp_path)
    path = probe.save_fixture("fred", "p3_test", {"request": f"x?api_key={FAKE_KEY}", "note": FAKE_KEY})
    assert FAKE_KEY not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Acme Beats Q3 Estimates, Raises Full-Year Guidance", "bullish"),
        ("Acme Misses Revenue Expectations", "bearish"),
        ("Goldman Downgrades Acme To Sell", "bearish"),
        ("FDA Approves Acme's Lead Drug", "bullish"),
        ("Acme Receives Complete Response Letter From FDA", "bearish"),
        ("Acme CEO Steps Down Effective Immediately", "bearish"),
        ("Acme Beats Estimates But Cuts Guidance", None),  # both directions -> no label
        ("Acme To Present At Investor Conference", None),
        ("Stifel Upgrades Acme to Buy, Raises Price Target", "bullish"),
        ("Apple's iPhone Upgrade Cycle Looks Strong", None),  # a product, not a rating
        ("Apple To $370? Here Are 10 Top Analyst Forecasts", None),  # a roundup, not a beat
    ],
)
def test_draft_patterns_label_or_abstain(
    probe: ModuleType, headline: str, expected: str | None
) -> None:
    assert probe.draft_label(headline)[0] == expected


def test_draft_attribution_credits_the_subject_and_not_the_broker(probe: ModuleType) -> None:
    headline = "JP Morgan Upgrades Meta Platforms to Overweight"
    assert probe.named_in(headline, {"META", "JPM"}) == {"META"}
    assert probe.named_in("Bank of America Upgrades Oracle to Buy", {"BAC", "ORCL"}) == {"ORCL"}
    assert probe.named_in("Oracle (ORCL) Q1 Earnings Beat", {"ALAB", "ORCL"}) == {"ORCL"}
    assert probe.named_in("Meta Platforms Raises Guidance", {"META"}) == {"META"}


def test_overnight_news_lands_on_the_next_session(probe: ModuleType) -> None:
    sessions = [
        (date(2026, 9, 21), datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)),
        (date(2026, 9, 22), datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)),
        (date(2026, 9, 23), datetime(2026, 9, 23, 20, 0, tzinfo=timezone.utc)),
    ]
    binner = probe.SessionBinner(sessions)
    assert binner.session(datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)) == date(2026, 9, 22)
    assert binner.session(datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)) == date(2026, 9, 22)
    assert binner.session(datetime(2026, 9, 22, 20, 1, tzinfo=timezone.utc)) == date(2026, 9, 23)
    assert binner.session(datetime(2026, 9, 21, 19, 0, tzinfo=timezone.utc)) is None
