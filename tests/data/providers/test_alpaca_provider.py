"""``AlpacaProvider`` against recorded responses from the real paper account.

Nothing here touches the network. The fixtures are real Alpaca payloads
captured on 2026-09-10 and replayed through ``httpx.MockTransport``, so the
request the provider builds is exercised while the response is deterministic.
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from corollary.data.providers.alpaca import (
    AlpacaCredentials,
    AlpacaProvider,
    CredentialsError,
    FeedConfig,
)
from corollary.data.providers.interface import (
    AnalyticsSource,
    BarTimeframe,
    FeedAccessError,
    OptionType,
    ProviderError,
    RateLimitedError,
)
from corollary.ratelimit import (
    ALPACA_DATA_HOST,
    ALPACA_PAPER_TRADING_HOST,
    DEFAULT_REQUESTS_PER_MINUTE,
)
from corollary.wire import ERROR_BODY_MAX, REDACTED

from .conftest import (
    BASIC_FEEDS,
    RECORDED_AT,
    TEST_CREDENTIALS,
    recorded_chain_instant,
    by_path,
    chain_page,
    contracts_page,
    load_fixture,
    sequence,
    single,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Money never touches a float
# --------------------------------------------------------------------------


async def test_prices_arrive_as_exact_decimals_not_floats(make_provider) -> None:
    """The claim the whole httpx-over-SDK decision rests on.

    Alpaca's market data API sends prices as JSON *numbers*. Left to
    ``json.loads``, ``4.019`` becomes an IEEE double and the exactness is gone
    before any of our code runs. ``parse_float=Decimal`` is what makes this
    assertion possible at all.
    """
    provider, _ = make_provider(single("stock_snapshots"))
    snapshots = await provider.stock_snapshots(["NVDA", "SPY", "AAPL"])

    quote = snapshots["NVDA"].latest_quote
    assert quote is not None
    assert isinstance(quote.bid, Decimal)
    assert isinstance(quote.ask, Decimal)
    # Exactness, not approximation: this is the assertion a float fails.
    raw = load_fixture("stock_snapshots")["body"]["NVDA"]["latestQuote"]
    assert quote.bid == Decimal(str(raw["bp"]))
    assert str(quote.bid) == str(raw["bp"])


async def test_a_high_precision_price_survives_exactly(make_provider) -> None:
    """VWAP carries six decimals, and a float cannot hold most of them.

    The expected values are read out of the fixture rather than written as
    literals: these are live prices and they move every time the recorder
    runs, but *exactness* is the property under test and it holds whatever the
    numbers are. Every assertion here fails under ``json.loads`` default
    parsing.
    """
    body = load_fixture("option_bars_daily")["body"]["bars"]
    symbol, rows = next(iter(body.items()))
    lossy = [
        row["vw"] for row in rows if Decimal(float(row["vw"])) != Decimal(str(row["vw"]))
    ]
    assert lossy, "no value in this fixture distinguishes Decimal from float"

    provider, _ = make_provider(single("option_bars_daily"))
    bars = await provider.option_bars(
        [symbol], start=datetime(2026, 8, 3, tzinfo=timezone.utc)
    )
    parsed = {str(bar.at.date()): bar.vwap for bar in bars[symbol]}
    for row in rows:
        vwap = parsed[row["t"][:10]]
        assert isinstance(vwap, Decimal)
        assert vwap == Decimal(str(row["vw"]))
        assert str(vwap) == str(row["vw"])


async def test_a_float_reaching_the_decimal_boundary_raises(make_provider) -> None:
    """Belt and braces: if _decode is ever bypassed, it fails loudly."""
    from corollary.data.providers.alpaca import _as_decimal

    with pytest.raises(TypeError, match="float"):
        _as_decimal(4.019)


# --------------------------------------------------------------------------
# Stock snapshots and bars
# --------------------------------------------------------------------------


async def test_stock_snapshots_map_every_member(make_provider) -> None:
    provider, transport = make_provider(single("stock_snapshots"))
    snapshots = await provider.stock_snapshots(["NVDA", "SPY", "AAPL"])

    assert set(snapshots) == {"NVDA", "SPY", "AAPL"}
    nvda = snapshots["NVDA"]
    assert nvda.latest_quote is not None
    assert nvda.latest_trade is not None
    assert nvda.daily_bar is not None
    assert nvda.previous_daily_bar is not None
    assert nvda.daily_bar.volume > 0
    assert nvda.previous_close == nvda.previous_daily_bar.close
    assert transport.params_for("/v2/stocks/snapshots")["feed"] == "iex"


async def test_the_stock_snapshot_response_is_a_bare_map(make_provider) -> None:
    """Unlike the option chain, which wraps its map in a `snapshots` key.

    Same word, two envelopes. Handling this wrongly returns an empty dict, not
    an error.
    """
    body = load_fixture("stock_snapshots")["body"]
    assert "snapshots" not in body
    assert set(body) == {"NVDA", "SPY", "AAPL"}


async def test_a_snapshots_price_falls_back_in_a_stated_order(make_provider) -> None:
    provider, _ = make_provider(single("stock_snapshots"))
    nvda = (await provider.stock_snapshots(["NVDA"]))["NVDA"]
    assert nvda.latest_quote is not None
    assert nvda.price == nvda.latest_quote.mid


async def test_stock_bars_parse_and_stay_ordered(make_provider) -> None:
    provider, transport = make_provider(single("stock_bars_daily"))
    bars = await provider.stock_bars(
        ["NVDA", "SPY"],
        timeframe=BarTimeframe.DAY,
        start=datetime(2026, 8, 3, tzinfo=timezone.utc),
        end=datetime(2026, 8, 14, tzinfo=timezone.utc),
    )
    assert set(bars) == {"NVDA", "SPY"}
    nvda = bars["NVDA"]
    assert len(nvda) > 1
    assert all(isinstance(bar.close, Decimal) for bar in nvda)
    assert [bar.at for bar in nvda] == sorted(bar.at for bar in nvda)
    assert all(bar.at.tzinfo is timezone.utc for bar in nvda)
    assert all(bar.low <= bar.close <= bar.high for bar in nvda)


async def test_bars_are_split_adjusted(make_provider) -> None:
    """Volume compared across a split boundary is otherwise wrong by the ratio."""
    provider, transport = make_provider(single("stock_bars_daily"))
    await provider.stock_bars(["NVDA"], start=datetime(2026, 8, 3, tzinfo=timezone.utc))
    assert transport.params_for("/v2/stocks/bars")["adjustment"] == "split"


async def test_latest_stock_quotes_parse(make_provider) -> None:
    provider, transport = make_provider(single("stock_quotes_latest"))
    quotes = await provider.latest_stock_quotes(["NVDA", "SPY"])
    assert set(quotes) == {"NVDA", "SPY"}
    assert isinstance(quotes["NVDA"].bid, Decimal)
    assert quotes["NVDA"].mid is not None
    assert transport.params_for("/v2/stocks/quotes/latest")["feed"] == "iex"


async def test_latest_option_quotes_parse(make_provider) -> None:
    provider, transport = make_provider(single("option_quotes_latest"))
    raw = load_fixture("option_quotes_latest")["body"]["quotes"]
    symbol, expected = next(iter(raw.items()))

    quote = (await provider.latest_option_quotes([symbol]))[symbol]
    assert quote.bid == Decimal(str(expected["bp"]))
    assert quote.ask == Decimal(str(expected["ap"]))
    assert quote.bid_size == expected["bs"]
    assert quote.mid == (quote.bid + quote.ask) / 2
    assert transport.params_for("/options/quotes/latest")["feed"] == "indicative"


async def test_option_bars_parse(make_provider) -> None:
    """Bars are the only historical option data that exists.

    There is no historical options *quotes* endpoint at all, and trades reach
    back seven days -- which is why the backtester needs the explicit spread
    model rather than a recorded one.
    """
    provider, _ = make_provider(single("option_bars_daily"))
    bars = await provider.option_bars(
        ["NVDA261016C00220000"],
        start=datetime(2026, 8, 3, tzinfo=timezone.utc),
        end=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    series = bars["NVDA261016C00220000"]
    assert len(series) > 5
    assert all(isinstance(bar.close, Decimal) for bar in series)
    assert [bar.at for bar in series] == sorted(bar.at for bar in series)
    assert all(bar.low <= bar.open <= bar.high for bar in series)


async def test_option_bars_send_no_feed_parameter(make_provider) -> None:
    """Verified live on 2026-09-10: this endpoint 400s on a `feed`.

    Every other options endpoint requires one, so this asymmetry is exactly
    the kind of thing a hand-written mapping gets wrong -- and did, until the
    recorder hit the 400.
    """
    provider, transport = make_provider(single("option_bars_daily"))
    await provider.option_bars(
        ["NVDA261016C00220000"], start=datetime(2026, 8, 3, tzinfo=timezone.utc)
    )
    params = transport.params_for("/v1beta1/options/bars")
    assert "feed" not in params
    assert params["timeframe"] == "1Day"


# --------------------------------------------------------------------------
# Feed selection -- the min_avg_volume rule
# --------------------------------------------------------------------------


async def test_historical_bars_use_sip_not_iex(make_provider) -> None:
    """The rule CLAUDE.md spends a paragraph on.

    IEX is ~2.5% of US volume. A 5,000,000 ``min_avg_volume`` computed from it
    filters on a fortieth of what the strategy author wrote, and nothing
    errors.
    """
    provider, transport = make_provider(single("stock_bars_daily"))
    await provider.stock_bars(
        ["NVDA"],
        start=datetime(2026, 8, 3, tzinfo=timezone.utc),
        end=datetime(2026, 8, 14, tzinfo=timezone.utc),
    )
    assert transport.params_for("/v2/stocks/bars")["feed"] == "sip"


async def test_an_explicit_recent_end_is_clamped_rather_than_downgraded(
    make_provider, caplog
) -> None:
    """The failure CLAUDE.md names by name, in its least visible form.

    ``end=datetime.now(tz)`` over a twenty-day window used to put the *whole*
    window on the real-time feed, because feed selection looked only at
    ``end``. Nothing errored and nothing logged: the scanner's 20-day average
    volume just became IEX's ~2.5% of real volume, so a ``min_avg_volume`` of
    5,000,000 filtered on a fortieth of what the strategy author wrote.

    Bars are the historical surface. The window is shortened to what SIP may
    serve, and the shortening is said out loud.
    """
    provider, transport = make_provider(single("stock_bars_daily"))
    with caplog.at_level(logging.WARNING):
        await provider.stock_bars(
            ["NVDA"],
            start=RECORDED_AT - timedelta(days=20),
            end=RECORDED_AT,
        )
    params = transport.params_for("/v2/stocks/bars")
    assert params["feed"] == "sip"
    assert params["end"] < RECORDED_AT.isoformat().replace("+00:00", "Z")
    messages = " ".join(record.getMessage().lower() for record in caplog.records)
    assert "volume" in messages, "a clamp nobody is told about is still silent"


async def test_bars_never_reach_for_the_realtime_feed(make_provider) -> None:
    """Not even when it is configured to something other than IEX.

    CLAUDE.md draws the line at the endpoint, not at the timestamp: *"Only the
    latest/snapshot endpoints and the live stream are IEX-limited."* So
    ``stock_realtime`` has no call site in the bars path at all, and a future
    edit cannot reintroduce one by adjusting a cutoff.
    """
    provider, transport = make_provider(
        single("stock_bars_daily"),
        feeds=FeedConfig(
            options="indicative", stock_historical="sip", stock_realtime="iex"
        ),
    )
    for end in (RECORDED_AT, RECORDED_AT - timedelta(days=1), None):
        await provider.stock_bars(
            ["NVDA"], start=RECORDED_AT - timedelta(days=30), end=end
        )
    for request in transport.requests:
        assert request.url.params["feed"] == "sip"


async def test_a_window_entirely_inside_the_embargo_is_refused(
    make_provider,
) -> None:
    """No honest historical answer exists, so no answer is given.

    Clamping here would invert the window; falling back to IEX would answer
    with a fortieth of the volume under the same name. The refusal names the
    endpoints that *are* real-time.
    """
    provider, transport = make_provider(single("stock_bars_daily"))
    with pytest.raises(ProviderError) as caught:
        await provider.stock_bars(
            ["NVDA"],
            timeframe=BarTimeframe.MINUTE,
            start=RECORDED_AT - timedelta(minutes=5),
            end=RECORDED_AT,
        )
    message = str(caught.value).lower()
    assert "volume" in message
    assert "snapshot" in message
    assert transport.requests == [], "a refused request must not cost a token"


async def test_bars_with_no_end_default_to_the_historical_window(
    make_provider,
) -> None:
    """"Up to the present" must not silently downgrade the whole request.

    Nobody writes ``end=`` when they mean now, so defaulting ``end`` to the
    clock would put every unbounded bars request on IEX -- the
    ``min_avg_volume`` failure in its most likely disguise.
    """
    provider, transport = make_provider(single("stock_bars_daily"))
    await provider.stock_bars(["NVDA"], start=datetime(2026, 8, 3, tzinfo=timezone.utc))
    params = transport.params_for("/v2/stocks/bars")
    assert params["feed"] == "sip"
    assert params["end"] < RECORDED_AT.isoformat().replace("+00:00", "Z")


async def test_snapshots_and_latest_quotes_use_the_realtime_feed(
    make_provider,
) -> None:
    provider, transport = make_provider(single("stock_snapshots"))
    await provider.stock_snapshots(["NVDA"])
    assert transport.params_for("/v2/stocks/snapshots")["feed"] == "iex"


async def test_option_requests_use_the_options_feed(make_provider) -> None:
    provider, transport = make_provider(chain_page("option_chain_nvda_page1"))
    await provider.option_snapshots("NVDA")
    assert transport.params_for("/options/snapshots")["feed"] == "indicative"


async def test_the_feed_is_never_a_literal(make_provider) -> None:
    """Change the config, and every request changes with it."""
    provider, transport = make_provider(
        chain_page("option_chain_nvda_page1"),
        feeds=FeedConfig(
            options="opra", stock_historical="sip", stock_realtime="sip"
        ),
    )
    await provider.option_snapshots("NVDA")
    assert transport.params_for("/options/snapshots")["feed"] == "opra"


async def test_a_naive_start_or_end_is_refused(make_provider) -> None:
    """Either bound, and the message says which.

    ``end`` matters as much as ``start`` now that ``end`` is compared against
    the embargo cutoff: a naive one used to reach that comparison and come
    back as a bare "can't compare offset-naive and offset-aware datetimes"
    from inside the provider, which names neither argument.
    """
    provider, _ = make_provider(single("stock_bars_daily"))
    with pytest.raises(ValueError, match="start must be timezone-aware"):
        await provider.stock_bars(["NVDA"], start=datetime(2026, 8, 3))
    with pytest.raises(ValueError, match="end must be timezone-aware"):
        await provider.stock_bars(
            ["NVDA"],
            start=datetime(2026, 8, 3, tzinfo=timezone.utc),
            end=datetime(2026, 8, 14),
        )


# --------------------------------------------------------------------------
# The option chain
# --------------------------------------------------------------------------


async def test_the_option_chain_response_wraps_its_map(make_provider) -> None:
    body = load_fixture("option_chain_nvda_page1")["body"]
    assert set(body) == {"snapshots", "next_page_token"}


async def test_pagination_follows_next_page_token_to_exhaustion(
    make_provider,
) -> None:
    """Two real pages, joined. A one-page implementation returns a short chain,
    and a short chain is a scanner filtering on a universe that is missing
    strikes -- a deterministic wrong answer."""
    provider, transport = make_provider(
        sequence(
            "option_chain_nvda_page1",
            "option_chain_nvda_page2",
            # A real last page: next_page_token is null, so the loop stops.
            "option_chain_nvda_dated",
        )
    )
    chain = await provider.option_snapshots("NVDA")

    page1 = load_fixture("option_chain_nvda_page1")["body"]["snapshots"]
    page2 = load_fixture("option_chain_nvda_page2")["body"]["snapshots"]
    last = load_fixture("option_chain_nvda_dated")["body"]["snapshots"]
    assert len(transport.requests) == 3
    assert len(chain) == len(set(page1) | set(page2) | set(last))
    assert len(chain) > len(page1)
    # The first request carried no token; each later one carried its
    # predecessor's.
    assert "page_token" not in transport.requests[0].url.params
    assert transport.requests[1].url.params["page_token"] == (
        load_fixture("option_chain_nvda_page1")["body"]["next_page_token"]
    )
    assert transport.requests[2].url.params["page_token"] == (
        load_fixture("option_chain_nvda_page2")["body"]["next_page_token"]
    )


async def test_a_page_loop_that_never_terminates_raises(make_provider) -> None:
    """Refuses to return a partial chain rather than truncating silently."""
    provider, _ = make_provider(single("option_chain_nvda_page1"))
    with pytest.raises(ProviderError, match="did not terminate"):
        await provider.option_snapshots("NVDA")


# --------------------------------------------------------------------------
# `limit` is a cap, not a page size
# --------------------------------------------------------------------------


def _endless_bars(served: dict[str, int]):
    """A bars endpoint that always has one more page.

    Alpaca's ``limit`` is *per page*, so a provider that passes it through and
    then follows ``next_page_token`` to exhaustion has a parameter that reads
    like a bound and bounds nothing. This handler makes that visible: it will
    keep answering forever, and honours the per-page ``limit`` exactly as
    Alpaca does.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        served["n"] += 1
        rows = [
            {
                "c": 1,
                "h": 1,
                "l": 1,
                "o": 1,
                "t": "2026-08-03T04:00:00Z",
                "v": 1,
                "n": 1,
                "vw": 1,
            }
            for _ in range(int(request.url.params["limit"]))
        ]
        return httpx.Response(
            200,
            json={"bars": {"NVDA": rows}, "next_page_token": "there-is-always-more"},
        )

    return handler


