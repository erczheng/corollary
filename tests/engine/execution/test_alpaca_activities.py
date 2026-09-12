"""``AlpacaBroker.activities()`` -- two object shapes behind one endpoint.

The ``200`` response is ``oneOf [TradingActivities, NonTradeActivities]``,
discriminated on ``activity_type``, and ingestion needs **two branches rather
than one**. The recording holds both: 15 ``FILL`` rows and 20 non-trade rows
(one ``JNLC`` funding journal and 19 ``FEE`` rows).

The non-trade branch is modelled permissively on purpose, because the
published schema is demonstrably incomplete. Three fields appear in live
responses and are **not** in ``NonTradeActivities``:

* ``description`` -- on every row of this recording.
* ``price`` -- on Alpaca's own ``OPTRD`` examples.
* ``execution_id`` -- found on 15 of the 19 ``FEE`` rows here, and it is the
  only per-fill linkage a fee gets. The published schema offers ``group_id``
  for sibling linkage, and **no row in this recording carries one.**

So an unknown field is *kept*, not dropped: ``extra`` is what stops the fourth
undocumented field being discovered by someone wondering where the fees went.
"""

import re
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from corollary.engine.execution.interface import (
    ActivityCategory,
    FillSide,
    NonTradeActivity,
    TradeActivity,
)

from .conftest import load_fixture, sequence, single

