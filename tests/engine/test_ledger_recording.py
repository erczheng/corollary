"""The ledger against the **real recording**. No authored rows in this file.

``tests/engine/test_ledger.py`` proves the arithmetic is self-consistent;
this proves it survives the shapes Alpaca actually sent. The bytes come from
``tests/fixtures/alpaca/``, captured from the live paper account, and they are
parsed by the real ``AlpacaBroker`` through an ``httpx.MockTransport`` rather
than by a second parser written for the test -- so the fields, the string
money, the composite ids and the two activity shapes are all the ones the
vendor produced.

What the recording contains: 15 ``FILL`` rows, one ``JNLC`` funding journal,
19 ``FEE`` rows, 11 orders with legs nested, and 13 position rows. One round
trip -- IWM 280P bought at 8.21, sold at 8.14 -- which is the only realized
trade that has ever happened on this account and the arithmetic ground truth
for the matcher.

What it does **not** contain is a single ``OPEXP``, ``OPEXC`` or ``OPASN``.
Those branches are authored, in ``test_ledger_option_events.py``, and that
file says so at the top.
"""

import dataclasses
import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from corollary.engine.execution.interface import (
    Activity,
    NonTradeActivity,
    Order,
    TradeActivity,
)
from corollary.engine.execution.alpaca import AlpacaBroker
from corollary.data.providers.alpaca import AlpacaCredentials
from corollary.engine.ledger import (
    FeeLink,
    Ledger,
    RejectionRule,
    build_ledger,
    intents_from_orders,
    summarise,
)
from corollary.instruments import parse_occ_symbol
from corollary.ratelimit import ALPACA_PAPER_TRADING_HOST, HostRateLimiter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alpaca"

PAPER = "paper"

#: Obviously fake, and pointed at the paper host. Rule 6: no key material in
#: tests, and rule 5: paper is the default everywhere.
CREDENTIALS = AlpacaCredentials(
    key_id="PKTESTTESTTESTTEST",
    secret_key="not-a-real-secret",
    trading_base_url=f"https://{ALPACA_PAPER_TRADING_HOST}",
    is_paper=True,
)


def fixture(name: str) -> dict:
    """One recorded response, decoded with ``parse_float=Decimal``.

    So a test comparing against a fixture compares two exact decimals rather
    than two views of one double.
    """
    text = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    decoded: dict = json.loads(text, parse_float=Decimal)
    return decoded


def body_bytes(name: str) -> bytes:
    """The fixture's ``body``, sliced out of the file rather than re-serialised.

    The broker parses these bytes itself, so nothing on the money path is ever
    round-tripped through a Python float on the way in.
    """
    text = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    start = text.index('"body":') + len('"body":')
    while text[start].isspace():
        start += 1
    _, end = json.JSONDecoder().raw_decode(text, start)
    return text[start:end].encode("utf-8")


async def _never_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError(f"a replay test waited {seconds}s on the rate limiter")


def _route(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if "/activities" in path:
        name = "activities_mixed"
    elif "/orders" in path:
        name = "orders_nested"
    else:  # pragma: no cover - an unrouted request is a test bug
        raise AssertionError(f"no fixture routed for {request.method} {request.url}")
    return httpx.Response(
        status_code=200,
        content=body_bytes(name),
        headers={"content-type": "application/json"},
        request=request,
    )


def _broker() -> tuple[AlpacaBroker, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_route))
    limiter = HostRateLimiter(
        requests_per_minute=10_000, clock=lambda: 0.0, sleep=_never_sleep
    )
    return AlpacaBroker(credentials=CREDENTIALS, client=client, limiter=limiter), client


async def recorded() -> tuple[list[Activity], list[Order]]:
    broker, client = _broker()
    try:
        return await broker.activities(), await broker.orders()
    finally:
        await client.aclose()


async def recorded_ledger() -> tuple[Ledger, list[Activity]]:
    activities, orders = await recorded()
    symbols = {
        row.symbol
        for row in activities
        if isinstance(row, TradeActivity) and row.symbol
    }
    return (
        build_ledger(
            activities,
            account=PAPER,
            intents=intents_from_orders(orders),
            # Every contract in this recording is a standard 100-share
            # deliverable -- asserted below rather than assumed, because that
            # assertion is the only thing licensing this constant. Real
            # multipliers come per contract from /v2/options/contracts.
            multipliers={symbol: Decimal(100) for symbol in symbols},
        ),
        activities,
    )


