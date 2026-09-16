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

import asyncio
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
from corollary.calendars import nyse_session_open
from corollary.data.providers.fundamentals import (
    FundamentalsError,
    FundamentalsProvider,
    MarketCap,
)

from .conftest import (
    FIXTURE_DIR,
    MARKET_DATA_RECORDED_AT,
    FakeFundamentals,
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

#: Noon in New York on the Saturday after it. The weekend case the Volume
#: column exists to survive: NYSE has no session on the 15th, so there is no
#: bar for today and the last completed session is Friday's -- the fixture's
#: final bar.
AUGUST_15 = datetime(2026, 8, 15, 16, 0, tzinfo=timezone.utc)


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


def test_a_symbol_with_no_bar_at_all_has_no_volume_rather_than_zero(
    make_market_client: MarketClient,
) -> None:
    """Null is now narrower, and it is still null.

    Since a closed market falls back to the last completed session, the two
    states that used to produce a null -- before the session's first print,
    and inside the historical feed's 15-minute embargo -- now produce
    yesterday's figure, labelled as yesterday's. What survives is the case
    with no bar anywhere in the window: ``stock_bars_daily`` carries NVDA and
    SPY and not AAPL, and a zero there would claim a symbol did not trade.

    The label goes null with the number. A session state beside an absent
    volume would be a claim about a measurement that was never made.
    """
    client, _ = make_market_client(market_data_routes())

    table = by_symbol(rows(client, "/api/markets/stocks", symbols="NVDA,AAPL"))

    assert table["AAPL"]["volume"] is None
    assert table["AAPL"]["volumeSession"] is None
    assert table["AAPL"]["volumeDate"] is None
    assert table["NVDA"]["volume"] is not None


def test_a_closed_market_shows_the_last_completed_session(
    make_market_client: MarketClient,
) -> None:
    """The weekend case, asked for by name.

    Asked at noon on Saturday the 15th there is no bar for today and never
    will be, so a column keyed strictly on *today* is blank for every symbol
    on the page. The last completed session is equally honest and answers the
    question the trader actually has, so that is what is served -- and it
    arrives stamped with the session it covers rather than left to the
    client's clock to work out.
    """
    client, _ = make_market_client(market_data_routes(), now=AUGUST_15)
    friday = fixture("stock_bars_daily")["bars"]["NVDA"][-1]

    row = by_symbol(rows(client, "/api/markets/stocks", symbols="NVDA"))["NVDA"]

    assert row["volume"] == int(friday["v"])
    assert row["volumeSession"] == "completed"
    assert row["volumeDate"] == "2026-08-14"


def test_the_same_session_reads_partial_during_it_and_final_after_it(
    make_market_client: MarketClient,
) -> None:
    """Why the column carries a state as well as a number.

    Both clients report 14 August and only one of them is a whole day: at
    15:10 on the Friday the figure is the session so far, and by Saturday it
    is the session. A reader who cannot tell the two apart compares a partial
    day against a full one and concludes a stock is quiet when it is
    mid-morning.
    """
    during, _ = make_market_client(market_data_routes(), now=AUGUST_14)
    after, _ = make_market_client(market_data_routes(), now=AUGUST_15)

    live = by_symbol(rows(during, "/api/markets/stocks", symbols="NVDA"))["NVDA"]
    closed = by_symbol(rows(after, "/api/markets/stocks", symbols="NVDA"))["NVDA"]

    assert (live["volumeDate"], live["volumeSession"]) == (
        "2026-08-14",
        "in_progress",
    )
    assert (closed["volumeDate"], closed["volumeSession"]) == (
        "2026-08-14",
        "completed",
    )
    assert live["volume"] == closed["volume"]


def test_the_average_excludes_the_session_the_volume_came_from(
    make_market_client: MarketClient,
) -> None:
    """Self-inclusion biases every ratio toward 1, which masks the outlier.

    During a session the denominator excludes today because today is partial.
    The fallback moves the numerator back a day, so the denominator has to
    move with it: averaging Friday against a window that contains Friday
    pulls the ratio toward 1 exactly when the number is meant to show a
    session standing out. The fixture makes the two answers different --
    nine sessions against ten.
    """
    volumes = [int(bar["v"]) for bar in fixture("stock_bars_daily")["bars"]["NVDA"]]
    before = volumes[:-1]
    client, _ = make_market_client(market_data_routes(), now=AUGUST_15)

    row = by_symbol(rows(client, "/api/markets/stocks", symbols="NVDA"))["NVDA"]

    assert row["volume"] == volumes[-1]
    assert row["avgVolume"] == sum(before) // len(before)
    assert row["avgVolume"] != sum(volumes) // len(volumes)


def test_the_closed_market_fallback_costs_no_extra_request(
    make_market_client: MarketClient,
) -> None:
    """The last session comes out of the series already fetched for the average.

    Two bars windows before this change and two after: the 90-day series and
    today's. The fallback reads the last entry of the series it already has,
    which is also what makes the numerator and the denominator provably one
    measurement -- in this case one *response*.
    """
    client, transport = make_market_client(market_data_routes(), now=AUGUST_15)

    rows(client, "/api/markets/stocks", symbols="NVDA,SPY")

    assert transport.count_for("/v2/stocks/bars") == 2


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

    **The clock advances past the coalescing TTL between the two polls**, or
    the second one never reaches these caches at all: decision 18's cache on
    the snapshot would answer it whole and this test would pass without
    testing anything. See :data:`~corollary.api.routes.markets.
    STOCK_SNAPSHOT_TTL`.
    """
    client, transport = make_market_client(market_data_routes())
    clock = [MARKET_DATA_RECORDED_AT]
    client.app.state.market_caches = markets_routes.MarketCaches(now=lambda: clock[0])

    rows(client, "/api/markets/stocks", symbols="NVDA,SPY")
    clock[0] = MARKET_DATA_RECORDED_AT + markets_routes.STOCK_SNAPSHOT_TTL
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


@pytest.mark.asyncio
async def test_a_figure_read_after_its_session_finished_outlives_the_ttl() -> None:
    """A finished session's volume cannot move, so nothing re-reads it.

    The TTL exists because today's volume climbs all session. Once the
    session is over -- and on a Saturday it is over before the page is even
    opened -- the same 60-second expiry spends a request a minute to be told
    the same integer until midnight.

    The predicate is asked about the instant the entry was **read**, not the
    instant of the question. An entry fetched at 15:59 holds a figure missing
    the last minutes of the session; judging it by the clock at 16:20 would
    freeze that short number in place and call it the close.
    """
    cache: markets_routes.IntradayCache[int | None] = markets_routes.IntradayCache(
        timedelta(seconds=60)
    )
    calls: list[tuple[str, ...]] = []
    fetch = await _counting_fetch(calls)
    today = date(2026, 8, 14)
    at = datetime(2026, 8, 14, 19, 0, tzinfo=timezone.utc)
    settled_at = at + timedelta(minutes=5)

    def settled(_value: int | None, read_at: datetime) -> bool:
        return read_at >= settled_at

    async def read(offset: timedelta) -> int | None:
        return (
            await cache.resolve(
                ("NVDA",),
                today=today,
                now=at + offset,
                fetch=fetch,
                settled=settled,
            )
        )["NVDA"]

    during = await read(timedelta(0))
    reread = await read(timedelta(minutes=10))
    held = await read(timedelta(hours=1))
    still_held = await read(timedelta(hours=6))

    assert (during, reread, held, still_held) == (1, 2, 2, 2)
    assert len(calls) == 2


# --------------------------------------------------------------------------
# The coalescing cache -- decision 18
# --------------------------------------------------------------------------
#
# The browser drives the Markets poll and this API forwards it to Alpaca, so
# two tabs, a reload loop or a hot-reloading dev server multiply the vendor
# rate by the number of clients. `ratelimit.py`'s bucket *waits* rather than
# refusing, so the symptom is not an error -- it is every Markets request
# getting slower until the page looks broken for a reason nothing logs. These
# tests are the property that stops it: the vendor rate is bounded by the
# wall clock, not by the client count.


class _ParkedFetch:
    """A fetch that parks until it is released, counting how often it ran.

    The count is the whole assertion, and the parking is what makes it about
    *concurrency* rather than about a TTL: the second caller has to arrive
    while the first request is still in flight, which is the case a
    fetch-then-store cache would get wrong and a lock-across-the-fetch one
    gets right.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self) -> int:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return self.calls


