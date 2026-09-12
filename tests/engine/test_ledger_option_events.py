"""Expiry, exercise and assignment -- **authored fixtures, not recorded ones**.

Read this first, because it is the honesty marker for the whole module.

``OPEXP``, ``OPEXC`` and ``OPASN`` have **no real data on this account**. Every
row below is written by hand from Alpaca's published documentation examples,
so these tests prove the matcher is *self-consistent* and prove nothing at all
about the shapes Alpaca actually sends. That distinction matters more here
than anywhere else in the ledger, because the money for an option event sits
on a **different row** than the event -- the event's own ``net_amount`` is
``"0"`` -- so the failure mode is silent and directional rather than loud.

The reconciliation checkpoint is dated, and it is not a formality
-----------------------------------------------------------------

Three near-dated NVDA contracts are held on the paper account, opened
2026-09-10 13:37 ET and expiring **2026-09-11**. With NVDA closing at 218.17
they cover all three branches at once:

===========================  ========  =================  ================
Contract                     Position  Close vs strike    Event
===========================  ========  =================  ================
``NVDA260911C00205000``      long      ITM by $13.17      ``OPEXC`` pair
``NVDA260911C00240000``      long      OTM                ``OPEXP``
``NVDA260911P00230000``      short     ITM                ``OPASN``
===========================  ========  =================  ================

They settle overnight, so the activity rows land **2026-09-14 at the latest**.
Whoever records them should replace the three tests named after those symbols
at the bottom of this file, first and before anything else -- each authored
test names the exact contract it stands in for, so the swap is mechanical.

Until then, the ``NVDA`` tests below are the *predicted* rows, and being
predicted is the point: if a real ``OPEXC`` disagrees with what is written
here, the disagreement is the finding.

The one deliberate deviation from the design spec
-------------------------------------------------

The spec says ``OPASN``/``OPEXC`` *"close at the strike"*. Taken literally,
the NVDA 205 call bought at 14.20 books a **+$19,080 gain** -- the account's
equity moved by -$103. The strike is an *input* to the close price, not the
close price: the lot settles at its **intrinsic value**, which needs the
strike *and* the underlying's settlement price. ``test_an_exercise_closes_at
_intrinsic_and_not_at_the_strike`` pins both failure modes at once -- not
``net_amount``'s zero, which the spec rightly warns against, and not the
strike either.

The third failure mode, and the one to know before recording
-------------------------------------------------------------

Intrinsic value is per underlying *share*, so turning it into dollars
multiplies by the shares the contract delivers -- and the matcher spells that
``x multiplier``, which is only the deliverable on a **standard** contract.
So an ``OPEXC``/``OPASN`` books no P&L at all unless ``contracts`` is
supplied and the terms say ``root_symbol == underlying_symbol``; see the last
section of this file. Every test below that expects an exercise to *book*
therefore passes ``contracts`` -- the ones that omit it are asserting the
refusal, and say so. Whoever records the real NVDA rows must supply the terms
alongside them, or find the trades quietly missing rather than wrong.
"""

from collections.abc import Mapping
from decimal import Decimal

from corollary.data.providers.interface import OptionContract, OptionDeliverable
from corollary.engine.execution.interface import FillSide, PositionIntent
from corollary.engine.ledger import (
    CloseKind,
    Ledger,
    RejectionRule,
    build_ledger,
    normalise_activities,
)
from corollary.instruments import OptionType

from .ledger_support import (
    at,
    contract,
    equity_deliverable,
    fill,
    option_event,
    optrd,
)

PAPER = "paper"

#: Alpaca's own documented example contract, used verbatim so the shapes below
#: are traceable to the docs rather than invented.
AAPL_CALL = "AAPL230721C00150000"
AAPL_SETTLEMENT = Decimal("160.00")

#: The three contracts whose real rows land 2026-09-14. See the module
#: docstring: these named constants are how a recorder finds what to replace.
NVDA_ITM_CALL = "NVDA260911C00205000"
NVDA_OTM_CALL = "NVDA260911C00240000"
NVDA_ITM_SHORT_PUT = "NVDA260911P00230000"
NVDA_CLOSE = Decimal("218.17")

MULTIPLIERS = {
    symbol: Decimal(100)
    for symbol in (AAPL_CALL, NVDA_ITM_CALL, NVDA_OTM_CALL, NVDA_ITM_SHORT_PUT)
}



def standard(symbol: str, underlying: str) -> OptionContract:
    """Terms for a standard contract with its deliverables requested.

    One equity deliverable of 100 shares -- which is what makes the share
    counts below statable at all. ``multiplier`` and ``size`` are also 100 on
    these and are *not* what is read: both are 100 on an adjusted contract too,
    where the deliverable is not.
    """
    return contract(symbol, deliverables=(equity_deliverable(underlying, "100"),))


