"""The FIFO matcher's arithmetic, its refusals, and the folds over its output.

Every fill in this module is **authored**, and that is a statement about what
it proves: hand-written rows prove the matcher is self-consistent and prove
nothing at all about the shapes Alpaca sends. The shapes are pinned in
``test_ledger_recording.py``, which replays real recorded bytes, and the one
real round trip on the account -- IWM 280P bought at 8.21, sold at 8.14, a
-$7.00 loss -- is reproduced in both places on purpose.
"""

import dataclasses
from decimal import Decimal
from typing import Any

import pytest

from corollary.db.models import CLOSE_KINDS, RealizedTrade
from corollary.engine.execution.interface import (
    FillSide,
    Order,
    OrderClass,
    OrderSide,
    PositionIntent,
)
from corollary.engine.ledger import (
    CloseKind,
    FeeLink,
    Ledger,
    LedgerRejection,
    LotMovement,
    RealizedTradeRecord,
    RejectionRule,
    as_row_kwargs,
    biggest_loser,
    build_ledger,
    intents_from_orders,
    match_movements,
    realized_pnl,
    summarise,
)

from corollary.wire import REDACTED

from .ledger_support import at, fee, fill, option_event

PAPER = "paper"

IWM = "IWM261218P00280000"
SPY = "SPY261130P00721000"

#: A deliberately non-standard deliverable. After a split or special dividend
#: OCC issues a modified root (``AAPL1``) and the contract no longer delivers
#: 100 shares -- so a matcher that assumes 100 computes max loss wrong on
#: exactly these, which is the failure CLAUDE.md rule 4 exists to prevent.
ADJUSTED = "AAPL1261218C00150000"


def multipliers(*symbols: str, value: str = "100") -> dict[str, Decimal]:
    return {symbol: Decimal(value) for symbol in symbols}


def ledger(*activities: Any, **kwargs: Any) -> Ledger:
    """``build_ledger`` with the boring arguments defaulted."""
    symbols = {
        getattr(row, "symbol", None) or "" for row in activities
    }
    kwargs.setdefault("multipliers", multipliers(*sorted(s for s in symbols if s)))
    kwargs.setdefault("intents", {})
    return build_ledger(list(activities), account=PAPER, **kwargs)


def rules(rejections: tuple[LedgerRejection, ...]) -> list[str]:
    return [rejection.rule.value for rejection in rejections]


# --------------------------------------------------------------------------
# Round trips, both directions
# --------------------------------------------------------------------------


def test_a_long_round_trip_reproduces_the_one_real_trade_exactly() -> None:
    """IWM 280P, one contract, bought 8.21 and sold 8.14 -- a -$7.00 loss.

    The arithmetic ground truth for this whole module: it is the only round
    trip that has actually happened on the account.
    """
    result = ledger(
        fill(IWM, FillSide.BUY, 1, "8.21", when=at(17, 11, 25), order_id="open"),
        fill(IWM, FillSide.SELL, 1, "8.14", when=at(17, 12, 42), order_id="close"),
        intents={"open": PositionIntent.BUY_TO_OPEN},
    )

    (trade,) = result.trades
    assert trade.pnl == Decimal("-7.00")
    assert isinstance(trade.pnl, Decimal)
    assert trade.open_price == Decimal("8.21")
    assert trade.close_price == Decimal("8.14")
    assert trade.qty == 1
    assert trade.close_kind is CloseKind.FILL
    assert trade.account == PAPER
    assert trade.opened_at == at(17, 11, 25)
    assert trade.closed_at == at(17, 12, 42)
    assert result.open_lots == ()
    assert result.rejections == ()


def test_a_long_round_trip_that_wins() -> None:
    result = ledger(
        fill(IWM, FillSide.BUY, 2, "1.00", when=at(14), order_id="open"),
        fill(IWM, FillSide.SELL, 2, "1.75", when=at(15), order_id="close"),
        intents={"open": PositionIntent.BUY_TO_OPEN},
    )
    (trade,) = result.trades
    assert trade.pnl == Decimal("150.00")


def test_a_short_round_trip_inverts_the_sign() -> None:
    """Sold at 2.00, bought back at 1.50 -- a short *gains* when price falls."""
    result = ledger(
        fill(SPY, FillSide.SELL_SHORT, 1, "2.00", when=at(14), order_id="open"),
        fill(SPY, FillSide.BUY, 1, "1.50", when=at(15), order_id="close"),
        intents={"close": PositionIntent.BUY_TO_CLOSE},
    )
    (trade,) = result.trades
    assert trade.pnl == Decimal("50.00")


def test_a_short_round_trip_that_loses() -> None:
    result = ledger(
        fill(SPY, FillSide.SELL_SHORT, 1, "1.50", when=at(14), order_id="open"),
        fill(SPY, FillSide.BUY, 1, "2.00", when=at(15), order_id="close"),
        intents={"close": PositionIntent.BUY_TO_CLOSE},
    )
    (trade,) = result.trades
    assert trade.pnl == Decimal("-50.00")


# --------------------------------------------------------------------------
# Partial closes and FIFO
# --------------------------------------------------------------------------