async def _yield_to_the_loop() -> None:
    """Let a just-created task run until it blocks. No wall-clock sleep."""
    for _ in range(3):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_two_concurrent_callers_share_one_in_flight_fetch() -> None:
    """The step, in one assertion: two callers, one vendor request.

    The second caller arrives while the first request is still open, waits on
    the lock, and is served the answer the first one got. A cache that only
    stored the result would have let both requests leave.
    """
    cache: markets_routes.CoalescingCache[int] = markets_routes.CoalescingCache(
        markets_routes.STOCK_SNAPSHOT_TTL
    )
    fetch = _ParkedFetch()
    at = MARKET_DATA_RECORDED_AT

    first = asyncio.create_task(cache.resolve(("NVDA",), now=at, fetch=fetch))
    await fetch.started.wait()
    second = asyncio.create_task(cache.resolve(("NVDA",), now=at, fetch=fetch))
    await _yield_to_the_loop()
    fetch.release.set()
    answers = await asyncio.gather(first, second)

    assert fetch.calls == 1
    assert answers == [1, 1]


@pytest.mark.asyncio
async def test_two_symbol_sets_are_two_fetches() -> None:
    """The key is the symbol set, so a different set is a different question.

    Sharing across sets would serve one caller a table it did not ask for --
    the failure the cache is not allowed to introduce while preventing the
    other one.
    """
    cache: markets_routes.CoalescingCache[int] = markets_routes.CoalescingCache(
        markets_routes.STOCK_SNAPSHOT_TTL
    )
    fetch = _ParkedFetch()
    fetch.release.set()
    at = MARKET_DATA_RECORDED_AT

    nvda = await cache.resolve(("NVDA",), now=at, fetch=fetch)
    spy = await cache.resolve(("SPY",), now=at, fetch=fetch)
    again = await cache.resolve(("NVDA",), now=at, fetch=fetch)

    assert fetch.calls == 2
    assert (nvda, spy, again) == (1, 2, 1)


@pytest.mark.asyncio
async def test_a_caller_past_the_ttl_gets_a_fresh_fetch() -> None:
    """Near-simultaneous is a window, and the window has to end.

    One microsecond inside the TTL is still the same poll; the instant it
    expires is the next one. Driven by an injected clock rather than by
    sleeping, so the boundary is exact.
    """
    cache: markets_routes.CoalescingCache[int] = markets_routes.CoalescingCache(
        markets_routes.STOCK_SNAPSHOT_TTL
    )
    fetch = _ParkedFetch()
    fetch.release.set()
    at = MARKET_DATA_RECORDED_AT
    ttl = markets_routes.STOCK_SNAPSHOT_TTL

    first = await cache.resolve(("NVDA",), now=at, fetch=fetch)
    held = await cache.resolve(
        ("NVDA",), now=at + ttl - timedelta(microseconds=1), fetch=fetch
    )
    expired = await cache.resolve(("NVDA",), now=at + ttl, fetch=fetch)

    assert (first, held, expired) == (1, 1, 2)
    assert fetch.calls == 2