def _bounded_provider(served: dict[str, int], limiter) -> tuple:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_endless_bars(served)))
    provider = AlpacaProvider(
        credentials=TEST_CREDENTIALS,
        feeds=BASIC_FEEDS,
        client=client,
        limiter=limiter,
        now=lambda: RECORDED_AT,
    )
    return provider, client


async def test_limit_caps_the_bars_returned_not_the_page_size(limiter) -> None:
    """``limit=200`` must mean 200 bars, not "200 per page, forever".

    The old behaviour spent one request per page until the chain ended -- so a
    caller deliberately bounding a request got an unbounded one, and a
    one-year minute window would have burned the whole ``data.`` minute budget
    before raising.
    """
    served = {"n": 0}
    provider, client = _bounded_provider(served, limiter)
    bars = await provider.stock_bars(
        ["NVDA"],
        timeframe=BarTimeframe.MINUTE,
        start=RECORDED_AT - timedelta(days=365),
        limit=200,
    )
    await client.aclose()
    assert len(bars["NVDA"]) == 200
    assert served["n"] == 1, "200 bars is one page, so it is one request"


async def test_a_limit_larger_than_a_page_spends_exactly_the_pages_it_needs(
    limiter,
) -> None:
    served = {"n": 0}
    provider, client = _bounded_provider(served, limiter)
    bars = await provider.stock_bars(
        ["NVDA"],
        timeframe=BarTimeframe.MINUTE,
        start=RECORDED_AT - timedelta(days=365),
        limit=25_000,
    )
    await client.aclose()
    assert len(bars["NVDA"]) == 25_000
    assert served["n"] == 3, "10,000 a page, so 25,000 is three requests"