def test_a_partial_close_books_the_slice_and_leaves_the_rest_open() -> None:
    result = ledger(
        fill(IWM, FillSide.BUY, 3, "2.00", when=at(14), order_id="open"),
        fill(IWM, FillSide.SELL, 1, "2.50", when=at(15), order_id="close"),
        intents={"open": PositionIntent.BUY_TO_OPEN},
    )
    (trade,) = result.trades
    assert trade.qty == 1
    assert trade.pnl == Decimal("50.00")
    (lot,) = result.open_lots
    assert (lot.symbol, lot.qty, lot.price) == (IWM, 2, Decimal("2.00"))


def test_fifo_matches_across_two_lots_at_different_prices() -> None:
    """One close spanning two lots emits **two** trades, not one averaged.

    Averaging the two bases loses the only thing the rows are for, and FIFO
    is the rule: the 2.00 lot goes first even though it is the winner here.
    """
    result = ledger(
        fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="open-a"),
        fill(IWM, FillSide.BUY, 1, "3.00", when=at(15), order_id="open-b"),
        fill(IWM, FillSide.SELL, 2, "2.50", when=at(16), order_id="close"),
        intents={
            "open-a": PositionIntent.BUY_TO_OPEN,
            "open-b": PositionIntent.BUY_TO_OPEN,
        },
    )
    first, second = result.trades
    assert [trade.open_price for trade in result.trades] == [
        Decimal("2.00"),
        Decimal("3.00"),
    ]
    assert first.pnl == Decimal("50.00")
    assert second.pnl == Decimal("-50.00")
    assert first.opened_at == at(14)
    assert second.opened_at == at(15)
    assert first.closed_at == second.closed_at == at(16)
    assert result.open_lots == ()


def test_fifo_is_not_lifo_when_only_part_of_the_position_closes() -> None:
    """A test FIFO passes and LIFO fails.

    Two lots, one closed. FIFO books the 2.00 lot and leaves 3.00 open; LIFO
    books 3.00 and leaves 2.00. Both emit one trade, so only the *prices*
    separate them.
    """
    result = ledger(
        fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="open-a"),
        fill(IWM, FillSide.BUY, 1, "3.00", when=at(15), order_id="open-b"),
        fill(IWM, FillSide.SELL, 1, "2.50", when=at(16), order_id="close"),
        intents={
            "open-a": PositionIntent.BUY_TO_OPEN,
            "open-b": PositionIntent.BUY_TO_OPEN,
        },
    )
    (trade,) = result.trades
    assert trade.open_price == Decimal("2.00")
    assert trade.pnl == Decimal("50.00")
    (lot,) = result.open_lots
    assert lot.price == Decimal("3.00")


# --------------------------------------------------------------------------
# The multiplier is an input, never a constant
# --------------------------------------------------------------------------


def test_the_multiplier_is_read_per_contract_and_is_not_always_a_hundred() -> None:
    """An adjusted contract's deliverable is not 100 shares.

    ``/v2/positions`` returns **no multiplier field at all**, so there is no
    fallback to reach for even if one were wanted: it comes from the
    contracts endpoint, cached, and arrives here as an input.
    """
    result = build_ledger(
        [
            fill(ADJUSTED, FillSide.BUY, 1, "2.00", when=at(14), order_id="open"),
            fill(ADJUSTED, FillSide.SELL, 1, "3.00", when=at(15), order_id="close"),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers={ADJUSTED: Decimal(84)},
    )
    (trade,) = result.trades
    assert trade.pnl == Decimal("84.00")
    assert trade.pnl != Decimal("100.00")


def test_a_symbol_with_no_multiplier_is_refused_rather_than_defaulted() -> None:
    result = build_ledger(
        [
            fill(ADJUSTED, FillSide.BUY, 1, "2.00", when=at(14), order_id="open"),
            fill(ADJUSTED, FillSide.SELL, 1, "3.00", when=at(15), order_id="close"),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers={},
    )
    assert result.trades == ()
    assert rules(result.rejections) == [
        RejectionRule.UNKNOWN_MULTIPLIER.value,
        RejectionRule.UNKNOWN_MULTIPLIER.value,
    ]
    assert all(rejection.symbol == ADJUSTED for rejection in result.rejections)


# --------------------------------------------------------------------------
# pnl_pct
# --------------------------------------------------------------------------


def test_pnl_pct_denominates_on_the_cost_basis_of_a_long() -> None:
    result = ledger(
        fill(IWM, FillSide.BUY, 1, "8.21", when=at(14), order_id="open"),
        fill(IWM, FillSide.SELL, 1, "8.14", when=at(15), order_id="close"),
        intents={"open": PositionIntent.BUY_TO_OPEN},
    )
    (trade,) = result.trades
    # -7 / 821 = -0.85261...%, in percent units to match the frontend.
    assert trade.pnl_pct == Decimal("-0.8526")


def test_pnl_pct_denominates_on_the_credit_received_for_a_short() -> None:
    """A short's basis is the credit, not a debit it never paid."""
    result = ledger(
        fill(SPY, FillSide.SELL_SHORT, 1, "2.00", when=at(14), order_id="open"),
        fill(SPY, FillSide.BUY, 1, "1.50", when=at(15), order_id="close"),
        intents={"close": PositionIntent.BUY_TO_CLOSE},
    )
    (trade,) = result.trades
    assert trade.pnl == Decimal("50.00")
    # A gain on a short is a *positive* percentage of the credit received.
    assert trade.pnl_pct == Decimal("25.0000")


def test_pnl_pct_is_none_when_the_basis_is_zero() -> None:
    """A percentage of nothing is not zero percent -- 0% would read as flat."""
    result = ledger(
        fill(IWM, FillSide.BUY, 1, "0", when=at(14), order_id="open"),
        fill(IWM, FillSide.SELL, 1, "0.50", when=at(15), order_id="close"),
        intents={"open": PositionIntent.BUY_TO_OPEN},
    )
    (trade,) = result.trades
    assert trade.pnl == Decimal("50.00")
    assert trade.pnl_pct is None


# --------------------------------------------------------------------------
# Intent comes from the order, never from `side`
# --------------------------------------------------------------------------


def test_a_bare_buy_without_the_order_join_is_refused_not_guessed() -> None:
    """``buy`` is both BTO and BTC, so alone it decides nothing.

    A matcher that guessed ``BUY_TO_OPEN`` would book every buy-to-close as a
    fresh lot and double the position it already held.
    """
    result = ledger(fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="lonely"))
    assert result.trades == ()
    assert result.open_lots == ()
    assert rules(result.rejections) == [RejectionRule.UNKNOWN_INTENT.value]
    (rejection,) = result.rejections
    assert rejection.at == at(14)
    assert rejection.inputs["side"] == "buy"
    assert rejection.inputs["order_id"] == "lonely"


def test_sell_short_opens_a_short_without_the_join() -> None:
    """One of the two actions ``side`` really does decide on its own."""
    result = ledger(fill(SPY, FillSide.SELL_SHORT, 1, "2.00", when=at(14)))
    assert result.rejections == ()
    (lot,) = result.open_lots
    assert lot.is_short is True
    assert lot.price == Decimal("2.00")


def test_sell_closes_a_long_without_the_join() -> None:
    """And the other. Only ``buy`` needs the order."""
    result = ledger(
        fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="open"),
        fill(IWM, FillSide.SELL, 1, "2.50", when=at(15), order_id="unjoined"),
        intents={"open": PositionIntent.BUY_TO_OPEN},
    )
    (trade,) = result.trades
    assert trade.pnl == Decimal("50.00")
    assert result.rejections == ()