@pytest.mark.asyncio
async def test_a_failed_fetch_is_not_served_to_the_next_caller(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A vendor failure is never cached as an answer, and it is logged.

    Caching a raise would turn one timeout into a whole TTL of them, and --
    worse -- a cache that stored *something* on failure would serve an
    absence as data. The next caller re-asks, inside the same TTL, and the
    refusal to cache records the rule, the inputs and the timestamp.
    """
    cache: markets_routes.CoalescingCache[int] = markets_routes.CoalescingCache(
        markets_routes.STOCK_SNAPSHOT_TTL
    )
    attempts: list[int] = []

    async def fetch() -> int:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("the vendor hung up")
        return 7

    at = MARKET_DATA_RECORDED_AT

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.markets"):
        with pytest.raises(RuntimeError):
            await cache.resolve(("NVDA", "SPY"), now=at, fetch=fetch)
        recovered = await cache.resolve(("NVDA", "SPY"), now=at, fetch=fetch)

    assert recovered == 7
    assert attempts == [1, 2]
    record = next(
        r for r in caplog.records if r.__dict__.get("event") == "coalesced_fetch_failed"
    )
    assert record.__dict__["symbols"] == ["NVDA", "SPY"]
    assert "never cached" in record.__dict__["rule"]
    assert record.__dict__["at"]


@pytest.mark.asyncio
async def test_a_failed_fetch_does_not_strand_the_caller_waiting_on_it() -> None:
    """The waiter is neither served the failure as data nor left holding it.

    A shared in-flight *result* would hand the second caller the first one's
    exception; a shared in-flight *lock* hands it the next attempt. The
    second is right here, because the two callers are two HTTP requests and
    the second one can still be answered.
    """
    cache: markets_routes.CoalescingCache[int] = markets_routes.CoalescingCache(
        markets_routes.STOCK_SNAPSHOT_TTL
    )
    started = asyncio.Event()
    release = asyncio.Event()
    attempts: list[int] = []

    async def fetch() -> int:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            started.set()
            await release.wait()
            raise RuntimeError("the vendor hung up")
        return 7

    at = MARKET_DATA_RECORDED_AT

    failing = asyncio.create_task(cache.resolve(("NVDA",), now=at, fetch=fetch))
    await started.wait()
    waiting = asyncio.create_task(cache.resolve(("NVDA",), now=at, fetch=fetch))
    await _yield_to_the_loop()
    release.set()

    with pytest.raises(RuntimeError):
        await failing
    assert await waiting == 7
    assert attempts == [1, 2]


class _ConcurrencyProbe:
    """Parked fetches that record the **peak** number in flight at once.

    :class:`_ParkedFetch` counts how often a fetch ran, which is enough while
    every caller shares one lock. This one counts how many were inside it at
    the same moment, which is the property a *dropped* lock breaks: the
    second request leaves while the first is still open, and a call count
    cannot tell that apart from two requests a minute apart.
    """

    def __init__(self) -> None:
        self.peak = 0
        self._in_flight = 0
        self.entered: dict[str, asyncio.Event] = {}
        self.gates: dict[str, asyncio.Event] = {}

    def _event(self, where: dict[str, asyncio.Event], name: str) -> asyncio.Event:
        return where.setdefault(name, asyncio.Event())

    def entering(self, name: str) -> asyncio.Event:
        """Set the moment ``name``'s fetch starts."""
        return self._event(self.entered, name)

    def gate(self, name: str) -> asyncio.Event:
        """Set by the test to let ``name``'s fetch return."""
        return self._event(self.gates, name)

    def fetch(self, name: str, *, counted: bool = True) -> markets_routes.FetchOne[str]:
        async def run() -> str:
            if counted:
                self._in_flight += 1
                self.peak = max(self.peak, self._in_flight)
            self.entering(name).set()
            await self.gate(name).wait()
            if counted:
                self._in_flight -= 1
            return name

        return run


@pytest.mark.asyncio
async def test_a_sweep_does_not_drop_a_lock_another_caller_is_waiting_on() -> None:
    """A waiting caller keeps its key's lock, so the coalescing survives.

    ``asyncio.Lock.locked()`` is not a liveness test. ``release()`` clears the
    flag and schedules the first waiter; the waiter sets it again only when it
    *resumes*, so in between there is a window where the lock reads unlocked
    while a caller is queued on it. A sweep that trusted ``locked()`` deleted
    the lock in that window, the waiter woke holding an orphan, and the next
    caller built a **second** lock for the same key -- two concurrent Alpaca
    snapshot requests for one symbol set, which is the whole thing this cache
    exists to prevent, and silent, because ``ratelimit.py``'s bucket waits
    rather than refusing.

    The interleaving below is ordinary, not contrived: ``a`` is a fetch slower
    than the 400ms TTL, so the entry it stores (``read_at`` is taken *before*
    the fetch, by design) is already expired when it lands; ``b`` arrives
    mid-flight; ``c`` is an unrelated symbol set whose own sweep runs in the
    same loop iteration. Driven by events and an injected clock -- no wall
    clock anywhere, so the ordering is exact rather than likely.
    """
    cache: markets_routes.CoalescingCache[str] = markets_routes.CoalescingCache(
        markets_routes.STOCK_SNAPSHOT_TTL
    )
    probe = _ConcurrencyProbe()
    at = MARKET_DATA_RECORDED_AT
    later = at + markets_routes.STOCK_SNAPSHOT_TTL + timedelta(milliseconds=100)

    slow = asyncio.create_task(
        cache.resolve(("NVDA",), now=at, fetch=probe.fetch("a"))
    )
    await probe.entering("a").wait()
    unrelated = asyncio.create_task(
        cache.resolve(("SPY",), now=later, fetch=probe.fetch("c", counted=False))
    )
    await probe.entering("c").wait()
    waiter = asyncio.create_task(
        cache.resolve(("NVDA",), now=later, fetch=probe.fetch("b"))
    )
    await _yield_to_the_loop()
    lock_before = cache._locks[("NVDA",)]

    # Both answers land in the same iteration: `a` stores an already-expired
    # entry and releases the lock, then `c`'s sweep -- at a `now` past the
    # TTL -- considers NVDA while `b` is queued but not yet resumed.
    probe.gate("a").set()
    probe.gate("c").set()
    await probe.entering("b").wait()

    assert cache._locks.get(("NVDA",)) is lock_before

    after = asyncio.create_task(
        cache.resolve(("NVDA",), now=later, fetch=probe.fetch("d"))
    )
    await _yield_to_the_loop()
    probe.gate("b").set()
    probe.gate("d").set()
    await asyncio.gather(slow, waiter, unrelated, after)

    assert probe.peak == 1


@pytest.mark.asyncio
async def test_a_sweep_keeps_the_lock_of_a_fetch_still_in_flight() -> None:
    """The boundary the fix must not overshoot.

    Dropping a lock that is genuinely held would hand two callers two locks
    for one key just as surely as dropping one with a waiter does. An expired
    entry under an open request keeps its lock; only the entry goes.
    """
    cache: markets_routes.CoalescingCache[str] = markets_routes.CoalescingCache(
        markets_routes.STOCK_SNAPSHOT_TTL
    )
    probe = _ConcurrencyProbe()
    at = MARKET_DATA_RECORDED_AT
    later = at + markets_routes.STOCK_SNAPSHOT_TTL + timedelta(milliseconds=100)

    held = asyncio.create_task(
        cache.resolve(("NVDA",), now=at, fetch=probe.fetch("a"))
    )
    await probe.entering("a").wait()
    lock_before = cache._locks[("NVDA",)]

    cache._forget_expired(now=later)

    assert cache._locks.get(("NVDA",)) is lock_before
    probe.gate("a").set()
    assert await held == "a"


@pytest.mark.asyncio
async def test_a_failed_fetch_leaves_neither_an_entry_nor_a_lock_behind() -> None:
    """The failure path sweeps too, or a failing key holds its lock forever.

    Every subset of the universe is a key a client may ask for, and the
    bookkeeping is dropped on the way out of :meth:`resolve` rather than only
    after a successful store -- a key whose vendor call always fails is
    exactly the key nothing will ever come back to clean up.
    """
    cache: markets_routes.CoalescingCache[int] = markets_routes.CoalescingCache(
        markets_routes.STOCK_SNAPSHOT_TTL
    )

    async def boom() -> int:
        raise RuntimeError("the vendor hung up")

    with pytest.raises(RuntimeError):
        await cache.resolve(("NVDA",), now=MARKET_DATA_RECORDED_AT, fetch=boom)

    assert cache._locks == {}
    assert cache._entries == {}


@pytest.mark.asyncio
async def test_an_expired_key_nobody_is_using_is_forgotten() -> None:
    """The key space stays bounded, which is what the sweep is for.

    A symbol set asked for once and never again would otherwise be held --
    entry *and* lock -- for the life of the process. The live key survives
    the same sweep, so "bounded" does not quietly mean "emptied".
    """
    cache: markets_routes.CoalescingCache[int] = markets_routes.CoalescingCache(
        markets_routes.STOCK_SNAPSHOT_TTL
    )
    fetch = _ParkedFetch()
    fetch.release.set()
    at = MARKET_DATA_RECORDED_AT
    later = at + markets_routes.STOCK_SNAPSHOT_TTL

    await cache.resolve(("NVDA",), now=at, fetch=fetch)
    await cache.resolve(("SPY",), now=later, fetch=fetch)

    assert set(cache._entries) == {("SPY",)}
    assert set(cache._locks) == {("SPY",)}


def test_the_cache_ttl_is_the_foreground_poll_interval() -> None:
    """Equal by construction, so the two halves cannot drift apart.

    The client half is the Markets page's foreground interval -- decision
    18's cadence table, landing in ``web/src/hooks/`` in step 14 -- and this
    is the server half. The cache exists so that N clients polling at that
    interval cost one Alpaca request per interval rather than N, which is a
    claim only true while the two numbers are the same number. Grep
    ``MARKETS_FOREGROUND_POLL_MS`` to find both.
    """
    assert markets_routes.MARKETS_FOREGROUND_POLL_MS == 400
    assert markets_routes.STOCK_SNAPSHOT_TTL == timedelta(
        milliseconds=markets_routes.MARKETS_FOREGROUND_POLL_MS
    )


def test_two_near_simultaneous_polls_cost_one_snapshot_request(
    make_market_client: MarketClient,
) -> None:
    """Two tabs, one vendor request -- the route half of the same property.

    Sequential here rather than concurrent because the interesting variable
    is the clock, not the scheduler: both polls are inside one TTL, which is
    what "two clients at 400ms" looks like from the server. The unit tests
    above cover the genuinely-in-flight case.

    **The clock is injected, like every neighbour here**, and the second poll
    is placed at ``ttl - 1µs`` rather than at the same instant -- so the test
    states the window it is asserting about instead of relying on two full
    route calls fitting inside 400ms of *wall* clock. On a loaded machine
    that race would read as "the cache broke", which is the one wrong
    conclusion this file must not invite.
    """
    client, transport = make_market_client(market_data_routes())
    clock = [MARKET_DATA_RECORDED_AT]
    client.app.state.market_caches = markets_routes.MarketCaches(now=lambda: clock[0])

    first = rows(client, "/api/markets/stocks", symbols="NVDA,SPY")
    clock[0] = (
        MARKET_DATA_RECORDED_AT
        + markets_routes.STOCK_SNAPSHOT_TTL
        - timedelta(microseconds=1)
    )
    second = rows(client, "/api/markets/stocks", symbols="NVDA,SPY")

    assert transport.count_for("/v2/stocks/snapshots") == 1
    assert first == second


def test_the_symbol_order_does_not_decide_whether_the_cache_hits(
    make_market_client: MarketClient,
) -> None:
    """Two tabs asking for the same names in a different order are one question.

    The key is the normalised set, so ``NVDA,SPY`` and ``spy,nvda`` share a
    request -- keyed on the raw query string they would miss each other and
    the cache would do nothing for the case it exists for. Row order still
    follows what each caller asked for.
    """
    client, transport = make_market_client(market_data_routes())

    forwards = rows(client, "/api/markets/stocks", symbols="NVDA,SPY")
    backwards = rows(client, "/api/markets/stocks", symbols="spy,nvda")

    assert transport.count_for("/v2/stocks/snapshots") == 1
    assert [row["symbol"] for row in forwards] == ["NVDA", "SPY"]
    assert [row["symbol"] for row in backwards] == ["SPY", "NVDA"]


def test_a_different_symbol_set_is_not_served_the_cached_one(
    make_market_client: MarketClient,
) -> None:
    """A narrower question costs its own request rather than a wrong answer."""
    client, transport = make_market_client(market_data_routes())

    both = rows(client, "/api/markets/stocks", symbols="NVDA,SPY")
    one = rows(client, "/api/markets/stocks", symbols="NVDA")

    assert transport.count_for("/v2/stocks/snapshots") == 2
    assert [row["symbol"] for row in both] == ["NVDA", "SPY"]
    assert [row["symbol"] for row in one] == ["NVDA"]


def test_a_poll_past_the_ttl_reaches_the_vendor_again(
    make_market_client: MarketClient,
) -> None:
    """The cache bounds the rate; it must not freeze the table.

    A price held past the interval is a screener reporting a stale market,
    which is the failure the poll exists to prevent. The clock is driven
    rather than slept on, so the boundary is exact and the test cannot flake.
    """
    client, transport = make_market_client(market_data_routes())
    clock = [MARKET_DATA_RECORDED_AT]
    client.app.state.market_caches = markets_routes.MarketCaches(now=lambda: clock[0])

    rows(client, "/api/markets/stocks", symbols="NVDA,SPY")
    clock[0] = MARKET_DATA_RECORDED_AT + markets_routes.STOCK_SNAPSHOT_TTL
    rows(client, "/api/markets/stocks", symbols="NVDA,SPY")

    assert transport.count_for("/v2/stocks/snapshots") == 2


def test_a_session_is_in_progress_until_its_own_close_plus_the_embargo() -> None:
    """The label comes from the market calendar, never from 16:00.

    14 August 2026 is an ordinary session closing at 16:00 ET, and its daily
    bar is still short of the close for fifteen minutes afterwards -- the
    historical feed serves up to fifteen minutes ago, so a bar read at 16:05
    is missing the last of the day it claims to cover.
    """
    close = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)
    session = date(2026, 8, 14)

    assert markets_routes._session_state(session, now=close) == "in_progress"
    assert (
        markets_routes._session_state(session, now=close + timedelta(minutes=5))
        == "in_progress"
    )
    assert (
        markets_routes._session_state(session, now=close + timedelta(minutes=15))
        == "completed"
    )