async def test_option_bars_honour_the_same_cap(limiter) -> None:
    """Two methods with one parameter name must not mean two things."""
    served = {"n": 0}
    provider, client = _bounded_provider(served, limiter)
    bars = await provider.option_bars(["NVDA261016C00220000"], limit=5)
    await client.aclose()
    assert served["n"] == 1
    assert sum(len(rows) for rows in bars.values()) == 5


async def test_the_cap_is_total_rows_across_symbols_as_the_abc_says(
    limiter,
) -> None:
    """One ambiguity worth removing rather than leaving to each call site.

    ``limit`` on a multi-symbol request could mean per symbol or in total, and
    the two differ by a factor of ``len(symbols)`` — enough to turn a bounded
    request back into an unbounded one on a 30-name scan. The ABC says total,
    which is also what Alpaca's own per-page ``limit`` means, so the provider
    can pass the remaining budget straight through as the page size instead of
    reconciling two different notions of "how many".
    """
    served = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        served["n"] += 1
        row = {
            "c": 1,
            "h": 1,
            "l": 1,
            "o": 1,
            "t": "2026-08-03T04:00:00Z",
            "v": 1,
            "n": 1,
            "vw": 1,
        }
        wanted = int(request.url.params["limit"])
        # Interleaved across symbols, which is how Alpaca fills a page.
        bars: dict[str, list[dict[str, object]]] = {"NVDA": [], "SPY": []}
        for index in range(wanted):
            bars["NVDA" if index % 2 == 0 else "SPY"].append(row)
        return httpx.Response(
            200, json={"bars": bars, "next_page_token": "there-is-always-more"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AlpacaProvider(
        credentials=TEST_CREDENTIALS,
        feeds=BASIC_FEEDS,
        client=client,
        limiter=limiter,
        now=lambda: RECORDED_AT,
    )
    bars = await provider.stock_bars(
        ["NVDA", "SPY"],
        timeframe=BarTimeframe.MINUTE,
        start=RECORDED_AT - timedelta(days=365),
        limit=10,
    )
    await client.aclose()
    assert sum(len(rows) for rows in bars.values()) == 10
    assert served["n"] == 1


async def test_an_unbounded_request_cannot_drain_a_hosts_minute_budget(
    limiter,
) -> None:
    """The page ceiling has to leave room for the polls running beside it.

    It used to equal the per-host minute ceiling exactly, so one runaway call
    consumed 100% of the ``data.`` budget and stalled every concurrent poll
    for a minute before raising.
    """
    served = {"n": 0}
    provider, client = _bounded_provider(served, limiter)
    with pytest.raises(ProviderError, match="did not terminate"):
        await provider.stock_bars(
            ["NVDA"],
            timeframe=BarTimeframe.MINUTE,
            start=RECORDED_AT - timedelta(days=365),
        )
    await client.aclose()
    assert served["n"] < DEFAULT_REQUESTS_PER_MINUTE // 2


async def test_a_nonsense_limit_is_refused_before_a_request_is_spent(
    limiter,
) -> None:
    served = {"n": 0}
    provider, client = _bounded_provider(served, limiter)
    with pytest.raises(ValueError, match="limit"):
        await provider.stock_bars(["NVDA"], limit=0)
    await client.aclose()
    assert served["n"] == 0


async def test_chain_filters_reach_the_query_string(make_provider) -> None:
    provider, transport = make_provider(single("option_chain_nvda_dated"))
    await provider.option_snapshots(
        "NVDA",
        expiration_lte=date(2026, 10, 16),
        strike_gte=Decimal("190"),
        strike_lte=Decimal("250"),
        option_type=OptionType.CALL,
    )
    params = transport.params_for("/options/snapshots")
    assert params["expiration_date_lte"] == "2026-10-16"
    assert params["strike_price_gte"] == "190"
    assert params["strike_price_lte"] == "250"
    assert params["type"] == "call"
    assert params["root_symbol"] == "NVDA"


async def test_a_decimal_strike_never_goes_out_in_scientific_notation(
    make_provider,
) -> None:
    """``str(Decimal('1E+2'))`` is ``'1E+2'``, which is not a strike filter."""
    provider, transport = make_provider(single("option_chain_nvda_dated"))
    await provider.option_snapshots("NVDA", strike_gte=Decimal("1E+2"))
    assert transport.params_for("/options/snapshots")["strike_price_gte"] == "100"


async def test_a_one_sided_quote_has_no_mid(make_provider) -> None:
    """0 means "no active bid", not "the bid is zero"."""
    provider, _ = make_provider(single("option_chain_nvda_dated"))
    chain = await provider.option_snapshots("NVDA")
    for snapshot in chain.values():
        quote = snapshot.latest_quote
        if quote is None:
            continue
        if quote.bid is None or quote.ask is None:
            assert quote.mid is None


# --------------------------------------------------------------------------
# IV and greeks -- decision 10
# --------------------------------------------------------------------------


async def test_raw_snapshots_never_derive_anything(make_provider) -> None:
    """``option_snapshots`` is the honest vendor mapping and computes nothing."""
    provider, _ = make_provider(single("option_chain_nvda_dated"))
    chain = await provider.option_snapshots("NVDA")
    assert all(
        snapshot.analytics_source is not AnalyticsSource.DERIVED
        for snapshot in chain.values()
    )


async def test_vendor_greeks_pass_through_and_are_labelled_vendor(
    make_provider,
) -> None:
    """A spec claim that fell: `indicative` **does** serve greeks, sometimes.

    The Phase 2 design states ``impliedVolatility`` and ``greeks`` are both
    ``None`` on this feed. Re-probing on 2026-09-10 found them populated on
    every strike of a five-week expiry, and near the money on the one-day
    expiry. So the VENDOR branch is live code, not a future-proofing gesture.
    """
    body = load_fixture("option_chain_nvda_dated")["body"]["snapshots"]
    assert all(entry.get("greeks") for entry in body.values())

    provider, _ = make_provider(single("option_chain_nvda_dated"))
    chain = await provider.option_snapshots("NVDA")
    sample = chain["NVDA261016C00220000"]
    assert sample.analytics_source is AnalyticsSource.VENDOR
    assert sample.greeks is not None
    assert isinstance(sample.greeks.delta, Decimal)
    assert isinstance(sample.implied_volatility, Decimal)
    assert sample.analytics_note == ""


async def test_our_derived_greeks_match_alpacas_on_the_same_contracts(
    make_provider,
) -> None:
    """The strongest check available: ground truth from the vendor itself.

    Decision 10 rests on Alpaca deriving IV and greeks with Black-Scholes too.
    On the five-week expiry the feed supplies both, so ours can be derived
    from the identical quote mid and identical spot and compared against
    theirs — an external check, not a self-consistency one.

    **This deliberately does not go through ``option_chain``.** That method
    passes vendor analytics through untouched (correctly — the vendor's are
    the ones to prefer once OPRA is bought), so comparing its output to the
    fixture would be comparing Alpaca's numbers to themselves and would pass
    with the maths deleted. The derivation is invoked directly instead.

    Measured agreement across the 26 contracts in the fixture: max absolute
    delta error 0.0067, max relative gamma error 2.0%, max relative IV error
    2.2%, and **mean relative IV error 0.06%** — that last one is the
    important number, because it says the residual is scatter rather than
    bias. The worst case is the deep-in-the-money 250 put, where the
    indicative quote is widest.

    "Now" comes from the fixture's own quote timestamps rather than a
    constant. Time to expiry is the input the greeks are most sensitive to,
    and an earlier version of this test used a hand-written instant eight
    minutes off, which alone produced an apparent 1% systematic IV bias. The
    bias was in the test.
    """
    from corollary.instruments import parse_occ_symbol
    from corollary.pricing.blackscholes import (
        AnalyticsUnavailable,
        derive_analytics,
        years_to_expiry,
    )

    provider, _ = make_provider(single("stock_snapshot_nvda"))
    spot = (await provider.stock_snapshots(["NVDA"]))["NVDA"].price
    assert spot is not None

    theirs = load_fixture("option_chain_nvda_dated")["body"]["snapshots"]
    captured_at = recorded_chain_instant("option_chain_nvda_dated")
    compared = 0
    iv_errors: list[float] = []
    for symbol, vendor in theirs.items():
        quote = vendor.get("latestQuote")
        if not quote or not vendor.get("greeks"):
            continue
        occ = parse_occ_symbol(symbol)
        mid = (Decimal(str(quote["bp"])) + Decimal(str(quote["ap"]))) / 2
        mine = derive_analytics(
            mid=mid,
            spot=spot,
            strike=occ.strike,
            years=years_to_expiry(occ.expiration, captured_at),
            is_call=occ.option_type is OptionType.CALL,
        )
        assert not isinstance(mine, AnalyticsUnavailable), symbol
        compared += 1
        # `load_fixture` hands back exact Decimals; the comparison itself is
        # approximate by nature, so both sides land in float for it.
        vendor_iv = float(vendor["impliedVolatility"])
        assert float(mine.greeks.delta) == pytest.approx(
            float(vendor["greeks"]["delta"]), abs=0.01
        ), symbol
        assert float(mine.greeks.gamma) == pytest.approx(
            float(vendor["greeks"]["gamma"]), rel=0.05
        ), symbol
        assert float(mine.implied_volatility) == pytest.approx(
            vendor_iv, rel=0.05
        ), symbol
        iv_errors.append((float(mine.implied_volatility) - vendor_iv) / vendor_iv)
    assert compared >= 20, "too few contracts carried vendor greeks to compare"
    # Scatter, not bias. A wrong rate, dividend or day-count convention would
    # show up here as a consistent offset even while every per-contract bound
    # above still passed.
    assert abs(sum(iv_errors) / len(iv_errors)) < 0.005


async def test_derivation_fills_the_gap_where_the_vendor_leaves_one(
    make_provider,
) -> None:
    """The near-dated chain: 84 of 100 strikes carry no vendor greeks.

    Those are the ones decision 10 exists for, and they must come back
    ``DERIVED`` with a real number or ``UNAVAILABLE`` with a real reason --
    never a plausible invented figure.
    """
    provider, _ = make_provider(
        by_path(
            {
                "/options/snapshots": sequence(
                    "option_chain_nvda_page1", "option_chain_nvda_dated"
                ),
                "/v2/stocks/snapshots": "stock_snapshot_nvda",
            }
        ),
    )
    chain = await provider.option_chain("NVDA")
    derived = [s for s in chain.values() if s.analytics_source is AnalyticsSource.DERIVED]
    vendor = [s for s in chain.values() if s.analytics_source is AnalyticsSource.VENDOR]
    unavailable = [
        s for s in chain.values() if s.analytics_source is AnalyticsSource.UNAVAILABLE
    ]
    assert derived, "nothing was derived; decision 10 is not wired up"
    assert vendor, "the vendor branch should still carry the near-the-money strikes"
    for snapshot in derived:
        assert snapshot.implied_volatility is not None
        assert snapshot.greeks is not None
        assert snapshot.analytics_note == ""
    for snapshot in unavailable:
        assert snapshot.implied_volatility is None
        assert snapshot.greeks is None
        assert snapshot.analytics_note, "an absence must carry its reason"


async def test_the_chain_drops_an_adjusted_symbol_the_server_filter_missed(
    make_provider, caplog
) -> None:
    """Defence in depth, and it has to be authored to be testable.

    ``option_snapshots`` sends ``root_symbol={underlying}`` so Alpaca will not
    return an ``NVDA1`` contract in the first place. That leaves the local
    check unexercised by any real recording -- so this fixture injects one.
    The local check is worth keeping because it does not depend on the vendor
    honouring a query parameter, and an adjusted contract silently priced as a
    standard one is a rule 4 failure.

    An unparseable symbol is dropped by the same path: a contract this app
    cannot read is one it cannot size.
    """
    import logging

    provider, _ = make_provider(
        by_path(
            {
                "/options/snapshots": "option_chain_with_adjusted",
                "/v2/stocks/snapshots": "stock_snapshot_nvda",
            }
        )
    )
    with caplog.at_level(logging.WARNING):
        chain = await provider.option_chain("NVDA")

    assert not any(symbol.startswith("NVDA1") for symbol in chain)
    assert "NOT-AN-OCC-SYMBOL" not in chain
    assert chain, "the standard contracts should survive"
    # Never silently.
    assert any("non-standard" in record.message for record in caplog.records)


async def test_analytics_are_unavailable_without_an_underlying_price(
    make_provider,
) -> None:
    """No spot, no Black-Scholes -- and no invented number either."""
    provider, _ = make_provider(
        by_path(
            {
                "/options/snapshots": sequence(
                    "option_chain_nvda_page1", "option_chain_nvda_dated"
                ),
                "/v2/stocks/snapshots": "stock_snapshot_empty",
            }
        )
    )
    chain = await provider.option_chain("NVDA")
    non_vendor = [
        s for s in chain.values() if s.analytics_source is not AnalyticsSource.VENDOR
    ]
    assert non_vendor
    assert all(s.analytics_source is AnalyticsSource.UNAVAILABLE for s in non_vendor)
    assert all("underlying" in s.analytics_note for s in non_vendor)


# --------------------------------------------------------------------------
# Contracts, adjusted contracts, and open interest
# --------------------------------------------------------------------------


async def test_contracts_come_from_the_trading_host_not_the_data_host(
    make_provider,
) -> None:
    """The concrete reason the rate limiter keys on host.

    One logical operation -- "show me NVDA's chain with its multipliers" --
    crosses two servers with two independent 200/min ceilings.
    """
    provider, transport = make_provider(contracts_page("option_contracts_nvda"))
    await provider.option_contracts("NVDA")
    assert transport.hosts
    assert set(transport.hosts) == {ALPACA_PAPER_TRADING_HOST}
    assert ALPACA_DATA_HOST not in transport.hosts


async def test_the_chain_and_the_contracts_hit_different_buckets(
    make_provider, limiter
) -> None:
    provider, transport = make_provider(
        by_path(
            {
                "/options/snapshots": "option_chain_nvda_dated",
                "/v2/stocks/snapshots": "stock_snapshot_nvda",
                "/v2/options/contracts": contracts_page("option_contracts_nvda"),
            }
        )
    )
    await provider.option_chain("NVDA")
    await provider.option_contracts("NVDA")

    assert ALPACA_DATA_HOST in transport.hosts
    assert ALPACA_PAPER_TRADING_HOST in transport.hosts
    data_bucket = limiter.bucket_for(ALPACA_DATA_HOST)
    trading_bucket = limiter.bucket_for(ALPACA_PAPER_TRADING_HOST)
    assert data_bucket is not trading_bucket
    # Each host was charged only for its own requests.
    data_calls = transport.hosts.count(ALPACA_DATA_HOST)
    trading_calls = transport.hosts.count(ALPACA_PAPER_TRADING_HOST)
    assert data_bucket.available == pytest.approx(10_000 - data_calls)
    assert trading_bucket.available == pytest.approx(10_000 - trading_calls)


async def test_contracts_parse_every_structural_field(make_provider) -> None:
    """Each field checked against the raw payload, not against a literal.

    The recorded contracts are near-dated and are replaced every time the
    recorder runs, so hardcoding a symbol here would rot within the week.
    """
    provider, _ = make_provider(contracts_page("option_contracts_nvda"))
    contracts = await provider.option_contracts("NVDA")
    raw = {
        c["symbol"]: c
        for c in load_fixture("option_contracts_nvda")["body"]["option_contracts"]
    }
    assert contracts

    for contract in contracts:
        source = raw[contract.symbol]
        assert contract.underlying_symbol == source["underlying_symbol"]
        assert contract.root_symbol == source["root_symbol"]
        assert contract.expiration == date.fromisoformat(source["expiration_date"])
        assert contract.option_type.value == source["type"]
        assert contract.strike == Decimal(source["strike_price"])
        assert contract.multiplier == Decimal(source["multiplier"])
        assert contract.size == Decimal(source["size"])
        assert contract.style == source["style"]
        assert contract.tradable is source["tradable"]
        assert contract.status == source["status"]
        assert contract.name == source["name"]


async def test_the_multiplier_is_read_and_never_hardcoded(make_provider) -> None:
    provider, _ = make_provider(contracts_page("option_contracts_nvda"))
    contracts = await provider.option_contracts("NVDA")
    assert all(isinstance(c.multiplier, Decimal) for c in contracts)
    # size is carried separately and must never be used as the multiplier.
    assert all(c.size is not None for c in contracts)


async def test_adjusted_contracts_are_filtered_on_root_symbol(
    make_provider,
) -> None:
    """GME1 and XRX1 are real, active, adjusted contracts."""
    provider, _ = make_provider(contracts_page("option_contracts_adjusted"))
    kept = await provider.option_contracts("GME")
    assert kept, "the standard controls should survive"
    assert all(not c.is_adjusted for c in kept)
    assert not any(c.root_symbol in {"GME1", "XRX1"} for c in kept)


async def test_adjusted_contracts_come_back_when_asked_for_explicitly(
    make_provider,
) -> None:
    provider, _ = make_provider(contracts_page("option_contracts_adjusted"))
    every = await provider.option_contracts("GME", include_adjusted=True)
    adjusted = [c for c in every if c.is_adjusted]
    assert adjusted
    assert {c.root_symbol for c in adjusted} >= {"GME1"}


async def test_an_adjusted_contract_reports_multiplier_100_like_any_other(
    make_provider,
) -> None:
    """The finding that makes ``root_symbol`` the *only* usable detector.

    All 270 active adjusted contracts reachable from this account report
    ``multiplier: "100"`` and ``size: "100"``. Reading the multiplier per
    contract -- which CLAUDE.md asks for and which this provider does -- is
    therefore **necessary but not sufficient**: it is identical on an adjusted
    contract, so it cannot be the thing that flags one.
    """
    provider, _ = make_provider(contracts_page("option_contracts_adjusted"))
    every = await provider.option_contracts("GME", include_adjusted=True)
    adjusted = [c for c in every if c.is_adjusted]
    assert adjusted
    assert all(c.multiplier == Decimal("100") for c in adjusted)
    assert all(c.size == Decimal("100") for c in adjusted)


async def test_the_deliverables_reveal_what_the_multiplier_hides(
    make_provider,
) -> None:
    """A GME1 contract delivers 100 GME **plus 10 GME.WS warrants**.

    Nothing in ``multiplier`` or ``size`` says so. This is the field that
    does, and it is why ``has_nonstandard_deliverable`` exists.
    """
    provider, transport = make_provider(contracts_page("option_contracts_deliverables"))
    contracts = await provider.option_contracts(
        "GME", include_adjusted=True, show_deliverables=True
    )
    assert transport.params_for("/v2/options/contracts")["show_deliverables"] == "true"

    sample = contracts[0]
    assert sample.is_adjusted
    assert len(sample.deliverables) == 2
    assert sample.has_nonstandard_deliverable is True
    equity, warrant = sample.deliverables
    assert equity.symbol == "GME"
    assert equity.amount == Decimal("100")
    assert warrant.symbol == "GME.WS"
    assert warrant.amount == Decimal("10")


async def test_deliverables_not_requested_answers_unknown_not_false(
    make_provider,
) -> None:
    """Three-valued on purpose: asking a question the data cannot answer and
    getting "no" is how an adjusted contract gets sized as a standard one."""
    provider, _ = make_provider(contracts_page("option_contracts_nvda"))
    contracts = await provider.option_contracts("NVDA")
    assert all(c.deliverables == () for c in contracts)
    assert all(c.has_nonstandard_deliverable is None for c in contracts)


async def test_a_null_open_interest_survives_as_null(make_provider) -> None:
    """Absent is never 0. A zero claims nobody holds the contract."""
    provider, _ = make_provider(contracts_page("option_contracts_nvda"))
    contracts = await provider.option_contracts("NVDA")
    raw = load_fixture("option_contracts_nvda")["body"]["option_contracts"]
    by_symbol = {c["symbol"]: c for c in raw}

    nulls = 0
    for contract in contracts:
        expected = by_symbol[contract.symbol].get("open_interest")
        if expected is None:
            nulls += 1
            assert contract.open_interest is None
        else:
            assert contract.open_interest == int(expected)
    assert nulls >= 1, (
        "the recording no longer contains a null open_interest, so this test "
        "proves nothing. Re-record against contracts that have one, or move "
        "the assertion to a fixture that does -- do not delete it."
    )


async def test_open_interest_is_populated_on_this_plan_after_all(
    make_provider,
) -> None:
    """A second spec claim that fell on 2026-09-10.

    The Phase 2 design states ``open_interest`` is null on *every* contract
    sampled, and decision 10 reports it as unavailable on that basis. It is
    populated on the great majority of NVDA contracts, with
    ``open_interest_date`` and ``close_price`` alongside it. The spec's own
    "Not verified" list asked whether the nulls were the plan or a settlement
    cycle this account had never had; the answer is the latter.
    """
    provider, _ = make_provider(contracts_page("option_contracts_nvda"))
    contracts = await provider.option_contracts("NVDA")
    populated = [c for c in contracts if c.open_interest is not None]
    assert len(populated) > len(contracts) // 2
    sample = populated[0]
    assert sample.open_interest_date is not None
    assert isinstance(sample.close_price, Decimal)


# --------------------------------------------------------------------------
# The replay itself
# --------------------------------------------------------------------------


async def test_the_replay_serves_the_bytes_on_disk(make_provider) -> None:
    """The fixture file is the source of truth, character for character.

    ``httpx.Response(json=...)`` re-serialised the parsed body, so what the
    provider parsed was Python's ``repr`` of a double rather than the text in
    the file. The loud half of the guarantee survived that -- a ``.json()``
    regression puts a float into ``_as_decimal``, which raises -- but a
    *quiet* precision regression would not have, because the expected side of
    every assertion was reading the same double back out.
    """
    from .conftest import fixture_body_bytes, fixture_text

    for name in ("option_bars_daily", "stock_snapshots", "option_contracts_nvda"):
        body = fixture_body_bytes(name).decode("utf-8")
        assert body in fixture_text(name), name
        # Not a re-serialisation: `json.dumps` reformats separators and would
        # not be a substring of the file.
        assert json.loads(body, parse_float=Decimal) == load_fixture(name)["body"]


async def test_a_fixture_digit_reaches_the_provider_unrounded(
    make_provider,
) -> None:
    """A value no double can hold survives the whole replay path.

    Written as a hand-made response rather than a recording, because the
    recorded fixtures pre-date ``record_alpaca.dumps_exact`` and were captured
    through a ``json.loads``/``json.dumps`` round trip of their own -- so none
    of them still carries a digit that proves this.
    """
    exact = "205.67753612345678901234"
    body = (
        '{"bars": {"NVDA": [{"c": 1, "h": 1, "l": 1, "o": 1, '
        '"t": "2026-08-03T04:00:00Z", "v": 1, "n": 1, "vw": ' + exact + "}]}, "
        '"next_page_token": null}'
    ).encode("utf-8")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            )
        )
    )
    provider = AlpacaProvider(
        credentials=TEST_CREDENTIALS,
        feeds=BASIC_FEEDS,
        client=client,
        now=lambda: RECORDED_AT,
    )
    bars = await provider.stock_bars(["NVDA"], limit=1)
    await client.aclose()
    assert str(bars["NVDA"][0].vwap) == exact
    assert Decimal(exact) != Decimal(float(exact)), (
        "pick a value a double cannot hold, or this test proves nothing"
    )