#: Contract terms for the same four. Only :class:`ShareDelivery` reads these.
CONTRACTS = {
    AAPL_CALL: standard(AAPL_CALL, "AAPL"),
    NVDA_ITM_CALL: standard(NVDA_ITM_CALL, "NVDA"),
    NVDA_OTM_CALL: standard(NVDA_OTM_CALL, "NVDA"),
    NVDA_ITM_SHORT_PUT: standard(NVDA_ITM_SHORT_PUT, "NVDA"),
}


def rules(result: object) -> list[str]:
    return [rejection.rule.value for rejection in result.rejections]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# OPEXP -- the out-of-the-money case, and only that case
# --------------------------------------------------------------------------


def test_an_otm_expiry_closes_a_long_at_zero_for_a_full_loss() -> None:
    """Alpaca's documented shape: a lone ``OPEXP``, ``qty "-2"``, and nothing else.

    ``OPEXP`` is the **out-of-the-money case only**. Alpaca auto-exercises ITM
    contracts absent a do-not-exercise instruction, so an ITM expiry arrives as
    an ``OPEXC`` pair and never reaches this branch.
    """
    result = build_ledger(
        [
            fill(AAPL_CALL, FillSide.BUY, 2, "4.00", when=at(14), order_id="open"),
            option_event("OPEXP", AAPL_CALL, "-2", when=at(21)),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers=MULTIPLIERS,
    )
    (trade,) = result.trades
    assert trade.close_kind is CloseKind.EXPIRY
    assert trade.close_price == Decimal(0)
    assert trade.pnl == Decimal("-800.00")
    assert trade.pnl_pct == Decimal("-100.0000")
    assert result.open_lots == ()
    assert result.deliveries == ()


def test_an_otm_expiry_of_a_short_keeps_the_full_credit() -> None:
    """The other direction: worthless is the *best* outcome for a short."""
    result = build_ledger(
        [
            fill(AAPL_CALL, FillSide.SELL_SHORT, 2, "4.00", when=at(14)),
            option_event("OPEXP", AAPL_CALL, "2", when=at(21)),
        ],
        account=PAPER,
        intents={},
        multipliers=MULTIPLIERS,
    )
    (trade,) = result.trades
    assert trade.close_kind is CloseKind.EXPIRY
    assert trade.pnl == Decimal("800.00")
    assert trade.pnl_pct == Decimal("100.0000")
    assert result.deliveries == ()


# --------------------------------------------------------------------------
# The two quantity conventions
# --------------------------------------------------------------------------


def test_a_signed_event_qty_normalises_to_the_same_movement_as_an_unsigned_fill() -> (
    None
):
    """``-2`` with no side, and ``2`` with ``side: "sell"``, are one movement.

    A ``FILL`` carries an unsigned ``qty`` with a separate ``side``; a
    non-trade row carries a **signed** ``qty`` and no ``side`` at all. The
    matcher sees one convention because normalisation produces one.
    """
    from_event = normalise_activities(
        [option_event("OPEXP", AAPL_CALL, "-2", when=at(21))], intents={}
    ).movements
    from_fill = normalise_activities(
        [fill(AAPL_CALL, FillSide.SELL, 2, "0", when=at(21))], intents={}
    ).movements

    (event_movement,) = from_event
    (fill_movement,) = from_fill
    assert event_movement.qty == fill_movement.qty == 2
    assert event_movement.intent is fill_movement.intent is PositionIntent.SELL_TO_CLOSE
    assert event_movement.side is fill_movement.side is FillSide.SELL


def test_a_positive_event_qty_closes_a_short_the_way_a_buy_would() -> None:
    """``+2`` is a short being assigned away, which is a buy-to-close."""
    (movement,) = normalise_activities(
        [option_event("OPEXP", AAPL_CALL, "2", when=at(21))], intents={}
    ).movements
    assert movement.qty == 2
    assert movement.intent is PositionIntent.BUY_TO_CLOSE
    assert movement.side is FillSide.BUY


def test_the_two_conventions_produce_an_identical_realized_trade() -> None:
    """Same lot, same close price, same arithmetic -- only ``close_kind`` differs."""
    opening = fill(AAPL_CALL, FillSide.BUY, 2, "4.00", when=at(14), order_id="open")
    intents = {"open": PositionIntent.BUY_TO_OPEN}
    by_event = build_ledger(
        [opening, option_event("OPEXP", AAPL_CALL, "-2", when=at(21))],
        account=PAPER,
        intents=intents,
        multipliers=MULTIPLIERS,
    )
    by_fill = build_ledger(
        [opening, fill(AAPL_CALL, FillSide.SELL, 2, "0", when=at(21))],
        account=PAPER,
        intents=intents,
        multipliers=MULTIPLIERS,
    )
    (event_trade,) = by_event.trades
    (fill_trade,) = by_fill.trades
    assert event_trade.pnl == fill_trade.pnl
    assert event_trade.qty == fill_trade.qty
    assert event_trade.close_price == fill_trade.close_price
    assert (event_trade.close_kind, fill_trade.close_kind) == (
        CloseKind.EXPIRY,
        CloseKind.FILL,
    )


# --------------------------------------------------------------------------
# OPEXC -- exercise. The money is not on the event row.
# --------------------------------------------------------------------------


def exercise_pair(group_id: str | None = None, *, paired_price: str = "150") -> list:
    """Alpaca's documented exercise example, verbatim.

    ``OPEXC`` on ``AAPL230721C00150000``, ``qty: "-2"``, ``net_amount: "0"``;
    paired ``OPTRD`` on ``AAPL``, ``qty: "200"``, ``price: "150"``,
    ``net_amount: "-30000"``.
    """
    return [
        option_event("OPEXC", AAPL_CALL, "-2", when=at(21), group_id=group_id),
        optrd(
            "AAPL",
            "200",
            paired_price,
            when=at(21),
            net_amount="-30000",
            group_id=group_id,
        ),
    ]


def test_an_exercise_closes_at_intrinsic_and_not_at_net_amounts_zero() -> None:
    """*"A matcher that trusts ``net_amount`` books every exercise as a total loss."*

    The event row's ``net_amount`` really is ``"0"`` -- a well-formed number
    that nothing raises on, which is exactly why this has to be asserted
    rather than assumed.

    ``contracts`` is supplied because an exercise's P&L is refused without it
    -- see the adjusted-contract section at the bottom of this file. Nothing
    about *this* assertion depends on the terms; they are what establishes
    that the close price may be derived from the underlying at all.
    """
    result = build_ledger(
        [
            fill(AAPL_CALL, FillSide.BUY, 2, "4.00", when=at(14), order_id="open"),
            *exercise_pair(),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers=MULTIPLIERS,
        settlements={AAPL_CALL: AAPL_SETTLEMENT},
        contracts=CONTRACTS,
    )
    (trade,) = result.trades
    assert trade.close_kind is CloseKind.EXERCISE
    assert trade.close_price == Decimal("10.00")  # 160.00 - 150 strike
    assert trade.close_price != Decimal(0)
    assert trade.pnl == Decimal("1200.00")


def test_an_exercise_closes_at_intrinsic_and_not_at_the_strike() -> None:
    """The other failure mode, and the one the spec's own wording would cause.

    Booking the strike as the close price would report this exercise as a
    ``(150 - 4.00) x 2 x 100 = +$29,200`` gain. The strike is an input to the
    close price, not the close price.
    """
    result = build_ledger(
        [
            fill(AAPL_CALL, FillSide.BUY, 2, "4.00", when=at(14), order_id="open"),
            *exercise_pair(),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers=MULTIPLIERS,
        settlements={AAPL_CALL: AAPL_SETTLEMENT},
        contracts=CONTRACTS,
    )
    (trade,) = result.trades
    assert trade.close_price != Decimal("150")
    assert trade.pnl != Decimal("29200.00")
    assert trade.pnl == Decimal("1200.00")


def test_an_exercise_with_no_settlement_price_is_refused_rather_than_zeroed() -> None:
    """No price, no trade. Refusing is the only honest answer here.

    Zero would report a total loss on every ITM expiry, and lifetime P&L,
    average win, average loss and win rate would all be wrong in the same
    direction with nothing on screen to say so. The first symptom is a
    terminal that believes you never win.
    """
    result = build_ledger(
        [
            fill(AAPL_CALL, FillSide.BUY, 2, "4.00", when=at(14), order_id="open"),
            *exercise_pair(),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers=MULTIPLIERS,
        settlements={},
    )
    assert result.trades == ()
    assert RejectionRule.UNPRICED_OPTION_EVENT.value in rules(result)
    # The lot is left open rather than silently closed at a made-up price.
    (lot,) = result.open_lots
    assert lot.qty == 2


def test_the_shares_an_exercise_delivers_are_named_and_not_tracked() -> None:
    """Corollary is an options terminal, so the stock is reported, not held."""
    result = build_ledger(
        [
            fill(AAPL_CALL, FillSide.BUY, 2, "4.00", when=at(14), order_id="open"),
            *exercise_pair(),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers=MULTIPLIERS,
        settlements={AAPL_CALL: AAPL_SETTLEMENT},
        contracts=CONTRACTS,
    )
    (delivery,) = result.deliveries
    assert delivery.underlying == "AAPL"
    assert delivery.is_adjusted is False
    assert delivery.option_type is OptionType.CALL
    # 200 because the *deliverables* say 100 shares a contract, not because
    # the multiplier is 100. On an adjusted contract those part company.
    assert delivery.shares == Decimal(200)  # received: a long call was exercised
    assert delivery.price == Decimal("150")
    assert delivery.close_kind is CloseKind.EXERCISE
    # No lot is opened for the stock, and none is tracked.
    assert [lot.symbol for lot in result.open_lots] == []


def test_the_paired_optrd_row_is_declined_because_it_is_not_a_contract() -> None:
    """Its ``price`` is the strike and its symbol is the **underlying**."""
    result = build_ledger(
        exercise_pair(),
        account=PAPER,
        intents={},
        multipliers=MULTIPLIERS,
        settlements={AAPL_CALL: AAPL_SETTLEMENT},
    )
    assert RejectionRule.NOT_A_LEDGER_ACTIVITY.value in rules(result)
    declined = [
        rejection
        for rejection in result.rejections
        if rejection.rule is RejectionRule.NOT_A_LEDGER_ACTIVITY
    ]
    assert [rejection.symbol for rejection in declined] == ["AAPL"]


def test_the_paired_optrd_price_corroborates_the_strike() -> None:
    """Agreement is silent; disagreement is reported and the symbol wins.

    The OCC symbol always carries the strike and needs no pairing, so it is
    the source. The ``OPTRD`` is a second witness -- and only where a
    ``group_id`` links the two, which is null on every recorded row of this
    account and is therefore itself untested against reality.
    """
    agreeing = build_ledger(
        exercise_pair("grp-1"),
        account=PAPER,
        intents={},
        multipliers=MULTIPLIERS,
        settlements={AAPL_CALL: AAPL_SETTLEMENT},
        contracts=CONTRACTS,
    )
    assert RejectionRule.PAIRED_STRIKE_DISAGREES.value not in rules(agreeing)

    disagreeing = build_ledger(
        [
            fill(AAPL_CALL, FillSide.BUY, 2, "4.00", when=at(14), order_id="open"),
            *exercise_pair("grp-1", paired_price="151"),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers=MULTIPLIERS,
        settlements={AAPL_CALL: AAPL_SETTLEMENT},
        contracts=CONTRACTS,
    )
    assert RejectionRule.PAIRED_STRIKE_DISAGREES.value in rules(disagreeing)
    (trade,) = disagreeing.trades
    assert trade.close_price == Decimal("10.00")


def test_both_rows_of_a_pair_sharing_one_id_is_detectable() -> None:
    """Alpaca's doc examples reuse one ``id`` across both rows of a pair.

    Unverified, and a schema risk rather than a logic one: if it is real then
    ``fill(activity_id UNIQUE)`` silently drops the second row of every option
    event. Neither row is discarded here, and the collision is reported.
    """
    shared = "20260910210000000::pair"
    result = build_ledger(
        [
            fill(AAPL_CALL, FillSide.BUY, 2, "4.00", when=at(14), order_id="open"),
            option_event("OPEXC", AAPL_CALL, "-2", when=at(21), activity_id=shared),
            optrd(
                "AAPL", "200", "150", when=at(21), net_amount="-30000",
                activity_id=shared,
            ),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers=MULTIPLIERS,
        settlements={AAPL_CALL: AAPL_SETTLEMENT},
        contracts=CONTRACTS,
    )
    assert RejectionRule.DUPLICATE_ACTIVITY_ID.value in rules(result)
    # The event is still matched: detection must not cost the row.
    (trade,) = result.trades
    assert trade.close_kind is CloseKind.EXERCISE


# --------------------------------------------------------------------------
# OPASN -- assignment
# --------------------------------------------------------------------------


def test_an_assignment_closes_a_short_at_intrinsic() -> None:
    """Alpaca's documented assignment example: ``qty: "2"``, ``net_amount: "0"``.

    The paired ``OPTRD`` is ``qty: "-200"``, ``price: "150"``,
    ``net_amount: "30000"`` -- shares delivered away, money in.
    """
    result = build_ledger(
        [
            fill(AAPL_CALL, FillSide.SELL_SHORT, 2, "5.00", when=at(14)),
            option_event("OPASN", AAPL_CALL, "2", when=at(21)),
            optrd("AAPL", "-200", "150", when=at(21), net_amount="30000"),
        ],
        account=PAPER,
        intents={},
        multipliers=MULTIPLIERS,
        settlements={AAPL_CALL: AAPL_SETTLEMENT},
        contracts=CONTRACTS,
    )
    (trade,) = result.trades
    assert trade.close_kind is CloseKind.ASSIGNMENT
    assert trade.close_price == Decimal("10.00")
    # Sold the call for 5.00, bought back at 10.00 of intrinsic.
    assert trade.pnl == Decimal("-1000.00")
    (delivery,) = result.deliveries
    assert delivery.shares == Decimal(-200)  # a short call delivers shares away


# --------------------------------------------------------------------------
# The three real contracts, predicted. Replace these first on 2026-09-14.
# --------------------------------------------------------------------------


def test_predicted_nvda_itm_call_exercises_for_a_small_loss() -> None:
    """Stands in for the real ``OPEXC`` on ``NVDA260911C00205000``.

    Bought at 14.20 on 2026-09-10; NVDA closed at 218.17, so the 205 call is
    worth 13.17 and the trade loses $1.03 a share. The equity check: cash goes
    -20,500 and 100 shares worth 21,817 arrive, a net +1,317, against the
    1,420 premium already paid -- **-$103**, which is what the row must say.
    """
    result = build_ledger(
        [
            fill(NVDA_ITM_CALL, FillSide.BUY, 1, "14.20", when=at(17, 37), order_id="o"),
            option_event("OPEXC", NVDA_ITM_CALL, "-1", when=at(21, 0, 0, day=11)),
        ],
        account=PAPER,
        intents={"o": PositionIntent.BUY_TO_OPEN},
        multipliers=MULTIPLIERS,
        settlements={NVDA_ITM_CALL: NVDA_CLOSE},
        contracts=CONTRACTS,
    )
    (trade,) = result.trades
    assert trade.close_price == Decimal("13.17")
    assert trade.pnl == Decimal("-103.00")
    (delivery,) = result.deliveries
    assert delivery.shares == Decimal(100)
    assert delivery.price == Decimal("205")


def test_predicted_nvda_otm_call_expires_worthless() -> None:
    """Stands in for the real ``OPEXP`` on ``NVDA260911C00240000``.

    Bought at 0.02 and out of the money at the close -- a $2.00 loss, and the
    only one of the three that reaches the ``OPEXP`` branch at all.
    """
    result = build_ledger(
        [
            fill(NVDA_OTM_CALL, FillSide.BUY, 1, "0.02", when=at(17, 37), order_id="o"),
            option_event("OPEXP", NVDA_OTM_CALL, "-1", when=at(21, 0, 0, day=11)),
        ],
        account=PAPER,
        intents={"o": PositionIntent.BUY_TO_OPEN},
        multipliers=MULTIPLIERS,
    )
    (trade,) = result.trades
    assert trade.close_kind is CloseKind.EXPIRY
    assert trade.pnl == Decimal("-2.00")
    assert result.deliveries == ()


def test_predicted_nvda_short_put_is_assigned() -> None:
    """Stands in for the real ``OPASN`` on ``NVDA260911P00230000``.

    Sold at 11.55; NVDA closed at 218.17, so the 230 put is worth 11.83 and the
    short loses $0.28 a share. The assignment delivers 100 shares **to** the
    account at 230 -- which, with the exercised call above, is the ~200 NVDA
    shares the account wakes up holding.
    """
    result = build_ledger(
        [
            fill(NVDA_ITM_SHORT_PUT, FillSide.SELL_SHORT, 1, "11.55", when=at(17, 37)),
            option_event("OPASN", NVDA_ITM_SHORT_PUT, "1", when=at(21, 0, 0, day=11)),
        ],
        account=PAPER,
        intents={},
        multipliers=MULTIPLIERS,
        settlements={NVDA_ITM_SHORT_PUT: NVDA_CLOSE},
        contracts=CONTRACTS,
    )
    (trade,) = result.trades
    assert trade.close_kind is CloseKind.ASSIGNMENT
    assert trade.close_price == Decimal("11.83")
    assert trade.pnl == Decimal("-28.00")
    (delivery,) = result.deliveries
    assert delivery.option_type is OptionType.PUT
    assert delivery.shares == Decimal(100)  # assigned short put buys the stock
    assert delivery.price == Decimal("230")


# --------------------------------------------------------------------------
# The deliverable -- on exactly the contract class CLAUDE.md singles out
# --------------------------------------------------------------------------

#: An adjusted root. After a split or special dividend OCC issues a modified
#: root with a numeric suffix and the deliverable is no longer 100 shares of
#: the underlying. ``AAPL1`` is *not* a ticker: the underlying is ``AAPL``.
ADJUSTED_CALL = "AAPL1261218C00150000"

#: What a ``GME1``-style adjusted contract actually delivers: the share leg
#: **plus** something else. No single share count states this, which is the
#: whole reason ``shares`` has to be allowed to say "unknown".
WARRANT_DELIVERABLE = OptionDeliverable(
    type="equity",
    symbol="AAPL.WS",
    amount=Decimal(10),
    allocation_percentage=None,
    settlement_type="PHYS",
    settlement_method="CC",
    delayed_settlement=False,
)


def exercised(
    symbol: str, contracts: Mapping[str, OptionContract] | None = None
) -> Ledger:
    """Buy two contracts of ``symbol``, then exercise them at a 160 settlement."""
    return build_ledger(
        [
            fill(symbol, FillSide.BUY, 2, "4.00", when=at(14), order_id="open"),
            option_event("OPEXC", symbol, "-2", when=at(21)),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers={symbol: Decimal(100)},
        settlements={symbol: Decimal("160.00")},
        contracts=contracts,
    )


def test_an_adjusted_contract_delivers_neither_its_root_nor_a_hundred_shares() -> None:
    """``AAPL1`` is not the underlying and ``qty x multiplier`` is not the count.

    Both halves were wrong at once here. The OCC root is the underlying's
    ticker on a standard contract and is a *modified* root on an adjusted one,
    so reading it as the underlying prints a ticker that does not trade. And
    the deliverable lives in ``deliverables`` -- Alpaca's spec says of ``size``
    that it *"should not be used as a multiplier"*, and using the multiplier as
    a share count is that sentence run backwards. Every live adjusted contract
    still reports ``multiplier: "100"`` while delivering 100 shares **plus**
    something else, so the multiplier cannot detect its own wrongness.
    """
    result = exercised(
        ADJUSTED_CALL,
        contracts={
            ADJUSTED_CALL: contract(
                ADJUSTED_CALL,
                underlying="AAPL",
                deliverables=(equity_deliverable("AAPL", "100"), WARRANT_DELIVERABLE),
            )
        },
    )
    (delivery,) = result.deliveries
    assert delivery.underlying == "AAPL"
    assert delivery.underlying != "AAPL1"
    assert delivery.is_adjusted is True
    # Two deliverables. "200 shares" would be a confident wrong sentence, and
    # the warrants would vanish without trace.
    assert delivery.shares is None


def test_a_share_count_comes_from_the_deliverables_and_not_the_multiplier() -> None:
    """The standard case, stated from the field that actually means it."""
    result = exercised(
        AAPL_CALL,
        contracts={
            AAPL_CALL: contract(
                AAPL_CALL, deliverables=(equity_deliverable("AAPL", "100"),)
            )
        },
    )
    (delivery,) = result.deliveries
    assert delivery.underlying == "AAPL"
    assert delivery.is_adjusted is False
    assert delivery.shares == Decimal(200)


def test_an_adjusted_contract_that_still_delivers_shares_alone_states_them() -> None:
    """Adjusted is not the test; the deliverable is.

    A special cash dividend modifies the root and leaves the deliverable at
    100 shares, so keying "unknown" off :attr:`OptionContract.is_adjusted`
    would refuse to state a count that is perfectly well known. A 3:2 split
    leaves one equity deliverable of **150** shares, which is equally
    statable and equally not 100.
    """
    result = exercised(
        ADJUSTED_CALL,
        contracts={
            ADJUSTED_CALL: contract(
                ADJUSTED_CALL,
                underlying="AAPL",
                deliverables=(equity_deliverable("AAPL", "150"),),
            )
        },
    )
    (delivery,) = result.deliveries
    assert delivery.is_adjusted is True
    assert delivery.shares == Decimal(300)  # 2 contracts x 150, not x 100
    assert delivery.shares != Decimal(200)


def test_deliverables_that_were_not_asked_for_leave_the_count_unknown() -> None:
    """*Unknown* is a third answer and must not collapse into 100.

    ``deliverables`` is empty unless the request carried
    ``show_deliverables=true``, and an empty tuple says nothing about what the
    contract delivers. Answering 100 to a question the data did not answer is
    how an adjusted contract gets sized as a standard one.
    """
    result = exercised(
        AAPL_CALL, contracts={AAPL_CALL: contract(AAPL_CALL)}
    )
    (delivery,) = result.deliveries
    assert delivery.underlying == "AAPL"
    assert delivery.is_adjusted is False
    assert delivery.shares is None


def test_a_delivery_with_no_contract_terms_falls_back_to_the_root_and_says_so() -> None:
    """The same fallback, and the same caveat, as ``grouping.PositionLeg``.

    Without terms the OCC root is all there is, and it is the underlying on
    every standard contract. ``is_adjusted is None`` is what stops a reader
    trusting it: unknown, not "no".
    """
    result = exercised(ADJUSTED_CALL)
    (delivery,) = result.deliveries
    assert delivery.underlying == "AAPL1"
    assert delivery.is_adjusted is None
    assert delivery.shares is None


def test_an_assigned_short_delivers_shares_away_at_the_deliverables_count() -> None:
    """The sign survives reading the count from the deliverables."""
    result = build_ledger(
        [
            fill(ADJUSTED_CALL, FillSide.SELL_SHORT, 1, "4.00", when=at(14)),
            option_event("OPASN", ADJUSTED_CALL, "1", when=at(21)),
        ],
        account=PAPER,
        intents={},
        multipliers={ADJUSTED_CALL: Decimal(100)},
        settlements={ADJUSTED_CALL: Decimal("160.00")},
        contracts={
            ADJUSTED_CALL: contract(
                ADJUSTED_CALL,
                underlying="AAPL",
                deliverables=(equity_deliverable("AAPL", "150"),),
            )
        },
    )
    (delivery,) = result.deliveries
    assert delivery.close_kind is CloseKind.ASSIGNMENT
    assert delivery.underlying == "AAPL"
    assert delivery.shares == Decimal(-150)  # a short call delivers shares away


# --------------------------------------------------------------------------
# The settlement P&L on an adjusted contract -- refused, never estimated
# --------------------------------------------------------------------------


def test_an_exercised_adjusted_contract_books_no_pnl_and_reports_why() -> None:
    """The money path is the one every deliverable test above leaves untouched.

    Everything from ``ADJUSTED_CALL`` down asserts a :class:`ShareDelivery`;
    none of it asserts a ``pnl``. That gap was the defect. An exercise prices
    the close off the **underlying**, and ``(close - open) x qty x multiplier``
    is the contract's settlement value only if the contract delivers
    ``multiplier`` shares of the underlying at the strike. On an adjusted
    contract that premise is false by construction -- and it is the premise
    this module already declines to make in ``_deliverable_shares``.

    The wrong number here would be ``(10.00 - 4.00) x 2 x 100 = +$1,200``,
    booked in silence: ``net_amount`` is never consulted, the multiplier was
    supplied, and every other rule passes.
    """
    result = exercised(
        ADJUSTED_CALL,
        contracts={
            ADJUSTED_CALL: contract(
                ADJUSTED_CALL,
                underlying="AAPL",
                deliverables=(equity_deliverable("AAPL", "150"),),
            )
        },
    )

    assert result.trades == ()
    assert result.realized_pnl == Decimal(0)
    assert RejectionRule.UNVERIFIED_DELIVERABLE.value in rules(result)

    (refusal,) = [
        rejection
        for rejection in result.rejections
        if rejection.rule is RejectionRule.UNVERIFIED_DELIVERABLE
    ]
    # Rule 8: the rule, the inputs and the timestamp, every one of them.
    assert refusal.symbol == ADJUSTED_CALL
    assert refusal.at == at(21)
    assert refusal.inputs["root_symbol"] == "AAPL1"
    assert refusal.inputs["underlying_symbol"] == "AAPL"
    assert refusal.inputs["activity_type"] == "OPEXC"
    assert refusal.inputs["multiplier"] == "100"

    # The shares still happened, and saying so costs nothing: no P&L is
    # derived from a delivery.
    (delivery,) = result.deliveries
    assert delivery.is_adjusted is True
    assert delivery.shares == Decimal(300)

    # And no phantom lot. The contracts really are gone, so reporting them as
    # still held would be its own wrong number, against ``/v2/positions``.
    assert result.open_lots == ()


def test_an_exercise_with_no_contract_terms_is_refused_because_it_cannot_be_asked() -> (
    None
):
    """Unknown is a third answer here too, and must not collapse into "standard".

    ``AAPL230721C00150000`` is a perfectly standard contract -- but without
    the terms there is no way to establish that, because detection is
    ``root_symbol != underlying_symbol`` and that comparison needs both sides.
    Reading "not adjusted" off the absence of an answer is the same shape of
    mistake as defaulting the multiplier to 100.
    """
    result = exercised(AAPL_CALL)

    assert result.trades == ()
    assert RejectionRule.UNVERIFIED_DELIVERABLE.value in rules(result)
    (refusal,) = [
        rejection
        for rejection in result.rejections
        if rejection.rule is RejectionRule.UNVERIFIED_DELIVERABLE
    ]
    assert refusal.symbol == AAPL_CALL
    assert refusal.at == at(21)
    assert refusal.inputs["terms"] == "absent"
    # Not a claim about the root: nobody supplied one to compare against.
    assert refusal.inputs["underlying_symbol"] == ""

    # The delivery is still stated, with both unknowns left unknown.
    (delivery,) = result.deliveries
    assert delivery.is_adjusted is None
    assert delivery.shares is None


def test_an_assigned_adjusted_contract_is_refused_on_the_same_rule() -> None:
    """Assignment prices the close off the underlying exactly as exercise does."""
    result = build_ledger(
        [
            fill(ADJUSTED_CALL, FillSide.SELL_SHORT, 1, "4.00", when=at(14)),
            option_event("OPASN", ADJUSTED_CALL, "1", when=at(21)),
        ],
        account=PAPER,
        intents={},
        multipliers={ADJUSTED_CALL: Decimal(100)},
        settlements={ADJUSTED_CALL: Decimal("160.00")},
        contracts={
            ADJUSTED_CALL: contract(
                ADJUSTED_CALL,
                underlying="AAPL",
                deliverables=(equity_deliverable("AAPL", "150"),),
            )
        },
    )

    assert result.trades == ()
    (refusal,) = [
        rejection
        for rejection in result.rejections
        if rejection.rule is RejectionRule.UNVERIFIED_DELIVERABLE
    ]
    assert refusal.inputs["activity_type"] == "OPASN"
    assert refusal.inputs["close_kind"] == CloseKind.ASSIGNMENT.value
    (delivery,) = result.deliveries
    assert delivery.shares == Decimal(-150)


def test_a_standard_contract_with_terms_still_books_its_exercise() -> None:
    """The permitting half. A refusal that refused everything would also pass.

    Same fill, same settlement, same multiplier as the adjusted case above.
    The only difference is that ``root_symbol == underlying_symbol``, which is
    the spec's detection test and nothing else.
    """
    result = exercised(AAPL_CALL, contracts={AAPL_CALL: standard(AAPL_CALL, "AAPL")})

    (trade,) = result.trades
    assert trade.close_kind is CloseKind.EXERCISE
    assert trade.pnl == Decimal("1200.00")
    assert RejectionRule.UNVERIFIED_DELIVERABLE.value not in rules(result)


def test_a_closing_fill_on_an_adjusted_contract_is_not_refused() -> None:
    """Scope. A premium difference times the multiplier is right on any contract.

    Nothing about this round trip touches the underlying's price or the
    deliverable: 4.00 in, 6.00 out, 2 contracts, 100 a point. Refusing it
    would delete real P&L from the Activity page over a hazard this path does
    not have.
    """
    result = build_ledger(
        [
            fill(ADJUSTED_CALL, FillSide.BUY, 2, "4.00", when=at(14), order_id="open"),
            fill(ADJUSTED_CALL, FillSide.SELL, 2, "6.00", when=at(15)),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers={ADJUSTED_CALL: Decimal(100)},
        contracts={
            ADJUSTED_CALL: contract(
                ADJUSTED_CALL,
                underlying="AAPL",
                deliverables=(equity_deliverable("AAPL", "150"),),
            )
        },
    )

    (trade,) = result.trades
    assert trade.close_kind is CloseKind.FILL
    assert trade.pnl == Decimal("400.00")
    assert RejectionRule.UNVERIFIED_DELIVERABLE.value not in rules(result)


def test_an_otm_expiry_of_an_adjusted_contract_is_not_refused() -> None:
    """Scope, the other half. ``OPEXP`` closes at zero and asks nothing else.

    The loss is the premium paid times the multiplier -- the same arithmetic
    as a closing fill, with a close price of zero. No settlement price and no
    deliverable enters it, so the adjustment cannot make it wrong.
    """
    result = build_ledger(
        [
            fill(ADJUSTED_CALL, FillSide.BUY, 2, "4.00", when=at(14), order_id="open"),
            option_event("OPEXP", ADJUSTED_CALL, "-2", when=at(21)),
        ],
        account=PAPER,
        intents={"open": PositionIntent.BUY_TO_OPEN},
        multipliers={ADJUSTED_CALL: Decimal(100)},
        contracts={
            ADJUSTED_CALL: contract(
                ADJUSTED_CALL,
                underlying="AAPL",
                deliverables=(equity_deliverable("AAPL", "150"),),
            )
        },
    )

    (trade,) = result.trades
    assert trade.close_kind is CloseKind.EXPIRY
    assert trade.pnl == Decimal("-800.00")
    assert RejectionRule.UNVERIFIED_DELIVERABLE.value not in rules(result)
