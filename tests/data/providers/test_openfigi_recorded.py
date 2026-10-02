"""OpenFIGI's real answers for the 29 ISIN-only SPDR holdings -- RECORDED, not synthetic.

``tests/fixtures/openfigi/`` was written by ``tests/fixtures/record_openfigi.py``
on 2026-10-01, keyless, in three requests of 10, 10 and 9 jobs. These tests
read those bodies through :func:`parse_mapping_response` -- the provider's own
parser -- so they pin both the live response shape the synthetic tests
assumed and the per-ISIN outcome the resolver (Q17) will see.

What the recording showed, and these pin:

* every one of the 29 matched; none answered ``warning`` or ``error``;
* every record of every ISIN is ``marketSector`` ``Equity`` and
  ``securityType`` ``Common Stock`` -- no ADR, preferred or non-equity line;
* each ISIN has exactly one composite (``exchCode`` ``"US"``) record and
  exactly one distinct ticker across its US exchange codes;
* no ticker carries a class-share separator, so these fixtures do **not**
  show how OpenFIGI spells one (``BRK/B`` vs ``BRK.B``); the resolver must not
  take a convention from them.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from corollary.data.providers.openfigi import (
    OPENFIGI_KEY_HEADER,
    MappingResult,
    mapping_jobs,
    parse_mapping_response,
)

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "openfigi"

#: OpenFIGI exchange codes for US venues seen in the recording. ``US`` is the
#: composite; the rest are the individual venues it aggregates.
US_EXCHANGE_CODES = frozenset(
    {"US", "UA", "UB", "UC", "UD", "UF", "UM", "UN", "UP", "UQ", "UR", "UT", "UV", "UW", "UX"}
)

#: ISIN -> (fund, records returned, the composite ``US`` record's ticker).
EXPECTED: dict[str, tuple[str, int, str]] = {
    "JE00BV7DQ550": ("XLB", 91, "AMCR"),
    "IE0001827041": ("XLB", 97, "CRH"),
    "IE000S9YS762": ("XLB", 130, "LIN"),
    "IE00028FXN24": ("XLB", 110, "SW"),
    "NL0009434992": ("XLB", 115, "LYB"),
    "IE00BLP1HW54": ("XLF", 118, "AON"),
    "BMG0450A1053": ("XLF", 112, "ACGL"),
    "BMG3223R1088": ("XLF", 111, "EG"),
    "BMG491BT1088": ("XLF", 112, "IVZ"),
    "IE00BDB6Q211": ("XLF", 110, "WTW"),
    "CH0044328745": ("XLF", 144, "CB"),
    "IE00BFRT3W74": ("XLI", 94, "ALLE"),
    "IE00B8KQN827": ("XLI", 128, "ETN"),
    "IE00BY7QL619": ("XLI", 120, "JCI"),
    "IE00BLS09M33": ("XLI", 94, "PNR"),
    "IE00BK9ZQ967": ("XLI", 126, "TT"),
    "IE00B4BNMY34": ("XLK", 148, "ACN"),
    "IE00BKVD2N49": ("XLK", 123, "STX"),
    "IE000IVNQZ81": ("XLK", 63, "TEL"),
    "NL0009538784": ("XLK", 128, "NXPI"),
    "SG9999000020": ("XLK", 101, "FLEX"),
    "CH1300646267": ("XLP", 113, "BG"),
    "IE00BTN1Y115": ("XLV", 167, "MDT"),
    "IE00BFY8C754": ("XLV", 110, "STE"),
    "BMG2004J1036": ("XLY", 109, "CCL"),
    "JE00BTDN8H13": ("XLY", 102, "APTV"),
    "BMG667211046": ("XLY", 122, "NCLH"),
    "CH0114405324": ("XLY", 145, "GRMN"),
    "LR0008862868": ("XLY", 132, "RCL"),
}


def envelopes() -> list[dict[str, Any]]:
    paths = sorted(FIXTURE_DIR.glob("mapping_batch_*.json"))
    assert [p.name for p in paths] == [f"mapping_batch_{n}.json" for n in (1, 2, 3)]
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


def recorded_results() -> dict[str, MappingResult]:
    results: dict[str, MappingResult] = {}
    for envelope in envelopes():
        isins = [job["idValue"] for job in envelope["request"]["jobs"]]
        results.update(parse_mapping_response(isins, envelope["response"]))
    return results


def test_the_recording_is_three_keyless_requests_of_10_10_9_with_the_pinned_body() -> None:
    batches = envelopes()
    assert [len(e["request"]["jobs"]) for e in batches] == [10, 10, 9]
    for n, envelope in enumerate(batches, start=1):
        assert (envelope["batch"], envelope["batches"]) == (n, 3)
        assert envelope["recorder"] == "tests/fixtures/record_openfigi.py"
        assert envelope["recorded_at"].endswith("+00:00")
        assert envelope["request"]["keyed"] is False
        jobs = envelope["request"]["jobs"]
        assert jobs == mapping_jobs([job["idValue"] for job in jobs])
        assert [c["isin"] for c in envelope["context"]] == [j["idValue"] for j in jobs]
    sent = [j["idValue"] for e in batches for j in e["request"]["jobs"]]
    assert sent == list(EXPECTED)
    assert [c["fund"] for e in batches for c in e["context"]] == [f for f, _, _ in EXPECTED.values()]


@pytest.mark.risk
def test_no_recorded_file_carries_the_key_header() -> None:
    """Rule 6, at rest: the recorder's scrub held, and stays held if a file is edited."""
    for path in FIXTURE_DIR.glob("*.json"):
        text = path.read_text(encoding="utf-8").lower()
        assert OPENFIGI_KEY_HEADER.lower() not in text
        assert "apikey" not in text and "api_key" not in text


@pytest.mark.parametrize("isin", list(EXPECTED))
def test_each_isin_matches_one_us_common_stock_with_the_pinned_ticker(isin: str) -> None:
    _, count, ticker = EXPECTED[isin]
    result = recorded_results()[isin]
    assert result.matched and result.warning is None and result.error is None
    assert len(result.records) == count
    assert {(r.market_sector, r.security_type) for r in result.records} == {
        ("Equity", "Common Stock")
    }
    composite = [r for r in result.records if r.exch_code == "US"]
    assert [r.ticker for r in composite] == [ticker]
    us = [r for r in result.records if r.exch_code in US_EXCHANGE_CODES]
    assert {r.ticker for r in us} == {ticker}
    assert {r.composite_figi for r in us} == {composite[0].figi}


def test_no_recorded_ticker_spells_a_class_share() -> None:
    """None of the 29 is a class share, so the recording shows no separator.

    Pinned so that a re-recording which *does* carry one fails here and is
    looked at, rather than a convention being assumed from these files.
    """
    tickers = {r.ticker or "" for res in recorded_results().values() for r in res.records}
    assert not any(sep in t for t in tickers for sep in "./ -")
