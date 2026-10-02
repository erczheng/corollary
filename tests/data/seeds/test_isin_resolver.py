"""The OpenFIGI ISIN resolver: Q17's acceptance rule, batching, and the ``isin_ticker`` cache.

Offline. OpenFIGI is served from the **live** recording in
``tests/fixtures/openfigi/`` (the 29 ISIN-only SPDR holdings, recorded
keyless through the provider's exempt call) via an ``httpx.MockTransport``
handed to a real :class:`OpenFigiProvider`, so every request asserted on is
the one the provider actually builds. Synthetic cases build
:class:`MappingResult` values directly and are labelled SYNTHETIC.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.data.providers.interface import (
    AssetDirectory,
    EquityAsset,
    ProviderError,
    RateLimitedError,
)
from corollary.data.providers.openfigi import (
    FigiRecord,
    MappingResult,
    OpenFigiCredentials,
    OpenFigiProvider,
    parse_mapping_response,
)
from corollary.data.seeds import normalize_symbol
from corollary.data.seeds.isin import (
    IsinRefusal,
    OpenFigiIsinResolver,
    accept_mapping,
)
from corollary.data.seeds.nport import BatchIsinResolver, IsinResolver, NoIsinResolver
from corollary.db.models import Base, IsinTicker
from corollary.db.session import create_db_engine, sqlite_url
from corollary.ratelimit import HostRateLimiter

OPENFIGI = Path(__file__).resolve().parents[2] / "fixtures" / "openfigi"
NOW = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


def _load_live() -> tuple[dict[str, Any], dict[str, str]]:
    """Each ISIN's raw recorded result, and its fund, from the three batch files."""
    raw: dict[str, Any] = {}
    fund: dict[str, str] = {}
    for n in (1, 2, 3):
        envelope = json.loads((OPENFIGI / f"mapping_batch_{n}.json").read_text("utf-8"))
        for context, result in zip(envelope["context"], envelope["response"], strict=True):
            raw[context["isin"]] = result
            fund[context["isin"]] = context["fund"]
    return raw, fund


LIVE_RAW, LIVE_FUND = _load_live()
LIVE_ISINS = tuple(LIVE_RAW)
LIVE_RESULTS = parse_mapping_response(LIVE_ISINS, [LIVE_RAW[i] for i in LIVE_ISINS])

#: What the live recording's single US composite equity record says per ISIN
#: (2026-10-01). Read off the fixture, not assumed: each has exactly one.
LIVE_TICKERS = {
    "JE00BV7DQ550": "AMCR", "IE0001827041": "CRH", "IE000S9YS762": "LIN",
    "IE00028FXN24": "SW", "NL0009434992": "LYB", "IE00BLP1HW54": "AON",
    "BMG0450A1053": "ACGL", "BMG3223R1088": "EG", "BMG491BT1088": "IVZ",
    "IE00BDB6Q211": "WTW", "CH0044328745": "CB", "IE00BFRT3W74": "ALLE",
    "IE00B8KQN827": "ETN", "IE00BY7QL619": "JCI", "IE00BLS09M33": "PNR",
    "IE00BK9ZQ967": "TT", "IE00B4BNMY34": "ACN", "IE00BKVD2N49": "STX",
    "IE000IVNQZ81": "TEL", "NL0009538784": "NXPI", "SG9999000020": "FLEX",
    "CH1300646267": "BG", "IE00BTN1Y115": "MDT", "IE00BFY8C754": "STE",
    "BMG2004J1036": "CCL", "JE00BTDN8H13": "APTV", "BMG667211046": "NCLH",
    "CH0114405324": "GRMN", "LR0008862868": "RCL",
}


def directory_of(symbols: set[str] | frozenset[str]) -> AssetDirectory:
    return AssetDirectory(tuple(
        EquityAsset(symbol=s, name="n/a", tradable=True, has_options=True, exchange="NYSE")
        for s in sorted({normalize_symbol(s) for s in symbols})
    ))