# --------------------------------------------------------------------------
# Claims about the data -- the ones that turned out to be wrong
# --------------------------------------------------------------------------


def _fixture_evidence() -> dict[str, tuple[int, int]]:
    """What the recordings actually contain, counted rather than remembered.

    Every "the Basic plan never serves X" claim in this codebase came from
    sampling a handful of contracts off endpoints that return them ordered by
    strike -- so the sample was the deep-ITM tail and the generalisation was
    wrong. These counts are the correction, and they live in a test so the
    claim cannot quietly come back.
    """
    counts: dict[str, tuple[int, int]] = {}
    for name in (
        "option_chain_nvda_page1",
        "option_chain_nvda_page2",
        "option_chain_nvda_dated",
    ):
        snapshots = load_fixture(name)["body"]["snapshots"]
        populated = sum(
            1
            for entry in snapshots.values()
            if entry.get("impliedVolatility") is not None
            and entry.get("greeks") is not None
        )
        counts[name] = (populated, len(snapshots))
    contracts = load_fixture("option_contracts_nvda")["body"]["option_contracts"]
    counts["option_contracts_nvda"] = (
        sum(1 for entry in contracts if entry.get("open_interest") is not None),
        len(contracts),
    )
    return counts


async def test_the_fixtures_disprove_the_never_claims() -> None:
    """The evidence, stated once, that the next three tests rest on."""
    evidence = _fixture_evidence()
    for name in (
        "option_chain_nvda_page1",
        "option_chain_nvda_page2",
        "option_chain_nvda_dated",
    ):
        populated, total = evidence[name]
        assert populated > 0, (
            f"{name} no longer carries vendor analytics, so the correction "
            "these tests encode has lost its evidence. Re-record or re-word -- "
            "do not delete."
        )
        assert total > 0
    populated, total = evidence["option_contracts_nvda"]
    assert populated > total // 2


