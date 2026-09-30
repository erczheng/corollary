"""The SPDR seed built from SEC N-PORT: validation, storage, and the DB loader.

Offline. SEC is served from the **real** recording in ``tests/fixtures/sec/``
(eleven sector funds at 2026-06-30, filed 2026-08-28) through an
``httpx.MockTransport``; Alpaca's ``asset_by_cusip`` is a fake built from the
recorded ``cusip_survey.json`` (all 474 EC CUSIPs, resolved live once).

**The real quarter is refused, and that is the first test.** 29 EC lines
carry ``<cusip>000000000</cusip>`` and only an ISIN -- foreign-domiciled S&P 500
members (Linde, CRH, Amcor, Smurfit Westrock, LyondellBasell in XLB; Chubb, Aon,
Eaton, Medtronic, Accenture, ...), not cash lines. The owner's rule skips an
unresolved line and never guesses, so XLB's resolved weight is 73.36 and the
90-110 band refuses the whole snapshot. That is the state of the running
system until the owner picks an ISIN source: the default ``NoIsinResolver``
resolves nothing.

Tests that need the accept path drop a fake :class:`IsinResolver` into the
seam, mapping XLB's five real ISINs to SYNTHETIC symbols (``SYNA``..``SYNE``)
-- deliberately not real tickers, so no test here asserts an ISIN->ticker
fact nobody has sourced. Every byte SEC serves is the recording.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.data.providers.interface import AssetDirectory, EquityAsset, ProviderError
from corollary.data.providers.sec import SecProvider, parse_nport_document
from corollary.data.seeds import (
    NPORT_STALE_AFTER_DAYS,
    SPDR_SECTORS,
    SpdrHolding,
    SpdrSeed,
    normalize_symbol,
)
from corollary.data.seeds import nport
from corollary.data.seeds.nport import (
    MIN_EQUITIES_PER_FUND,
    WEIGHT_BAND_HIGH,
    WEIGHT_BAND_LOW,
    IsinResolver,
    NoIsinResolver,
    ResolvedHolding,
    SnapshotRule,
    build_spdr_snapshot,
    latest_snapshot_attempt,
    load_spdr_seed_from_db,
    validate_funds,
)
from corollary.db.models import Base, SpdrHoldingRow, SpdrHoldingsSnapshot
from corollary.db.session import create_db_engine, sqlite_url
from corollary.ratelimit import SEC_DATA_HOST, SEC_WWW_HOST, HostRateLimiter

SEC = Path(__file__).resolve().parents[2] / "fixtures" / "sec"
USER_AGENT = "Synthetic Tester synthetic.tester@example.com"  # SYNTHETIC
REPORT = date(2026, 6, 30)
FILED = date(2026, 8, 28)
FUNDS = ("XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY")
NOW = datetime(2026, 9, 28, 21, 0, tzinfo=timezone.utc)

# ------------------------------------------------------------------ fixtures


def _meta(ticker: str) -> dict[str, Any]:
    return json.loads((SEC / f"nport_{ticker}_primary_doc.meta.json").read_text("utf-8"))


ACCESSION_OF = {t: _meta(t)["accession"] for t in FUNDS}
DOC_OF_ACCESSION = {a: SEC / f"nport_{t}_primary_doc.xml" for t, a in ACCESSION_OF.items()}
EXCLUDED_DOC = SEC / "nport_excluded_S000093831_header.xml"
SURVEY = {
    row["cusip"]: row["symbol"]
    for row in json.loads((SEC / "cusip_survey.json").read_text("utf-8"))["rows"]
}

#: EC lines carrying a usable CUSIP across the eleven recorded documents.
EC_LINES_WITH_CUSIP = sum(
    1 for path in DOC_OF_ACCESSION.values()
    for h in parse_nport_document(path.read_bytes()).equity if h.cusip is not None
)

#: Every EC line filed with no CUSIP, as (fund, ISIN, name, weight), in fund order.
ISIN_ONLY_LINES = tuple(
    (t, h.isin, h.name, h.pct_val)
    for t in FUNDS
    for h in parse_nport_document(DOC_OF_ACCESSION[ACCESSION_OF[t]].read_bytes()).equity
    if h.cusip is None
)

#: XLB's five real ISINs (Amcor, CRH, Linde, Smurfit Westrock, LyondellBasell,
#: in document order) -> SYNTHETIC symbols. Not real tickers, on purpose.
XLB_ISINS = {
    "JE00BV7DQ550": "SYNA",
    "IE0001827041": "SYNB",
    "IE000S9YS762": "SYNC",
    "IE00028FXN24": "SYND",
    "NL0009434992": "SYNE",
}


def submissions(extra_rows: Sequence[dict[str, str]] = ()) -> str:
    payload = json.loads((SEC / "CIK0001064641.trimmed.json").read_text("utf-8"))
    recent = payload["filings"]["recent"]
    for extra in extra_rows:
        for key, column in recent.items():
            column.append(extra.get(key, column[0]))
    return json.dumps(payload)


class FakeSec:
    """Routes SEC's three documents; each piece overridable per test."""

    def __init__(self) -> None:
        self.docs: dict[str, bytes] = {a: p.read_bytes() for a, p in DOC_OF_ACCESSION.items()}
        self.submissions = submissions()
        self.status: int | None = None
        self.archive_requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.status is not None:
            return httpx.Response(self.status, text="Forbidden")
        url = request.url
        if url.host == SEC_WWW_HOST and url.path == "/files/company_tickers_mf.json":
            return httpx.Response(200, text=(SEC / "company_tickers_mf.trimmed.json").read_text("utf-8"))
        if url.host == SEC_DATA_HOST and url.path == "/submissions/CIK0001064641.json":
            return httpx.Response(200, text=self.submissions)
        prefix = "/Archives/edgar/data/1064641/"
        if url.host == SEC_WWW_HOST and url.path.startswith(prefix):
            digits = url.path[len(prefix):].split("/")[0]
            accession = f"{digits[:10]}-{digits[10:12]}-{digits[12:]}"
            self.archive_requests.append(accession)
            # The ten Premium Income filings nobody recorded get the one
            # recorded Premium Income header: same trust, same quarter.
            return httpx.Response(200, content=self.docs.get(accession, EXCLUDED_DOC.read_bytes()))
        raise AssertionError(f"unrouted {request.method} {url}")


