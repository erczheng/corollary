"""The Markets page's three routes, against recorded Alpaca responses.

Nothing here makes a live call. Every figure asserted on is read out of
``tests/fixtures/alpaca/`` rather than typed in, because these are real prices
captured on 2026-09-10 and the property under test is usually *exactness*
rather than the number itself.

Decision 10, as amended, is what most of the chain tests are about. The first
probe sampled two contracts off an endpoint that returns them ordered by
strike, so it sampled the deep-ITM tail and generalised two conclusions that
were both wrong. The recorded page proves the amended reading:

* ``impliedVolatility`` and ``greeks`` arrive on **16 of the 100** contracts in
  ``option_chain_nvda_page1``. Partial, not absent -- so vendor analytics pass
  through where they exist, are derived where they do not, and every row says
  which of the two it is.
* ``open_interest`` is populated on **98 of 100** and null on 2. The null is
  the exception rather than the column, and it has to survive as null: a
  zero there is a claim about the market where a null is a claim about the
  data.

And one the probe did not have to look for, which falls out of the same file:
**49 of those 100 contracts have no bid at all** (``bp: 0``, which Alpaca
documents as *"the security has no active bid"*). A chain that renders those
as ``$0.00`` is inventing a price on half its rows.
"""

import json
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from corollary.api.routes import markets as markets_routes

from .conftest import (
    FIXTURE_DIR,
    MARKET_DATA_RECORDED_AT,
    RecordingTransport,
    Route,
    chain_page,
    contracts_page,
    market_data_routes,
    single,
)

MarketClient = Callable[..., tuple[TestClient, RecordingTransport]]

MODULE = Path(markets_routes.__file__)

#: The recorded instant in New York. 2026-09-10 19:10 UTC is 15:10 ET, so the
#: trading date is the 10th and every bar in ``stock_bars_daily`` (3--14
#: August) is a completed session.
RECORDED_TRADING_DATE = date(2026, 9, 10)

#: 15:10 in New York on the last session the daily-bar recording covers. The
#: instant that makes today's *partial* bar reachable: the fixture's final bar
#: is 14 August, so a client asked on the 14th sees it as the session in
#: progress and the nine before it as the average.
AUGUST_14 = datetime(2026, 8, 14, 19, 10, tzinfo=timezone.utc)