async def test_an_absent_analytics_note_is_about_the_contract_not_the_feed(
    make_provider,
) -> None:
    """The note ships to the screen, so a false one is a false statement to the user.

    §8.5 renders ``analytics_note`` as the reason an absence is an absence.
    The default used to read "the indicative feed serves no implied volatility
    or greeks" -- and ``option_chain_nvda_page1`` carries them on 16 of its 100
    contracts, so the sentence was untrue of the very response that produced
    it. The honest claim is about the contract in hand.
    """
    provider, _ = make_provider(chain_page("option_chain_nvda_page1"))
    snapshots = await provider.option_snapshots("NVDA")

    vendor = [
        s for s in snapshots.values() if s.analytics_source is AnalyticsSource.VENDOR
    ]
    absent = [
        s
        for s in snapshots.values()
        if s.analytics_source is AnalyticsSource.UNAVAILABLE
    ]
    assert vendor and absent, (
        "this fixture must contain both kinds for the test to make its point"
    )
    for snapshot in absent:
        note = snapshot.analytics_note.lower()
        assert "this contract" in note, note
        assert "the indicative feed serves no" not in note, note


async def test_no_module_still_claims_the_plan_never_serves_analytics() -> None:
    """The stale prose, named literally so it cannot be re-typed by accident."""
    import corollary.data.providers.alpaca as alpaca_module
    import corollary.data.providers.interface as interface_module
    import corollary.pricing.blackscholes as blackscholes_module

    stale = {
        alpaca_module: (
            "the indicative feed serves no implied volatility or greeks",
            "On the Basic plan every contract returns ``open_interest: null``",
        ),
        interface_module: (
            "``None`` on the Basic plan for every contract sampled",
        ),
        blackscholes_module: (
            "serves ``impliedVolatility: None`` and ``greeks: None``",
        ),
    }
    for module, claims in stale.items():
        source = module.__file__
        assert source is not None
        with open(source, encoding="utf-8") as handle:
            flat = " ".join(handle.read().split())
        for claim in claims:
            assert " ".join(claim.split()) not in flat, (
                f"{claim!r} still appears in {module.__name__}, and the "
                "fixtures in this repository contradict it"
            )