def test_an_intent_that_contradicts_the_side_is_refused() -> None:
    """A join that disagrees with the row is not a tie to break by preference."""
    result = ledger(
        fill(SPY, FillSide.SELL_SHORT, 1, "2.00", when=at(14), order_id="open"),
        intents={"open": PositionIntent.SELL_TO_CLOSE},
    )
    assert result.trades == ()
    assert rules(result.rejections) == [RejectionRule.INTENT_CONTRADICTS_SIDE.value]


def test_intents_from_orders_indexes_legs_because_a_fill_carries_the_leg_id() -> None:
    """A fill's ``order_id`` is the **leg's** id on an mleg order."""
    leg = Order(
        id="leg-1",
        symbol=SPY,
        asset_class="us_option",
        order_class=OrderClass.MLEG,
        side=OrderSide.SELL,
        position_intent=PositionIntent.SELL_TO_OPEN,
        order_type="limit",
        time_in_force="day",
        status="filled",
        quantity=Decimal(1),
        filled_quantity=Decimal(1),
        filled_avg_price=Decimal("9.41"),
        limit_price=None,
        stop_price=None,
        ratio_qty=Decimal(1),
        created_at=at(14),
        submitted_at=at(14),
        filled_at=at(14),
        canceled_at=None,
        expired_at=None,
        updated_at=at(14),
        extended_hours=False,
    )
    parent = Order(
        id="parent",
        symbol="",
        asset_class="",
        order_class=OrderClass.MLEG,
        side=None,
        position_intent=None,
        order_type="limit",
        time_in_force="day",
        status="filled",
        quantity=Decimal(1),
        filled_quantity=Decimal(1),
        filled_avg_price=Decimal("-2.01"),
        limit_price=None,
        stop_price=None,
        ratio_qty=None,
        created_at=at(14),
        submitted_at=at(14),
        filled_at=at(14),
        canceled_at=None,
        expired_at=None,
        updated_at=at(14),
        extended_hours=False,
        legs=(leg,),
    )
    assert intents_from_orders([parent]) == {"leg-1": PositionIntent.SELL_TO_OPEN}


# --------------------------------------------------------------------------
# Refusals the queue itself produces
# --------------------------------------------------------------------------


def test_closing_more_than_is_open_books_the_matched_part_and_reports_the_rest() -> None:
    result = ledger(
        fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="open"),
        fill(IWM, FillSide.SELL, 3, "2.50", when=at(15), order_id="close"),
        intents={"open": PositionIntent.BUY_TO_OPEN},
    )
    (trade,) = result.trades
    assert trade.qty == 1
    assert rules(result.rejections) == [RejectionRule.OVER_CLOSE.value]
    (rejection,) = result.rejections
    assert rejection.inputs["unmatched_qty"] == "2"


def test_an_open_against_the_live_queues_direction_is_refused() -> None:
    """A broker nets; a long lot and a short lot in one symbol cannot coexist."""
    result = ledger(
        fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="open"),
        fill(IWM, FillSide.SELL_SHORT, 1, "2.50", when=at(15), order_id="short"),
        intents={"open": PositionIntent.BUY_TO_OPEN},
    )
    assert result.trades == ()
    assert rules(result.rejections) == [RejectionRule.DIRECTION_CONFLICT.value]
    assert len(result.open_lots) == 1


