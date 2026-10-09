"""Decision 21's tradeability inputs from Alpaca: assets, standard root, ADV bars.

Replayed from ``tests/fixtures/alpaca/p4_*.json``, recorded 2026-09-24 by
``tests/fixtures/record_alpaca_news.py``. The asset fixture is **trimmed**
(nine of 14,379 rows; its envelope says so). Anything synthesised in a test
is labelled SYNTHETIC where it is built.
"""

import json
import logging
from datetime import date, datetime, time, timezone
from typing import Any

import pytest

from corollary.calendars import NYSE_TZ
from corollary.data.news.tradeability import (
    TradeabilityFailure,
    ADV_LOOKBACK_SESSIONS,
    ADV_SESSIONS,
    adv_request_start,
    adv_window,
    adv_window_start,
    assess_tradeability,
)
from corollary.data.providers.alpaca import ADV_MAX_SYMBOLS, FeedConfig
from corollary.data.providers.interface import AssetDirectory, EquityAsset, ProviderError
from corollary.ratelimit import ALPACA_DATA_HOST, ALPACA_PAPER_TRADING_HOST
from tests.data.providers.conftest import load_fixture, single

pytestmark = pytest.mark.asyncio

#: The recording day, frozen so the root-check horizon and ADV window are fixed.
RECORDED_P4 = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
SESSION = date(2026, 9, 24)


def literal(body: Any, status: int = 200) -> tuple[int, str]:
    return (status, json.dumps(body))


def asset_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = load_fixture("p4_assets_active_sample")["body"]
    return rows


# ------------------------------------------------------------ asset list


async def test_one_unfiltered_request_on_the_trading_host(make_provider):
    provider, transport = make_provider(single("p4_assets_active_sample"), now=RECORDED_P4)
    await provider.active_equities()

    (request,) = transport.requests
    assert request.url.host == ALPACA_PAPER_TRADING_HOST
    assert request.url.path == "/v2/assets"
    params = dict(request.url.params)
    assert params == {"status": "active", "asset_class": "us_equity"}
    # Measured: the unfiltered list carries `attributes` on every row, so
    # filtering on has_options would only hide the non-optionable equities
    # the ingest's tag filter needs to recognise.
    assert "attributes" not in params


async def test_the_recorded_sample_decodes_with_has_options_and_names(make_provider):
    provider, _ = make_provider(single("p4_assets_active_sample"), now=RECORDED_P4)
    directory = await provider.active_equities()

    assert isinstance(directory, AssetDirectory)
    assert directory.skipped == 0
    assert len(directory) == len(asset_rows()) == 9
    aapl = directory.get("AAPL")
    assert aapl == EquityAsset(
        symbol="AAPL",
        name="Apple Inc. Common Stock",
        tradable=True,
        has_options=True,
        exchange="NASDAQ",
    )
    # Recorded: an ARCA-listed name without options, and an untradable OTC right.
    assert directory.get("AAA") is not None and directory.get("AAA").has_options is False
    right = directory.get("AACRF")
    assert right is not None and right.tradable is False and right.has_options is False
    assert directory.optionable() == frozenset(
        {"AAPL", "AMC", "BRK.B", "GME", "NVDA", "SPY", "XRX"}
    )


async def test_lookups_normalise_class_shares_and_case(make_provider):
    provider, _ = make_provider(single("p4_assets_active_sample"), now=RECORDED_P4)
    directory = await provider.active_equities()
    assert "brk/b" in directory and "BRK-B" in directory
    assert "BTCUSD" not in directory
    assert directory.get("brk.b") is directory.get("BRK.B")


async def test_malformed_and_foreign_rows_are_skipped_counted_and_logged(make_provider, caplog):
    good = asset_rows()[0]
    bad: list[Any] = [
        "not an object",
        {**good, "symbol": None},
        {**good, "symbol": "BTC/USD", "class": "crypto"},
        {**good, "symbol": "OLD", "status": "inactive"},
        {**good, "symbol": "TRD", "tradable": "yes"},
        {**good, "symbol": "ATT", "attributes": "has_options"},
        dict(good),  # the same symbol twice: the first row is kept
    ]
    # SYNTHETIC rows around one recorded row.
    provider, _ = make_provider(lambda _r: literal([good, *bad]), now=RECORDED_P4)
    with caplog.at_level(logging.WARNING, logger="corollary.data.providers.alpaca"):
        directory = await provider.active_equities()
    assert [a.symbol for a in directory.assets] == [good["symbol"]]
    assert directory.skipped == len(bad)
    events = [r for r in caplog.records if getattr(r, "event", None) == "alpaca_asset_row_skipped"]
    assert len(events) == len(bad)