async def test_the_derived_caveat_names_iex_rather_than_saying_delayed() -> None:
    """A derived IV's spot is an IEX mid, not merely a delayed one.

    IEX is ~2.5% of US volume. On a thin underlying that is a materially
    different caveat from "15 minutes stale", and the UI quotes this docstring
    to say what a derived number is worth.
    """
    from corollary.data.providers.interface import AnalyticsSource

    assert AnalyticsSource.__doc__ is not None
    assert "IEX" in AnalyticsSource.__doc__


# --------------------------------------------------------------------------
# One process, one budget per host
# --------------------------------------------------------------------------


async def test_two_providers_share_one_default_request_budget() -> None:
    """Two limiters against one server-side ceiling is over-spending by two.

    ``ratelimit``'s own docstring is written against the mirror image of this
    bug -- two buckets for one host, because of a case difference. A provider
    that mints a private limiter has the same defect one level up: nothing
    errors, the process simply believes it holds 400/min where Alpaca grants
    200 and the 429s arrive as a surprise.
    """
    clients = [
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        for _ in range(2)
    ]
    providers = [
        AlpacaProvider(credentials=TEST_CREDENTIALS, feeds=BASIC_FEEDS, client=client)
        for client in clients
    ]
    assert providers[0].limiter is providers[1].limiter
    assert providers[0].limiter.bucket_for(ALPACA_DATA_HOST) is (
        providers[1].limiter.bucket_for(ALPACA_DATA_HOST)
    )
    for client in clients:
        await client.aclose()