# --------------------------------------------------------------------------
# No global sort: per contract symbol is the invariant that holds
# --------------------------------------------------------------------------


def test_two_symbols_do_not_share_a_queue() -> None:
    result = ledger(
        fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="a"),
        fill(SPY, FillSide.BUY, 1, "5.00", when=at(14, 0, 1), order_id="b"),
        fill(IWM, FillSide.SELL, 1, "2.50", when=at(15), order_id="c"),
        intents={
            "a": PositionIntent.BUY_TO_OPEN,
            "b": PositionIntent.BUY_TO_OPEN,
        },
    )
    (trade,) = result.trades
    assert trade.symbol == IWM
    assert trade.open_price == Decimal("2.00")
    (lot,) = result.open_lots
    assert lot.symbol == SPY


def test_the_result_does_not_depend_on_the_order_activities_arrive_in() -> None:
    """No global chronological order exists, so none may be assumed.

    Measured on the real recording: neither the composite activity ``id`` nor
    ``transaction_time`` orders rows across symbols -- ``...438268`` came back
    before ``...438263``. What *does* hold is per contract symbol, and the
    open-lot queue is per contract symbol anyway.
    """
    rows = [
        fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="a"),
        fill(SPY, FillSide.SELL_SHORT, 1, "5.00", when=at(14), order_id="b"),
        fill(IWM, FillSide.BUY, 1, "3.00", when=at(15), order_id="c"),
        fill(IWM, FillSide.SELL, 2, "2.50", when=at(16), order_id="d"),
        fill(SPY, FillSide.BUY, 1, "4.00", when=at(16), order_id="e"),
    ]
    intents = {
        "a": PositionIntent.BUY_TO_OPEN,
        "c": PositionIntent.BUY_TO_OPEN,
        "e": PositionIntent.BUY_TO_CLOSE,
    }
    forwards = build_ledger(
        rows, account=PAPER, intents=intents, multipliers=multipliers(IWM, SPY)
    )
    backwards = build_ledger(
        list(reversed(rows)),
        account=PAPER,
        intents=intents,
        multipliers=multipliers(IWM, SPY),
    )
    assert forwards.trades == backwards.trades
    assert forwards.open_lots == backwards.open_lots
    assert [trade.pnl for trade in forwards.trades] == [
        Decimal("50.00"),
        Decimal("-50.00"),
        Decimal("100.00"),
    ]


# --------------------------------------------------------------------------
# Idempotency, and the schema risk that would hide a lost row
# --------------------------------------------------------------------------


def test_ingestion_is_idempotent_on_activity_id() -> None:
    """Re-pulling an overlapping window must not book the position twice."""
    opened = fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="open")
    closed = fill(IWM, FillSide.SELL, 1, "2.50", when=at(15), order_id="close")
    result = build_ledger(
        [opened, closed, opened, closed],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers=multipliers(IWM),
    )
    (trade,) = result.trades
    assert trade.pnl == Decimal("50.00")
    assert result.rejections == ()


def test_two_different_rows_sharing_one_activity_id_are_reported_not_dropped() -> None:
    """Alpaca's doc examples give **both** rows of an event pair the same ``id``.

    Probably a copy-paste artifact, and unverified either way -- but if it is
    real then ``fill(activity_id UNIQUE)`` silently drops the second row of
    every option event. So a collision is reported here rather than being
    resolved by keeping whichever row happened to arrive first.
    """
    shared = "20260910131125217::collision"
    result = ledger(
        fill(
            IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="a", activity_id=shared
        ),
        fill(
            SPY, FillSide.BUY, 1, "5.00", when=at(14), order_id="b", activity_id=shared
        ),
        intents={"a": PositionIntent.BUY_TO_OPEN, "b": PositionIntent.BUY_TO_OPEN},
    )
    assert RejectionRule.DUPLICATE_ACTIVITY_ID.value in rules(result.rejections)
    # Both rows still reach the matcher. Dropping either is the silent loss.
    assert {lot.symbol for lot in result.open_lots} == {IWM, SPY}


# --------------------------------------------------------------------------
# Every decline is reported, with the rule, the inputs and the timestamp
# --------------------------------------------------------------------------


def test_an_activity_the_ledger_does_not_model_is_reported_not_swallowed() -> None:
    journal = option_event("JNLC", "", 0, when=at(9))
    result = ledger(journal)
    assert rules(result.rejections) == [RejectionRule.NOT_A_LEDGER_ACTIVITY.value]
    (rejection,) = result.rejections
    assert rejection.activity_id == journal.id
    assert rejection.inputs["activity_type"] == "JNLC"


def test_a_fill_on_something_that_is_not_an_option_is_reported() -> None:
    result = ledger(fill("NVDA", FillSide.BUY, 100, "218.17", when=at(14), order_id="s"))
    assert rules(result.rejections) == [RejectionRule.NOT_AN_OPTION.value]


def test_every_rejection_carries_a_rule_the_inputs_and_a_timestamp() -> None:
    result = ledger(
        fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="lonely"),
        fill("NVDA", FillSide.BUY, 100, "218.17", when=at(15), order_id="stock"),
    )
    assert len(result.rejections) == 2
    for rejection in result.rejections:
        assert rejection.rule in set(RejectionRule)
        assert rejection.at is not None
        assert rejection.at.tzinfo is not None
        assert rejection.detail
        assert rejection.inputs