async def _never_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError(f"a test waited {seconds}s on the limiter")


def sec_provider(fake: FakeSec) -> SecProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    limiter = HostRateLimiter(10_000, per_host={}, clock=lambda: 0.0, sleep=_never_sleep)
    return SecProvider(user_agent=USER_AGENT, client=client, limiter=limiter)


class FakeResolver:
    """``asset_by_cusip`` from the recorded survey. ``missing`` answers 404; ``broken`` raises."""

    def __init__(
        self,
        mapping: dict[str, str] | None = None,
        *,
        missing: frozenset[str] = frozenset(),
        broken: frozenset[str] = frozenset(),
    ) -> None:
        self.mapping = dict(SURVEY if mapping is None else mapping)
        self.missing = missing
        self.broken = broken
        self.calls: list[str] = []

    async def asset_by_cusip(self, cusip: str) -> EquityAsset | None:
        self.calls.append(cusip)
        if cusip in self.broken:
            raise ProviderError(f"GET /v2/assets/{cusip}: HTTP 503")
        if cusip in self.missing or cusip not in self.mapping:
            return None
        return EquityAsset(
            symbol=self.mapping[cusip], name="n/a", tradable=True, has_options=True, exchange="NYSE"
        )


class FakeIsinResolver:
    """The :class:`IsinResolver` seam, from a fixed map. ``broken`` raises ``ProviderError``."""

    def __init__(
        self, mapping: dict[str, str] | None = None, *, broken: frozenset[str] = frozenset()
    ) -> None:
        self.mapping = dict(XLB_ISINS if mapping is None else mapping)
        self.broken = broken
        self.calls: list[tuple[str, str, str]] = []

    async def resolve(self, isin: str, *, fund: str, name: str) -> str | None:
        self.calls.append((isin, fund, name))
        if isin in self.broken:
            raise ProviderError(f"ISIN source for {isin}: HTTP 503")
        return self.mapping.get(isin)


def directory_of(symbols: set[str]) -> AssetDirectory:
    return AssetDirectory(tuple(
        EquityAsset(symbol=s, name="n/a", tradable=True, has_options=True, exchange="NYSE")
        for s in sorted({normalize_symbol(s) for s in symbols})
    ))


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


async def build(
    sessions: Callable[[], Session],
    fake: FakeSec,
    resolver: FakeResolver,
    *,
    isin_resolver: IsinResolver | None = None,
    directory: AssetDirectory | None = None,
) -> nport.SnapshotOutcome:
    """The builder as the running system calls it: ``isin_resolver=None`` is the default seam."""
    provider = sec_provider(fake)
    try:
        return await build_spdr_snapshot(
            provider, resolver, sessions,
            isin_resolver=isin_resolver, directory=directory, clock=lambda: NOW,
        )
    finally:
        await provider.aclose()