def test_a_half_day_finishes_three_hours_before_a_hardcoded_close_would() -> None:
    """Half-days are real, and they are not distributed randomly.

    NYSE closes at 13:00 ET on the Friday after Thanksgiving. Measured
    against a hardcoded 16:00 the column would call that session "in
    progress" for three hours after it ended, and keep re-reading a figure
    that had stopped moving.
    """
    black_friday = date(2026, 11, 27)
    one_twenty = datetime(2026, 11, 27, 18, 20, tzinfo=timezone.utc)

    assert markets_routes._session_state(black_friday, now=one_twenty) == "completed"
    assert (
        markets_routes._session_state(black_friday, now=one_twenty - timedelta(hours=2))
        == "in_progress"
    )


def test_a_day_with_no_session_has_no_session_in_progress() -> None:
    """Saturday, which is what makes the weekend figure settle on first read."""
    saturday = date(2026, 8, 15)
    noon = datetime(2026, 8, 15, 16, 0, tzinfo=timezone.utc)

    assert markets_routes._session_state(saturday, now=noon) == "completed"


def test_a_companys_market_cap_is_dollars_and_a_funds_is_null(
    make_market_client: MarketClient,
) -> None:
    """Decision 7's column, and the null that is not a zero.

    The provider converts Finnhub's millions to whole dollars and
    ``MarketCap.reported`` rounds there -- ``…522.442`` becomes ``…522`` --
    so what the route owes is that *exact* integer and no rounding of its
    own. Asserted exactly rather than through ``pytest.approx``, whose
    relative tolerance on a figure this size is about ±$4.8M: a regression
    quantising market caps to the nearest ten million dollars passed it,
    which was measured rather than supposed. Every integer below 2**53 is
    exact in a double, so equality against the JSON number is well defined.

    The other half is a fund's absence serving as ``null``. CLAUDE.md:
    coerced to zero, SPY sorts to the top of an ascending column of dollars
    and reads as worth nothing.
    """
    fundamentals = FakeFundamentals(
        {
            "NVDA": MarketCap.reported("NVDA", Decimal("4849208193522.442")),
            "SPY": MarketCap.not_filed("SPY", "a fund files no share count"),
        }
    )
    client, _ = make_market_client(market_data_routes(), fundamentals=fundamentals)

    table = rows(client, "/api/markets/stocks", symbols="NVDA,SPY")

    assert [row["symbol"] for row in table] == ["NVDA", "SPY"]
    assert table[0]["marketCap"] == 4849208193522
    assert table[1]["marketCap"] is None


def test_a_market_cap_that_could_not_be_fetched_is_null_not_a_guess(
    make_market_client: MarketClient,
) -> None:
    fundamentals = FakeFundamentals(
        {
            "NVDA": MarketCap.unavailable("NVDA", "GET /stock/profile2 returned 503"),
            "SPY": MarketCap.not_filed("SPY"),
        }
    )
    client, _ = make_market_client(market_data_routes(), fundamentals=fundamentals)

    table = rows(client, "/api/markets/stocks", symbols="NVDA,SPY")

    assert [row["marketCap"] for row in table] == [None, None]
    # The prices are untouched: a reference-data outage empties one column.
    assert all(row["price"] is not None for row in table)