async def test_the_recorded_sample_has_no_row_missing_attributes(make_provider, caplog):
    provider, _ = make_provider(single("p4_assets_active_sample"), now=RECORDED_P4)
    with caplog.at_level(logging.WARNING, logger="corollary.data.providers.alpaca"):
        directory = await provider.active_equities()
    assert directory.missing_attributes == 0
    assert not [
        r for r in caplog.records if getattr(r, "event", None) == "alpaca_asset_attributes_missing"
    ]


async def test_a_row_without_attributes_fails_closed_counted_and_logged(make_provider, caplog):
    # SYNTHETIC rows derived from the recorded AAPL row. A row with no
    # ``attributes`` array still reads as has_options=False -- failing closed
    # -- but a directory full of them would silently empty the optionable
    # set, so the count is carried and one WARNING names it.
    aapl = next(r for r in asset_rows() if r["symbol"] == "AAPL")
    absent = {k: v for k, v in aapl.items() if k != "attributes"}
    null = {**aapl, "symbol": "NVDA", "attributes": None}
    empty = {**aapl, "symbol": "SPY", "attributes": []}  # present, just empty
    provider, _ = make_provider(lambda _r: literal([absent, null, empty]), now=RECORDED_P4)
    with caplog.at_level(logging.WARNING, logger="corollary.data.providers.alpaca"):
        directory = await provider.active_equities()

    assert len(directory) == 3 and directory.skipped == 0
    assert directory.optionable() == frozenset()
    assert directory.missing_attributes == 2
    (warning,) = [
        r for r in caplog.records if getattr(r, "event", None) == "alpaca_asset_attributes_missing"
    ]
    assert warning.levelno == logging.WARNING
    assert warning.count == 2
    assert warning.sample == ["AAPL", "NVDA"]


async def test_a_body_that_is_not_a_list_raises(make_provider):
    provider, _ = make_provider(lambda _r: literal({"assets": []}), now=RECORDED_P4)
    with pytest.raises(ProviderError):
        await provider.active_equities()


async def test_a_directory_refuses_two_assets_on_one_symbol():
    one = EquityAsset(symbol="BRK.B", name="a", tradable=True, has_options=True, exchange="NYSE")
    two = EquityAsset(symbol="BRK/B", name="b", tradable=True, has_options=True, exchange="NYSE")
    with pytest.raises(ValueError, match="BRK.B"):
        AssetDirectory(assets=(one, two))


# ------------------------------------------------------------ standard root


async def test_the_root_check_is_one_narrow_request_with_an_explicit_horizon(make_provider):
    provider, transport = make_provider(single("p4_contracts_root_aapl"), now=RECORDED_P4)
    assert await provider.has_standard_root("aapl") is True

    (request,) = transport.requests
    assert request.url.host == ALPACA_PAPER_TRADING_HOST
    assert request.url.path == "/v2/options/contracts"
    params = dict(request.url.params)
    assert params["underlying_symbols"] == "AAPL"
    assert params["root_symbol"] == "AAPL"
    assert params["limit"] == "1"
    assert params["status"] == "active"
    # The default is *the next weekend*: without this, a name with no weekly
    # expiry this week reads as having no standard contract at all.
    horizon = date.fromisoformat(params["expiration_date_lte"])
    assert (horizon - SESSION).days >= 3 * 365


async def test_the_default_window_is_why_the_horizon_is_sent():
    # Recorded evidence, not provider behaviour: the same XRX request without
    # a horizon came back empty; with one, a standard XRX contract.
    without = load_fixture("p4_contracts_root_default_window_xrx")
    with_horizon = load_fixture("p4_contracts_root_xrx")
    assert "expiration_date_lte" not in without["request"]
    assert without["body"]["option_contracts"] == []
    (contract,) = with_horizon["body"]["option_contracts"]
    assert contract["root_symbol"] == contract["underlying_symbol"] == "XRX"