async def build_accept(
    sessions: Callable[[], Session], fake: FakeSec, resolver: FakeResolver
) -> nport.SnapshotOutcome:
    """The accept path: XLB's five ISIN-only lines resolved through a fake seam."""
    return await build(sessions, fake, resolver, isin_resolver=FakeIsinResolver())


def snapshot_rows(sessions: Callable[[], Session]) -> list[SpdrHoldingsSnapshot]:
    with sessions() as session:
        return list(session.scalars(select(SpdrHoldingsSnapshot).order_by(SpdrHoldingsSnapshot.id)))


def holding_count(sessions: Callable[[], Session]) -> int:
    with sessions() as session:
        return len(list(session.scalars(select(SpdrHoldingRow))))


def insert_accepted(sessions: Callable[[], Session], report: date, filed: date) -> None:
    """A SYNTHETIC earlier quarter: five equal holdings per fund, 20% each."""
    with sessions() as session, session.begin():
        snap = SpdrHoldingsSnapshot(
            report_date=report, filed_date=filed, built_at=NOW - timedelta(days=90),
            status="accepted", rule=None, reason=None, skipped_lines=0,
        )
        session.add(snap)
        session.flush()
        for n, (etf, sector) in enumerate(SPDR_SECTORS.items()):
            for i in range(5):
                session.add(SpdrHoldingRow(
                    snapshot_id=snap.id, etf=etf, symbol=f"{etf}{chr(65 + i)}", sector=sector,
                    series_id="S000000000", accession="0000000000-26-000000",
                    cusip=f"SYN{n:05d}{i}", name="SYNTHETIC",
                    weight=Decimal("20"),
                ))


# ------------------------------------------------------------------ the real quarter