#: ``<17-digit stamp>::<uuid>``. A composite id, not a bare UUID, and it must
#: not be typed as one. The whole id is a sound unique key on the ``fill``
#: table and a sound resume cursor for *fetching* -- Alpaca's ``page_token``
#: is the last row's id -- but it is **not** a chronological sort key. Only
#: the stamp half orders; the two tests below measure that, and
#: ``_ActivityBase.stamp`` is the one authority for it.
COMPOSITE_ID = re.compile(r"^\d{17}::[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


@pytest.mark.asyncio
async def test_both_shapes_come_back_from_one_response(make_broker) -> None:
    broker, _ = make_broker(single("activities_mixed"))
    activities = await broker.activities()

    trades = [row for row in activities if isinstance(row, TradeActivity)]
    others = [row for row in activities if isinstance(row, NonTradeActivity)]

    assert len(activities) == len(load_fixture("activities_mixed")["body"]) == 35
    assert len(trades) == 15
    assert len(others) == 20


@pytest.mark.asyncio
async def test_the_discriminator_is_activity_type(make_broker) -> None:
    broker, _ = make_broker(single("activities_mixed"))
    for row in await broker.activities():
        if row.activity_type == "FILL":
            assert isinstance(row, TradeActivity)
        else:
            assert isinstance(row, NonTradeActivity), row.activity_type


@pytest.mark.asyncio
async def test_a_fill_row_carries_order_id_unsigned_qty_and_a_side(
    make_broker,
) -> None:
    broker, _ = make_broker(single("activities_fill"))
    fills = await broker.activities()

    for fill in fills:
        assert isinstance(fill, TradeActivity)
        assert fill.order_id
        assert fill.quantity > 0, "FILL rows carry an unsigned qty"
        assert fill.side is not None
        assert isinstance(fill.price, Decimal)
        assert fill.fill_type in ("fill", "partial_fill")


@pytest.mark.asyncio
async def test_a_fill_price_is_exact(make_broker) -> None:
    """The IWM round trip: bought 8.21, sold 8.14. A -$7.00 realized loss."""
    broker, _ = make_broker(single("activities_fill"))
    iwm = [
        fill
        for fill in await broker.activities()
        if fill.symbol == "IWM261218P00280000"
    ]
    assert [fill.price for fill in iwm] == [Decimal("8.21"), Decimal("8.14")]
    assert (Decimal("8.14") - Decimal("8.21")) * 1 * 100 == Decimal("-7.00")


@pytest.mark.asyncio
async def test_the_activity_id_is_composite_and_a_string(make_broker) -> None:
    broker, _ = make_broker(single("activities_mixed"))
    for row in await broker.activities():
        assert isinstance(row.id, str)
        assert COMPOSITE_ID.match(row.id), row.id
        assert row.stamp == row.id.split("::")[0]


@pytest.mark.asyncio
async def test_chronological_order_holds_only_to_the_millisecond(
    make_broker,
) -> None:
    """A correction to the spec, found by sorting the real recording.

    The design says the composite id *"sorts chronologically as a string,
    which is convenient for the ``fill(activity_id UNIQUE)`` upsert and for
    resuming ingestion."* Measured, that is true of the **17-digit stamp** and
    false of two things it is easy to assume follow from it:

    * The **whole id** does not sort. Three stamps repeat here, and inside a
      repeated stamp the UUID half orders arbitrarily: ``…217::a9d5…`` is
      returned first and sorts second.
    * **``transaction_time`` does not sort either**, at microsecond
      resolution, even with ``direction=asc``. The two legs of the TSLA
      vertical came back ``…438268`` then ``…438263``. Alpaca is ordering by
      the millisecond stamp, and the sub-millisecond tail is not a tiebreak.

    Neither breaks anything here: resuming uses Alpaca's own cursor, not a
    local sort. What it constrains is step 5. **Within one millisecond there
    is no field that orders fills**, so a FIFO matcher must not depend on one
    — and it does not have to, because the queue is per contract symbol and no
    symbol in this recording has two fills inside a millisecond. That is the
    invariant to assert on real data rather than the global sort.
    """
    broker, _ = make_broker(single("activities_fill"))
    fills = await broker.activities()

    stamps = [fill.stamp for fill in fills]
    assert stamps == sorted(stamps), "the stamp half does sort"

    ids = [fill.id for fill in fills]
    assert ids != sorted(ids), (
        "the whole id does NOT sort chronologically -- if this ever starts "
        "passing, re-read the docstring before relying on it"
    )

    times = [fill.transaction_time for fill in fills]
    assert times != sorted(times), (
        "nor does transaction_time, at microsecond resolution"
    )

    # What does hold, and what the matcher actually needs.
    per_symbol: dict[str, list] = {}
    for fill in fills:
        per_symbol.setdefault(fill.symbol, []).append(fill.transaction_time)
    for symbol, moments in per_symbol.items():
        assert moments == sorted(moments), symbol
        assert len({moment.replace(microsecond=0) for moment in moments}) == len(
            moments
        ) or len(moments) == 1, f"{symbol} has two fills in one second"


@pytest.mark.asyncio
async def test_the_first_id_order_divergence_is_two_legs_of_one_vertical(
    make_broker,
) -> None:
    """The concrete harm behind *"the id does not sort"*, pinned on real data.

    :attr:`~corollary.engine.execution.interface._ActivityBase.stamp` is the
    one authority for how the composite id orders, and it says the first
    divergence is **two legs of one vertical**. That sentence is what makes
    the consequence legible: a global ``ORDER BY activity_id`` does not merely
    shuffle unrelated rows, it reverses the two legs of one spread. Fed to a
    FIFO matcher that is a wrong lot order and therefore a wrong
    ``open_price`` on the realized trade.

    Asserted here so the authority cannot rot against a re-recording. It does
    **not** catch a future ``order_by(Fill.activity_id)`` -- nothing in a test
    can see code that has not been written -- it only keeps the reason for
    refusing one true.
    """
    broker, _ = make_broker(single("activities_fill"))
    fills = await broker.activities()

    as_returned = [fill.id for fill in fills]
    # Python's string sort and SQLite's default BINARY collation on a TEXT
    # column are the same comparison, so this is the order a bare
    # `ORDER BY activity_id` would hand back.
    lexicographic = sorted(as_returned)
    first = next(
        index
        for index, (returned, ordered) in enumerate(zip(as_returned, lexicographic))
        if returned != ordered
    )
    assert first == 0, "the very first pair of rows is already inverted"

    symbol_of = {fill.id: fill.symbol for fill in fills}
    side_of = {fill.id: fill.side for fill in fills}
    pair = {as_returned[first], lexicographic[first]}
    symbols = {symbol_of[activity_id] for activity_id in pair}

    assert symbols == {"SPY261130P00721000", "SPY261130P00706000"}
    # OCC is underlying + YYMMDD + right + 8-digit strike: one shared root
    # with two strikes is a vertical, and the opposing sides make it a spread
    # rather than two unrelated opens.
    assert len({symbol[:-8] for symbol in symbols}) == 1, "one root, one expiry"
    assert len({symbol[-8:] for symbol in symbols}) == 2, "two strikes"
    assert {side_of[activity_id] for activity_id in pair} == {
        FillSide.BUY,
        FillSide.SELL_SHORT,
    }

    # And they really are inverted rather than merely adjacent: the row the
    # broker returned first is the one lexicographic order puts second.
    assert as_returned[0] == lexicographic[1]


@pytest.mark.asyncio
async def test_two_fills_in_the_same_millisecond_keep_distinct_ids(
    make_broker,
) -> None:
    """Uniqueness rests on the UUID half, not the stamp.

    Two legs of one spread fill inside the same millisecond, so the 17-digit
    stamp repeats. ``fill(activity_id UNIQUE)`` still holds because the whole
    composite is the key -- but a schema that keyed on the stamp alone would
    drop one leg of every spread.
    """
    broker, _ = make_broker(single("activities_fill"))
    fills = await broker.activities()
    stamps = [fill.stamp for fill in fills]
    ids = [fill.id for fill in fills]

    assert len(set(stamps)) < len(stamps), "the recording holds a repeated stamp"
    assert len(set(ids)) == len(ids)


@pytest.mark.asyncio
async def test_a_journal_row_has_no_symbol_qty_price_or_side(make_broker) -> None:
    """The live ``JNLC``, which is what showed the schema split is real."""
    broker, _ = make_broker(single("activities_non_trade"))
    journals = [
        row for row in await broker.activities() if row.activity_type == "JNLC"
    ]
    assert len(journals) == 1
    journal = journals[0]

    assert journal.symbol is None
    assert journal.quantity is None
    assert journal.price is None
    assert journal.net_amount == Decimal("100000")
    assert journal.status == "executed"
    assert journal.currency == "USD"
    assert not hasattr(journal, "side"), "a non-trade row has no side at all"


@pytest.mark.asyncio
async def test_a_non_trade_row_has_no_order_id(make_broker) -> None:
    """``group_id`` is the only linkage the schema offers -- and it is absent.

    So a fee's only route back to what caused it, on this account, is
    ``execution_id``: an undocumented field. That is worth knowing before
    step 5 tries to attribute fees through ``group_id`` and finds nulls.
    """
    broker, _ = make_broker(single("activities_non_trade"))
    rows = await broker.activities()

    assert all(not hasattr(row, "order_id") for row in rows)
    assert all(row.group_id is None for row in rows), (
        "no row in this recording carries a group_id"
    )
    with_execution = [row for row in rows if row.execution_id is not None]
    assert len(with_execution) == 15


@pytest.mark.asyncio
async def test_the_undocumented_description_survives(make_broker) -> None:
    broker, _ = make_broker(single("activities_non_trade"))
    fees = [row for row in await broker.activities() if row.activity_type == "FEE"]
    assert fees
    assert any("OCC Clearing Fee" == row.description for row in fees)
    assert all(row.activity_sub_type for row in fees)


@pytest.mark.asyncio
async def test_a_fee_is_a_negative_net_amount(make_broker) -> None:
    broker, _ = make_broker(single("activities_non_trade"))
    fees = [row for row in await broker.activities() if row.activity_type == "FEE"]
    assert all(row.net_amount is not None and row.net_amount < 0 for row in fees)
    assert sum(row.net_amount for row in fees) == Decimal("-0.92")


@pytest.mark.asyncio
async def test_an_unknown_field_is_carried_rather_than_dropped(make_broker) -> None:
    """Three undocumented fields have already turned up. Expect a fourth.

    The published ``NonTradeActivities`` schema is incomplete as a matter of
    observed fact, so a field this model does not name is kept in ``extra``
    instead of being discarded at the vendor boundary.
    """
    broker, _ = make_broker(lambda _request: (200, UNKNOWN_FIELD_ROW))
    row = (await broker.activities())[0]
    assert isinstance(row, NonTradeActivity)
    assert row.extra["some_new_alpaca_field"] == "surprise"
    assert row.net_amount == Decimal("-1.25")


@pytest.mark.asyncio
async def test_a_signed_non_trade_quantity_survives(make_broker) -> None:
    """Two conventions in one ingest path, and they must not be flattened.

    ``FILL`` rows carry an unsigned ``qty`` plus a ``side``; non-trade rows
    carry a **signed** ``qty`` and no side at all. ``-2`` means two contracts
    left a long. Normalising happens in the matcher (step 5); losing the sign
    here would make that impossible.
    """
    broker, _ = make_broker(lambda _request: (200, SIGNED_QTY_ROW))
    row = (await broker.activities())[0]
    assert row.quantity == Decimal(-2)
    assert row.net_amount == Decimal(0)
    assert row.symbol == "AAPL230721C00150000"


@pytest.mark.asyncio
async def test_page_size_is_the_documented_maximum(make_broker) -> None:
    """``page_size`` maxes at 100, so ingestion has to paginate."""
    broker, transport = make_broker(single("activities_fill"))
    await broker.activities()
    assert transport.params_for("/v2/account/activities")["page_size"] == "100"


@pytest.mark.asyncio
async def test_pagination_follows_the_last_row_id(make_broker) -> None:
    """There is no ``next_page_token`` here. The cursor *is* the row id.

    Alpaca: *"page_token represents the ID of the last item on your current
    page of results."* So the loop resends the last id it saw and stops on an
    empty array -- there is nothing else to stop on.
    """
    broker, transport = make_broker(
        sequence("activities_fill_page1", "activities_fill_page2", "activities_end")
    )
    rows = await broker.activities(page_size=2)

    assert len(rows) == 4
    calls = transport.all_params_for("/v2/account/activities")
    assert len(calls) == 3
    assert "page_token" not in calls[0]
    assert calls[1]["page_token"] == load_fixture("activities_fill_page1")["body"][-1][
        "id"
    ]
    assert calls[2]["page_token"] == load_fixture("activities_fill_page2")["body"][-1][
        "id"
    ]


@pytest.mark.asyncio
async def test_a_short_page_stops_the_loop(make_broker) -> None:
    """15 rows against a page size of 100: one request, not two."""
    broker, transport = make_broker(single("activities_fill"))
    rows = await broker.activities()
    assert len(rows) == 15
    assert len(transport.all_params_for("/v2/account/activities")) == 1


@pytest.mark.asyncio
async def test_a_resume_cursor_is_sent_as_the_page_token(make_broker) -> None:
    """Incremental ingest: resume from the last id already in ``fill``."""
    cursor = "20260910131125598::b9802aa8-d779-518b-a929-0569ba1f391d"
    broker, transport = make_broker(single("activities_end"))
    await broker.activities(since_id=cursor)
    assert transport.params_for("/v2/account/activities")["page_token"] == cursor


@pytest.mark.parametrize("size", [0, -1, 101, 500])
@pytest.mark.asyncio
async def test_a_page_size_outside_the_documented_range_is_refused(
    make_broker, size
) -> None:
    """101 would 400; 0 would loop forever asking for nothing."""
    broker, transport = make_broker(single("activities_fill"))
    with pytest.raises(ValueError):
        await broker.activities(page_size=size)
    assert transport.requests == []


@pytest.mark.asyncio
async def test_a_partial_fill_is_recognised(make_broker) -> None:
    """``type: "partial_fill"`` with ``leaves_qty`` above zero.

    Not in the recording -- every order on this account filled whole -- so
    this row is shaped from the published ``TradingActivities`` schema. The
    fields it exercises are the three a partial fill is made of: ``qty`` is
    *this* execution, ``cum_qty`` is the running total, and ``leaves_qty`` is
    what the order still owes. Summing ``qty`` and reading ``cum_qty`` as
    another execution would double the position.
    """
    broker, _ = make_broker(lambda _request: (200, PARTIAL_FILL_ROWS))
    first, second = await broker.activities()

    assert first.fill_type == "partial_fill"
    assert first.is_partial is True
    assert (first.quantity, first.cumulative_quantity, first.leaves_quantity) == (
        Decimal(1),
        Decimal(1),
        Decimal(2),
    )

    assert second.fill_type == "fill"
    assert second.is_partial is False
    assert (second.quantity, second.cumulative_quantity, second.leaves_quantity) == (
        Decimal(2),
        Decimal(3),
        Decimal(0),
    )
    assert first.order_id == second.order_id, "one order, two executions"


@pytest.mark.asyncio
async def test_types_and_category_together_are_refused(make_broker) -> None:
    """Alpaca: *"Cannot be used with activity_types parameter."*

    Refused here rather than sent and 400'd three layers away.
    """
    broker, transport = make_broker(single("activities_fill"))
    with pytest.raises(ValueError):
        await broker.activities(
            types=["FILL"], category=ActivityCategory.NON_TRADE
        )
    assert transport.requests == [], "nothing was sent"


@pytest.mark.asyncio
async def test_a_type_filter_is_sent_as_a_comma_list(make_broker) -> None:
    broker, transport = make_broker(single("activities_fill"))
    await broker.activities(types=["FILL", "OPEXP"])
    params = transport.params_for("/v2/account/activities")
    assert params["activity_types"] == "FILL,OPEXP"
    assert "category" not in params


@pytest.mark.asyncio
async def test_a_category_filter_is_sent_alone(make_broker) -> None:
    broker, transport = make_broker(single("activities_non_trade"))
    await broker.activities(category=ActivityCategory.NON_TRADE)
    params = transport.params_for("/v2/account/activities")
    assert params["category"] == "non_trade_activity"
    assert "activity_types" not in params


@pytest.mark.asyncio
async def test_an_after_timestamp_must_be_aware(make_broker) -> None:
    """Naive datetimes are refused at every boundary in this codebase.

    Market data is Eastern and the server clock is whatever the machine says;
    guessing between them is a silent multi-hour window error that returns
    plausible data.
    """
    broker, _ = make_broker(single("activities_fill"))
    with pytest.raises(ValueError):
        await broker.activities(after=datetime(2026, 9, 10, 12, 0, 0))


@pytest.mark.asyncio
async def test_an_aware_after_timestamp_is_sent_as_rfc3339(make_broker) -> None:
    broker, transport = make_broker(single("activities_end"))
    await broker.activities(
        after=datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
    )
    params = transport.params_for("/v2/account/activities")
    assert params["after"] == "2026-09-10T12:00:00Z"


#: Two executions of one three-lot order, shaped from the published
#: ``TradingActivities`` schema. Hand-authored and marked as such: nothing on
#: this account has ever partially filled.
PARTIAL_FILL_ROWS = """[
  {
    "activity_type": "FILL",
    "id": "20260910131125100::00000000-0000-4000-8000-000000000010",
    "order_id": "00000000-0000-4000-8000-0000000000a0",
    "order_status": "partially_filled",
    "symbol": "NVDA261016C00220000",
    "side": "buy",
    "qty": "1",
    "cum_qty": "1",
    "leaves_qty": "2",
    "price": "14.20",
    "transaction_time": "2026-09-10T17:11:25.100000Z",
    "type": "partial_fill"
  },
  {
    "activity_type": "FILL",
    "id": "20260910131125200::00000000-0000-4000-8000-000000000011",
    "order_id": "00000000-0000-4000-8000-0000000000a0",
    "order_status": "filled",
    "symbol": "NVDA261016C00220000",
    "side": "buy",
    "qty": "2",
    "cum_qty": "3",
    "leaves_qty": "0",
    "price": "14.25",
    "transaction_time": "2026-09-10T17:11:25.200000Z",
    "type": "fill"
  }
]"""

UNKNOWN_FIELD_ROW = """[
  {
    "activity_type": "FEE",
    "activity_sub_type": "ORF",
    "id": "20260910000000000::00000000-0000-4000-8000-000000000001",
    "date": "2026-09-10",
    "created_at": "2026-09-10T17:11:45.526893Z",
    "net_amount": "-1.25",
    "status": "executed",
    "currency": "USD",
    "some_new_alpaca_field": "surprise"
  }
]"""

#: Shaped from Alpaca's documented ``OPEXC`` example. Hand-authored on
#: purpose and marked as such: no real option event exists to record *yet*.
#: The three near-dated NVDA contracts expired 2026-09-11 and cover all three
#: branches, but as of 19:45 ET that day no ``OPEXP``, ``OPEXC`` or ``OPASN``
#: row had posted to ``/v2/account/activities`` -- expiry processing runs
#: overnight and the rows are expected 2026-09-14. Re-record this from the
#: live account then; the option-event path belongs to step 5 anyway, and
#: this row is here only to pin the *signed quantity* convention, which is a
#: fact about the non-trade schema rather than about option events.
SIGNED_QTY_ROW = """[
  {
    "activity_type": "OPEXC",
    "id": "20260910000000000::00000000-0000-4000-8000-000000000002",
    "date": "2026-09-10",
    "created_at": "2026-09-10T17:11:45.526893Z",
    "symbol": "AAPL230721C00150000",
    "qty": "-2",
    "net_amount": "0",
    "status": "executed"
  }
]"""