def test_rejections_are_logged_as_well_as_returned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Silent rejection is a bug.

    Returning the rejection is the contract; logging it is so a human sees it
    without a caller remembering to look.
    """
    with caplog.at_level("INFO", logger="corollary.engine.ledger"):
        ledger(fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="lonely"))
    logged = [
        record for record in caplog.records if getattr(record, "rule", None) is not None
    ]
    assert [record.rule for record in logged] == [RejectionRule.UNKNOWN_INTENT.value]
    (record,) = logged
    assert record.activity_symbol == IWM
    assert record.at == at(14).isoformat()


# --------------------------------------------------------------------------
# Fee attribution -- order_id, then execution_id, then group_id
# --------------------------------------------------------------------------


def test_a_fee_attributes_through_order_id_first() -> None:
    result = ledger(
        fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="open"),
        fee("-0.03", when=at(14, 1), order_id="open"),
        intents={"open": PositionIntent.BUY_TO_OPEN},
    )
    (charge,) = result.fees
    assert charge.link is FeeLink.ORDER_ID
    assert charge.symbol == IWM
    assert charge.amount == Decimal("-0.03")
    assert result.total_fees == Decimal("-0.03")
    assert result.unattributed_fees == Decimal(0)


def test_a_fee_attributes_through_execution_id_when_there_is_no_order_id() -> None:
    """``execution_id`` is undocumented, and is the only per-fill link a fee gets."""
    result = ledger(
        option_event("OPEXP", IWM, -1, when=at(21), execution_id="exec-1"),
        fee("-0.03", when=at(21, 1), execution_id="exec-1"),
        multipliers=multipliers(IWM),
    )
    (charge,) = result.fees
    assert charge.link is FeeLink.EXECUTION_ID
    assert charge.symbol == IWM


def test_a_fees_execution_id_is_the_uuid_half_of_the_fills_activity_id() -> None:
    """The join that makes hop 2 reach a *fill*, and it needs no new field.

    ``TradeActivity`` carries no ``execution_id``, which looks like a dead end
    until you notice where Alpaca puts the execution's identity: the composite
    activity id is ``<17-digit stamp>::<uuid>`` and that uuid **is** the
    execution. Measured on the repaired recording, 15 of 15 fees carrying an
    ``execution_id`` match the tail of a fill's id, one to one.
    """
    execution = "68cda3e9-0000-4000-8000-0000000000ff"
    result = ledger(
        fill(
            IWM,
            FillSide.BUY,
            1,
            "2.00",
            when=at(14),
            order_id="open",
            activity_id=f"20260910131125592::{execution}",
        ),
        fee("-0.03", when=at(14, 1), execution_id=execution),
        intents={"open": PositionIntent.BUY_TO_OPEN},
    )
    (charge,) = result.fees
    assert charge.link is FeeLink.EXECUTION_ID
    assert charge.symbol == IWM
    assert charge.movement_activity_id == f"20260910131125592::{execution}"


def test_a_bare_stamp_is_not_an_execution_id() -> None:
    """An id with no ``::`` has no uuid half, so there is nothing to join on.

    Without this the whole id would stand in for the tail, and a fee whose
    ``execution_id`` happened to equal some other row's id would attribute to
    it -- an attribution built on a coincidence rather than on a relation.
    """
    result = ledger(
        fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="o", activity_id="bare"),
        fee("-0.03", when=at(14, 1), execution_id="bare"),
        intents={"o": PositionIntent.BUY_TO_OPEN},
    )
    (charge,) = result.fees
    assert charge.link is None
    assert result.unattributed_fees == Decimal("-0.03")


def test_a_fee_attributes_through_group_id_last() -> None:
    result = ledger(
        option_event("OPEXP", IWM, -1, when=at(21), group_id="grp-1"),
        fee("-0.03", when=at(21, 1), group_id="grp-1"),
        multipliers=multipliers(IWM),
    )
    (charge,) = result.fees
    assert charge.link is FeeLink.GROUP_ID
    assert charge.symbol == IWM


def test_a_fee_with_no_net_amount_is_refused_rather_than_booked_at_zero() -> None:
    """The one absence this module used to answer with a number.

    ``net_amount`` is ``Decimal | None`` and ``wire.as_decimal("")`` returns
    ``None``, so a missing field and an empty string arrive identically. Booked
    at ``Decimal(0)`` the charge disappeared from :attr:`Ledger.total_fees`
    with nothing in :attr:`Ledger.rejections` to say so -- the reconciliation
    invariant would simply stop closing. Refused and reported instead, which is
    what ``UNPRICED_OPTION_EVENT``, ``UNKNOWN_MULTIPLIER`` and
    ``MISSING_EVENT_QUANTITY`` already do with the absences they meet.
    """
    unpriced = fee(None, when=at(20, 31), sub_type="REG", description="OPT REG fee")
    result = ledger(
        fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="open"),
        fee("-0.21", when=at(20, 30), sub_type="CAT", description="CAT fee"),
        unpriced,
        intents={"open": PositionIntent.BUY_TO_OPEN},
    )

    # Not booked at zero, and not silently absent either.
    (charge,) = result.fees
    assert charge.amount == Decimal("-0.21")
    assert result.total_fees == Decimal("-0.21")
    assert [rejection.rule for rejection in result.rejections] == [
        RejectionRule.MISSING_FEE_AMOUNT
    ]

    # Rule 8: the rule, the inputs and the timestamp.
    (rejection,) = result.rejections
    assert rejection.activity_id == unpriced.id
    assert rejection.at == at(20, 31)
    assert rejection.inputs["activity_type"] == "FEE"
    assert rejection.inputs["activity_sub_type"] == "REG"
    assert rejection.detail


def test_a_refused_fee_is_logged_as_well_as_returned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Silent rejection is a bug, and this one was silent in both directions."""
    with caplog.at_level("INFO", logger="corollary.engine.ledger"):
        ledger(fee(None, when=at(20, 31), sub_type="TAF"))
    logged = [
        record.rule
        for record in caplog.records
        if getattr(record, "rule", None) is not None
    ]
    assert logged == [RejectionRule.MISSING_FEE_AMOUNT.value]