@pytest.mark.asyncio
async def test_the_real_quarter_is_refused_because_xlb_isin_only_lines_leave_it_below_90(
    sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    outcome = await build(sessions, FakeSec(), FakeResolver())

    assert outcome.status == "refused"
    assert outcome.rule == SnapshotRule.WEIGHT_BAND
    assert outcome.report_date == REPORT
    # XLB alone fails, at exactly the recorded resolved sum.
    assert outcome.reason is not None and "XLB" in outcome.reason
    assert "73.364358238176" in outcome.reason
    for etf in FUNDS:
        if etf != "XLB":
            assert f"{etf} " not in outcome.reason
    # The 29 skipped lines are the ISIN-only ones, and none was guessed.
    assert len(outcome.skipped) == 29
    assert all(line.cusip is None and line.isin is not None for line in outcome.skipped)
    assert {line.rule for line in outcome.skipped} == {SnapshotRule.UNRESOLVED_ISIN}
    assert [(s.etf, s.isin, s.name, s.weight) for s in outcome.skipped] == list(ISIN_ONLY_LINES)
    by_fund: dict[str, int] = {}
    for line in outcome.skipped:
        by_fund[line.etf] = by_fund.get(line.etf, 0) + 1
    assert by_fund == {"XLB": 5, "XLF": 6, "XLI": 5, "XLK": 5, "XLP": 1, "XLV": 2, "XLY": 5}
    # The resolution itself worked: XLK's five heaviest, from real pctVal.
    xlk = sorted(outcome.holdings["XLK"], key=lambda h: h.weight, reverse=True)
    assert [h.symbol for h in xlk[:5]] == ["NVDA", "AAPL", "MSFT", "MU", "AVGO"]
    # Recorded, never current: nothing ever accepted, so the loader has no seed.
    rows = snapshot_rows(sessions)
    assert [(r.status, r.rule, r.report_date, r.filed_date) for r in rows] == [
        ("refused", "weight_band", REPORT, FILED)
    ]
    assert rows[0].skipped_lines == 29
    assert holding_count(sessions) == 0
    assert load_spdr_seed_from_db(sessions) is None
    attempt = latest_snapshot_attempt(sessions)
    assert attempt is not None and attempt.status == "refused" and attempt.rule == "weight_band"
    # Each skipped line is logged with its fund and name; the refusal with its rule.
    skipped_logs = [r for r in caplog.records if getattr(r, "event", "") == "spdr_holding_skipped"]
    assert len(skipped_logs) == 29
    linde = next(r for r in skipped_logs if getattr(r, "holding_name", "") == "Linde PLC")
    assert (getattr(linde, "etf"), getattr(linde, "cusip"), getattr(linde, "isin")) == (
        "XLB", None, "IE000S9YS762"
    )
    assert getattr(linde, "weight") == "14.05959437799"
    assert getattr(linde, "rule") == "unresolved_isin"
    refused = [r for r in caplog.records if getattr(r, "event", "") == "spdr_snapshot_refused"]
    assert len(refused) == 1 and getattr(refused[0], "rule") == "weight_band"
    assert USER_AGENT.lower() not in caplog.text.lower()
    assert "synthetic.tester" not in caplog.text.lower()


@pytest.mark.asyncio
async def test_with_an_isin_seam_for_xlb_the_real_quarter_is_accepted_and_served(
    sessions: Callable[[], Session],
) -> None:
    fake = FakeSec()
    outcome = await build_accept(sessions, fake, FakeResolver())

    assert outcome.status == "accepted", outcome.reason
    assert outcome.rule is None and outcome.snapshot_id is not None
    # 22 NPORT-P fetched -- 11 sector funds, 11 Premium Income -- 11 kept by seriesId.
    assert len(fake.archive_requests) == 22
    with sessions() as session:
        series = set(session.scalars(select(SpdrHoldingRow.series_id)))
        accessions = set(session.scalars(select(SpdrHoldingRow.accession)))
    assert series == {_meta(t)["series_id"] for t in FUNDS}
    assert "S000093831" not in series
    assert accessions == set(ACCESSION_OF.values())
    # The seam's lines are stored with no CUSIP and the filing's ISIN.
    with sessions() as session:
        via_isin = {
            (r.symbol, r.cusip, r.isin)
            for r in session.scalars(select(SpdrHoldingRow).where(SpdrHoldingRow.etf == "XLB"))
            if r.cusip is None
        }
    assert via_isin == {(sym, None, isin) for isin, sym in XLB_ISINS.items()}
    # The other 24 ISIN-only lines were asked, answered None, and skipped.
    assert len(outcome.skipped) == len(ISIN_ONLY_LINES) - 5 == 24
    assert {s.rule for s in outcome.skipped} == {SnapshotRule.UNRESOLVED_ISIN}
    assert all(s.etf != "XLB" for s in outcome.skipped)

    seed = load_spdr_seed_from_db(sessions)
    assert isinstance(seed, SpdrSeed)
    assert (seed.as_of, seed.filed_date, seed.newer_report_date) == (REPORT, FILED, None)
    assert seed.stale_after_days == NPORT_STALE_AFTER_DAYS
    assert seed.leaders(5)["XLK"] == ("NVDA", "AAPL", "MSFT", "MU", "AVGO")
    assert seed.leaders(5) == seed.leaders(5)
    assert list(seed.leaders(5)) == list(FUNDS)
    assert seed.sector_of("nvda") == "Technology"
    assert seed.sector_of("BRK/B") == "Financials"
    assert seed.sector_of("SYNC") == "Materials"  # Linde's ISIN, through the seam
    assert seed.sector_of("LIN") is None  # nothing derived a ticker from the name
    assert "BF.B" in seed.symbols()
    # Every EC line with a CUSIP, plus XLB's five from the seam: nothing dropped.
    assert len(seed.rows) == sum(len(v) for v in outcome.holdings.values())
    assert len(seed.rows) == EC_LINES_WITH_CUSIP + 5
    # Weights are the filing's exact text as Decimal.
    nvda = next(r for r in seed.rows if r.etf == "XLK" and r.symbol == "NVDA")
    doc = parse_nport_document(DOC_OF_ACCESSION[ACCESSION_OF["XLK"]].read_bytes())
    assert nvda.weight == next(h.pct_val for h in doc.equity if h.cusip == "67066G104")
    assert type(nvda.weight) is Decimal


@pytest.mark.asyncio
async def test_a_rebuild_is_deterministic(tmp_path: Path) -> None:
    seeds = []
    for name in ("a.db", "b.db"):
        eng = create_db_engine(sqlite_url(tmp_path / name))
        Base.metadata.create_all(eng)
        def factory(eng: Engine = eng) -> Session:
            return Session(eng)

        nport.invalidate_spdr_seed_cache()
        await build_accept(factory, FakeSec(), FakeResolver())
        seeds.append(load_spdr_seed_from_db(factory))
        eng.dispose()
    a, b = seeds
    assert a is not None and b is not None
    assert a.rows == b.rows and a.leaders(5) == b.leaders(5) and a.symbols() == b.symbols()


@pytest.mark.asyncio
async def test_only_common_equity_lines_are_resolved(sessions: Callable[[], Session]) -> None:
    resolver = FakeResolver()
    outcome = await build_accept(sessions, FakeSec(), resolver)
    assert outcome.status == "accepted"
    non_equity = {
        h.cusip
        for t in ("XLK", "XLU")
        for h in parse_nport_document(DOC_OF_ACCESSION[ACCESSION_OF[t]].read_bytes()).holdings
        if h.asset_cat != "EC" and h.cusip is not None
    }
    assert non_equity  # the recording has DE and STIV lines in XLK and XLU
    assert not non_equity & set(resolver.calls)
    # Each CUSIP is resolved once even when two funds hold it.
    assert len(resolver.calls) == len(set(resolver.calls))


@pytest.mark.asyncio
async def test_a_cusip_alpaca_does_not_know_is_logged_and_skipped(
    sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    xlk = parse_nport_document(DOC_OF_ACCESSION[ACCESSION_OF["XLK"]].read_bytes()).equity
    # XLK's lightest line with a CUSIP: a 404 on it cannot move the band.
    line = min((h for h in xlk if h.cusip is not None), key=lambda h: h.pct_val)
    assert line.cusip is not None
    outcome = await build_accept(sessions, FakeSec(), FakeResolver(missing=frozenset({line.cusip})))
    assert outcome.status == "accepted", outcome.reason
    seed = load_spdr_seed_from_db(sessions)
    assert seed is not None and SURVEY[line.cusip] not in seed.symbols()
    assert seed.leaders(5)["XLK"] == ("NVDA", "AAPL", "MSFT", "MU", "AVGO")
    [record] = [
        r for r in caplog.records
        if getattr(r, "event", "") == "spdr_holding_skipped" and getattr(r, "cusip", None) == line.cusip
    ]
    assert getattr(record, "etf") == "XLK"
    assert getattr(record, "holding_name") == line.name
    assert getattr(record, "rule") == "unresolved_cusip"
    assert [s.rule for s in outcome.skipped].count(SnapshotRule.UNRESOLVED_CUSIP) == 1



# ------------------------------------------------------------------ the ISIN seam


@pytest.mark.asyncio
async def test_the_default_seam_refuses_xlb_and_keeps_the_previous(
    sessions: Callable[[], Session],
) -> None:
    """``NoIsinResolver`` passed explicitly behaves as the default: fail closed on XLB."""
    insert_accepted(sessions, date(2026, 3, 31), date(2026, 5, 29))
    outcome = await build(sessions, FakeSec(), FakeResolver(), isin_resolver=NoIsinResolver())
    assert (outcome.status, outcome.rule) == ("refused", SnapshotRule.WEIGHT_BAND)
    assert outcome.reason is not None
    assert "XLB resolved equity weights sum to 73.364358238176" in outcome.reason
    seed = load_spdr_seed_from_db(sessions)
    assert seed is not None and seed.as_of == date(2026, 3, 31)
    assert seed.newer_report_date == REPORT
    assert holding_count(sessions) == 55  # only the earlier snapshot's


@pytest.mark.asyncio
async def test_the_seam_is_asked_once_per_isin_with_its_fund_and_name(
    sessions: Callable[[], Session],
) -> None:
    isins = FakeIsinResolver()
    outcome = await build(sessions, FakeSec(), FakeResolver(), isin_resolver=isins)
    assert outcome.status == "accepted", outcome.reason
    asked = [isin for isin, _, _ in isins.calls]
    assert len(asked) == len(set(asked))
    assert set(asked) == {isin for _, isin, _, _ in ISIN_ONLY_LINES}
    assert ("IE000S9YS762", "XLB", "Linde PLC") in isins.calls
    # The ISIN seam never sees a line that has a CUSIP.
    assert not set(asked) & set(SURVEY)


@pytest.mark.asyncio
async def test_an_isin_two_funds_hold_is_asked_once_with_the_first_fund(
    sessions: Callable[[], Session],
) -> None:
    """No ISIN recurs across the recording, so XLF's Chubb line is given Linde's ISIN."""
    assert len({isin for _, isin, _, _ in ISIN_ONLY_LINES}) == len(ISIN_ONLY_LINES)
    fake = FakeSec()
    xlf = ACCESSION_OF["XLF"]
    text = DOC_OF_ACCESSION[xlf].read_text("utf-8")
    assert text.count('<isin value="CH0044328745"/>') == 1
    fake.docs[xlf] = text.replace(
        '<isin value="CH0044328745"/>', '<isin value="IE000S9YS762"/>'  # SYNTHETIC: shared ISIN
    ).encode("utf-8")
    isins = FakeIsinResolver()
    await build(sessions, fake, FakeResolver(), isin_resolver=isins)
    assert [c for c in isins.calls if c[0] == "IE000S9YS762"] == [
        ("IE000S9YS762", "XLB", "Linde PLC")
    ]


@pytest.mark.asyncio
async def test_an_isin_seam_error_aborts_without_storing(
    sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    insert_accepted(sessions, date(2026, 3, 31), date(2026, 5, 29))
    before = snapshot_rows(sessions)
    isins = FakeIsinResolver(broken=frozenset({"IE000S9YS762"}))
    outcome = await build(sessions, FakeSec(), FakeResolver(), isin_resolver=isins)
    assert (outcome.status, outcome.rule) == ("aborted", SnapshotRule.ISIN_LOOKUP_FAILED)
    assert outcome.reason is not None and "IE000S9YS762" in outcome.reason
    assert [r.id for r in snapshot_rows(sessions)] == [r.id for r in before]
    assert holding_count(sessions) == 55
    seed = load_spdr_seed_from_db(sessions)
    assert seed is not None and seed.as_of == date(2026, 3, 31) and seed.newer_report_date is None
    [record] = [r for r in caplog.records if getattr(r, "event", "") == "spdr_snapshot_aborted"]
    assert record.levelno == logging.ERROR and getattr(record, "rule") == "isin_lookup_failed"


@pytest.mark.asyncio
async def test_a_seam_symbol_the_directory_does_not_list_is_skipped_as_unknown(
    sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    listed = set(SURVEY.values()) | {"SYNA", "SYNB", "SYNC", "SYND"}  # SYNE is not listed
    outcome = await build(
        sessions, FakeSec(), FakeResolver(),
        isin_resolver=FakeIsinResolver(), directory=directory_of(listed),
    )
    # LyondellBasell (1.82%) dropping leaves XLB near 97.5: inside the band.
    assert outcome.status == "accepted", outcome.reason
    [unknown] = [s for s in outcome.skipped if s.rule == SnapshotRule.UNKNOWN_SYMBOL]
    assert (unknown.etf, unknown.isin, unknown.cusip) == ("XLB", "NL0009434992", None)
    seed = load_spdr_seed_from_db(sessions)
    assert seed is not None and "SYNE" not in seed.symbols() and "SYNC" in seed.symbols()
    [record] = [
        r for r in caplog.records
        if getattr(r, "event", "") == "spdr_holding_skipped"
        and getattr(r, "rule", "") == "unknown_symbol"
    ]
    assert getattr(record, "holding_name") == "LyondellBasell Industries NV"
    assert getattr(record, "isin") == "NL0009434992"


@pytest.mark.asyncio
async def test_a_seam_answer_that_is_not_a_ticker_is_skipped_as_unusable(
    sessions: Callable[[], Session],
) -> None:
    isins = FakeIsinResolver({**XLB_ISINS, "NL0009434992": "LYB N.V."})
    outcome = await build(sessions, FakeSec(), FakeResolver(), isin_resolver=isins)
    assert outcome.status == "accepted", outcome.reason
    [unusable] = [s for s in outcome.skipped if s.rule == SnapshotRule.UNUSABLE_SYMBOL]
    assert (unusable.etf, unusable.isin) == ("XLB", "NL0009434992")


@pytest.mark.asyncio
async def test_a_malformed_isin_is_never_resolved_by_name(sessions: Callable[[], Session]) -> None:
    """Linde's ISIN broken in the XML: nothing to resolve by, so skipped -- and XLB refuses.

    The SEC parser already drops an ISIN of the wrong shape, so the line reaches
    the builder with neither identifier and is skipped as ``no_cusip``.
    """
    fake = FakeSec()
    xlb = ACCESSION_OF["XLB"]
    text = DOC_OF_ACCESSION[xlb].read_text("utf-8")
    assert text.count('<isin value="IE000S9YS762"/>') == 1
    fake.docs[xlb] = text.replace(
        '<isin value="IE000S9YS762"/>', '<isin value="IE000S9YS76X"/>'  # SYNTHETIC: bad check digit
    ).encode("utf-8")
    isins = FakeIsinResolver()
    outcome = await build(sessions, fake, FakeResolver(), isin_resolver=isins)
    assert [s for s in outcome.skipped if s.rule == SnapshotRule.NO_CUSIP] == [
        nport.SkippedLine("XLB", "Linde PLC", None, None, Decimal("14.05959437799"),
                          SnapshotRule.NO_CUSIP)
    ]
    assert all(name != "Linde PLC" for _, _, name in isins.calls)
    # Without Linde's 14.06, XLB's other lines sum below 90: the whole snapshot is refused.
    assert (outcome.status, outcome.rule) == ("refused", SnapshotRule.WEIGHT_BAND)
    assert outcome.reason is not None and "XLB" in outcome.reason

# ------------------------------------------------------------------ refusals keep the previous


@pytest.mark.asyncio
async def test_a_fund_pushed_below_90_by_unresolved_lines_refuses_and_keeps_the_previous(
    sessions: Callable[[], Session],
) -> None:
    insert_accepted(sessions, date(2026, 3, 31), date(2026, 5, 29))
    nvidia = "67066G104"
    outcome = await build_accept(sessions, FakeSec(), FakeResolver(missing=frozenset({nvidia})))

    assert (outcome.status, outcome.rule) == ("refused", SnapshotRule.WEIGHT_BAND)
    assert outcome.reason is not None and "XLK" in outcome.reason
    seed = load_spdr_seed_from_db(sessions)
    assert seed is not None and seed.as_of == date(2026, 3, 31)
    assert seed.newer_report_date == REPORT
    assert seed.is_stale(date(2026, 9, 28))
    assert any("newer filing" in why for why in seed.staleness(date(2026, 9, 28)))
    assert holding_count(sessions) == 55  # only the earlier snapshot's


@pytest.mark.asyncio
async def test_fewer_than_five_equities_refuses(sessions: Callable[[], Session]) -> None:
    xle = parse_nport_document(DOC_OF_ACCESSION[ACCESSION_OF["XLE"]].read_bytes()).equity
    keep = {h.cusip for h in xle[:4]}
    gone = frozenset(h.cusip for h in xle if h.cusip not in keep and h.cusip is not None)
    outcome = await build_accept(sessions, FakeSec(), FakeResolver(missing=gone))
    assert (outcome.status, outcome.rule) == ("refused", SnapshotRule.TOO_FEW_EQUITIES)
    assert outcome.reason is not None and "XLE" in outcome.reason
    assert load_spdr_seed_from_db(sessions) is None


@pytest.mark.asyncio
async def test_a_missing_fund_refuses(sessions: Callable[[], Session]) -> None:
    fake = FakeSec()
    fake.docs[ACCESSION_OF["XLU"]] = EXCLUDED_DOC.read_bytes()  # XLU's filing is someone else's
    outcome = await build_accept(sessions, fake, FakeResolver())
    assert (outcome.status, outcome.rule) == ("refused", SnapshotRule.MISSING_FUND)
    assert outcome.reason is not None and "XLU" in outcome.reason
    assert [r.rule for r in snapshot_rows(sessions)] == ["missing_fund"]
    assert load_spdr_seed_from_db(sessions) is None


@pytest.mark.asyncio
async def test_an_amendment_refuses_and_keeps_the_previous(sessions: Callable[[], Session]) -> None:
    insert_accepted(sessions, date(2026, 3, 31), date(2026, 5, 29))
    fake = FakeSec()
    amendment = "0001410368-26-099999"
    fake.submissions = submissions([{
        "accessionNumber": amendment, "form": "NPORT-P/A", "filingDate": "2026-09-15",
        "reportDate": "2026-06-30", "primaryDocument": "primary_doc.xml",
    }])
    fake.docs[amendment] = DOC_OF_ACCESSION[ACCESSION_OF["XLK"]].read_bytes()
    outcome = await build_accept(sessions, fake, FakeResolver())
    assert (outcome.status, outcome.rule) == ("refused", SnapshotRule.AMENDMENT)
    assert outcome.reason is not None and "XLK" in outcome.reason
    seed = load_spdr_seed_from_db(sessions)
    assert seed is not None and seed.as_of == date(2026, 3, 31)


# ------------------------------------------------------------------ aborts store nothing


@pytest.mark.asyncio
async def test_an_alpaca_failure_aborts_without_storing(
    sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    outcome = await build_accept(sessions, FakeSec(), FakeResolver(broken=frozenset({"037833100"})))
    assert (outcome.status, outcome.rule) == ("aborted", SnapshotRule.CUSIP_LOOKUP_FAILED)
    assert snapshot_rows(sessions) == []
    assert any(getattr(r, "event", "") == "spdr_snapshot_aborted" for r in caplog.records)


@pytest.mark.asyncio
async def test_sec_refusing_access_aborts_loudly_without_storing(
    sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeSec()
    fake.status = 403
    resolver = FakeResolver()
    outcome = await build(sessions, fake, resolver)
    assert (outcome.status, outcome.rule) == ("aborted", SnapshotRule.SEC_ACCESS_REFUSED)
    assert snapshot_rows(sessions) == [] and resolver.calls == []
    [record] = [r for r in caplog.records if getattr(r, "event", "") == "spdr_snapshot_aborted"]
    assert record.levelno == logging.ERROR
    assert USER_AGENT.lower() not in caplog.text.lower()


@pytest.mark.asyncio
async def test_an_already_loaded_quarter_is_not_rebuilt(sessions: Callable[[], Session]) -> None:
    await build_accept(sessions, FakeSec(), FakeResolver())
    fake, resolver = FakeSec(), FakeResolver()
    outcome = await build(sessions, fake, resolver)
    assert outcome.status == "unchanged"
    assert fake.archive_requests == [] and resolver.calls == []
    assert len(snapshot_rows(sessions)) == 1


# ------------------------------------------------------------------ loader and staleness


@pytest.mark.asyncio
async def test_the_loader_caches_until_the_builder_records(sessions: Callable[[], Session]) -> None:
    insert_accepted(sessions, date(2026, 3, 31), date(2026, 5, 29))
    first = load_spdr_seed_from_db(sessions)
    assert first is load_spdr_seed_from_db(sessions)
    await build_accept(sessions, FakeSec(), FakeResolver())
    second = load_spdr_seed_from_db(sessions)
    assert second is not None and second.as_of == REPORT



def test_the_loader_sees_a_snapshot_another_process_wrote(sessions: Callable[[], Session]) -> None:
    """No invalidate call: the newest-id check alone must notice the new row."""
    assert load_spdr_seed_from_db(sessions) is None
    insert_accepted(sessions, date(2026, 3, 31), date(2026, 5, 29))
    seed = load_spdr_seed_from_db(sessions)
    assert seed is not None and seed.as_of == date(2026, 3, 31)

def test_staleness_by_age_is_more_than_200_days() -> None:
    seed = SpdrSeed(
        as_of=REPORT,
        rows=tuple(
            SpdrHolding(etf, sector, f"{etf}A", Decimal("100"))
            for etf, sector in SPDR_SECTORS.items()
        ),
        filed_date=FILED,
        stale_after_days=NPORT_STALE_AFTER_DAYS,
    )
    assert NPORT_STALE_AFTER_DAYS == 200
    assert not seed.is_stale(REPORT + timedelta(days=200))
    assert seed.staleness(REPORT + timedelta(days=200)) == ()
    assert seed.is_stale(REPORT + timedelta(days=201))
    # 90 days after filing is ordinary for N-PORT: decision 6's 100 would have fired.
    assert not seed.is_stale(FILED + timedelta(days=90))


# ------------------------------------------------------------------ the validator's boundaries


def _funds(weights: dict[str, list[str]]) -> dict[str, tuple[ResolvedHolding, ...]]:
    return {
        etf: tuple(
            ResolvedHolding(etf, SPDR_SECTORS[etf], "S000000000", "0000000000-26-000000",
                            f"SYN{i:05d}0", f"{etf}{chr(65 + i)}", "SYNTHETIC", Decimal(w))
            for i, w in enumerate(ws)
        )
        for etf, ws in weights.items()
    }


def _all(ws: list[str]) -> dict[str, list[str]]:
    return {etf: ws for etf in FUNDS}


@pytest.mark.parametrize(
    "weights",
    [
        ["18", "18", "18", "18", "18"],  # 90 exactly: permitted
        ["22", "22", "22", "22", "22"],  # 110 exactly: permitted
        ["20", "20", "20", "20", "20"],  # five exactly: permitted
    ],
)
def test_the_validator_permits_at_the_boundaries(weights: list[str]) -> None:
    assert MIN_EQUITIES_PER_FUND == 5
    assert (WEIGHT_BAND_LOW, WEIGHT_BAND_HIGH) == (Decimal("90"), Decimal("110"))
    assert validate_funds(_funds(_all(weights))) == []


@pytest.mark.parametrize(
    ("weights", "rule"),
    [
        (["18", "18", "18", "18", "17.999999999999"], SnapshotRule.WEIGHT_BAND),
        (["22", "22", "22", "22", "22.000000000001"], SnapshotRule.WEIGHT_BAND),
        (["25", "25", "25", "25"], SnapshotRule.TOO_FEW_EQUITIES),
    ],
)
def test_the_validator_refuses_past_the_boundaries(weights: list[str], rule: SnapshotRule) -> None:
    funds = _funds({**_all(["20"] * 5), "XLRE": weights})
    problems = validate_funds(funds)
    assert [(p.rule, p.etf) for p in problems] == [(rule, "XLRE")]


def test_the_validator_refuses_a_missing_fund() -> None:
    funds = _funds(_all(["20"] * 5))
    del funds["XLC"]
    assert [(p.rule, p.etf) for p in validate_funds(funds)] == [(SnapshotRule.MISSING_FUND, "XLC")]