ALL_LISTED = directory_of(set(LIVE_TICKERS.values()))


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class LiveOpenFigi:
    """Answers each job from the live recording; ``status`` overrides with an error code."""

    def __init__(self) -> None:
        self.requests: list[list[str]] = []
        self.status: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v3/mapping"
        jobs = json.loads(request.content)
        self.requests.append([job["idValue"] for job in jobs])
        if self.status is not None:
            return httpx.Response(self.status, text="Too Many Requests")
        return httpx.Response(
            200,
            json=[LIVE_RAW.get(job["idValue"], {"warning": "No identifier found."}) for job in jobs],
        )


def provider_for(fake: LiveOpenFigi) -> OpenFigiProvider:
    clock = FakeClock()
    return OpenFigiProvider(
        credentials=OpenFigiCredentials(api_key=None),
        transport=httpx.MockTransport(fake),
        limiter=HostRateLimiter(clock=clock, sleep=clock.sleep),
    )


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_db_engine(sqlite_url(tmp_path / "corollary.db"))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def sessions(engine: Engine) -> Callable[[], Session]:
    return lambda: Session(engine)


def cache_rows(sessions: Callable[[], Session]) -> dict[str, IsinTicker]:
    with sessions() as session:
        return {row.isin: row for row in session.scalars(select(IsinTicker))}


async def resolve_all(
    resolver: OpenFigiIsinResolver, isins: tuple[str, ...] = LIVE_ISINS
) -> dict[str, str | None]:
    """The builder's call pattern: one prefetch, then one resolve per ISIN."""
    await resolver.prefetch(list(isins))
    return {isin: await resolver.resolve(isin, fund=LIVE_FUND.get(isin, "XLB")) for isin in isins}


# ------------------------------------------------------------------ seam shape


def test_the_resolver_satisfies_both_seams_and_noisin_has_no_prefetch(
    sessions: Callable[[], Session],
) -> None:
    resolver = OpenFigiIsinResolver(
        provider_for(LiveOpenFigi()), sessions, directory=ALL_LISTED
    )
    assert isinstance(resolver, BatchIsinResolver)
    seam: IsinResolver = resolver
    assert seam is resolver
    assert not isinstance(NoIsinResolver(), BatchIsinResolver)


# --------------------------------------------------- acceptance: live fixtures


def test_all_29_live_isins_resolve_when_the_directory_lists_them() -> None:
    assert len(LIVE_RESULTS) == 29
    got = {isin: accept_mapping(result, ALL_LISTED) for isin, result in LIVE_RESULTS.items()}
    assert {isin: v.ticker for isin, v in got.items()} == LIVE_TICKERS
    assert all(v.refusal is None for v in got.values())
    linde = got["IE000S9YS762"]
    assert linde.composite_figi == "BBG01FND0CC1"
    assert linde.security_types == ("Common Stock",)
    # Many records per ISIN (other exchanges), exactly one US composite equity.
    assert linde.records > 1 and linde.candidates == ("LIN",)


@pytest.mark.risk
def test_a_live_ticker_missing_from_the_directory_is_unresolved() -> None:
    without_linde = directory_of(set(LIVE_TICKERS.values()) - {"LIN"})
    verdict = accept_mapping(LIVE_RESULTS["IE000S9YS762"], without_linde)
    assert verdict.ticker is None
    assert verdict.refusal is IsinRefusal.NOT_LISTED
    assert verdict.candidates == ("LIN",)
    # The other 28 are unaffected.
    others = [accept_mapping(r, without_linde) for i, r in LIVE_RESULTS.items() if i != "IE000S9YS762"]
    assert all(v.ticker is not None for v in others)


@pytest.mark.risk
def test_no_directory_means_every_live_isin_is_unresolved() -> None:
    for result in LIVE_RESULTS.values():
        verdict = accept_mapping(result, None)
        assert verdict.ticker is None and verdict.refusal is IsinRefusal.NO_DIRECTORY