def test_a_fee_of_exactly_zero_is_booked_and_not_refused() -> None:
    """The boundary: ``Decimal(0)`` is a stated amount, ``None`` is an absence.

    ``Decimal(0)`` is falsy, so a refusal written ``if not net_amount`` would
    decline a fee the broker really did report as zero -- the mirror-image
    failure of booking an absence at zero, and just as silent.
    """
    result = ledger(fee("0", when=at(20, 31), sub_type="REG"))
    (charge,) = result.fees
    assert charge.amount == Decimal(0)
    assert result.rejections == ()
    assert result.total_fees == Decimal(0)


def test_an_unattributed_fee_is_reported_separately_rather_than_dropped() -> None:
    """This is the case that actually happens: every recorded fee lands here."""
    result = ledger(
        fill(IWM, FillSide.BUY, 1, "2.00", when=at(14), order_id="open"),
        fee("-0.21", when=at(20, 31), sub_type="REG", description="OPT REG fee"),
        intents={"open": PositionIntent.BUY_TO_OPEN},
    )
    (charge,) = result.fees
    assert charge.link is None
    assert charge.symbol is None
    assert charge.is_attributed is False
    assert result.total_fees == Decimal("-0.21")
    assert result.unattributed_fees == Decimal("-0.21")


# --------------------------------------------------------------------------
# Rule 6: a fee's description is vendor prose, and prose carries identifiers
# --------------------------------------------------------------------------

#: Account-number **shaped**, and deliberately not an account number. Rule 6
#: keeps real key material and real identifiers out of code, tests and
#: fixtures alike -- and the shape is the whole of what a redactor can see.
PLACEHOLDER_ACCOUNT = "PA0EXAMPLE00"

#: The sentence this exists for, in the form the vendor actually sends it.
FEE_PROSE = f"CAT fee for proceed of 15 trades on 2026-09-10 by {PLACEHOLDER_ACCOUNT}"