@pytest.mark.parametrize(
    "fixture,ticker,expected",
    [
        ("p4_contracts_root_aapl", "AAPL", True),
        ("p4_contracts_root_gme", "GME", True),
        ("p4_contracts_root_xrx", "XRX", True),
        # A has_options underlying whose only live contracts are adjusted
        # (AIFU1), found by the recorder's hunt on 2026-09-24.
        ("p4_contracts_root_adjusted_only", "AIFU", False),
    ],
)
async def test_recorded_root_checks(make_provider, fixture, ticker, expected):
    provider, _ = make_provider(single(fixture), now=RECORDED_P4)
    assert await provider.has_standard_root(ticker) is expected


async def test_the_adjusted_only_fixture_really_is_adjusted_only():
    recorded = load_fixture("p4_contracts_root_adjusted_only")
    assert recorded["body"]["option_contracts"] == []
    sample = recorded["underlying_contracts_sample"]["option_contracts"]
    assert sample, "the recording must show AIFU does have (adjusted) contracts"
    assert {(c["underlying_symbol"], c["root_symbol"]) for c in sample} == {("AIFU", "AIFU1")}


async def test_an_ignored_root_filter_fails_closed(make_provider):
    # If the vendor ever ignored root_symbol, limit=1 could hand back an
    # adjusted contract. Served here: the recorded GME1 contract, for a GME
    # standard-root question. It must answer False, not pass.
    provider, _ = make_provider(single("p4_contracts_root_gme1"), now=RECORDED_P4)
    assert await provider.has_standard_root("GME") is False


async def test_a_class_share_answers_false_because_occ_drops_the_dot(make_provider):
    # SYNTHETIC: a BRKB-rooted contract for BRK.B (the recorded AAPL row, re-keyed).
    row = dict(load_fixture("p4_contracts_root_aapl")["body"]["option_contracts"][0])
    row.update(underlying_symbol="BRK.B", root_symbol="BRKB", symbol="BRKB260925C00110000")
    provider, transport = make_provider(
        lambda _r: literal({"option_contracts": [row], "next_page_token": None}), now=RECORDED_P4
    )
    assert await provider.has_standard_root("BRK.B") is False
    assert dict(transport.requests[0].url.params)["underlying_symbols"] == "BRK.B"


@pytest.mark.parametrize("ticker", ["AAPL,GME", "", "AAPL1X2", "BTC/USD1"])
async def test_a_malformed_ticker_is_refused_before_any_request(make_provider, ticker):
    provider, transport = make_provider(single("p4_contracts_root_aapl"), now=RECORDED_P4)
    with pytest.raises(ValueError):
        await provider.has_standard_root(ticker)
    assert transport.requests == []


async def test_a_body_without_a_contract_list_raises_rather_than_answering_no(make_provider):
    provider, _ = make_provider(lambda _r: literal({"next_page_token": None}), now=RECORDED_P4)
    with pytest.raises(ProviderError):
        await provider.has_standard_root("AAPL")


def _aapl_contract() -> dict[str, Any]:
    row: dict[str, Any] = dict(load_fixture("p4_contracts_root_aapl")["body"]["option_contracts"][0])
    return row


MALFORMED_CONTRACTS: list[tuple[str, Any]] = [
    ("not an object", "a string row"),
    ("missing symbol", {k: v for k, v in _aapl_contract().items() if k != "symbol"}),
    ("missing expiration", {k: v for k, v in _aapl_contract().items() if k != "expiration_date"}),
    ("garbage strike", {**_aapl_contract(), "strike_price": "one hundred"}),
    ("garbage expiration", {**_aapl_contract(), "expiration_date": "someday"}),
    ("null multiplier", {**_aapl_contract(), "multiplier": None}),
    # SYNTHETIC: deliverables entries that are not objects raise AttributeError
    # inside the decoder (``item.get``) -- it must still surface as ProviderError.
    ("deliverables of strings", {**_aapl_contract(), "deliverables": ["x"]}),
    ("deliverables of nulls", {**_aapl_contract(), "deliverables": [None]}),
]


@pytest.mark.parametrize("label,bad", MALFORMED_CONTRACTS, ids=[m[0] for m in MALFORMED_CONTRACTS])
async def test_a_malformed_contract_row_raises_a_provider_error_never_a_bare_one(
    make_provider, label, bad
):
    # SYNTHETIC rows derived from the recorded AAPL contract. The caller
    # catches ProviderError and records *unchecked* (None); a KeyError or
    # ValueError escaping instead would crash the tradeability pass.
    provider, _ = make_provider(
        lambda _r: literal({"option_contracts": [bad], "next_page_token": None}), now=RECORDED_P4
    )
    with pytest.raises(ProviderError, match="AAPL"):
        await provider.has_standard_root("AAPL")