# ----------------------------------------------- acceptance: synthetic cases


def rec(
    ticker: str | None,
    *,
    exch: str | None = "US",
    sector: str | None = "Equity",
    security_type: str | None = "Common Stock",
    composite: str | None = "BBG000SYNTH1",
) -> FigiRecord:
    """A SYNTHETIC OpenFIGI record."""
    return FigiRecord(
        figi=composite, ticker=ticker, exch_code=exch, market_sector=sector,
        security_type=security_type, security_type2=None, composite_figi=composite,
        share_class_figi=None, name="SYNTHETIC", security_description=ticker,
    )


SYN_ISIN = "IE0000000001"  # SYNTHETIC
SYN_DIR = directory_of({"SYNA", "SYNB", "BRK.B"})


@pytest.mark.risk
def test_two_us_composite_tickers_are_ambiguous_and_unresolved() -> None:
    result = MappingResult(SYN_ISIN, records=(rec("SYNA"), rec("SYNB", composite="BBG000SYNTH2")))
    verdict = accept_mapping(result, SYN_DIR)
    assert verdict.ticker is None and verdict.refusal is IsinRefusal.AMBIGUOUS_TICKER
    assert verdict.candidates == ("SYNA", "SYNB")


@pytest.mark.risk
def test_no_us_composite_record_is_unresolved() -> None:
    result = MappingResult(SYN_ISIN, records=(rec("SYNA", exch="LN"), rec("SYNA", exch="UN")))
    verdict = accept_mapping(result, SYN_DIR)
    assert verdict.ticker is None and verdict.refusal is IsinRefusal.NO_US_COMPOSITE_EQUITY


@pytest.mark.risk
def test_a_us_composite_record_outside_the_equity_sector_is_not_counted() -> None:
    result = MappingResult(SYN_ISIN, records=(rec("SYNA", sector="Corp"),))
    verdict = accept_mapping(result, SYN_DIR)
    assert verdict.ticker is None and verdict.refusal is IsinRefusal.NO_US_COMPOSITE_EQUITY
    # A non-Equity US record does not make an Equity one ambiguous, either.
    mixed = MappingResult(SYN_ISIN, records=(rec("SYNA"), rec("SYNB", sector="Corp")))
    assert accept_mapping(mixed, SYN_DIR).ticker == "SYNA"


@pytest.mark.risk
def test_a_warning_result_is_unresolved() -> None:
    verdict = accept_mapping(MappingResult(SYN_ISIN, warning="No identifier found."), SYN_DIR)
    assert verdict.ticker is None and verdict.refusal is IsinRefusal.WARNING
    assert verdict.detail == "No identifier found."


@pytest.mark.risk
def test_an_error_result_is_unresolved() -> None:
    verdict = accept_mapping(MappingResult(SYN_ISIN, error="Invalid idValue format"), SYN_DIR)
    assert verdict.ticker is None and verdict.refusal is IsinRefusal.ERROR


@pytest.mark.risk
def test_a_us_composite_equity_record_with_no_ticker_is_unresolved() -> None:
    result = MappingResult(SYN_ISIN, records=(rec("SYNA"), rec(None)))
    verdict = accept_mapping(result, SYN_DIR)
    assert verdict.ticker is None and verdict.refusal is IsinRefusal.MISSING_TICKER
    blank = MappingResult(SYN_ISIN, records=(rec("  "),))
    assert accept_mapping(blank, SYN_DIR).refusal is IsinRefusal.MISSING_TICKER


def test_a_class_share_resolves_in_the_dot_form() -> None:
    verdict = accept_mapping(MappingResult(SYN_ISIN, records=(rec("BRK/B"),)), SYN_DIR)
    assert verdict.ticker == "BRK.B"
    # Two spellings of one class share are one ticker, not two.
    both = MappingResult(SYN_ISIN, records=(rec("BRK/B"), rec("BRK.B")))
    assert accept_mapping(both, SYN_DIR).ticker == "BRK.B"