def test_a_whole_call_failure_empties_one_column_and_is_logged(
    make_market_client: MarketClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 8. The table still renders; the log says why the column did not.

    Without the log, the first time this column empties nobody can tell an
    outage from two funds.
    """

    class Exploding(FundamentalsProvider):
        async def market_caps(self, symbols: Any) -> dict[str, MarketCap]:
            raise FundamentalsError("GET /stock/profile2 failed: timed out")

    client, _ = make_market_client(market_data_routes(), fundamentals=Exploding())

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.markets"):
        table = rows(client, "/api/markets/stocks", symbols="NVDA,SPY")

    assert [row["marketCap"] for row in table] == [None, None]
    failures = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "market_caps_unavailable"
    ]
    assert len(failures) == 1
    assert "timed out" in failures[0].__dict__["cause"]
    assert failures[0].__dict__["symbols"] == ["NVDA", "SPY"]


def test_an_answer_is_fetched_once_a_day_rather_than_once_a_poll(
    make_market_client: MarketClient,
) -> None:
    """The daily cache, which is what keeps 26 symbols inside 60 req/min.

    A two-second poll re-asking every symbol would be 780 requests a minute
    against a ceiling of sixty.
    """
    fundamentals = FakeFundamentals(
        {
            "NVDA": MarketCap.reported("NVDA", Decimal("4.5e12")),
            "SPY": MarketCap.not_filed("SPY"),
        }
    )
    client, _ = make_market_client(market_data_routes(), fundamentals=fundamentals)

    table: list[dict[str, Any]] = []
    for _ in range(3):
        table = rows(client, "/api/markets/stocks", symbols="NVDA,SPY")

    assert fundamentals.calls == [("NVDA", "SPY")]
    assert table[0]["marketCap"] == pytest.approx(4.5e12)


def test_a_failure_is_retried_rather_than_blanking_the_column_all_day(
    make_market_client: MarketClient,
) -> None:
    """A transient 503 must not cost the column until tomorrow.

    The other half of the same predicate: an *answer* is pinned for the
    trading date, a failure expires after ``MARKET_CAP_RETRY_TTL``. Judged on
    the clock alone -- the shape ``settled`` had before this column existed --
    the two would be held identically, and a vendor blip would empty the
    column until the date rolled.
    """
    fundamentals = FakeFundamentals(
        {
            "NVDA": MarketCap.unavailable("NVDA", "503"),
            "SPY": MarketCap.not_filed("SPY"),
        }
    )
    client, _ = make_market_client(market_data_routes(), fundamentals=fundamentals)

    clock = [MARKET_DATA_RECORDED_AT]
    client.app.state.market_caches = markets_routes.MarketCaches(now=lambda: clock[0])

    first = rows(client, "/api/markets/stocks", symbols="NVDA,SPY")
    assert first[0]["marketCap"] is None

    fundamentals.caps["NVDA"] = MarketCap.reported("NVDA", Decimal("4.5e12"))
    # Inside the retry window: still the failure, and no second request.
    clock[0] = MARKET_DATA_RECORDED_AT + timedelta(minutes=1)
    second = rows(client, "/api/markets/stocks", symbols="NVDA,SPY")
    assert second[0]["marketCap"] is None
    assert len(fundamentals.calls) == 1

    clock[0] = MARKET_DATA_RECORDED_AT + markets_routes.MARKET_CAP_RETRY_TTL
    third = rows(client, "/api/markets/stocks", symbols="NVDA,SPY")

    assert third[0]["marketCap"] == pytest.approx(4.5e12)
    # SPY answered the first time and is never asked again.
    assert fundamentals.calls == [("NVDA", "SPY"), ("NVDA",)]


def test_a_cold_start_serves_null_rather_than_a_stale_or_invented_figure(
    make_market_client: MarketClient,
) -> None:
    """Nothing is cached when the process comes up, and nothing is guessed.

    A provider that answers for no symbol at all is the sharpest form of the
    cold-start state: no entry, so no figure, so ``null`` -- never a zero and
    never yesterday's.
    """

    class Silent(FundamentalsProvider):
        async def market_caps(self, symbols: Any) -> dict[str, MarketCap]:
            return {}

    client, _ = make_market_client(market_data_routes(), fundamentals=Silent())

    table = rows(client, "/api/markets/stocks", symbols="NVDA,SPY")

    assert [row["marketCap"] for row in table] == [None, None]


def test_an_app_with_no_fundamentals_vendor_still_serves_the_table(
    make_market_client: MarketClient,
) -> None:
    """The shipped state when ``FINNHUB_API_KEY`` is unset.

    Decision 7 designed the null path for this column; 503-ing a screener of
    prices over a reference-data key nobody set would be the larger error.
    """
    client, _ = make_market_client(market_data_routes())

    table = rows(client, "/api/markets/stocks", symbols="NVDA,SPY")

    assert [row["marketCap"] for row in table] == [None, None]
    assert all(row["price"] is not None for row in table)


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


# --------------------------------------------------------------------------
# The series window: a period, a timeframe, and a ceiling on the two together
# --------------------------------------------------------------------------
#
# The defect these cover: `/underlyings` served daily closes and nothing else,
# so `UnderlyingChart`'s `1D` range drew a single point and `1W` drew about
# five. The control implied a resolution the data could not supply.


#: 09:30 ET on the recorded trading date, in UTC. The synthetic intraday bars
#: start here so they sit inside the window the provider will ask for -- and
#: comfortably inside its fifteen-minute embargo, which at 15:10 ET cuts the
#: request off around 14:55.
RECORDED_SESSION_OPEN = datetime(2026, 9, 10, 13, 30, tzinfo=timezone.utc)


def intraday_bars(
    symbol: str = "NVDA",
    *,
    start: datetime = RECORDED_SESSION_OPEN,
    step: timedelta = timedelta(minutes=5),
    count: int = 12,
) -> Route:
    """A synthetic intraday bars page for one symbol.

    Synthesised rather than recorded, and the exception is stated rather than
    quiet: every other figure in this module comes out of
    ``tests/fixtures/alpaca/``, but the recordings there are *daily* bars and
    the property under test is the **shape** of an intraday series -- one
    point per interval, stamped at the interval's open, oldest first -- not
    any particular price. Recording a minute series would pin a day of prices
    into the repository to assert something none of them decide.
    """
    rows_out = [
        {
            "t": (start + step * index).isoformat().replace("+00:00", "Z"),
            "o": 175.0 + index,
            "h": 176.0 + index,
            "l": 174.0 + index,
            "c": 175.5 + index,
            "v": 1_000 + index,
            "n": 10 + index,
            "vw": 175.4 + index,
        }
        for index in range(count)
    ]
    body = json.dumps({"bars": {symbol: rows_out}, "next_page_token": None})
    return lambda _request: (200, body)


def refusal(client: TestClient, path: str, **params: Any) -> dict[str, Any]:
    """The ``error`` object of a 422, asserted to be one."""
    response = client.get(path, params=params)
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert isinstance(body, dict)
    return body


def test_an_intraday_timeframe_serves_the_resolution_the_range_control_implies(
    make_market_client: MarketClient,
) -> None:
    """A day at 5Min is a day's worth of points, not one.

    The whole defect in one assertion: sliced out of a daily series, ``1D``
    is a single point and the chart is a dot.
    """
    client, _ = make_market_client(market_data_routes(bars=intraday_bars()))

    quote = by_symbol(
        rows(
            client,
            "/api/markets/underlyings",
            symbols="NVDA",
            period="1D",
            timeframe="5Min",
        )
    )["NVDA"]

    assert len(quote["intraday"]) > 1
    stamps = [point["at"] for point in quote["intraday"]]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)
    # The daily series is not half-filled alongside it: one field carries the
    # answer and the other is empty, so no client can read both and disagree.
    assert quote["history"] == []


def test_the_intraday_series_ends_at_the_live_price_not_fifteen_minutes_short(
    make_market_client: MarketClient,
) -> None:
    """The embargo must not be visible as a gap at the right-hand edge.

    ``stock_bars`` resolves ``end`` to fifteen minutes ago, so the last bar is
    stale by construction. The range control reports the move *over the window
    on screen*, and a chart that stops a quarter of an hour short reports on a
    window nobody is looking at.
    """
    client, _ = make_market_client(market_data_routes(bars=intraday_bars()))

    quote = by_symbol(
        rows(
            client,
            "/api/markets/underlyings",
            symbols="NVDA",
            period="1D",
            timeframe="5Min",
        )
    )["NVDA"]

    last = quote["intraday"][-1]
    assert last["value"] == quote["price"]
    last_bar_at = RECORDED_SESSION_OPEN + timedelta(minutes=55)
    assert datetime.fromisoformat(last["at"]) > last_bar_at


def test_the_intraday_request_asks_for_the_timeframe_and_the_historical_feed(
    make_market_client: MarketClient,
) -> None:
    client, transport = make_market_client(market_data_routes(bars=intraday_bars()))

    rows(
        client,
        "/api/markets/underlyings",
        symbols="NVDA",
        period="1D",
        timeframe="15Min",
    )

    params = transport.params_for("/v2/stocks/bars")
    assert params["timeframe"] == "15Min"
    assert feeds_for(transport, "/v2/stocks/bars") == ["sip"]


def test_the_default_window_is_the_four_hundred_day_daily_series(
    make_market_client: MarketClient,
) -> None:
    """The existing behaviour, reachable and unchanged.

    Named explicitly rather than left implied: ``period=400D`` and
    ``timeframe=1D`` must produce identical output to asking for nothing at
    all, or the parameter broke the callers it was added for.
    """
    client, _ = make_market_client(market_data_routes())

    default = rows(client, "/api/markets/underlyings", symbols="NVDA")
    spelled = rows(
        client,
        "/api/markets/underlyings",
        symbols="NVDA",
        period="400D",
        timeframe="1D",
    )

    assert default == spelled
    assert default[0]["intraday"] == []


def test_a_timeframe_outside_the_vocabulary_is_refused_naming_the_five(
    make_market_client: MarketClient,
) -> None:
    """Same vocabulary and same refusal style as ``/api/account/history``.

    Two endpoints answering the same question in two grammars is how a client
    learns one and gets a vendor 400 from the other.
    """
    client, transport = make_market_client(market_data_routes())

    body = refusal(
        client, "/api/markets/underlyings", symbols="NVDA", timeframe="5min"
    )

    assert body["code"] == "invalid_series_window"
    assert "15Min, 1D, 1H, 1Min, 5Min" in body["message"]
    # Refused before it cost a request against either bucket.
    assert transport.requests == []


def test_a_malformed_period_is_refused_and_says_a_year_is_a_not_y(
    make_market_client: MarketClient,
) -> None:
    client, transport = make_market_client(market_data_routes())

    body = refusal(client, "/api/markets/underlyings", symbols="NVDA", period="1Y")

    assert body["code"] == "invalid_series_window"
    assert "A, not Y" in body["message"]
    assert transport.requests == []


def test_a_window_deeper_than_the_endpoint_serves_is_refused_naming_the_limit(
    make_market_client: MarketClient,
) -> None:
    """``3A`` at ``1D`` would otherwise be answerable and wrong.

    The daily cache holds 400 calendar days. A period reaching past it would
    come back silently short -- three years asked for, thirteen months served,
    nothing said.
    """
    client, _ = make_market_client(market_data_routes())

    body = refusal(client, "/api/markets/underlyings", symbols="NVDA", period="3A")

    assert body["code"] == "invalid_series_window"
    assert "400D" in body["message"]


def test_a_period_and_timeframe_too_fine_to_draw_are_refused_naming_a_coarser_one(
    make_market_client: MarketClient,
) -> None:
    """The combination is what is bounded, not either half.

    A year at ``5Min`` is ~19,600 points per symbol -- 251 sessions of 78:
    paginated vendor requests against a shared 200/min budget, megabytes on
    the wire, and roughly twenty points per pixel on a chart that can draw
    about nine hundred. The refusal has to name what to ask for instead.

    **It names ``1D``, and deliberately not ``1H``.** A year of hourly bars
    fits the ceiling once the series is scoped to regular hours -- 251 x 7 =
    1,757 estimated, where the extended-session estimate put it at 4,016 and
    refused it -- and it is still the wrong thing to steer a caller onto,
    because an hourly grid cannot begin a bar at 09:30 and every session in
    it would arrive without its opening half hour. A year of intraday bars
    is not something a ~900px chart can draw either way.
    """
    client, transport = make_market_client(market_data_routes())

    body = refusal(
        client,
        "/api/markets/underlyings",
        symbols="NVDA",
        period="1A",
        timeframe="5Min",
    )

    assert body["code"] == "invalid_series_window"
    assert "Ask for 1A at 1D instead" in body["message"]
    assert "1H" not in body["message"]
    assert transport.requests == []


def test_the_ceiling_permits_the_largest_window_that_still_draws(
    make_market_client: MarketClient,
) -> None:
    """Every limit proves it rejects **and** proves it permits at the boundary.

    Five sessions at ``1Min`` is 1,950 points -- inside the 2,000 ceiling by
    fifty. Six is 2,340 and is not. ``8D`` back from the recorded Thursday
    reaches five sessions (a weekend and Labor Day are not sessions) and
    ``9D`` reaches six, so the pair is one calendar day apart at the boundary
    the ceiling actually draws.
    """
    client, _ = make_market_client(market_data_routes(bars=intraday_bars()))

    permitted = client.get(
        "/api/markets/underlyings",
        params={"symbols": "NVDA", "period": "8D", "timeframe": "1Min"},
    )
    assert permitted.status_code == 200, permitted.text

    refused = client.get(
        "/api/markets/underlyings",
        params={"symbols": "NVDA", "period": "9D", "timeframe": "1Min"},
    )
    assert refused.status_code == 422, refused.text


def test_the_estimate_counts_sessions_from_the_calendar_not_calendar_days(
    make_market_client: MarketClient,
) -> None:
    """Half-days and holidays are real, and this is where that bites.

    1--10 September 2026 is ten calendar days and **seven** sessions: the
    5th and 6th are a weekend and the 7th is Labor Day. Counting days rather
    than sessions would put the estimate at 3,900 points where the window
    holds 2,730 -- and the difference is what decides the refusal.
    """
    client, _ = make_market_client(market_data_routes())

    body = refusal(
        client,
        "/api/markets/underlyings",
        symbols="NVDA",
        period="10D",
        timeframe="1Min",
    )

    assert "2,730" in body["message"]
    assert "3,900" not in body["message"]


def test_the_ceiling_counts_every_symbol_in_the_request(
    make_market_client: MarketClient,
) -> None:
    """One symbol's series is not the response, and the response is the cost.

    ``/underlyings`` takes a list. Two sessions at ``1Min`` is 780 points,
    which any one symbol can afford four times over; across the 26-name
    universe it is 20,280 and over the response ceiling by 280.
    """
    client, _ = make_market_client(market_data_routes(bars=intraday_bars()))

    one = client.get(
        "/api/markets/underlyings",
        params={"symbols": "NVDA", "period": "2D", "timeframe": "1Min"},
    )
    assert one.status_code == 200, one.text

    many = client.get(
        "/api/markets/underlyings",
        params={
            "symbols": ",".join(markets_routes.UNIVERSE_SYMBOLS),
            "period": "2D",
            "timeframe": "1Min",
        },
    )
    assert many.status_code == 422, many.text
    assert "narrow" in many.json()["error"]["message"]


def test_a_refused_window_records_the_rule_the_inputs_and_the_timestamp(
    make_market_client: MarketClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 8. A silent rejection is a bug."""
    client, _ = make_market_client(market_data_routes())

    with caplog.at_level(logging.WARNING, logger=markets_routes.logger.name):
        refusal(
            client,
            "/api/markets/underlyings",
            symbols="NVDA",
            period="1A",
            timeframe="1Min",
        )

    record = next(
        entry
        for entry in caplog.records
        if getattr(entry, "event", None) == "series_window_rejected"
    )
    assert record.rule
    assert record.period == "1A"
    assert record.timeframe == "1Min"
    assert record.symbols == 1
    assert record.at


def test_the_deprecated_history_days_still_narrows_the_daily_window(
    make_market_client: MarketClient,
) -> None:
    """The parameter the live frontend sends today keeps working.

    Dropping it would not have errored -- FastAPI ignores an unknown query
    parameter -- so a caller asking for 90 days would have been handed 400 and
    told nothing.
    """
    client, _ = make_market_client(market_data_routes())

    aliased = rows(client, "/api/markets/underlyings", symbols="NVDA", history_days=5)
    spelled = rows(client, "/api/markets/underlyings", symbols="NVDA", period="5D")

    assert aliased == spelled


def test_history_days_and_period_together_are_refused_rather_than_ranked(
    make_market_client: MarketClient,
) -> None:
    """Two windows in one request have no correct answer, so it gets none."""
    client, _ = make_market_client(market_data_routes())

    body = refusal(
        client,
        "/api/markets/underlyings",
        symbols="NVDA",
        period="1W",
        history_days=90,
    )

    assert body["code"] == "invalid_series_window"
    assert "history_days" in body["message"]


# --------------------------------------------------------------------------
# Regular trading hours: the extended-hours bars are not the chart
# --------------------------------------------------------------------------
#
# Measured, not assumed. Alpaca's bars feed runs roughly 04:00--20:00 ET, so
# one full session of NVDA at `5Min` comes back **192 bars, not 78**. The 1D
# chart therefore drew mostly thin pre- and post-market prints, and the change
# figure beside it measured a window nobody asked for.
#
# The bounds come from the market calendar per bar, for that bar's own session
# date -- never a hardcoded 09:30--16:00, because a half-day closes at 13:00
# and those are real.


#: 04:00 ET on the recorded trading date: the first bar the extended feed
#: serves. 192 five-minute bars from here reach 19:55 ET, the last one.
SEP_10_EXTENDED_OPEN = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)