def fixture(name: str) -> Any:
    """A recorded body, parsed the way the provider parses it."""
    raw = json.loads(
        (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"), parse_float=Decimal
    )
    return raw["body"]


def nvda_chain() -> Route:
    """The NVDA chain and its contracts, each followed by a terminal page.

    Built per call rather than held as a constant: ``sequence`` is stateful,
    and a shared router would serve page two of the chain to whichever test
    ran second.
    """
    return market_data_routes(
        chain=chain_page("option_chain_nvda_page1"),
        contracts=contracts_page("option_contracts_nvda"),
    )


def rows(client: TestClient, path: str, **params: Any) -> list[dict[str, Any]]:
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    return body


def by_symbol(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["symbol"]: item for item in items}


def feeds_for(transport: RecordingTransport, path_fragment: str) -> list[str]:
    """The ``feed`` of **every** request to a path, in order.

    ``params_for`` answers for the first request only, and the property this
    module has to pin is about more than one: relative volume is a ratio, and
    a ratio whose halves were measured on different feeds is not a ratio.
    """
    return [
        request.url.params["feed"]
        for request in transport.requests
        if path_fragment in request.url.path
    ]


def starts_for(transport: RecordingTransport, path_fragment: str) -> list[str]:
    """The ``start`` of every request to a path -- which window it asked for."""
    return [
        request.url.params["start"]
        for request in transport.requests
        if path_fragment in request.url.path
    ]


# --------------------------------------------------------------------------
# The stock table
# --------------------------------------------------------------------------


def test_the_stock_table_serves_one_row_per_requested_symbol(
    make_market_client: MarketClient,
) -> None:
    client, _ = make_market_client(market_data_routes())

    table = rows(client, "/api/markets/stocks", symbols="NVDA,SPY,AAPL")

    assert [row["symbol"] for row in table] == ["NVDA", "SPY", "AAPL"]


def test_the_price_is_the_quote_mid_and_it_is_exact(
    make_market_client: MarketClient,
) -> None:
    """The assertion a float fails.

    Alpaca sends prices as JSON numbers; ``parse_float=Decimal`` in the
    provider is what keeps 217.81/217.83 from becoming two doubles before any
    of our arithmetic runs.
    """
    client, _ = make_market_client(market_data_routes())
    quote = fixture("stock_snapshots")["NVDA"]["latestQuote"]
    expected = (Decimal(str(quote["bp"])) + Decimal(str(quote["ap"]))) / 2

    table = by_symbol(rows(client, "/api/markets/stocks", symbols="NVDA"))

    assert Decimal(str(table["NVDA"]["price"])) == expected


def test_the_change_is_measured_from_the_previous_close(
    make_market_client: MarketClient,
) -> None:
    """Yesterday's settle, never the first point of a series.

    NVDA is *down* 5.95 on this recording. A change measured from anything
    else would still look like a plausible number.
    """
    client, _ = make_market_client(market_data_routes())
    snapshot = fixture("stock_snapshots")["NVDA"]
    quote = snapshot["latestQuote"]
    price = (Decimal(str(quote["bp"])) + Decimal(str(quote["ap"]))) / 2
    previous = Decimal(str(snapshot["prevDailyBar"]["c"]))

    row = by_symbol(rows(client, "/api/markets/stocks", symbols="NVDA"))["NVDA"]

    assert Decimal(str(row["change"])) == price - previous
    assert Decimal(str(row["changePct"])) == Decimal("-2.66")


def test_average_volume_counts_completed_sessions_only(
    make_market_client: MarketClient,
) -> None:
    """Today's bar is partial until the close, and it drags the average down.

    The fixture's last bar is 14 August. Asked on the 14th, the average must
    be over the nine sessions before it -- including the tenth is a number
    that changes every time the poll runs, which is the opposite of what an
    *average daily* volume is for.
    """
    volumes = [int(bar["v"]) for bar in fixture("stock_bars_daily")["bars"]["NVDA"]]
    completed = volumes[:-1]
    client, _ = make_market_client(
        market_data_routes(), now=datetime(2026, 8, 14, 19, 10, tzinfo=timezone.utc)
    )

    row = by_symbol(rows(client, "/api/markets/stocks", symbols="NVDA"))["NVDA"]

    assert row["avgVolume"] == sum(completed) // len(completed)
    assert row["avgVolume"] != sum(volumes) // len(volumes)


def test_a_symbol_with_no_bars_has_no_average_volume_rather_than_zero(
    make_market_client: MarketClient,
) -> None:
    """Absent is not zero, and here it is load-bearing twice over.

    ``stock_bars_daily`` carries NVDA and SPY and not AAPL. Zero would make
    relative volume -- today's volume over this figure -- a division by zero,
    and *"trending"* is exactly relative volume, so a data gap would rank
    first on the screen that exists to find unusual activity.
    """
    client, _ = make_market_client(market_data_routes())

    table = by_symbol(rows(client, "/api/markets/stocks", symbols="NVDA,AAPL"))

    assert table["NVDA"]["avgVolume"] is not None
    assert table["AAPL"]["avgVolume"] is None


def test_session_volume_and_its_average_are_measured_on_one_feed(
    make_market_client: MarketClient,
) -> None:
    """Relative volume is a ratio, so both halves have to be the same quantity.

    The numerator used to be the snapshot's ``dailyBar`` and the denominator
    daily bars: the snapshot endpoint is *latest*, which is IEX-only on Basic,
    and bars are historical, which is SIP. IEX is ~2.5% of US equity volume,
    so NVDA printed 2,162,109 over an average of 119,238,467 -- a relative
    volume of **0.018**, and every name on the screen read as near-dead.

    Both numbers now come out of ``/v2/stocks/bars``, which carries one feed.
    The assertions are the two halves of that: the served volume is today's
    *bar*, not the snapshot's, and every bars request that fed the ratio
    carried the same ``feed`` -- one that the snapshot request demonstrably
    does not share.
    """
    client, transport = make_market_client(market_data_routes(), now=AUGUST_14)
    daily = fixture("stock_bars_daily")["bars"]["NVDA"]
    partial_session = int(daily[-1]["v"])
    iex_daily_bar = int(fixture("stock_snapshots")["NVDA"]["dailyBar"]["v"])

    row = by_symbol(rows(client, "/api/markets/stocks", symbols="NVDA"))["NVDA"]

    assert row["volume"] == partial_session
    assert row["volume"] != iex_daily_bar
    bar_feeds = feeds_for(transport, "/v2/stocks/bars")
    assert len(set(bar_feeds)) == 1
    # Not a tautology: this is what makes the first two assertions matter.
    # The snapshot request really is on another feed, so sourcing either half
    # from it is a ratio of two different measurements.
    assert set(bar_feeds).isdisjoint(feeds_for(transport, "/v2/stocks/snapshots"))


def test_relative_volume_is_a_plausible_multiple_rather_than_a_routing_share(
    make_market_client: MarketClient,
) -> None:
    """The symptom, asserted directly: the ratio has to be able to reach 1.

    NVDA on the recording is 76,502,320 against an average of 119,238,467 --
    0.64 of a normal session, at 15:10 on the 14th. Read off the IEX snapshot
    the same row was 0.018, and no symbol could ever trend.
    """
    client, _ = make_market_client(market_data_routes(), now=AUGUST_14)

    table = by_symbol(rows(client, "/api/markets/stocks", symbols="NVDA,SPY"))

    for row in table.values():
        relative = Decimal(row["volume"]) / Decimal(row["avgVolume"])
        assert Decimal("0.25") < relative < Decimal(4), row


def test_a_session_with_no_bar_yet_has_no_volume_rather_than_zero(
    make_market_client: MarketClient,
) -> None:
    """Null survives the feed change, and it now covers one more case.

    The recording's bars stop on 14 August and this client is asked on 10
    September, which stands in for the two states that really produce it:
    before the session's first print, and inside the historical feed's
    15-minute embargo, where the partial daily bar is not servable yet. A
    zero would claim the market opened and nothing traded.
    """
    client, _ = make_market_client(market_data_routes())

    row = by_symbol(rows(client, "/api/markets/stocks", symbols="NVDA"))["NVDA"]

    assert row["volume"] is None
    assert row["avgVolume"] is not None


def test_the_daily_series_is_fetched_once_per_trading_date(
    make_market_client: MarketClient,
) -> None:
    """The rate budget, made structural.

    The Markets page polls this route every 2 seconds -- 30 requests a minute
    against ``data.alpaca.markets``'s 200. Re-downloading 90 days of bars for
    every symbol on every poll would multiply that by the number of pages and
    turn a comfortable budget into a rationed one. An average daily volume
    moves once a session; the cache key is the session.

    **Two bars windows, not one**, since relative volume's numerator moved off
    the snapshot: 90 days for the average and today for the session so far.
    Two polls still cost one request each -- the count is 2 rather than 4 --
    and today's is what the second window's shorter life is for.
    """
    client, transport = make_market_client(market_data_routes())

    rows(client, "/api/markets/stocks", symbols="NVDA,SPY")
    rows(client, "/api/markets/stocks", symbols="NVDA,SPY")

    assert transport.count_for("/v2/stocks/bars") == 2
    assert len(set(starts_for(transport, "/v2/stocks/bars"))) == 2
    assert transport.count_for("/v2/stocks/snapshots") == 2


def test_a_symbol_asked_for_twice_is_not_fetched_twice(
    make_market_client: MarketClient,
) -> None:
    """A cached *absence* is still an answer.

    AAPL has no bars in the recording. If only populated symbols were
    remembered, every poll would re-ask for the ones that have nothing --
    which is the case where the extra request buys the least.

    Two requests, not four: one per bars window -- the 90-day average and
    today's session volume -- and neither repeated on the second poll.
    """
    client, transport = make_market_client(market_data_routes())

    rows(client, "/api/markets/stocks", symbols="AAPL")
    rows(client, "/api/markets/stocks", symbols="AAPL")

    assert transport.count_for("/v2/stocks/bars") == 2


async def _counting_fetch(
    calls: list[tuple[str, ...]],
) -> Callable[[tuple[str, ...]], Any]:
    """A ``FetchMany`` that answers with the ordinal of the call that ran."""

    async def fetch(symbols: tuple[str, ...]) -> dict[str, int | None]:
        calls.append(symbols)
        return {symbol: len(calls) for symbol in symbols}

    return fetch


@pytest.mark.asyncio
async def test_todays_volume_is_re_read_once_it_can_have_moved() -> None:
    """The other half of the cache: it has to expire, or the column freezes.

    An average over completed sessions is keyed on the session because that is
    when it changes. Today's volume changes all session long, so the same key
    would serve the 09:30 reading at 15:55 and relative volume would fall
    through the afternoon as the denominator stood still.
    """
    cache: markets_routes.IntradayCache[int | None] = markets_routes.IntradayCache(
        timedelta(seconds=60)
    )
    calls: list[tuple[str, ...]] = []
    fetch = await _counting_fetch(calls)
    today = date(2026, 8, 14)
    at = datetime(2026, 8, 14, 17, 0, tzinfo=timezone.utc)

    first = await cache.resolve(("NVDA",), today=today, now=at, fetch=fetch)
    held = await cache.resolve(
        ("NVDA",), today=today, now=at + timedelta(seconds=59), fetch=fetch
    )
    expired = await cache.resolve(
        ("NVDA",), today=today, now=at + timedelta(seconds=60), fetch=fetch
    )

    assert (first["NVDA"], held["NVDA"], expired["NVDA"]) == (1, 1, 2)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_todays_volume_does_not_survive_the_session_boundary() -> None:
    """Yesterday's total served as today's volume is the same error, a day late.

    The TTL here is a full day, so the only thing that can drop the value is
    the trading date -- which is the point: both halves of the key are load
    bearing.
    """
    cache: markets_routes.IntradayCache[int | None] = markets_routes.IntradayCache(
        timedelta(days=1)
    )
    calls: list[tuple[str, ...]] = []
    fetch = await _counting_fetch(calls)
    at = datetime(2026, 8, 14, 17, 0, tzinfo=timezone.utc)

    friday = await cache.resolve(
        ("NVDA",), today=date(2026, 8, 14), now=at, fetch=fetch
    )
    monday = await cache.resolve(
        ("NVDA",),
        today=date(2026, 8, 17),
        now=at + timedelta(hours=1),
        fetch=fetch,
    )

    assert (friday["NVDA"], monday["NVDA"]) == (1, 2)


def test_market_cap_is_absent_because_it_is_not_alpacas_to_give(
    make_market_client: MarketClient,
) -> None:
    """Finnhub is step 9. Until then the column is honestly empty.

    A fund's market cap is legitimately null and a company's is merely not
    fetched yet; the wire cannot tell those apart, and inventing a number for
    either is the failure PRD 8.5 names.
    """
    client, _ = make_market_client(market_data_routes())

    table = rows(client, "/api/markets/stocks", symbols="NVDA,SPY")

    assert [row["marketCap"] for row in table] == [None, None]


def test_a_symbol_outside_the_universe_is_refused_and_the_rule_is_logged(
    make_market_client: MarketClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 8: a rejection records the rule, the inputs and the timestamp."""
    client, _ = make_market_client(market_data_routes())

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.markets"):
        response = client.get("/api/markets/stocks", params={"symbols": "NVDA,ZZZZ"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_symbol"
    assert "ZZZZ" in response.json()["error"]["message"]
    record = next(r for r in caplog.records if r.__dict__.get("event") == "symbol_rejected")
    assert record.__dict__["rule"]
    assert record.__dict__["symbols"] == ["ZZZZ"]
    assert record.__dict__["at"]


def test_a_symbol_with_no_price_is_omitted_rather_than_priced_at_zero(
    make_market_client: MarketClient, caplog: pytest.LogCaptureFixture
) -> None:
    """No price is no row, and the omission is recorded rather than silent.

    The alternative -- a row whose price is null -- needs a nullable price on
    a model whose whole purpose is a price. The empty response is a state the
    table already draws; a column of dashes is not.
    """
    client, _ = make_market_client(
        market_data_routes(snapshots="stock_snapshot_empty")
    )

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.markets"):
        table = rows(client, "/api/markets/stocks", symbols="NVDA")

    assert table == []
    record = next(r for r in caplog.records if r.__dict__.get("event") == "symbol_unpriced")
    assert record.__dict__["symbols"] == ["NVDA"]
    assert record.__dict__["at"]


def test_a_snapshot_with_no_quote_no_print_and_no_bar_is_not_a_row(
    make_market_client: MarketClient,
) -> None:
    """The harder half of the same rule: *present* is not *priced*.

    The body is synthesised rather than recorded because the failure it stands
    for -- a symbol the feed answers for and has nothing to say about -- is a
    halt or an outage, and neither was happening when the fixtures were
    captured. Omitting only the symbols the response left out entirely would
    let this one through at whatever a fallback invented.
    """
    client, _ = make_market_client(
        market_data_routes(snapshots=lambda _request: (200, '{"NVDA": {}}'))
    )

    assert rows(client, "/api/markets/stocks", symbols="NVDA") == []


def test_the_name_column_comes_from_the_curated_universe(
    make_market_client: MarketClient,
) -> None:
    """There is no name on any market-data endpoint. This one is reference
    data, written down once, and the symbol set it covers is the universe."""
    client, _ = make_market_client(market_data_routes())

    row = by_symbol(rows(client, "/api/markets/stocks", symbols="NVDA"))["NVDA"]

    assert row["name"] == "NVIDIA Corp."


def test_the_whole_universe_is_served_when_no_symbols_are_named(
    make_market_client: MarketClient,
) -> None:
    """One snapshot request covers every symbol, which is what keeps the 2s
    poll at one request rather than one per name."""
    client, transport = make_market_client(market_data_routes())

    rows(client, "/api/markets/stocks")

    asked = transport.params_for("/v2/stocks/snapshots")["symbols"].split(",")
    assert set(asked) == set(markets_routes.UNIVERSE_SYMBOLS)
    assert transport.count_for("/v2/stocks/snapshots") == 1


# --------------------------------------------------------------------------
# The chain -- decision 10
# --------------------------------------------------------------------------


def test_the_chain_carries_the_contract_terms_from_the_occ_symbol(
    make_market_client: MarketClient,
) -> None:
    """Strike, expiry and right come from the symbol, which is the only place
    the snapshot endpoint carries them -- it returns a bare map keyed by OCC
    symbol with no contract metadata at all."""
    client, _ = make_market_client(nvda_chain())

    chain = by_symbol(rows(client, "/api/markets/chain/NVDA"))
    row = chain["NVDA260911C00050000"]

    assert Decimal(str(row["strike"])) == Decimal(50)
    assert row["expiration"] == "2026-09-11"
    assert row["type"] == "call"


def test_a_vendor_implied_volatility_passes_through_labelled_vendor(
    make_market_client: MarketClient,
) -> None:
    """16 of the 100 rows in this page carry Alpaca's own solve.

    Passing it through rather than recomputing is the point: what the vendor
    served is a measurement of their inputs, and replacing it with ours would
    throw away the better half of the chain to make the column uniform.
    """
    client, _ = make_market_client(nvda_chain())
    served = fixture("option_chain_nvda_page1")["snapshots"]["NVDA260911C00205000"]

    row = by_symbol(rows(client, "/api/markets/chain/NVDA"))["NVDA260911C00205000"]

    assert Decimal(str(row["iv"])) == Decimal(str(served["impliedVolatility"]))
    assert row["ivSource"] == "vendor"


def test_a_derived_implied_volatility_is_labelled_derived(
    make_market_client: MarketClient,
) -> None:
    """The gaps are filled locally, and they say so.

    Same method the vendor documents -- Black-Scholes from the quote mid --
    with thinner inputs: a 15-minute-delayed contract mid against an IEX spot.
    Correct method, stale inputs, and a column that admits it.
    """
    client, _ = make_market_client(nvda_chain())
    vendor_rows = {
        symbol
        for symbol, snap in fixture("option_chain_nvda_page1")["snapshots"].items()
        if "impliedVolatility" in snap
    }

    chain = by_symbol(rows(client, "/api/markets/chain/NVDA"))
    derived = [s for s, row in chain.items() if row["ivSource"] == "derived"]

    assert derived, "nothing was derived; the whole chain came from the vendor"
    assert not (set(derived) & vendor_rows)
    assert all(chain[symbol]["iv"] is not None for symbol in derived)


def test_the_chain_mixes_both_sources_and_never_hides_which(
    make_market_client: MarketClient,
) -> None:
    """A chain silently mixing measured and derived values is worse than
    either alone. So the two invariants are: a source is one of the two known
    words, and it is present exactly where a number is."""
    client, _ = make_market_client(nvda_chain())

    chain = rows(client, "/api/markets/chain/NVDA")

    assert {row["ivSource"] for row in chain} == {"vendor", "derived", None}
    for row in chain:
        assert (row["iv"] is None) == (row["ivSource"] is None), row["symbol"]


def test_open_interest_is_served_from_the_contracts_endpoint(
    make_market_client: MarketClient,
) -> None:
    """PRD 8.4's "highest open interest" screen has a ranking key after all.

    It is on the *trading* host, not the data host, which is why a chain costs
    a request against each of the two 200/min buckets.
    """
    client, _ = make_market_client(nvda_chain())
    served = {
        contract["symbol"]: contract["open_interest"]
        for contract in fixture("option_contracts_nvda")["option_contracts"]
    }

    chain = by_symbol(rows(client, "/api/markets/chain/NVDA"))

    assert chain["NVDA260911C00050000"]["openInterest"] == int(
        served["NVDA260911C00050000"]
    )
    populated = [row for row in chain.values() if row["openInterest"] is not None]
    assert len(populated) == 98


def test_a_null_open_interest_survives_as_null(
    make_market_client: MarketClient,
) -> None:
    """Never coerced, and never ranked on.

    Zero says nobody holds the contract, which is a claim about the market. A
    null says nobody told us, which is a claim about the data. These two
    contracts are newly listed and have no settled interest yet.
    """
    client, _ = make_market_client(nvda_chain())

    chain = by_symbol(rows(client, "/api/markets/chain/NVDA"))

    assert chain["NVDA260911C00055000"]["openInterest"] is None
    assert chain["NVDA260911P00065000"]["openInterest"] is None


def test_a_one_sided_market_serves_a_null_bid_rather_than_zero(
    make_market_client: MarketClient,
) -> None:
    """``bp: 0`` is "no active bid", not "the bid is zero dollars".

    49 of this page's 100 contracts are bid-less. Rendering them at $0.00
    invents a price on half the chain, and a mid taken from an invented bid is
    half the ask -- which would then be the input to a derived IV.
    """
    client, _ = make_market_client(nvda_chain())

    row = by_symbol(rows(client, "/api/markets/chain/NVDA"))["NVDA260911C00245000"]

    assert row["bid"] is None
    assert Decimal(str(row["ask"])) == Decimal("0.03")


def test_an_adjusted_contract_never_reaches_the_chain(
    make_market_client: MarketClient,
) -> None:
    """``root_symbol != underlying_symbol`` -- the only test that works.

    An adjusted contract reports ``multiplier: 100`` like any other, and its
    deliverable is not 100 shares. Sizing it as standard computes max loss
    wrong, which is the failure rule 4 exists to prevent.
    """
    client, _ = make_market_client(
        market_data_routes(
            chain="option_chain_with_adjusted",
            contracts=contracts_page("option_contracts_adjusted"),
        )
    )

    chain = by_symbol(rows(client, "/api/markets/chain/NVDA"))

    assert "NVDA1261016C00190000" not in chain
    assert "NOT-AN-OCC-SYMBOL" not in chain
    assert "NVDA261016C00190000" in chain


def test_the_chain_is_deterministic_and_ordered(
    make_market_client: MarketClient,
) -> None:
    """Identical inputs, identical output -- byte for byte.

    Snapshots arrive in a map, and a map's order is not an ordering. The
    ladder the page draws is expiry, then right, then strike.
    """
    first_client, _ = make_market_client(nvda_chain())
    second_client, _ = make_market_client(nvda_chain())

    first = first_client.get("/api/markets/chain/NVDA")
    second = second_client.get("/api/markets/chain/NVDA")

    assert first.json() == second.json()
    keys = [
        (row["expiration"], row["type"], Decimal(str(row["strike"])))
        for row in first.json()
    ]
    assert keys == sorted(keys)


def test_the_chain_window_is_bounded_before_the_request_leaves(
    make_market_client: MarketClient,
) -> None:
    """Filters, not a ``limit``.

    Decision 10's error had one shape: both endpoints return contracts ordered
    by strike, so a small ``limit`` returns the deep-ITM tail and nothing about
    it generalises. The bound is therefore a window -- 60 DTE and the strikes
    within 15% of spot -- pushed to the server so the pagination loop is short
    rather than truncated.
    """
    client, transport = make_market_client(nvda_chain())

    rows(client, "/api/markets/chain/NVDA")
    params = transport.params_for("/v1beta1/options/snapshots/")

    assert params["expiration_date_gte"] == str(RECORDED_TRADING_DATE)
    assert params["expiration_date_lte"] == str(
        RECORDED_TRADING_DATE + timedelta(days=60)
    )
    # NVDA's mid is 217.82 on this recording; the band widens to whole cents
    # rather than narrowing, so rounding never drops a listed strike.
    assert Decimal(params["strike_price_gte"]) == Decimal("185.14")
    assert Decimal(params["strike_price_lte"]) == Decimal("250.50")
    assert "limit" not in params or int(params["limit"]) >= 100


def test_the_chain_costs_one_bucket_each(make_market_client: MarketClient) -> None:
    """Two token buckets, not one: ``data.`` and ``paper-api.`` each carry
    their own 200/min, which is why the chain and its open interest can be
    fetched together without either one starving the other."""
    client, transport = make_market_client(nvda_chain())

    rows(client, "/api/markets/chain/NVDA")

    assert "data.alpaca.markets" in transport.hosts
    assert "paper-api.alpaca.markets" in transport.hosts
    # Pinned as a count, not just as a pair of hosts: a chain is a hundred
    # contracts and the way this gets expensive is one request per row. The
    # snapshot is fetched twice -- once here for the strike band, once inside
    # the provider for the Black-Scholes spot -- and that redundancy is
    # recorded rather than hidden, because removing it means changing the
    # provider's signature.
    assert transport.count_for("/v2/stocks/snapshots") == 2
    assert transport.count_for("/v1beta1/options/snapshots/") == 2
    assert transport.count_for("/v2/options/contracts") == 2
    assert len(transport.requests) == 6


def test_nothing_this_route_computes_loses_precision_on_the_wire(
    make_market_client: MarketClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Every number on a hundred-row chain round-trips its own ``Decimal``.

    ``JsonMoney`` is the one sanctioned float in the codebase and it logs when
    a value cannot survive the conversion. A derived percentage is where that
    would happen: an unquantized ``change / previous * 100`` is a repeating
    decimal, and it would warn on every row of every response rather than
    failing once somewhere findable.
    """
    client, _ = make_market_client(nvda_chain())

    with caplog.at_level(logging.WARNING, logger="corollary.api.schemas"):
        chain = rows(client, "/api/markets/chain/NVDA")

    assert len(chain) == 100
    lossy = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "money_serialization_lossy"
    ]
    assert lossy == []


def test_a_denied_feed_is_a_stated_condition_not_a_traceback(
    make_market_client: MarketClient,
) -> None:
    """403 "OPRA agreement is not signed" is the plan, not a bug in the code.

    It reaches the shared envelope as ``feed_unavailable`` so a client can
    branch on it, and 502 rather than 403 because it is the *upstream*
    refusing us, not the person at the keyboard being unauthorised.
    """
    client, _ = make_market_client(
        market_data_routes(
            chain="option_chain_opra_denied",
            contracts=contracts_page("option_contracts_nvda"),
        )
    )

    response = client.get("/api/markets/chain/NVDA")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "feed_unavailable"


def test_an_underlying_that_is_not_a_symbol_is_refused(
    make_market_client: MarketClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A path parameter is client input, and it reaches the vendor.

    Refused before it costs three requests, with the rule, the input and the
    timestamp recorded.
    """
    client, transport = make_market_client(nvda_chain())

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.markets"):
        response = client.get("/api/markets/chain/not-a-symbol")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_symbol"
    assert transport.requests == []
    assert any(r.__dict__.get("event") == "symbol_rejected" for r in caplog.records)


def test_the_chain_can_be_narrowed_to_one_right(
    make_market_client: MarketClient,
) -> None:
    """The filter is server-side, so the narrowing saves pages rather than
    discarding rows that were already paid for."""
    client, transport = make_market_client(nvda_chain())

    rows(client, "/api/markets/chain/NVDA", type="put")

    assert transport.params_for("/v1beta1/options/snapshots/")["type"] == "put"
    assert transport.params_for("/v2/options/contracts")["type"] == "put"


def test_an_exact_expiration_replaces_the_dte_window(
    make_market_client: MarketClient,
) -> None:
    client, transport = make_market_client(nvda_chain())

    rows(client, "/api/markets/chain/NVDA", expiration="2026-09-11")
    params = transport.params_for("/v1beta1/options/snapshots/")

    assert params["expiration_date_gte"] == "2026-09-11"
    assert params["expiration_date_lte"] == "2026-09-11"


# --------------------------------------------------------------------------
# Underlyings
# --------------------------------------------------------------------------


def test_an_underlying_carries_a_price_a_previous_close_and_a_series(
    make_market_client: MarketClient,
) -> None:
    client, _ = make_market_client(market_data_routes())
    snapshot = fixture("stock_snapshots")["NVDA"]

    quote = by_symbol(rows(client, "/api/markets/underlyings", symbols="NVDA"))["NVDA"]

    assert Decimal(str(quote["previousClose"])) == Decimal(
        str(snapshot["prevDailyBar"]["c"])
    )
    assert quote["history"]
    assert all(point["date"] and point["value"] is not None for point in quote["history"])


def test_the_series_is_ordered_oldest_first_and_ends_at_the_live_price(
    make_market_client: MarketClient,
) -> None:
    """The chart's last point and the quoted price are the same number.

    They have to be: the range control reports the move *over the window on
    screen*, and a series that stops at yesterday's close beside a price from
    a second ago reports on a chart nobody is looking at.
    """
    client, _ = make_market_client(market_data_routes())

    quote = by_symbol(rows(client, "/api/markets/underlyings", symbols="NVDA"))["NVDA"]
    dates = [point["date"] for point in quote["history"]]

    assert dates == sorted(dates)
    assert dates[-1] == str(RECORDED_TRADING_DATE)
    assert quote["history"][-1]["value"] == quote["price"]


def test_the_series_is_fetched_once_per_trading_date(
    make_market_client: MarketClient,
) -> None:
    client, transport = make_market_client(market_data_routes())

    rows(client, "/api/markets/underlyings", symbols="NVDA")
    rows(client, "/api/markets/underlyings", symbols="NVDA")

    assert transport.count_for("/v2/stocks/bars") == 1


def test_the_series_is_bounded_by_the_requested_window(
    make_market_client: MarketClient,
) -> None:
    """A 400-day fetch, sliced per request rather than re-fetched.

    ``UNDERLYINGS`` carries 400 calendar days because at a quarter the 3M, YTD,
    1Y and All ranges all return the same points.
    """
    client, transport = make_market_client(market_data_routes())

    full = by_symbol(rows(client, "/api/markets/underlyings", symbols="NVDA"))["NVDA"]
    narrowed = by_symbol(
        rows(client, "/api/markets/underlyings", symbols="NVDA", history_days=5)
    )["NVDA"]

    assert len(narrowed["history"]) < len(full["history"])
    assert transport.count_for("/v2/stocks/bars") == 1


def test_the_underlyings_default_to_the_names_a_position_can_be_written_on(
    make_market_client: MarketClient,
) -> None:
    client, transport = make_market_client(market_data_routes())

    rows(client, "/api/markets/underlyings")

    asked = transport.params_for("/v2/stocks/snapshots")["symbols"].split(",")
    assert set(asked) == set(markets_routes.UNDERLYING_SYMBOLS)


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------


def test_the_module_never_imports_the_vendor_sdk() -> None:
    """The vendor surface is two files, and this is not one of them.

    Swapping Alpaca for ThetaData has to stay a config change.
    """
    source = MODULE.read_text(encoding="utf-8")

    assert "import alpaca" not in source
    assert "from alpaca" not in source


def test_the_module_never_names_a_feed() -> None:
    """Feed names are configuration, read only inside the provider."""
    source = MODULE.read_text(encoding="utf-8")

    for literal in ('"indicative"', "'indicative'", '"opra"', "'opra'"):
        assert literal not in source


def test_no_route_here_can_reach_an_order() -> None:
    """Rule 1, structurally: there is one path to ``submit_order`` and a
    market-data route is not it. ``BrokerExecution`` does not exist."""
    source = MODULE.read_text(encoding="utf-8")

    assert "submit_order" not in source
    assert "BrokerDep" not in source