# --------------------------------------------------------------------------
# The one real round trip
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_one_real_round_trip_books_minus_seven_dollars() -> None:
    """IWM 280P, one contract, 8.21 in and 8.14 out, at a 100 multiplier.

    Parsed from the recorded bytes end to end: the broker's own parser, the
    fill-to-order join built from the recorded orders, the matcher's queue.
    """
    result, _ = await recorded_ledger()

    (trade,) = result.trades
    assert trade.symbol == "IWM261218P00280000"
    assert trade.qty == 1
    assert trade.open_price == Decimal("8.21")
    assert trade.close_price == Decimal("8.14")
    assert trade.pnl == Decimal("-7.00")
    assert trade.pnl_pct == Decimal("-0.8526")
    assert result.realized_pnl == Decimal("-7.00")


@pytest.mark.asyncio
async def test_none_of_the_recorded_contracts_is_an_adjusted_root() -> None:
    """What licenses the 100 multiplier above, stated rather than assumed.

    OCC marks an adjusted contract with a numeric suffix on the root
    (``AAPL1``), and the deliverable is then not 100 shares. The authoritative
    test is ``root_symbol != underlying_symbol`` from ``/v2/options/contracts``;
    the symbol alone answers it well enough to know this recording is clean.
    """
    activities, _ = await recorded()
    roots = {
        parse_occ_symbol(row.symbol).root
        for row in activities
        if isinstance(row, TradeActivity)
    }
    assert roots == {"SPY", "AAPL", "TSLA", "QQQ", "NVDA", "IWM", "AMD"}
    assert not any(root[-1].isdigit() for root in roots)


@pytest.mark.asyncio
async def test_the_fill_to_order_join_resolves_every_recorded_fill() -> None:
    """Every one of the 15 fills reaches an intent, including the mleg legs.

    A fill carries its *leg's* order id, so a join that indexed only top-level
    orders would resolve the seven single-leg fills and none of the eight leg
    fills -- and eight of nine ``buy`` rows would then be refused.
    """
    activities, orders = await recorded()
    intents = intents_from_orders(orders)
    fills = [row for row in activities if isinstance(row, TradeActivity)]

    assert len(fills) == 15
    assert len(intents) == 15
    assert all(row.order_id in intents for row in fills)


# --------------------------------------------------------------------------
# The open book, reconciled against the broker's own position rows
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_open_lots_reproduce_the_brokers_position_rows() -> None:
    """13 lots, and each one matches a ``/v2/positions`` row exactly.

    The strongest single check in this file: the matcher's leftover queue is
    an independent reconstruction of the book, and the broker's own list is the
    answer key. A signed quantity becomes an unsigned one plus ``is_short``
    here, which is the same normalisation the ``fill`` table applies.
    """
    result, _ = await recorded_ledger()
    rows = fixture("positions")["body"]

    assert len(result.open_lots) == len(rows) == 13
    expected = {
        row["symbol"]: (
            abs(int(Decimal(row["qty"]))),
            Decimal(row["avg_entry_price"]),
            row["side"] == "short",
        )
        for row in rows
    }
    actual = {
        lot.symbol: (lot.qty, lot.price, lot.is_short) for lot in result.open_lots
    }
    assert actual == expected


# --------------------------------------------------------------------------
# The reconciliation invariant
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_realized_plus_unrealized_plus_fees_equals_the_accounts_own_pnl() -> None:
    """Closes **to the cent** on the real recording, which is the point.

    The spec asks for ``realized + unrealized ~= account P&L`` *within fees*.
    On this account it is exact once the fees are included as a term rather
    than as a tolerance, and exactness is worth having: a tolerance the width
    of the fees would hide an error the width of the fees.

    Every term comes from a different source -- realized from the matcher,
    unrealized and equity from the broker, deposits from the one ``JNLC`` --
    so agreement is evidence rather than arithmetic restated.
    """
    result, activities = await recorded_ledger()

    deposits = sum(
        (
            row.net_amount or Decimal(0)
            for row in activities
            if isinstance(row, NonTradeActivity) and row.activity_type == "JNLC"
        ),
        Decimal(0),
    )
    account = fixture("account")["body"]
    equity = Decimal(account["equity"])
    unrealized = sum(
        (Decimal(row["unrealized_pl"]) for row in fixture("positions")["body"]),
        Decimal(0),
    )

    assert deposits == Decimal("100000")
    assert equity == Decimal("99728.08")
    assert unrealized == Decimal("-264")
    assert result.realized_pnl == Decimal("-7.00")
    assert result.total_fees == Decimal("-0.92")

    assert result.realized_pnl + unrealized + result.total_fees == equity - deposits
    assert equity - deposits == Decimal("-271.92")

    # Attribution labels a fee; it does not move money. `realized_trade` has no
    # fee column and `pnl` is the price difference, so the two halves of the
    # split have to add back to the whole -- otherwise an attributed fee would
    # be counted once in a position's line and again in the total.
    assert result.attributed_fees + result.unattributed_fees == result.total_fees

    # `total_fees` is a *term* in the equation above, so the equation closing
    # proves nothing unless the term is complete. Both halves of completeness
    # are asserted, because the equality alone hid an understatement: a fee
    # booked at zero left every figure here unchanged and `-0.92` was simply
    # the sum of the ones that survived.
    fee_rows = [
        row
        for row in activities
        if isinstance(row, NonTradeActivity) and row.activity_type == "FEE"
    ]
    assert len(fee_rows) == 19

    # One: the total is the sum of the *recorded* amounts, recomputed from the
    # raw rows rather than from the ledger's own records.
    assert all(row.net_amount is not None for row in fee_rows)
    assert result.total_fees == sum(
        (row.net_amount for row in fee_rows if row.net_amount is not None), Decimal(0)
    )

    # Two: every recorded row is either counted or refused with a reason, and
    # never both. A row absent from both sides is one that vanished.
    counted = {charge.activity_id for charge in result.fees}
    refused = {
        rejection.activity_id
        for rejection in result.rejections
        if rejection.rule is RejectionRule.MISSING_FEE_AMOUNT
    }
    assert counted | refused == {row.id for row in fee_rows}
    assert not counted & refused