async def test_a_malformed_contract_error_quotes_no_credential(make_provider):
    from tests.data.providers.conftest import TEST_CREDENTIALS

    bad = {**_aapl_contract(), "strike_price": f"x{TEST_CREDENTIALS.secret_key}"}
    del bad["symbol"]
    bad["note"] = TEST_CREDENTIALS.key_id
    provider, _ = make_provider(
        lambda _r: literal({"option_contracts": [bad], "next_page_token": None}), now=RECORDED_P4
    )
    with pytest.raises(ProviderError) as caught:
        await provider.has_standard_root("AAPL")
    assert TEST_CREDENTIALS.secret_key not in str(caught.value)
    assert TEST_CREDENTIALS.key_id not in str(caught.value)


def _load_recorder() -> Any:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "fixtures" / "record_alpaca_news.py"
    spec = importlib.util.spec_from_file_location("_record_alpaca_news_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_recorder_builds_its_root_request_from_the_providers_helper(make_provider):
    from corollary.data.providers import alpaca

    recorder = _load_recorder()
    assert not hasattr(recorder, "_root_params"), "one source for the root request"
    assert recorder.standard_root_params is alpaca.standard_root_params

    # And the provider sends exactly what the helper builds.
    provider, transport = make_provider(single("p4_contracts_root_aapl"), now=RECORDED_P4)
    await provider.has_standard_root("AAPL")
    sent = dict(transport.requests[0].url.params)
    built = alpaca.standard_root_params("AAPL", "AAPL", today=SESSION)
    assert sent == {k: str(v) for k, v in built.items()}


# ------------------------------------------------------------------ ADV bars

#: Two configurations whose historical and realtime values differ, so an
#: assertion on the feed can never pass by the two coinciding.
FEED_PAIRS = [
    FeedConfig(options="indicative", stock_historical="sip", stock_realtime="iex"),
    FeedConfig(options="indicative", stock_historical="iex", stock_realtime="delayed_sip"),
]


@pytest.mark.parametrize("feeds", FEED_PAIRS, ids=lambda f: f"hist={f.stock_historical}")
async def test_adv_bars_carry_the_historical_feed_never_the_realtime_one(make_provider, feeds):
    assert feeds.stock_historical != feeds.stock_realtime
    provider, transport = make_provider(
        single("p4_stock_bars_adv"), feeds=feeds, now=RECORDED_P4
    )
    await provider.adv_daily_bars(["AAPL", "SPY", "XRX"], session_date=SESSION)

    (request,) = transport.requests
    assert request.url.host == ALPACA_DATA_HOST
    assert request.url.path == "/v2/stocks/bars"
    params = dict(request.url.params)
    assert params["feed"] == feeds.stock_historical
    assert params["feed"] != feeds.stock_realtime


async def test_adv_bars_request_the_window_and_its_listing_lookback(make_provider):
    """Q10: the request reaches 252 sessions past the window's start, so the
    filter can tell a recent listing from an established name with gaps --
    including one suspended for anything up to a year."""
    provider, transport = make_provider(single("p4_stock_bars_adv"), now=RECORDED_P4)
    await provider.adv_daily_bars(["aapl", "SPY", "XRX", "AAPL"], session_date=SESSION)
    params = transport.params_for("/v2/stocks/bars")

    first = adv_request_start(SESSION)
    assert first < adv_window_start(SESSION)
    assert ADV_LOOKBACK_SESSIONS + ADV_SESSIONS == 272
    assert params["symbols"] == "AAPL,SPY,XRX"
    assert params["timeframe"] == "1Day"
    assert params["adjustment"] == "split"
    start = datetime.fromisoformat(params["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(params["end"].replace("Z", "+00:00"))
    assert start == datetime.combine(first, time(0), tzinfo=NYSE_TZ)
    # The session_date bar is stamped midnight New York; the request stops
    # one second short of it.
    assert end < datetime.combine(SESSION, time(0), tzinfo=NYSE_TZ)
    assert end.astimezone(NYSE_TZ).date() == date(2026, 9, 23)


#: ``p4_stock_bars_adv_lookback.json`` was recorded on Saturday 2026-09-26
#: (23:49 UTC) by ``record_alpaca_news.py bars``, requesting from
#: ``adv_request_start(2026-09-26)`` -- the window plus the one-year lookback.
RECORDED_LOOKBACK = datetime(2026, 9, 26, 23, 49, 15, tzinfo=timezone.utc)
LOOKBACK_SESSION = date(2026, 9, 26)


async def test_recorded_adv_bars_feed_the_tradeability_filter(make_provider):
    """Real SIP bars, requested from ``adv_request_start``, drive the
    established branch: every name has bars before its window."""
    provider, transport = make_provider(
        single("p4_stock_bars_adv_lookback"), now=RECORDED_LOOKBACK
    )
    bars = await provider.adv_daily_bars(
        ["AAPL", "SPY", "XRX"], session_date=LOOKBACK_SESSION
    )
    params = transport.params_for("/v2/stocks/bars")
    first = adv_request_start(LOOKBACK_SESSION)
    assert datetime.fromisoformat(params["start"].replace("Z", "+00:00")) == datetime.combine(
        first, time(0), tzinfo=NYSE_TZ
    )

    assert set(bars) == {"AAPL", "SPY", "XRX"}
    window = adv_window(LOOKBACK_SESSION)
    for symbol, series in bars.items():
        days = sorted(item.at.astimezone(NYSE_TZ).date() for item in series)
        # The recording starts on the request's first session, well before the
        # window -- so these names are judged as established, not as listings.
        assert days[0] == first, symbol
        assert days[0] < window[0], symbol
        assert days[-1] == window[-1], symbol

    aapl = assess_tradeability(
        "AAPL",
        has_options=True,
        standard_root=True,
        daily_bars=bars["AAPL"],
        session_date=LOOKBACK_SESSION,
    )
    assert aapl.sessions_available == len(window) == 20
    assert aapl.passes, aapl.failures

    # The branch, proven on real data: drop AAPL's bar on the window's first
    # session. Its lookback bars still mark it established, so it is judged
    # over all 20 sessions with the gap as zero volume -- not re-read as a
    # 19-session listing.
    gapped = [b for b in bars["AAPL"] if b.at.astimezone(NYSE_TZ).date() != window[0]]
    gapped_result = assess_tradeability(
        "AAPL", has_options=True, standard_root=True, daily_bars=gapped,
        session_date=LOOKBACK_SESSION,
    )
    assert gapped_result.sessions_available == 20
    kept = [b.volume for b in gapped if b.at.astimezone(NYSE_TZ).date() in set(window)]
    assert len(kept) == 19
    assert gapped_result.avg_volume_20d == sum(kept) // 20

    xrx = assess_tradeability(
        "XRX",
        has_options=True,
        standard_root=True,
        daily_bars=bars["XRX"],
        session_date=LOOKBACK_SESSION,
    )
    # Recorded, not asserted as a fact about Xerox: whatever XRX printed, the
    # filter ran on 20 complete SIP sessions as an established name.
    assert xrx.sessions_available == 20
    assert TradeabilityFailure.NO_COMPLETED_SESSION not in xrx.failures
    assert TradeabilityFailure.STALE_BARS not in xrx.failures


async def test_more_than_the_symbol_ceiling_is_refused_not_split(make_provider):
    provider, transport = make_provider(single("p4_stock_bars_adv"), now=RECORDED_P4)
    too_many = [f"A{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(ADV_MAX_SYMBOLS + 1)]
    with pytest.raises(ValueError, match=str(ADV_MAX_SYMBOLS)):
        await provider.adv_daily_bars(too_many, session_date=SESSION)
    assert transport.requests == []


async def test_exactly_the_symbol_ceiling_is_one_request(make_provider):
    provider, transport = make_provider(single("p4_stock_bars_adv"), now=RECORDED_P4)
    at_ceiling = [f"A{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(ADV_MAX_SYMBOLS)]
    await provider.adv_daily_bars(at_ceiling, session_date=SESSION)
    assert len(transport.requests) == 1


async def test_no_symbols_is_no_request_and_a_bad_symbol_raises(make_provider):
    provider, transport = make_provider(single("p4_stock_bars_adv"), now=RECORDED_P4)
    assert await provider.adv_daily_bars([], session_date=SESSION) == {}
    with pytest.raises(ValueError):
        await provider.adv_daily_bars(["AAPL", "BTC,USD"], session_date=SESSION)
    assert transport.requests == []