#: 09:30 and 15:55 ET on that session -- the first and last bars a regular
#: session yields at ``5Min``, both stamped at their interval's open.
SEP_10_REGULAR_OPEN = datetime(2026, 9, 10, 13, 30, tzinfo=timezone.utc)
SEP_10_LAST_REGULAR_BAR = datetime(2026, 9, 10, 19, 55, tzinfo=timezone.utc)

#: 16:30 ET on the recorded trading date. The session has closed, the daily
#: bar exists, and the live price is a post-market print.
AFTER_THE_CLOSE = datetime(2026, 9, 10, 20, 30, tzinfo=timezone.utc)

#: The Friday after Thanksgiving 2025, which closes at **13:00 ET**, asked at
#: 14:00 ET -- an hour into what is post-market on that day alone.
HALF_DAY_AFTER_THE_CLOSE = datetime(2025, 11, 28, 19, 0, tzinfo=timezone.utc)


def bars_at(stamps: list[datetime], symbol: str = "NVDA") -> Route:
    """A bars page stamped at exactly these instants, in the order given.

    ``intraday_bars`` walks a fixed step from one start, which cannot express
    the cases this section is about: a bar either side of a boundary, a
    Saturday, or two sessions with the overnight gap between them.
    """
    rows_out = [
        {
            "t": stamp.isoformat().replace("+00:00", "Z"),
            "o": 175.0,
            "h": 176.0,
            "l": 174.0,
            "c": 175.5 + index,
            "v": 1_000,
            "n": 10,
            "vw": 175.4,
        }
        for index, stamp in enumerate(stamps)
    ]
    body = json.dumps({"bars": {symbol: rows_out}, "next_page_token": None})
    return lambda _request: (200, body)