def test_security_type_is_recorded_and_never_filtered_on() -> None:
    result = MappingResult(SYN_ISIN, records=(rec("SYNA", security_type="ADR"),))
    verdict = accept_mapping(result, SYN_DIR)
    assert verdict.ticker == "SYNA" and verdict.security_types == ("ADR",)


def test_disagreeing_composite_figis_store_none_rather_than_one_of_them() -> None:
    result = MappingResult(
        SYN_ISIN, records=(rec("SYNA", composite="BBG000SYNTH1"), rec("SYNA", composite="BBG000SYNTH2"))
    )
    verdict = accept_mapping(result, SYN_DIR)
    assert verdict.ticker == "SYNA" and verdict.composite_figi is None


# ------------------------------------------------------------------ batching


@pytest.mark.asyncio
async def test_29_uncached_isins_are_three_requests_and_every_one_is_cached(
    sessions: Callable[[], Session],
) -> None:
    fake = LiveOpenFigi()
    provider = provider_for(fake)
    try:
        resolver = OpenFigiIsinResolver(provider, sessions, directory=ALL_LISTED, clock=lambda: NOW)
        answers = await resolve_all(resolver)
    finally:
        await provider.aclose()
    assert [len(r) for r in fake.requests] == [10, 10, 9]
    assert [i for r in fake.requests for i in r] == list(LIVE_ISINS)
    assert answers == LIVE_TICKERS
    rows = cache_rows(sessions)
    assert {isin: row.ticker for isin, row in rows.items()} == LIVE_TICKERS
    linde = rows["IE000S9YS762"]
    assert (linde.source, linde.composite_figi) == ("openfigi", "BBG01FND0CC1")
    assert linde.resolved_at == NOW and linde.resolved_at.tzinfo is not None


@pytest.mark.asyncio
async def test_a_resolve_nobody_prefetched_still_works_with_one_request(
    sessions: Callable[[], Session],
) -> None:
    fake = LiveOpenFigi()
    provider = provider_for(fake)
    try:
        resolver = OpenFigiIsinResolver(provider, sessions, directory=ALL_LISTED)
        assert await resolver.resolve("IE000S9YS762", fund="XLB") == "LIN"
    finally:
        await provider.aclose()
    assert fake.requests == [["IE000S9YS762"]]


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_an_openfigi_failure_propagates_and_caches_nothing(
    sessions: Callable[[], Session], status: int
) -> None:
    fake = LiveOpenFigi()
    fake.status = status
    provider = provider_for(fake)
    try:
        resolver = OpenFigiIsinResolver(provider, sessions, directory=ALL_LISTED)
        with pytest.raises(RateLimitedError if status == 429 else ProviderError):
            await resolver.prefetch(list(LIVE_ISINS))
    finally:
        await provider.aclose()
    assert len(fake.requests) == 1  # the first batch failed; nothing after it was asked
    assert cache_rows(sessions) == {}