@pytest.mark.asyncio
async def test_a_recorded_fee_stripped_of_its_amount_is_refused_and_reported() -> None:
    """The understatement the reconciliation used to absorb, on real bytes.

    All 19 recorded ``FEE`` rows carry a ``net_amount``, so the trigger is
    unobserved -- and ``net_amount`` is ``Decimal | None`` with
    ``wire.as_decimal("")`` answering ``None``, so a missing field and an empty
    string would both arrive as one. Booked at ``Decimal(0)`` the charge left
    :attr:`Ledger.total_fees` entirely and the reconciliation stopped closing
    with **nothing** in :attr:`Ledger.rejections` to say why.

    Here one recorded row is stripped and the rest is left exactly as Alpaca
    sent it. The total must move by that row's whole amount -- no number is
    substituted for the unknown -- and the move must come with a refusal
    naming the row, so the gap in the reconciliation has an explanation
    attached to it rather than looking like an arithmetic error.
    """
    activities, orders = await recorded()
    symbols = {
        row.symbol for row in activities if isinstance(row, TradeActivity) and row.symbol
    }
    stripped = next(
        row
        for row in activities
        if isinstance(row, NonTradeActivity)
        and row.activity_type == "FEE"
        and row.activity_sub_type == "OCC"
    )
    assert stripped.net_amount == Decimal("-0.03")

    result = build_ledger(
        [
            dataclasses.replace(row, net_amount=None) if row is stripped else row
            for row in activities
        ],
        account=PAPER,
        intents=intents_from_orders(orders),
        multipliers={symbol: Decimal(100) for symbol in symbols},
    )

    assert len(result.fees) == 18
    assert stripped.id not in {charge.activity_id for charge in result.fees}
    # Moved by the whole charge, not by zero and not by a guess.
    assert result.total_fees == Decimal("-0.92") - Decimal("-0.03")
    assert result.attributed_fees + result.unattributed_fees == result.total_fees

    refusals = [
        rejection
        for rejection in result.rejections
        if rejection.rule is RejectionRule.MISSING_FEE_AMOUNT
    ]
    assert len(refusals) == 1
    assert refusals[0].activity_id == stripped.id
    assert refusals[0].at is not None
    assert refusals[0].inputs["activity_sub_type"] == "OCC"