def five_minutes_from(start: datetime, count: int) -> list[datetime]:
    return [start + timedelta(minutes=5 * index) for index in range(count)]


def stamps_of(quote: dict[str, Any]) -> list[datetime]:
    return [datetime.fromisoformat(point["at"]) for point in quote["intraday"]]


def intraday_quote(
    make_market_client: MarketClient,
    route: Route,
    *,
    now: datetime,
    period: str = "1D",
    timeframe: str = "5Min",
) -> dict[str, Any]:
    """One symbol's quote, asked at ``now`` over bars the caller chose."""
    client, _ = make_market_client(market_data_routes(bars=route), now=now)
    return by_symbol(
        rows(
            client,
            "/api/markets/underlyings",
            symbols="NVDA",
            period=period,
            timeframe=timeframe,
        )
    )["NVDA"]


def test_the_intraday_series_drops_the_extended_hours_bars(
    make_market_client: MarketClient,
) -> None:
    """192 bars in, 78 out -- the regular session and nothing either side.

    The defect in one assertion: three quarters of the points on the 1D chart
    were pre- and post-market prints on a fraction of the volume, and the
    change figure beside the chart was measured from 04:00 ET.
    """
    quote = intraday_quote(
        make_market_client,
        bars_at(five_minutes_from(SEP_10_EXTENDED_OPEN, 192)),
        now=AFTER_THE_CLOSE,
    )

    stamps = stamps_of(quote)
    assert len(stamps) == 78
    assert stamps[0] == SEP_10_REGULAR_OPEN
    assert stamps[-1] == SEP_10_LAST_REGULAR_BAR


def test_the_session_bounds_are_half_open_so_the_close_bar_is_post_market(
    make_market_client: MarketClient,
) -> None:
    """09:30 in, 09:25 out; 15:55 in, 16:00 out.

    ``IntradayPoint.at`` is stamped at the interval's **open**, which is the
    vendor's convention and the one the no-look-ahead rule depends on. So a
    bar stamped 15:55 is the session's last regular bar -- it covers
    15:55--16:00 -- and one stamped 16:00 covers 16:00--16:05, which is
    post-market. A closed interval would append one post-market bar to every
    session in the window.
    """
    inside = [SEP_10_REGULAR_OPEN, SEP_10_LAST_REGULAR_BAR]
    outside = [
        SEP_10_REGULAR_OPEN - timedelta(minutes=5),  # 09:25 ET
        SEP_10_LAST_REGULAR_BAR + timedelta(minutes=5),  # 16:00 ET
    ]

    quote = intraday_quote(
        make_market_client,
        bars_at(sorted(inside + outside)),
        now=AFTER_THE_CLOSE,
    )

    # Equality, not membership: the kept stamps are the vendor's own, in the
    # vendor's order. Filtering moves no point and renumbers nothing.
    assert stamps_of(quote) == inside


def test_a_half_day_is_measured_from_the_calendar_not_a_hardcoded_four(
    make_market_client: MarketClient,
) -> None:
    """13:00 ET on the Friday after Thanksgiving: 12:55 kept, 13:00 dropped.

    The case that makes this belong in ``calendars.py``. A hardcoded
    09:30--16:00 filter passes every other test in this section and appends
    three hours of post-market prints here, on about ten days a year that
    cluster in the busiest weekly-expiry season there is.
    """
    opening = datetime(2025, 11, 28, 14, 30, tzinfo=timezone.utc)  # 09:30 ET
    last_regular = datetime(2025, 11, 28, 17, 55, tzinfo=timezone.utc)  # 12:55 ET
    first_post = datetime(2025, 11, 28, 18, 0, tzinfo=timezone.utc)  # 13:00 ET

    quote = intraday_quote(
        make_market_client,
        bars_at([opening, last_regular, first_post]),
        now=HALF_DAY_AFTER_THE_CLOSE,
    )

    assert stamps_of(quote) == [opening, last_regular]


def test_a_multi_session_window_is_sessions_times_bars_per_session(
    make_market_client: MarketClient,
) -> None:
    """Two extended sessions in, 156 regular points out, the gap intact.

    Per-bar bounds, looked up for **that bar's own session date** -- not the
    request's. Resolving one day's bounds and applying them across the window
    would be right on a window of one session and quietly wrong on every
    other.
    """
    day_before = SEP_10_EXTENDED_OPEN - timedelta(days=1)
    quote = intraday_quote(
        make_market_client,
        bars_at(
            five_minutes_from(day_before, 192)
            + five_minutes_from(SEP_10_EXTENDED_OPEN, 192)
        ),
        now=AFTER_THE_CLOSE,
        period="2D",
    )

    stamps = stamps_of(quote)
    assert len(stamps) == 2 * 78
    eastern = [stamp.astimezone(markets_routes.EASTERN) for stamp in stamps]
    assert {stamp.date() for stamp in eastern} == {
        date(2026, 9, 9),
        date(2026, 9, 10),
    }
    assert all(
        (stamp.hour, stamp.minute) >= (9, 30) and stamp.hour < 16
        for stamp in eastern
    )