@pytest.mark.risk
@pytest.mark.asyncio
async def test_with_no_directory_nothing_is_asked_and_nothing_resolves(
    sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    fake = LiveOpenFigi()
    provider = provider_for(fake)
    try:
        resolver = OpenFigiIsinResolver(provider, sessions, directory=None)
        answers = await resolve_all(resolver)
    finally:
        await provider.aclose()
    assert fake.requests == []
    assert set(answers.values()) == {None}
    assert cache_rows(sessions) == {}
    refusals = [r for r in caplog.records if getattr(r, "event", "") == "isin_unresolved"]
    assert len(refusals) == 29
    assert {getattr(r, "reason") for r in refusals} == {"no_directory"}


# --------------------------------------------------------------------- cache


@pytest.mark.asyncio
async def test_a_cache_hit_makes_no_request(sessions: Callable[[], Session]) -> None:
    first = LiveOpenFigi()
    provider = provider_for(first)
    try:
        await resolve_all(OpenFigiIsinResolver(provider, sessions, directory=ALL_LISTED))
    finally:
        await provider.aclose()
    assert len(first.requests) == 3

    second = LiveOpenFigi()
    provider = provider_for(second)
    try:
        answers = await resolve_all(OpenFigiIsinResolver(provider, sessions, directory=ALL_LISTED))
    finally:
        await provider.aclose()
    assert second.requests == []
    assert answers == LIVE_TICKERS


@pytest.mark.risk
@pytest.mark.asyncio
async def test_an_unresolved_isin_is_never_cached_and_is_asked_again_next_run(
    sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    without_linde = directory_of(set(LIVE_TICKERS.values()) - {"LIN"})
    for run in (1, 2):
        fake = LiveOpenFigi()
        provider = provider_for(fake)
        try:
            answers = await resolve_all(
                OpenFigiIsinResolver(provider, sessions, directory=without_linde)
            )
        finally:
            await provider.aclose()
        assert answers["IE000S9YS762"] is None
        assert "IE000S9YS762" not in cache_rows(sessions)
        # Run 1 asks all 29; run 2 asks only the one it could not accept.
        assert fake.requests == ([list(LIVE_ISINS[:10]), list(LIVE_ISINS[10:20]),
                                  list(LIVE_ISINS[20:])] if run == 1 else [["IE000S9YS762"]])
    refusals = [r for r in caplog.records if getattr(r, "event", "") == "isin_unresolved"]
    assert len(refusals) == 2
    for record in refusals:
        assert getattr(record, "isin") == "IE000S9YS762"
        assert getattr(record, "fund") == "XLB"
        assert getattr(record, "reason") == "not_listed"
        assert getattr(record, "security_types") == ["Common Stock"]
        assert getattr(record, "candidates") == ["LIN"]
    # The holding's name was never anywhere near the resolver.
    assert "Linde" not in caplog.text


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_cached_ticker_the_directory_dropped_is_unresolved_and_evicted(
    sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    fake = LiveOpenFigi()
    provider = provider_for(fake)
    try:
        await resolve_all(OpenFigiIsinResolver(provider, sessions, directory=ALL_LISTED))
    finally:
        await provider.aclose()
    assert "IE000S9YS762" in cache_rows(sessions)

    without_linde = directory_of(set(LIVE_TICKERS.values()) - {"LIN"})
    fake = LiveOpenFigi()
    provider = provider_for(fake)
    try:
        answers = await resolve_all(OpenFigiIsinResolver(provider, sessions, directory=without_linde))
    finally:
        await provider.aclose()
    # Unresolved for this run, without asking OpenFIGI, and the row is gone.
    assert answers["IE000S9YS762"] is None
    assert fake.requests == []
    assert "IE000S9YS762" not in cache_rows(sessions)
    assert len(cache_rows(sessions)) == 28
    evicted = [r for r in caplog.records if getattr(r, "reason", "") == "cached_not_listed"]
    assert len(evicted) == 1 and getattr(evicted[0], "isin") == "IE000S9YS762"

    # The next run asks OpenFIGI afresh, and accepts once the directory lists it again.
    fake = LiveOpenFigi()
    provider = provider_for(fake)
    try:
        answers = await resolve_all(OpenFigiIsinResolver(provider, sessions, directory=ALL_LISTED))
    finally:
        await provider.aclose()
    assert fake.requests == [["IE000S9YS762"]]
    assert answers["IE000S9YS762"] == "LIN"
    assert cache_rows(sessions)["IE000S9YS762"].ticker == "LIN"


def test_the_resolver_module_reaches_no_broker_and_no_http_client() -> None:
    """Outside the vendor surface: no broker, no engine execution, no HTTP of its own."""
    import ast

    path = Path(__file__).resolve().parents[3] / "corollary" / "data" / "seeds" / "isin.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for module in imported:
        assert "alpaca" not in module.lower(), module
        assert not module.startswith("corollary.engine"), module
        assert module.split(".")[0] not in {"httpx", "urllib", "http", "requests", "aiohttp"}, module