# --------------------------------------------------------------------------
# Fees on the real recording
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fifteen_of_nineteen_fees_attribute_and_the_other_four_are_reported() -> (
    None
):
    """The measured split, and both halves matter.

    ``group_id`` is null on all 20 non-trade rows and no fee carries an
    ``order_id``, so hops 1 and 3 find nothing here. Hop 2 finds everything it
    can: each of the 15 ``OCC`` clearing fees carries an ``execution_id`` that
    **is the uuid half of a fill's composite activity id**, one to one across
    15 distinct tails.

    The four that do not attribute are not a failure of the chain -- they are
    ``CAT``, ``REG``, ``ORF`` and ``TAF``, charged per batch rather than per
    execution ("CAT fee for proceed of 15 trades", "ORF fee for proceed of 15
    contracts"). They genuinely belong to no single fill, which is why the rule
    is *reported separately*, never dropped.

    Worth recording how this was nearly missed: the recorder pseudonymised
    ``id`` through ``uuid5`` and left ``execution_id`` raw, so the two sides
    could never match and the first measurement read "every fee is
    unattributed". That was an artifact of the scrubber, not a fact about
    Alpaca. Both halves are fixed now.
    """
    result, activities = await recorded_ledger()

    fee_rows = [
        row
        for row in activities
        if isinstance(row, NonTradeActivity) and row.activity_type == "FEE"
    ]
    assert len(fee_rows) == 19
    assert sum(1 for row in fee_rows if row.execution_id) == 15
    assert not any(row.group_id for row in fee_rows)

    attributed = [charge for charge in result.fees if charge.is_attributed]
    orphaned = [charge for charge in result.fees if not charge.is_attributed]

    assert len(result.fees) == 19
    assert len(attributed) == 15
    assert {charge.link for charge in attributed} == {FeeLink.EXECUTION_ID}
    assert {charge.activity_sub_type for charge in attributed} == {"OCC"}
    assert {charge.activity_sub_type for charge in orphaned} == {
        "CAT",
        "REG",
        "ORF",
        "TAF",
    }

    # Every attributed fee names the contract it belongs to, and every one of
    # them lands on a symbol that really traded.
    traded = {row.symbol for row in activities if isinstance(row, TradeActivity)}
    assert all(charge.symbol in traded for charge in attributed)
    assert all(charge.movement_activity_id is not None for charge in attributed)

    assert result.attributed_fees == Decimal("-0.45")
    assert result.unattributed_fees == Decimal("-0.47")
    assert result.total_fees == Decimal("-0.92")


@pytest.mark.asyncio
async def test_the_funding_journal_is_declined_with_a_reason() -> None:
    """A ``JNLC`` is money, but it is not a lot movement. Declined, not ignored."""
    result, _ = await recorded_ledger()
    journals = [
        rejection
        for rejection in result.rejections
        if rejection.inputs.get("activity_type") == "JNLC"
    ]
    assert len(journals) == 1
    assert journals[0].rule.value == "not_a_ledger_activity"
    assert journals[0].inputs["net_amount"] == "100000"


# --------------------------------------------------------------------------
# The ordering invariant, measured rather than assumed
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_symbol_the_recorded_fills_are_ordered_and_a_second_apart() -> None:
    """The invariant the matcher is built on, asserted on the data it came from.

    No global order exists: stamps repeat, the UUID half then breaks ties
    arbitrarily, and ``transaction_time`` does not order rows at microsecond
    resolution either (``...438268`` came back before ``...438263``). Per
    contract symbol it does hold, and no symbol has two fills inside a second.
    """
    activities, _ = await recorded()
    by_symbol: dict[str, list[TradeActivity]] = {}
    for row in activities:
        if isinstance(row, TradeActivity):
            by_symbol.setdefault(row.symbol, []).append(row)

    for symbol, rows in by_symbol.items():
        stamps = [row.transaction_time for row in rows]
        assert stamps == sorted(stamps), symbol
        gaps = [
            (later - earlier).total_seconds()
            for earlier, later in zip(stamps, stamps[1:])
        ]
        assert all(gap >= 1 for gap in gaps), symbol


@pytest.mark.asyncio
async def test_the_ledger_is_unchanged_by_the_order_the_activities_arrive_in() -> None:
    """Sorted by id, sorted by time, reversed -- one answer.

    A matcher that leaned on a global sort would be at the mercy of which of
    those the caller happened to hand it.
    """
    activities, orders = await recorded()
    intents = intents_from_orders(orders)
    symbols = {
        row.symbol for row in activities if isinstance(row, TradeActivity) and row.symbol
    }
    multipliers = {symbol: Decimal(100) for symbol in symbols}

    def run(rows: Sequence[Activity]) -> Ledger:
        return build_ledger(
            list(rows), account=PAPER, intents=intents, multipliers=multipliers
        )

    by_id = run(sorted(activities, key=lambda row: row.id))
    reversed_ = run(list(reversed(activities)))
    as_served = run(activities)

    assert by_id.trades == reversed_.trades == as_served.trades
    assert set(by_id.open_lots) == set(reversed_.open_lots) == set(as_served.open_lots)
    assert by_id.total_fees == reversed_.total_fees == as_served.total_fees


@pytest.mark.asyncio
async def test_the_folds_over_the_real_ledger_report_one_losing_trade() -> None:
    """One trade, and it lost -- so there is no average win to report.

    ``None`` rather than 0.00: a card reading +$0.00 average win would say
    something false about a book that has never won.
    """
    result, _ = await recorded_ledger()
    stats = summarise(result.trades)
    assert stats.trades == 1
    assert stats.wins == 0
    assert stats.losses == 1
    assert stats.win_rate == Decimal("0.00")
    assert stats.average_win is None
    assert stats.average_loss == Decimal("-7.00")
    assert stats.realized_pnl == Decimal("-7.00")
