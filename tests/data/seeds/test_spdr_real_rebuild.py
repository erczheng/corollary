"""The real 2026-06-30 SPDR quarter, rebuilt from recorded inputs only, and served.

Phase 3 step 4 / spec Q17. Every input is a recording, and nothing is faked
that has a recording:

* **SEC** -- the eleven sector N-PORT filings in ``tests/fixtures/sec/``,
  through ``test_nport_snapshot.FakeSec`` (an ``httpx.MockTransport``).
* **CUSIPs** -- the recorded ``cusip_survey.json`` (all 474 EC CUSIPs,
  resolved live once), through ``test_nport_snapshot.FakeResolver``.
* **ISINs** -- the **real** :class:`OpenFigiIsinResolver` and the real
  :class:`OpenFigiProvider`, keyless, over ``tests/fixtures/openfigi/``. The
  transport replays the recording *batch by batch*: the n-th request must
  carry exactly the n-th recorded batch's jobs, and is answered with that
  batch's recorded reply.
* **The asset directory** -- ``tests/fixtures/alpaca/p4_assets_active_isin_tickers.json``,
  the rows Alpaca's live active list carried for the 29 OpenFIGI tickers
  (recorded by ``tests/fixtures/record_alpaca_isin_assets.py``), replayed
  through the real :meth:`AlpacaProvider.active_equities` decoder. The
  directory is checked only for ISIN-resolved symbols (``nport._resolve``),
  so a trimmed directory is the whole of what the build reads from it.

The snapshot is then read back through the DB seed loader the running system
uses (:class:`DatabaseSeedLoader` over :func:`load_spdr_seed_from_db`), and
what it serves is pinned: status, all eleven funds, XLB's exact resolved sum,
no unresolved ISIN, and each fund's five leaders with their filed weights.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.data.providers.alpaca import AlpacaProvider
from corollary.data.providers.interface import AssetDirectory
from corollary.data.providers.openfigi import OpenFigiCredentials, OpenFigiProvider
from corollary.data.seeds import SPDR_FUNDS, SpdrSeed
from corollary.data.seeds import nport
from corollary.data.seeds.isin import OpenFigiIsinResolver
from corollary.data.seeds.nport import (
    WEIGHT_BAND_LOW,
    DatabaseSeedLoader,
    SnapshotOutcome,
    SnapshotRule,
    build_spdr_snapshot,
    load_spdr_seed_from_db,
)
from corollary.db.models import Base
from corollary.db.session import create_db_engine, sqlite_url
from corollary.ratelimit import HostRateLimiter
from tests.data.providers.conftest import (
    BASIC_FEEDS,
    TEST_CREDENTIALS,
    fixture_body_bytes,
    load_fixture,
)
from tests.data.seeds.test_isin_resolver import LIVE_TICKERS, FakeClock
from tests.data.seeds.test_nport_snapshot import (
    FILED,
    FUNDS,
    NOW,
    REPORT,
    FakeResolver,
    FakeSec,
    sec_provider,
)

OPENFIGI = Path(__file__).resolve().parents[2] / "fixtures" / "openfigi"
ASSETS_FIXTURE = "p4_assets_active_isin_tickers"

#: XLB's resolved weight, the exact sum of its stored rows' filed ``pctVal``.
#: Was 73.364358238176 with no ISIN source (``test_nport_snapshot``).
XLB_RESOLVED_SUM = Decimal("99.837786585047")

#: Each fund's five leaders as the loader serves them: (symbol, filed weight).
LEADERS: dict[str, tuple[tuple[str, str], ...]] = {
    "XLB": (("LIN", "14.05959437799"), ("NEM", "5.842876871992"), ("FCX", "5.297849904975"),
            ("CTVA", "4.958234925028"), ("SHW", "4.938868565985")),
    "XLC": (("META", "19.90082045156"), ("GOOGL", "13.07847812387"), ("GOOG", "10.42268211408"),
            ("TTWO", "5.248166017455"), ("NFLX", "4.836807852214")),
    "XLE": (("XOM", "22.67157977475"), ("CVX", "16.09026176677"), ("COP", "6.567131692396"),
            ("WMB", "5.027378473742"), ("VLO", "4.640925671555")),
    "XLF": (("BRK.B", "12.08282315005"), ("JPM", "11.55210407687"), ("V", "7.499945581989"),
            ("MA", "5.458224446995"), ("BAC", "4.899799691281")),
    "XLI": (("CAT", "8.511724649185"), ("GE", "6.766581639405"), ("GEV", "5.478700166827"),
            ("RTX", "4.433897339151"), ("BA", "2.961235585139")),
    "XLK": (("NVDA", "14.65033005212"), ("AAPL", "12.84756055497"), ("MSFT", "8.376601875041"),
            ("MU", "5.418836907367"), ("AVGO", "5.406703163900")),
    "XLP": (("WMT", "10.80325246068"), ("COST", "9.029726718053"), ("PG", "7.429326018886"),
            ("KO", "6.846909601475"), ("PM", "6.134631106306")),
    "XLRE": (("WELL", "11.00607887966"), ("PLD", "8.676216530294"), ("EQIX", "7.062224313034"),
             ("AMT", "5.234792353264"), ("SPG", "4.981935048663")),
    "XLU": (("NEE", "12.88663081002"), ("SO", "7.596501559281"), ("DUK", "6.947828953517"),
            ("CEG", "5.590233765850"), ("AEP", "5.241024231571")),
    "XLV": (("LLY", "16.51059185375"), ("JNJ", "10.63834786201"), ("ABBV", "7.736491040305"),
            ("UNH", "6.568146350325"), ("MRK", "5.522661108069")),
    "XLY": (("AMZN", "22.22150378005"), ("TSLA", "19.64021357160"), ("HD", "5.826480369952"),
            ("MCD", "4.153882211240"), ("TJX", "3.923057555327")),
}

#: Holdings stored per fund: every EC line with a CUSIP, plus the 29 via OpenFIGI.
HOLDINGS_PER_FUND = {
    "XLB": 26, "XLC": 23, "XLE": 21, "XLF": 76, "XLI": 81, "XLK": 74,
    "XLP": 34, "XLRE": 31, "XLU": 31, "XLV": 59, "XLY": 47,
}


# ------------------------------------------------------------------ replay


def openfigi_batches() -> list[dict[str, Any]]:
    paths = sorted(OPENFIGI.glob("mapping_batch_*.json"))
    batches = [json.loads(p.read_text("utf-8")) for p in paths]
    assert [b["batch"] for b in batches] == [1, 2, 3]
    return batches


class BatchReplay:
    """Serves the OpenFIGI recording one recorded batch per request, in order.

    A request whose jobs are not exactly the next recorded batch's is a test
    failure, not a best-effort answer: the recording is evidence for those
    jobs and no others.
    """

    def __init__(self) -> None:
        self.batches = openfigi_batches()
        self.requests: list[list[dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v3/mapping"
        assert all("key" not in name.lower() for name in request.headers), "keyless replay"
        jobs = json.loads(request.content)
        index = len(self.requests)
        self.requests.append(jobs)
        assert index < len(self.batches), "more requests than recorded batches"
        recorded = self.batches[index]
        assert jobs == recorded["request"]["jobs"], f"batch {index + 1} jobs differ"
        return httpx.Response(200, text=json.dumps(recorded["response"]))


def openfigi_provider(replay: BatchReplay) -> OpenFigiProvider:
    clock = FakeClock()
    return OpenFigiProvider(
        credentials=OpenFigiCredentials(api_key=None),
        transport=httpx.MockTransport(replay),
        limiter=HostRateLimiter(clock=clock, sleep=clock.sleep),
    )


async def _never_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError(f"a replay waited {seconds}s on the limiter")


async def recorded_directory(drop: frozenset[str] = frozenset()) -> AssetDirectory:
    """The recorded asset rows through the real provider decoder; ``drop`` removes rows (SYNTHETIC)."""
    if drop:
        rows = [r for r in load_fixture(ASSETS_FIXTURE)["body"] if r["symbol"] not in drop]
        body = json.dumps(rows, default=str).encode("utf-8")
    else:
        body = fixture_body_bytes(ASSETS_FIXTURE)

    def route(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/v2/assets"
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    limiter = HostRateLimiter(10_000, clock=lambda: 0.0, sleep=_never_sleep)
    provider = AlpacaProvider(
        credentials=TEST_CREDENTIALS, feeds=BASIC_FEEDS, client=client, limiter=limiter,
        now=lambda: NOW,
    )
    try:
        return await provider.active_equities()
    finally:
        await client.aclose()


async def rebuild(
    sessions: Callable[[], Session], directory: AssetDirectory
) -> tuple[SnapshotOutcome, BatchReplay]:
    """The real quarter as the running system builds it, from recorded inputs."""
    replay = BatchReplay()
    figi = openfigi_provider(replay)
    sec = sec_provider(FakeSec())
    resolver = OpenFigiIsinResolver(figi, sessions, directory=directory, clock=lambda: NOW)
    try:
        outcome = await build_spdr_snapshot(
            sec, FakeResolver(), sessions,
            isin_resolver=resolver, directory=directory, clock=lambda: NOW,
        )
    finally:
        await sec.aclose()
        await figi.aclose()
    return outcome, replay


def leaders_with_weights(seed: SpdrSeed) -> dict[str, tuple[tuple[str, str], ...]]:
    weight = {(r.etf, r.symbol): r.weight for r in seed.rows}
    return {
        etf: tuple((symbol, str(weight[(etf, symbol)])) for symbol in symbols)
        for etf, symbols in seed.leaders(5).items()
    }


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    nport.invalidate_spdr_seed_cache()
    yield
    nport.invalidate_spdr_seed_cache()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_db_engine(sqlite_url(tmp_path / "corollary.db"))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def sessions(engine: Engine) -> Callable[[], Session]:
    return lambda: Session(engine)


# ------------------------------------------------------------------ the evidence


def test_the_asset_recording_carries_every_openfigi_ticker_active_and_optionable() -> None:
    recorded = load_fixture(ASSETS_FIXTURE)
    assert recorded["status_code"] == 200
    assert recorded["absent"] == []
    assert set(recorded["tickers"]) == set(LIVE_TICKERS.values())
    rows = recorded["body"]
    assert sorted(r["symbol"] for r in rows) == sorted(LIVE_TICKERS.values())
    assert recorded["trimmed_from"] > len(rows)
    for row in rows:
        assert (row["status"], row["class"], row["tradable"]) == ("active", "us_equity", True)
        assert "has_options" in row["attributes"], row["symbol"]


@pytest.mark.asyncio
async def test_the_recorded_rows_decode_to_a_directory_of_the_29() -> None:
    directory = await recorded_directory()
    assert (len(directory), directory.skipped, directory.missing_attributes) == (29, 0, 0)
    assert directory.optionable() == frozenset(LIVE_TICKERS.values())


# ------------------------------------------------------------------ the rebuild


@pytest.mark.asyncio
async def test_the_real_quarter_rebuilt_through_openfigi_is_accepted_and_the_loader_serves_it(
    sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    outcome, replay = await rebuild(sessions, await recorded_directory())

    assert outcome.status == "accepted", outcome.reason
    assert outcome.rule is None and outcome.snapshot_id is not None
    assert outcome.report_date == REPORT
    # Three requests, exactly the recorded batches; no ISIN left unresolved.
    assert [len(jobs) for jobs in replay.requests] == [10, 10, 9]
    assert outcome.skipped == ()
    unresolved = [r for r in caplog.records if getattr(r, "event", "") == "isin_unresolved"]
    assert unresolved == []
    resolved = {
        getattr(r, "isin"): getattr(r, "ticker")
        for r in caplog.records if getattr(r, "event", "") == "isin_resolved"
    }
    assert resolved == LIVE_TICKERS

    # Served through the loader the app state holds, not the builder's return.
    seed = DatabaseSeedLoader(sessions)()
    assert isinstance(seed, SpdrSeed)
    assert seed == load_spdr_seed_from_db(sessions)
    assert (seed.as_of, seed.filed_date, seed.newer_report_date) == (REPORT, FILED, None)
    assert list(seed.leaders(5)) == list(FUNDS) == list(SPDR_FUNDS)
    assert {row.etf for row in seed.rows} == set(FUNDS)
    per_fund = {etf: sum(1 for r in seed.rows if r.etf == etf) for etf in FUNDS}
    assert per_fund == HOLDINGS_PER_FUND
    assert len(seed.rows) == sum(len(v) for v in outcome.holdings.values()) == 503

    xlb_sum = sum((r.weight for r in seed.rows if r.etf == "XLB"), Decimal(0))
    assert xlb_sum == XLB_RESOLVED_SUM
    assert xlb_sum >= WEIGHT_BAND_LOW
    assert seed.sector_of("LIN") == "Materials"
    assert leaders_with_weights(seed) == LEADERS


@pytest.mark.risk
@pytest.mark.asyncio
async def test_an_openfigi_ticker_alpaca_does_not_list_comes_out_not_listed_and_never_guessed(
    sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    """Fail closed on the real quarter: Linde dropped from the directory (SYNTHETIC).

    OpenFIGI's recorded answer for Linde's ISIN is LIN. With LIN absent from
    the asset directory the resolver refuses it as ``not_listed``, the line
    is skipped rather than resolved by name, XLB falls below the band, and
    nothing is served.
    """
    caplog.set_level(logging.INFO)
    directory = await recorded_directory(drop=frozenset({"LIN"}))
    outcome, _ = await rebuild(sessions, directory)

    assert (outcome.status, outcome.rule) == ("refused", SnapshotRule.WEIGHT_BAND)
    assert outcome.reason is not None and "XLB" in outcome.reason
    assert [(s.etf, s.isin, s.rule) for s in outcome.skipped] == [
        ("XLB", "IE000S9YS762", SnapshotRule.UNRESOLVED_ISIN)
    ]
    (refusal,) = [r for r in caplog.records if getattr(r, "event", "") == "isin_unresolved"]
    assert (getattr(refusal, "isin"), getattr(refusal, "reason")) == ("IE000S9YS762", "not_listed")
    assert getattr(refusal, "candidates") == ["LIN"]
    assert load_spdr_seed_from_db(sessions) is None