def test_a_fee_description_reaches_neither_a_log_nor_a_record_unredacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both halves, because the description travels two ways out of here.

    A ``FEE`` row's ``description`` is the one field known to carry the
    account number in running text, which is exactly why a rule written about
    field *names* never fires on it. Two exits:

    * a missing ``net_amount`` refuses the row, and the refusal logs its
      ``inputs`` verbatim at WARNING -- a modelled state rather than an
      impossible one, since the published ``NonTradeActivities`` schema is
      known-incomplete;
    * an amount that *is* present builds a :class:`FeeRecord`, which leaves on
      ``IngestResult.fees`` and from there to an API route.

    Redacted, not dropped: the fee is still identifiable as a CAT fee, which
    is the whole reason the log line is worth keeping.
    """
    with caplog.at_level("INFO", logger="corollary.engine.ledger"):
        result = ledger(
            fee("-0.21", when=at(20, 30), sub_type="CAT", description=FEE_PROSE),
            fee(None, when=at(20, 31), sub_type="CAT", description=FEE_PROSE),
        )

    (charge,) = result.fees
    assert charge.description is not None
    assert PLACEHOLDER_ACCOUNT not in charge.description
    assert REDACTED in charge.description
    assert charge.description.startswith("CAT fee for proceed of 15 trades")

    (rejection,) = result.rejections
    assert rejection.rule is RejectionRule.MISSING_FEE_AMOUNT
    assert PLACEHOLDER_ACCOUNT not in rejection.inputs["description"]
    assert REDACTED in rejection.inputs["description"]

    logged = [record for record in caplog.records if getattr(record, "rule", None)]
    assert len(logged) == 1
    record = logged[0]
    assert PLACEHOLDER_ACCOUNT not in record.getMessage()
    assert PLACEHOLDER_ACCOUNT not in caplog.text
    inputs: dict[str, str] = getattr(record, "inputs")
    assert PLACEHOLDER_ACCOUNT not in inputs["description"]
    # Nothing else on the record smuggles it out either.
    assert not any(
        PLACEHOLDER_ACCOUNT in str(value) for value in record.__dict__.values()
    )


# --------------------------------------------------------------------------
# The folds -- every one of these is Python, because Money refuses SQL
# --------------------------------------------------------------------------


def a_trade(pnl: str, pct: str | None = None) -> RealizedTradeRecord:
    return RealizedTradeRecord(
        account=PAPER,
        symbol=IWM,
        opened_at=at(14),
        closed_at=at(15),
        qty=1,
        open_price=Decimal("1.00"),
        close_price=Decimal("1.00"),
        pnl=Decimal(pnl),
        pnl_pct=None if pct is None else Decimal(pct),
        close_kind=CloseKind.FILL,
    )


def test_the_folds_are_python_over_loaded_rows() -> None:
    stats = summarise(
        [a_trade("120", "12"), a_trade("-7", "-0.85"), a_trade("-41", "-4.1")]
    )
    assert stats.realized_pnl == Decimal("72")
    assert stats.trades == 3
    assert stats.wins == 1
    assert stats.losses == 2
    assert stats.win_rate == Decimal("33.33")
    assert stats.average_win == Decimal("120.00")
    assert stats.average_loss == Decimal("-24.00")
    assert stats.average_win_pct == Decimal("12.0000")
    assert stats.average_loss_pct == Decimal("-2.4750")


def test_biggest_loser_is_a_python_sort_and_not_a_lexicographic_one() -> None:
    """``ORDER BY pnl`` over TEXT gives ``'-41' < '-7'``; Decimal does not."""
    rows = [a_trade("120"), a_trade("-7"), a_trade("-41")]
    assert biggest_loser(rows) is rows[2]
    assert biggest_loser([rows[0]]) is None
    assert biggest_loser([]) is None


def test_summarising_nothing_reports_none_rather_than_zero() -> None:
    stats = summarise([])
    assert stats.realized_pnl == Decimal(0)
    assert stats.trades == 0
    assert stats.win_rate is None
    assert stats.average_win is None
    assert stats.average_loss is None


def test_a_scratch_trade_is_neither_a_win_nor_a_loss() -> None:
    stats = summarise([a_trade("0"), a_trade("10")])
    assert (stats.wins, stats.losses, stats.scratches) == (1, 0, 1)
    assert stats.win_rate == Decimal("50.00")


def test_close_kind_matches_the_database_check_constraint() -> None:
    assert tuple(kind.value for kind in CloseKind) == CLOSE_KINDS


def test_the_record_mirrors_the_realized_trade_table() -> None:
    """``RealizedTrade(**asdict(record))`` has to work, so the names must match."""
    columns = [
        column.name for column in RealizedTrade.__table__.columns if column.name != "id"
    ]
    assert [field.name for field in dataclasses.fields(RealizedTradeRecord)] == columns


def test_a_record_constructs_a_realized_trade_row_directly() -> None:
    """The mirroring proved by doing it, not only by comparing names."""
    record = a_trade("-7.00", "-0.85")
    row = RealizedTrade(**as_row_kwargs(record))
    assert row.account == PAPER
    assert row.symbol == IWM
    assert row.pnl == Decimal("-7.00")
    assert row.close_kind == "fill"
    assert row.qty == 1


def test_realized_pnl_folds_the_same_way_the_stats_do() -> None:
    rows = [a_trade("120"), a_trade("-7"), a_trade("-41")]
    assert realized_pnl(rows) == summarise(rows).realized_pnl == Decimal("72")
    assert realized_pnl([]) == Decimal(0)


# --------------------------------------------------------------------------
# A movement with no price at all
# --------------------------------------------------------------------------
#
# Normalisation cannot produce one: a fill always carries a price and an
# unpriced option event is refused before it becomes a movement. But
# `LotMovement.price` is nullable -- `build_ledger` nulls the intrinsic on an
# exercise whose deliverable it could not verify -- and `match_movements` is
# public, so the matcher has to answer for the shape rather than assume it
# away. Both answers are the same: refuse, log, and never invent a basis.


def unpriced_movement(
    *, intent: PositionIntent, side: FillSide, qty: int, hour: int, price: str | None
) -> LotMovement:
    return LotMovement(
        activity_id=f"2026091{hour}::{intent.value}",
        activity_type="FILL",
        symbol=IWM,
        side=side,
        intent=intent,
        qty=qty,
        price=None if price is None else Decimal(price),
        at=at(hour),
    )


def test_an_opening_movement_with_no_price_opens_no_lot_and_says_why() -> None:
    """A lot with no basis makes every P&L measured from it measured from nothing.

    Zero is not the fallback: zero is a price, and it would report the entire
    proceeds of the eventual close as profit.
    """
    result = match_movements(
        [
            unpriced_movement(
                intent=PositionIntent.BUY_TO_OPEN,
                side=FillSide.BUY,
                qty=2,
                hour=14,
                price=None,
            )
        ],
        account=PAPER,
        multipliers=multipliers(IWM),
    )

    assert result.open_lots == ()
    assert result.trades == ()
    (refusal,) = result.rejections
    assert refusal.rule is RejectionRule.UNPRICED_OPTION_EVENT
    assert refusal.symbol == IWM
    assert refusal.at == at(14)


def test_a_close_with_no_price_books_nothing_and_still_consumes_the_lot() -> None:
    """The same two halves the unverified deliverable takes.

    No trade, because there is no close price to subtract the basis from --
    and no surviving open lot, because the contracts really are gone. A
    phantom lot would put ``open_lots`` at odds with ``/v2/positions``, which
    is the more misleading of the two wrong numbers: it reads as a position
    you could still act on.
    """
    result = match_movements(
        [
            unpriced_movement(
                intent=PositionIntent.BUY_TO_OPEN,
                side=FillSide.BUY,
                qty=2,
                hour=14,
                price="8.21",
            ),
            unpriced_movement(
                intent=PositionIntent.SELL_TO_CLOSE,
                side=FillSide.SELL,
                qty=2,
                hour=15,
                price=None,
            ),
        ],
        account=PAPER,
        multipliers=multipliers(IWM),
    )

    assert result.trades == ()
    assert result.open_lots == ()
    (refusal,) = result.rejections
    assert refusal.rule is RejectionRule.UNPRICED_OPTION_EVENT
    assert refusal.at == at(15)


# --------------------------------------------------------------------------
# The lot key is the canonical symbol, not the row's spelling
# --------------------------------------------------------------------------

#: An adjusted contract taken from the recording:
#: ``tests/fixtures/alpaca/option_contracts_adjusted.json`` carries it with
#: ``root_symbol: "GME1"``, ``underlying_symbol: "GME"`` and -- as every live
#: adjusted contract does -- ``multiplier: "100"``.
GME1 = "GME1261016C00003000"

#: The same contract, spelled the way it must never be keyed: lower-cased and
#: untrimmed. ``parse_occ_symbol`` answers ``GME1`` for both, which is the
#: whole reason :class:`~corollary.instruments.OccSymbol` carries a canonical
#: ``symbol`` at all.
#:
#: Unobserved on this account -- all 15 recorded ``FILL`` rows spell the
#: symbol canonically -- which is exactly why it needs a fixture rather than a
#: wait. The consequence is not a formatting wobble: lots are queued per
#: symbol, so two spellings of one contract become two queues, the close finds
#: nothing to match, and the round trip books **no realized P&L at all**
#: rather than reporting a refusal anyone could read.
GME1_AS_SENT = " gme1261016c00003000 "

#: The *unadjusted* GME contract at the same strike and expiry. A different
#: contract with a different deliverable, and it must stay a different queue:
#: canonicalisation folds case and whitespace and nothing else.
GME_PLAIN = "GME261016C00003000"


@pytest.mark.risk
def test_a_close_matches_an_open_whose_row_spelled_the_symbol_differently() -> None:
    """One contract, two spellings, one lot queue.

    Keyed on the row's own ``symbol`` instead of the parsed contract's, the
    open lands in one bucket and the close in another: the close over-closes
    an empty queue, the open lot never closes, and lifetime P&L is short the
    whole trade. Rule 4's failure mode arriving as silence.
    """
    result = build_ledger(
        [
            fill(GME1_AS_SENT, FillSide.BUY, 2, "3.10", when=at(14), order_id="open"),
            fill(GME1, FillSide.SELL, 2, "4.35", when=at(15), order_id="close"),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        # Keyed canonically, because that is how the contracts endpoint sends
        # it. A lot keyed on the raw row would not find its own multiplier
        # either -- two wrong answers from one wrong key.
        multipliers={GME1: Decimal(100)},
    )

    assert rules(result.rejections) == []
    (trade,) = result.trades
    assert trade.symbol == GME1
    assert trade.qty == 2
    assert trade.pnl == Decimal("250.00")
    assert result.open_lots == ()
    assert {movement.symbol for movement in result.movements} == {GME1}


@pytest.mark.risk
def test_an_expiry_closes_a_lot_whose_row_spelled_the_symbol_differently() -> None:
    """The same invariant on the option-event path, which builds its own movement.

    Both ``LotMovement`` constructors have to key on the parsed contract: this
    one is the ``OPEXP`` branch, where a mismatch leaves a worthless lot open
    forever and never books the loss.
    """
    result = build_ledger(
        [
            fill(GME1, FillSide.BUY, 2, "3.10", when=at(14), order_id="open"),
            option_event("OPEXP", GME1_AS_SENT, "-2", when=at(21)),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers={GME1: Decimal(100)},
    )

    assert rules(result.rejections) == []
    (trade,) = result.trades
    assert trade.symbol == GME1
    assert trade.close_kind is CloseKind.EXPIRY
    assert trade.close_price == Decimal(0)
    assert trade.pnl == Decimal("-620.00")
    assert result.open_lots == ()


@pytest.mark.risk
def test_an_adjusted_root_is_never_folded_into_the_unadjusted_contract() -> None:
    """The boundary on the other side: ``GME1`` and ``GME`` are two contracts.

    Canonicalisation must fold case and whitespace and *stop*. Folding the
    numeric suffix would match a close on the plain contract against a lot in
    the adjusted one, whose deliverable is not the same -- so the refusal
    here is the correct answer, and it names which symbol went unmatched.
    """
    result = build_ledger(
        [
            fill(GME1, FillSide.BUY, 2, "3.10", when=at(14), order_id="open"),
            fill(GME_PLAIN, FillSide.SELL, 2, "4.35", when=at(15), order_id="close"),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers={GME1: Decimal(100), GME_PLAIN: Decimal(100)},
    )

    assert result.trades == ()
    (refusal,) = result.rejections
    assert refusal.rule is RejectionRule.OVER_CLOSE
    assert refusal.symbol == GME_PLAIN
    (lot,) = result.open_lots
    assert lot.symbol == GME1
    assert lot.qty == 2