async def test_an_injected_limiter_still_wins(limiter) -> None:
    """Sharing a default must not take the seam the tests depend on."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    provider = AlpacaProvider(
        credentials=TEST_CREDENTIALS,
        feeds=BASIC_FEEDS,
        client=client,
        limiter=limiter,
    )
    assert provider.limiter is limiter
    await client.aclose()


async def test_a_contract_without_root_symbol_falls_back_to_the_occ_root(
    make_provider,
) -> None:
    """``root_symbol`` is not in the endpoint's ``required`` list.

    Every contract sampled carried it, but the schema permits its absence, and
    defaulting a missing root to the underlying would call every adjusted
    contract standard -- the failure direction that costs money.
    """
    from corollary.data.providers.alpaca import _option_contract

    payload = dict(
        load_fixture("option_contracts_adjusted")["body"]["option_contracts"][0]
    )
    symbol = payload["symbol"]
    payload.pop("root_symbol")
    contract = _option_contract(payload)
    assert contract.root_symbol == symbol[: len(symbol) - 15]
    assert contract.is_adjusted is True


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


async def test_opra_returns_a_feed_access_error_not_a_generic_failure(
    make_provider,
) -> None:
    """The recorded 403 is *"OPRA agreement is not signed"*, still true today.

    Its own exception type because the remedy is a subscription rather than a
    retry -- CLAUDE.md: "check the plan before debugging the code."
    """
    assert load_fixture("option_chain_opra_denied")["status_code"] == 403
    provider, _ = make_provider(
        single("option_chain_opra_denied"),
        feeds=FeedConfig(
            options="opra", stock_historical="sip", stock_realtime="iex"
        ),
    )
    with pytest.raises(FeedAccessError, match="OPRA agreement"):
        await provider.option_snapshots("NVDA")


async def test_a_429_is_its_own_error(make_provider) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "too many"}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    from .conftest import BASIC_FEEDS, TEST_CREDENTIALS

    provider = AlpacaProvider(
        credentials=TEST_CREDENTIALS, feeds=BASIC_FEEDS, client=client
    )
    with pytest.raises(RateLimitedError):
        await provider.stock_snapshots(["NVDA"])
    await client.aclose()


async def test_a_transport_failure_becomes_a_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    from .conftest import BASIC_FEEDS, TEST_CREDENTIALS

    provider = AlpacaProvider(
        credentials=TEST_CREDENTIALS, feeds=BASIC_FEEDS, client=client
    )
    with pytest.raises(ProviderError, match="failed"):
        await provider.stock_snapshots(["NVDA"])
    await client.aclose()


async def test_an_empty_symbol_list_costs_no_request(make_provider) -> None:
    provider, transport = make_provider(single("stock_snapshots"))
    assert await provider.stock_snapshots([]) == {}
    assert await provider.latest_stock_quotes([]) == {}
    assert await provider.stock_bars([]) == {}
    assert transport.requests == []


# --------------------------------------------------------------------------
# Credentials -- rule 6
# --------------------------------------------------------------------------


async def test_the_key_never_appears_in_a_url(make_provider) -> None:
    provider, transport = make_provider(single("stock_snapshots"))
    await provider.stock_snapshots(["NVDA"])
    for request in transport.requests:
        assert "PKTEST" not in str(request.url)
        assert request.headers["APCA-API-KEY-ID"] == "PKTESTTESTTESTTEST"


# --------------------------------------------------------------------------
# What a vendor error body may write into a log -- rule 6
# --------------------------------------------------------------------------
#
# `_get` used to interpolate `response.text` verbatim into both of its error
# branches, with no redaction and no bound at all. The broker's equivalent
# already went through `vendor_detail`; this file's did not, and the 403
# branch -- the one an entitlement or auth rejection takes, and so the one
# most likely to meet a middlebox that echoes request headers back -- was the
# worse of the two.


#: Forty characters, the width Alpaca issues, and obviously not one of them.
#: The width is the point: a real secret fits inside :data:`ERROR_BODY_MAX`
#: five times over, so the bound is no protection against one arriving in a
#: body. Registered by value in ``tests/fixtures/test_record_alpaca.py``'s
#: ``PLACEHOLDER_IDENTIFIERS``; ``conftest``'s stand-in secret is seventeen
#: characters and would not prove the claim under test.
FAKE_SECRET_KEY = "PROVIDERnotarealsecretPROVIDERnotareal00"

#: The pair the echo tests authenticate with. The key id is ``conftest``'s,
#: because that half is already the right shape.
ECHOING_CREDENTIALS = AlpacaCredentials(
    key_id="PKTESTTESTTESTTEST",
    secret_key=FAKE_SECRET_KEY,
    trading_base_url="https://paper-api.alpaca.markets",
    is_paper=True,
)

#: A paper account number's shape, invented, and deliberately a different
#: value from the broker tests' -- a test that passed because some other
#: file's constant happened to be redacted would still have to fail here.
FAKE_ACCOUNT_NUMBER = "PA9PROVIDER0"

#: The body under test: a gateway quoting the request's own auth headers back.
ECHO_BODY = (
    '{"message": "blocked at the gateway; request headers were '
    f"APCA-API-KEY-ID: {ECHOING_CREDENTIALS.key_id}, "
    f'APCA-API-SECRET-KEY: {FAKE_SECRET_KEY}"}}'
)


async def test_a_403_body_that_echoes_the_key_pair_is_redacted(
    make_provider,
) -> None:
    """The realistic branch, and the one that had a raw ``response.text`` in it.

    A 403 is an entitlement or auth rejection -- exactly the response a WAF or
    a corporate proxy answers by reflecting what it rejected, which on every
    request this provider makes includes ``APCA-API-SECRET-KEY``. The message
    then reaches the logs, every traceback holding the provider, and rule 9's
    watchdog path.

    Of the two halves the secret is the one that must not survive, so both are
    asserted separately rather than through one combined check.
    """
    assert len(ECHO_BODY) <= ERROR_BODY_MAX, (
        "the whole body sits inside the bound, so truncation cannot be what "
        "removes anything below"
    )

    provider, _ = make_provider(
        lambda _request: (403, ECHO_BODY), credentials=ECHOING_CREDENTIALS
    )
    with pytest.raises(FeedAccessError) as raised:
        await provider.stock_snapshots(["NVDA"])

    message = str(raised.value)
    assert FAKE_SECRET_KEY not in message
    assert ECHOING_CREDENTIALS.key_id not in message
    # Two substitutions, not a dropped body: an error nobody can read is its
    # own failure, and a `_get` that quoted nothing would pass a bare
    # "secret not in message" check while telling whoever is on call nothing.
    assert message.count(REDACTED) == 2
    assert "blocked at the gateway" in message
    # The branch still says what it is for.
    assert "entitlement" in message


async def test_a_generic_4xx_body_that_echoes_the_key_pair_is_redacted(
    make_provider,
) -> None:
    """The other interpolating branch. Both call sites, not just the first read."""
    assert len(ECHO_BODY) <= ERROR_BODY_MAX

    provider, _ = make_provider(
        lambda _request: (500, ECHO_BODY), credentials=ECHOING_CREDENTIALS
    )
    with pytest.raises(ProviderError) as raised:
        await provider.stock_snapshots(["NVDA"])

    message = str(raised.value)
    assert FAKE_SECRET_KEY not in message
    assert ECHOING_CREDENTIALS.key_id not in message
    assert message.count(REDACTED) == 2
    assert "blocked at the gateway" in message
    assert "500" in message


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_an_account_number_in_an_error_body_is_redacted(
    make_provider, status
) -> None:
    """Matched by shape, because it is not a credential the caller holds.

    Real: a ``FEE`` activity's ``description`` on this host reads *"CAT fee
    for proceed of N trades on <date> by PA..."*, so this vendor does put an
    account number in free text where a rule about field names cannot see it.
    An error ``message`` is free text from the same vendor.
    """
    body = f'{{"message": "rejected for account {FAKE_ACCOUNT_NUMBER}"}}'
    provider, _ = make_provider(lambda _request: (status, body))
    with pytest.raises(ProviderError) as raised:
        await provider.stock_snapshots(["NVDA"])

    message = str(raised.value)
    assert FAKE_ACCOUNT_NUMBER not in message
    # Redacted, not deleted: the sentence still says what happened.
    assert "rejected for account" in message


async def test_an_occ_symbol_survives_the_redaction(make_provider) -> None:
    """The bound and the redaction are not allowed to eat the error.

    ``PANW251219C00150000`` opens with the same two letters as a paper account
    number and is the single most useful token in a 404 about a contract --
    which, on a market-data provider, is the 404 that actually happens.
    """
    contract = "PANW251219C00150000"
    body = f'{{"code": 40410000, "message": "contract {contract} not found"}}'
    provider, _ = make_provider(lambda _request: (404, body))
    with pytest.raises(ProviderError) as raised:
        await provider.stock_snapshots(["NVDA"])
    assert contract in str(raised.value)


@pytest.mark.parametrize("status", [403, 500])
async def test_a_huge_error_body_is_bounded(make_provider, status) -> None:
    """An HTML error page from a proxy is not a reason to write 40kB to a log.

    ``_get`` had no cap on either branch, so a middlebox answering with a
    stack trace copied the whole thing into an exception message.
    """
    body = '{"message": "' + "x" * 40_000 + '"}'
    provider, _ = make_provider(lambda _request: (status, body))
    with pytest.raises(ProviderError) as raised:
        await provider.stock_snapshots(["NVDA"])

    message = str(raised.value)
    assert len(message) < 1_000
    # Truncation that does not announce itself is indistinguishable from a
    # vendor that sent exactly that much.
    assert "truncated" in message


async def test_a_multiline_error_body_becomes_one_line(make_provider) -> None:
    """One record per line, so a stray HTML page cannot fake log records."""
    body = '{\n  "code": 40110000,\n  "message": "nope"\n}'
    provider, _ = make_provider(lambda _request: (500, body))
    with pytest.raises(ProviderError) as raised:
        await provider.stock_snapshots(["NVDA"])
    assert "\n" not in str(raised.value)
    assert "nope" in str(raised.value)


async def test_a_429_quotes_the_reset_header_and_not_the_body(
    make_provider,
) -> None:
    """The 429 branch interpolates one header value, and that stays true.

    ``X-RateLimit-Reset`` is an epoch second -- not credential-shaped, and the
    single most useful thing in a 429. The body is deliberately *not* quoted
    there, and this pins that: adding it later means adding the redaction with
    it, and a test that failed is a cheaper reminder than a leaked key.
    """
    provider, _ = make_provider(
        lambda _request: (429, ECHO_BODY), credentials=ECHOING_CREDENTIALS
    )
    with pytest.raises(RateLimitedError) as raised:
        await provider.stock_snapshots(["NVDA"])

    message = str(raised.value)
    assert FAKE_SECRET_KEY not in message
    assert "blocked at the gateway" not in message
    assert "Reset header" in message