def test_a_bar_on_a_day_with_no_session_is_dropped_not_bucketed(
    make_market_client: MarketClient,
) -> None:
    """A Saturday stamp has no session bounds, so it is not a point.

    Consistent with ``_sessions_in``, which does not count a day the calendar
    has nothing to say about. The alternative -- keeping it, or filing it
    under a neighbouring session -- would draw a point on a day the market
    never opened.
    """
    saturday = datetime(2026, 9, 5, 17, 0, tzinfo=timezone.utc)

    quote = intraday_quote(
        make_market_client,
        bars_at([saturday, SEP_10_REGULAR_OPEN]),
        now=AFTER_THE_CLOSE,
        period="1W",
    )

    assert stamps_of(quote) == [SEP_10_REGULAR_OPEN]


def test_the_live_point_is_omitted_once_the_session_has_closed(
    make_market_client: MarketClient,
) -> None:
    """A chart of the last session must not grow a point at the wall clock.

    Bars stop fifteen minutes short of now and the quote closes that gap
    during the session. After the close the same append would draw a
    post-market tick on a chart that has just been scoped to regular hours --
    and stamping it at 16:00 instead would be worse, since the price is a
    post-market print and that would report it as the close.
    """
    quote = intraday_quote(
        make_market_client,
        bars_at(five_minutes_from(SEP_10_REGULAR_OPEN, 78)),
        now=AFTER_THE_CLOSE,
    )

    assert stamps_of(quote)[-1] == SEP_10_LAST_REGULAR_BAR
    # The price is still served; it is the *series* that stops at the close.
    assert quote["price"]


def test_the_live_point_is_still_appended_while_the_session_is_open(
    make_market_client: MarketClient,
) -> None:
    """The clamp is a bound, not a removal -- the boundary case that permits.

    15:10 ET is inside the session, so the fifteen-minute gap at the
    right-hand edge is still closed by the quote.
    """
    quote = intraday_quote(
        make_market_client,
        bars_at(five_minutes_from(SEP_10_REGULAR_OPEN, 12)),
        now=MARKET_DATA_RECORDED_AT,
    )

    stamps = stamps_of(quote)
    assert stamps[-1] == MARKET_DATA_RECORDED_AT
    assert quote["intraday"][-1]["value"] == quote["price"]


# --------------------------------------------------------------------------
# 1H: the one grid that cannot begin a bar at the open
# --------------------------------------------------------------------------
#
# Alpaca's hourly bars are aligned to the *Eastern hour*, so the bar covering
# 09:30--10:00 is stamped 09:00 -- before the open -- and the regular-session
# filter drops it along with the half hour of pre-market it also carries. An
# hourly series therefore begins at 10:00 ET and runs six points on a full
# session, three on a half-day. There is no exact alternative: the bar
# straddles the boundary, so keeping it imports pre-market into a figure
# labelled "the move over the window" and dropping it loses the opening half
# hour.
#
# `stock_bars_hourly.json` is a *recorded* response and not a synthetic one,
# because everything here rests on the vendor's alignment and a fixture
# stamped by hand would only pin the assumption being tested.


def hourly_series(make_market_client: MarketClient) -> list[datetime]:
    """The served hourly series over the two sessions the recording covers.

    Asked at 14:00 ET on the half-day, which is after its 13:00 close, so no
    live point is appended and the series is bars alone.
    """
    quote = intraday_quote(
        make_market_client,
        single("stock_bars_hourly"),
        now=HALF_DAY_AFTER_THE_CLOSE,
        period="1W",
        timeframe="1H",
    )
    return [stamp.astimezone(markets_routes.EASTERN) for stamp in stamps_of(quote)]


def test_the_recorded_hourly_bars_are_stamped_on_the_eastern_hour() -> None:
    """The vendor fact the rest of this section rests on.

    Every stamp in the recording is exactly on the hour in New York, 04:00
    through 19:00, which is what puts 09:30 off the grid. Asserted against
    the file rather than described in a comment: if Alpaca ever re-aligns its
    hourly bars to the session, this is the test that should fail first.

    The 09:00 bar is **present** in the recording, so what the next test
    measures is the filter dropping it rather than the vendor omitting it.
    """
    stamps = [
        datetime.fromisoformat(bar["t"].replace("Z", "+00:00")).astimezone(
            markets_routes.EASTERN
        )
        for bar in fixture("stock_bars_hourly")["bars"]["NVDA"]
    ]

    assert stamps, "the recording is empty"
    assert all((stamp.minute, stamp.second) == (0, 0) for stamp in stamps)
    assert [
        stamp.strftime("%H:%M")
        for stamp in stamps
        if stamp.date() == date(2025, 11, 26)
    ] == [f"{hour:02d}:00" for hour in range(4, 20)]


def test_an_hourly_series_begins_at_ten_because_the_open_bar_holds_pre_market(
    make_market_client: MarketClient,
) -> None:
    """Six points on a full session and three on a half-day, from 10:00 ET.

    The 6-vs-7 question, answered here rather than only in a docstring.
    ``_bars_per_session("1H")`` estimates seven a session and the response
    carries six; the estimate keeps the seventh on purpose, because it feeds
    a ceiling and over-counting is the safe side of one.

    ``1H`` is still served when it is asked for explicitly -- this request
    is a 200 -- it is only no longer *suggested*. See
    ``test_the_suggestion_skips_a_timeframe_that_fits_but_misses_the_open``.

    The recording also opens with a 19:00 ET bar from the evening of the
    25th, because a date-only ``start`` is UTC midnight. It is post-market on
    a session outside the window and is dropped like any other.
    """
    served = hourly_series(make_market_client)

    assert [(stamp.date(), stamp.strftime("%H:%M")) for stamp in served] == [
        (date(2025, 11, 26), "10:00"),
        (date(2025, 11, 26), "11:00"),
        (date(2025, 11, 26), "12:00"),
        (date(2025, 11, 26), "13:00"),
        (date(2025, 11, 26), "14:00"),
        (date(2025, 11, 26), "15:00"),
        # 13:00 close: the 13:00 bar is the first post-market one.
        (date(2025, 11, 28), "10:00"),
        (date(2025, 11, 28), "11:00"),
        (date(2025, 11, 28), "12:00"),
    ]


def test_the_suggestion_skips_a_timeframe_that_fits_but_misses_the_open(
    make_market_client: MarketClient,
) -> None:
    """``1H`` fits a year comfortably and is still not offered.

    Both halves matter. If the first assertion failed -- if ``1H`` were
    merely too large here -- the second would pass for the wrong reason and
    go on passing after the rule that skips it had been deleted.
    """
    sessions = 251

    assert markets_routes._fits(
        markets_routes._series_size(sessions, "1H", symbol_count=1)
    )
    assert (
        markets_routes._finest_timeframe_to_suggest(sessions, symbol_count=1) == "1D"
    )


def test_every_published_session_opens_at_the_same_offset_past_the_hour() -> None:
    """``_SESSION_OPEN_PAST_THE_HOUR`` is read off the calendar, not assumed.

    Two years of dates, half-days and holidays included -- an early close is
    the only thing a half-day changes, and this is what says so. The constant
    decides which grids can represent the open, so a calendar that ever
    disagreed with it would silently change which timeframe a refusal names.
    """
    day, sessions = date(2025, 1, 1), 0
    while day <= date(2026, 12, 31):
        opens = nyse_session_open(day)
        if opens is not None:
            eastern = opens.astimezone(markets_routes.EASTERN)
            assert (
                eastern.minute,
                eastern.second,
            ) == (markets_routes._SESSION_OPEN_PAST_THE_HOUR, 0), day
            sessions += 1
        day += timedelta(days=1)

    assert sessions > 450, sessions
