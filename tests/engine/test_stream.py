"""``engine/stream.py`` -- who gets a websocket slot when there are not enough.

The caps are Alpaca's; the consequence is Corollary's. There are **two** of
them, one per stream: thirty equity symbols and two hundred option quotes on
Basic. An ordinary full book -- eight grouped multi-leg positions, 32 option
symbols and eight underlyings -- fits inside both with room to spare, and
:func:`test_a_full_ordinary_book_fits_both_budgets_whole` is the test that
says so, because the module previously fitted all forty of those symbols into
one budget of thirty and dropped held contracts for a shortage that did not
exist. Scarcity is real further out -- a wide chain view, a long viewport
list, a cap of one in a test -- and these tests pin *which* something loses,
that it is the same something every poll for the same book, and that nothing
is ever dropped quietly.

The same allocation runs at both caps: every budget test below is written
against :data:`EQUITY_STREAM_SYMBOL_CAP`, and a parallel set re-runs the ones
that are about the number against :data:`OPTION_STREAM_QUOTE_CAP`. That the
first set still passes unchanged at 30 is the evidence the two-budget
correction moved the inputs and not the logic.

Four policies are asserted here rather than assumed, because each is a choice
the module states in its docstring and each could defensibly have gone the
other way:

* **A logical position's legs are all-or-nothing.** Three live legs and one
  stale one net to a figure that ticks and is wrong. Both halves of that limit
  are pinned: a unit too wide for the room left is refused whole, *and* a unit
  that exactly fills the last slots is admitted whole.
* **The drop is a strict prefix cut on slot consumption.** Once a unit is
  refused, everything behind it that needs a new symbol is refused too, even
  where it would have fitted in the slots the refused unit left behind.
* **A unit that spends nothing is admitted anyway, drop or no drop.** Two
  logical positions on one contract -- a roll in flight, two lots of a strike
  -- and the second is fully marked, so calling it dropped would misreport a
  live position as stale.
* **``exceeds_cap`` is decided on the unit's own width**, not on what is left
  of it after dedup. The two rules mean different things to an operator: one
  clears when the book shrinks and one never clears.
"""

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

import pytest

from corollary.engine import stream as stream_module
from corollary.engine.stream import (
    EQUITY_STREAM_SYMBOL_CAP,
    OPTION_STREAM_QUOTE_CAP,
    DropRule,
    Stream,
    SubscriptionPlan,
    SubscriptionPriority,
    SubscriptionUnit,
    contract_unit,
    markets_visible_unit,
    plan_subscriptions,
    recommendation_unit,
    underlying_unit,
)

#: A fixed aware instant. The module reads no clock, so the plan's timestamp
#: is whatever the caller hands it and a literal keeps the tests deterministic.
AT = datetime(2026, 9, 14, 13, 31, tzinfo=timezone.utc)

#: One plan is one decision, and this is the handle that says so.
CORRELATION_ID = "poll-0f3a"


def occ(underlying: str, strike: int, option_type: str = "C") -> str:
    """A plausible OCC symbol. Nothing here parses it; it is a stream key."""
    return f"{underlying}261218{option_type}{strike * 1000:08d}"


def singles(count: int, underlying: str = "AAA") -> list[SubscriptionUnit]:
    """``count`` one-leg positions, each its own unit."""
    return [
        contract_unit(f"pos-{index}", [occ(underlying, 100 + index)])
        for index in range(count)
    ]


def plan_for(
    units: Iterable[SubscriptionUnit],
    *,
    cap: int = EQUITY_STREAM_SYMBOL_CAP,
    stream: Stream = Stream.OPTION,
    at: datetime = AT,
    correlation_id: str = CORRELATION_ID,
) -> SubscriptionPlan:
    """:func:`plan_subscriptions` with the audit arguments filled in.

    ``at`` and ``correlation_id`` are required of every caller, and repeating
    the same two literals in thirty calls would bury the thing each test is
    actually about. The tests that are *about* those two arguments call
    :func:`plan_subscriptions` directly, so the requirement itself is still
    exercised rather than papered over here.

    ``stream`` defaults to :attr:`Stream.OPTION` because :func:`singles` and
    :func:`condor` build OCC symbols; the tests whose units are tickers pass
    :attr:`Stream.EQUITY` explicitly, and the tests that are *about* the
    parameter call :func:`plan_subscriptions` directly for the same reason as
    above. ``cap`` stays independent of it: the budget tests below run the
    same allocation at 30 and at 200, and which socket a number came from is
    not what they are measuring.
    """
    return plan_subscriptions(
        units, cap=cap, stream=stream, at=at, correlation_id=correlation_id
    )


# --------------------------------------------------------------------------
# The budget
# --------------------------------------------------------------------------


def test_under_the_cap_nothing_is_dropped() -> None:
    plan = plan_for(singles(5))

    assert len(plan.subscribed) == 5
    assert plan.dropped == ()
    assert plan.not_streamed == 0
    assert plan.message is None
    assert plan.spare_capacity == EQUITY_STREAM_SYMBOL_CAP - 5


def test_exactly_at_the_cap_nothing_is_dropped() -> None:
    """The boundary. A limit that rejects at its own value is off by one."""
    plan = plan_for(singles(EQUITY_STREAM_SYMBOL_CAP))

    assert len(plan.subscribed) == EQUITY_STREAM_SYMBOL_CAP
    assert plan.dropped == ()
    assert plan.not_streamed == 0
    assert plan.spare_capacity == 0


def test_one_over_the_cap_drops_exactly_one_and_says_so() -> None:
    plan = plan_for(singles(EQUITY_STREAM_SYMBOL_CAP + 1))

    assert len(plan.subscribed) == EQUITY_STREAM_SYMBOL_CAP
    assert [dropped.key for dropped in plan.dropped] == [f"pos-{EQUITY_STREAM_SYMBOL_CAP}"]
    assert plan.dropped[0].rule is DropRule.NO_ROOM
    assert plan.not_streamed == 1
    assert plan.message == "1 symbol not streamed"


def test_the_tail_that_drops_is_the_lowest_priority_one() -> None:
    """Contracts outrank recommendations, which outrank viewport rows.

    All four tiers exist on both sockets, but a contract and its *underlying*
    can never contend for the same slot -- the contract is an option symbol
    and the underlying is an equity one, so they are two calls against two
    budgets. A plan orders the tiers that can actually meet inside it, which
    is why the ladder is exercised once per socket rather than once.
    """
    plan = plan_for(
        [
            *singles(3),
            recommendation_unit("rec-1", [occ("ZZZ", 500)]),
            markets_visible_unit(occ("YYY", 700)),
        ],
        cap=4,
    )

    assert plan.subscribed == (
        occ("AAA", 100),
        occ("AAA", 101),
        occ("AAA", 102),
        occ("ZZZ", 500),
    )
    assert [dropped.key for dropped in plan.dropped] == [occ("YYY", 700)]
    assert plan.dropped[0].priority is SubscriptionPriority.MARKETS_VISIBLE


def test_subscribed_symbols_come_back_in_priority_order() -> None:
    """The option socket's ladder, sent in reverse and returned in order."""
    plan = plan_for(
        [
            markets_visible_unit(occ("ZZZ", 500)),
            recommendation_unit("rec-1", [occ("REC", 100)]),
            contract_unit("pos-0", [occ("AAA", 100)]),
        ]
    )

    assert plan.subscribed == (occ("AAA", 100), occ("REC", 100), occ("ZZZ", 500))


def test_a_cap_of_zero_streams_nothing_and_reports_all_of_it() -> None:
    plan = plan_for(singles(2), cap=0)

    assert plan.subscribed == ()
    assert len(plan.dropped) == 2
    assert plan.not_streamed == 2
    assert plan.message == "2 symbols not streamed"


# --------------------------------------------------------------------------
# All-or-nothing legs, and the strict prefix
# --------------------------------------------------------------------------


def condor(underlying: str = "BBB") -> SubscriptionUnit:
    """One four-leg position. Four symbols, one yes-or-no."""
    return contract_unit(
        f"condor-{underlying}",
        [
            occ(underlying, 90, "P"),
            occ(underlying, 95, "P"),
            occ(underlying, 105),
            occ(underlying, 110),
        ],
    )


def test_a_spread_is_dropped_whole_rather_than_split() -> None:
    """Three legs streaming and one polled net a figure that ticks and lies."""
    four_legs = condor()

    plan = plan_for([*singles(2), four_legs], cap=4)

    assert plan.subscribed == (occ("AAA", 100), occ("AAA", 101))
    assert [dropped.key for dropped in plan.dropped] == [four_legs.key]
    assert plan.dropped[0].symbols == four_legs.symbols
    assert plan.not_streamed == 4
    # Two slots the condor could not use. Reported, not quietly backfilled.
    assert plan.spare_capacity == 2


def test_a_spread_that_exactly_fills_the_last_slots_is_admitted_whole() -> None:
    """The permitting half of all-or-nothing, at the boundary.

    Four legs and exactly four slots left. An off-by-one to ``required <
    remaining`` drops a whole spread off the stream and no other test here
    would notice.
    """
    four_legs = condor()

    plan = plan_for([*singles(2), four_legs], cap=6)

    assert plan.dropped == ()
    assert [unit.key for unit in plan.admitted] == [
        "pos-0",
        "pos-1",
        four_legs.key,
    ]
    assert plan.subscribed[2:] == four_legs.symbols
    assert plan.spare_capacity == 0
    assert plan.message is None


def test_a_unit_exactly_as_wide_as_the_cap_is_admitted_on_an_empty_stream() -> None:
    """The other permitting boundary: width == cap is not ``exceeds_cap``."""
    four_legs = condor()

    plan = plan_for([four_legs], cap=4)

    assert plan.subscribed == four_legs.symbols
    assert plan.dropped == ()
    assert plan.spare_capacity == 0


def test_a_spread_wider_than_the_whole_cap_is_its_own_rule() -> None:
    """Not a transient shortage: no book is small enough to make this fit."""
    wide = contract_unit("wide", [occ("CCC", strike) for strike in range(100, 105)])

    plan = plan_for([wide], cap=4)

    assert plan.subscribed == ()
    assert plan.dropped[0].rule is DropRule.EXCEEDS_CAP
    assert plan.dropped[0].width == 5


def test_exceeds_cap_is_decided_on_the_units_own_width_not_what_dedup_left() -> None:
    """A unit that can never fit must not be labelled a transient shortage.

    ``required`` is what this unit still needs *given what is already
    streaming*; two of its five symbols are shared with ``pre``, so it is
    three. Classify on that and the rule reads ``no_room``, whose promise is
    that it clears when the book shrinks -- and this one does not. Remove
    ``pre`` and the unit needs five of four slots, forever. An operator
    alerted on ``exceeds_cap`` would never be paged, and a held position would
    mark at its last known price indefinitely.
    """
    legs = [occ("AAA", 100 + index) for index in range(5)]
    pre = contract_unit("pre", legs[:2])
    wide = contract_unit("wide", legs)

    plan = plan_for([pre, wide], cap=4)

    assert [unit.key for unit in plan.admitted] == ["pre"]
    dropped = plan.dropped[0]
    assert dropped.key == "wide"
    assert dropped.rule is DropRule.EXCEEDS_CAP
    assert dropped.width == 5
    # The situational figure is still reported; it is just not the classifier.
    assert dropped.required == 3
    assert dropped.remaining == 2
    assert plan.dropped_symbols == tuple(legs[2:])


def test_a_unit_that_would_fit_an_empty_stream_is_no_room_not_exceeds_cap() -> None:
    """The other direction, at the boundary: width == cap is transient.

    Same shape as the test above -- a unit sharing symbols with an admitted
    one -- but this one is exactly as wide as the whole budget. Drop ``pre``
    and it fits, so the shortage really does clear and ``no_room`` is the
    honest rule.
    """
    legs = [occ("AAA", 100 + index) for index in range(5)]
    pre = contract_unit("pre", legs[:2])
    wide = contract_unit("wide", [legs[0], *legs[2:]])

    plan = plan_for([pre, wide], cap=4)

    dropped = plan.dropped[0]
    assert dropped.key == "wide"
    assert dropped.rule is DropRule.NO_ROOM
    assert dropped.width == 4
    assert dropped.required == 3


def test_admission_stops_at_the_first_drop() -> None:
    """A strict prefix: nothing behind a refused unit slips into its slots."""
    four_legs = condor()

    plan = plan_for([*singles(2), four_legs, *singles(1, "DDD")], cap=4)

    assert plan.subscribed == (occ("AAA", 100), occ("AAA", 101))
    rules = {dropped.key: dropped.rule for dropped in plan.dropped}
    assert rules == {four_legs.key: DropRule.NO_ROOM, "pos-0": DropRule.BEHIND_A_DROP}


def test_the_dropped_list_is_never_truncated() -> None:
    plan = plan_for(singles(EQUITY_STREAM_SYMBOL_CAP + 25))

    assert len(plan.dropped) == 25
    assert plan.not_streamed == 25


# --------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------


def test_an_underlying_requested_twice_consumes_one_slot() -> None:
    plan = plan_for(
        [underlying_unit("AAA"), underlying_unit("AAA"), underlying_unit("BBB")],
        stream=Stream.EQUITY,
    )

    assert plan.subscribed == ("AAA", "BBB")
    assert plan.dropped == ()


def test_dedup_across_tiers_does_not_disturb_priority_order() -> None:
    """A recommendation naming a held contract is free, and stays last.

    The recommendation carries option symbols only. It named its underlying
    too until the two budgets were separated, and that is now a refusal: one
    unit cannot straddle two streams and still be all-or-nothing. Phase 4
    builds a recommendation as an option unit plus an equity unit, and the
    units it shares this plan with are the ones that share its *socket* --
    the held contract above it and a chain row below.
    """
    held = occ("AAA", 100)
    plan = plan_for(
        [
            contract_unit("pos-0", [held]),
            markets_visible_unit(occ("AAA", 110)),
            recommendation_unit("rec-1", [held, occ("AAA", 105)]),
        ],
        cap=3,
    )

    assert plan.subscribed == (held, occ("AAA", 105), occ("AAA", 110))
    assert plan.dropped == ()


def test_a_symbol_already_streaming_is_not_counted_as_not_streamed() -> None:
    """The UI's N is distinct symbols missing, not dropped-unit arithmetic."""
    held = occ("AAA", 100)
    fresh = occ("AAA", 105)
    plan = plan_for(
        [
            contract_unit("pos-0", [held]),
            recommendation_unit("rec-1", [held, fresh]),
        ],
        cap=1,
    )

    assert plan.subscribed == (held,)
    assert plan.dropped[0].key == "rec-1"
    # `held` is streaming; only the second contract is missing.
    assert plan.not_streamed == 1
    assert plan.dropped_symbols == (fresh,)


def test_a_repeated_symbol_inside_one_unit_costs_one_slot() -> None:
    leg = occ("AAA", 100)
    plan = plan_for([contract_unit("pos-0", [leg, leg])], cap=1)

    assert plan.subscribed == (leg,)
    assert plan.dropped == ()


def test_a_unit_needing_no_new_symbols_is_admitted_behind_a_drop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Zero-cost admission survives the prefix cut, because it spends nothing.

    Two logical positions on the same contract -- a roll in flight, or two
    lots of one strike -- with a drop between them. ``pos-C``'s only symbol is
    already streaming, so it is fully marked; recording it as refused would
    tell a caller asking *"which positions are marked live?"* that a live one
    is not, and would emit a warning about a position nothing is wrong with.
    """
    with caplog.at_level(logging.WARNING, logger="corollary.engine.stream"):
        plan = plan_for(
            [
                contract_unit("pos-A", [occ("AAA", 100)]),
                contract_unit("pos-B", [occ("AAA", 105)]),
                contract_unit("pos-C", [occ("AAA", 100)]),
            ],
            cap=1,
        )

    assert plan.subscribed == (occ("AAA", 100),)
    assert [unit.key for unit in plan.admitted] == ["pos-A", "pos-C"]
    assert [dropped.key for dropped in plan.dropped] == ["pos-B"]
    assert plan.dropped_symbols == (occ("AAA", 105),)
    assert plan.not_streamed == 1

    drops = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "stream_subscription_dropped"
    ]
    assert [record.key for record in drops] == ["pos-B"]


# --------------------------------------------------------------------------
# Determinism -- the scanner standard, applied to a subscription list
# --------------------------------------------------------------------------


def test_identical_inputs_give_identical_output() -> None:
    """A plan that reshuffles between polls churns the socket for nothing."""
    units = [
        *singles(20),
        markets_visible_unit(occ("MKT", 1)),
        markets_visible_unit(occ("MKT", 2)),
        *[
            recommendation_unit(f"rec-{index}", [occ("ZZZ", index)])
            for index in range(15)
        ],
    ]

    first = plan_for(units)
    second = plan_for(list(units))

    assert first == second
    assert first.subscribed == second.subscribed
    assert first.dropped == second.dropped


def test_equal_priority_keeps_the_callers_order() -> None:
    forward = plan_for(
        [underlying_unit("AAA"), underlying_unit("BBB")],
        cap=1,
        stream=Stream.EQUITY,
    )
    reverse = plan_for(
        [underlying_unit("BBB"), underlying_unit("AAA")],
        cap=1,
        stream=Stream.EQUITY,
    )

    assert forward.subscribed == ("AAA",)
    assert reverse.subscribed == ("BBB",)


# --------------------------------------------------------------------------
# Rule 8 -- the rule, the inputs, the timestamp
# --------------------------------------------------------------------------


def test_every_drop_is_logged_with_its_rule_and_its_inputs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="corollary.engine.stream"):
        plan = plan_for(singles(2), cap=1)

    drops = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "stream_subscription_dropped"
    ]
    assert len(drops) == len(plan.dropped) == 1
    assert drops[0].rule == DropRule.NO_ROOM.value
    assert drops[0].key == "pos-1"
    assert drops[0].symbols == [occ("AAA", 101)]
    assert drops[0].cap == 1
    assert drops[0].required == 1
    assert drops[0].width == 1
    assert drops[0].remaining == 0
    assert drops[0].priority == SubscriptionPriority.POSITION_CONTRACT.label

    summary = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "stream_subscription_budget_exceeded"
    ]
    assert len(summary) == 1
    assert summary[0].not_streamed == 1


def test_a_clean_plan_logs_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="corollary.engine.stream"):
        plan_for(singles(2))

    assert caplog.records == []


def test_the_timestamp_reaches_the_plan_the_drop_and_both_log_records(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The third of rule 8 that is easiest to leave unexercised.

    A drop record is read back later against a fill, to decide whether a mark
    was stale at the time. Without the stamp on every one of these surfaces
    that question has no answer.
    """
    with caplog.at_level(logging.WARNING, logger="corollary.engine.stream"):
        plan = plan_subscriptions(
            singles(2),
            cap=1,
            stream=Stream.OPTION,
            at=AT,
            correlation_id=CORRELATION_ID,
        )

    assert plan.at == AT
    assert plan.dropped[0].at == AT

    stamped = {
        getattr(record, "event", ""): getattr(record, "at", None)
        for record in caplog.records
    }
    assert stamped == {
        "stream_subscription_dropped": AT.isoformat(),
        "stream_subscription_budget_exceeded": AT.isoformat(),
    }


def test_an_aware_timestamp_is_normalised_to_utc() -> None:
    """Stored UTC, displayed Eastern -- the boundary is here, not the reader."""
    eastern = AT.astimezone(timezone(timedelta(hours=-4)))

    plan = plan_subscriptions(
        singles(2),
        cap=1,
        stream=Stream.OPTION,
        at=eastern,
        correlation_id=CORRELATION_ID,
    )

    assert plan.at == AT
    assert plan.at.utcoffset() == timedelta(0)
    assert plan.dropped[0].at.isoformat() == AT.isoformat()


def test_a_naive_timestamp_is_refused() -> None:
    """Coercing one would read it as this machine's local time, silently."""
    with pytest.raises(ValueError, match="timezone-aware"):
        plan_subscriptions(
            singles(1),
            at=datetime(2026, 9, 13, 9, 31),
            correlation_id=CORRELATION_ID,
            cap=EQUITY_STREAM_SYMBOL_CAP,
            stream=Stream.OPTION,
        )


def test_the_timestamp_is_required() -> None:
    """No default, because a rule-8 record with no timestamp is incomplete."""
    with pytest.raises(TypeError):
        plan_subscriptions(  # type: ignore[call-arg]
            singles(1),
            correlation_id=CORRELATION_ID,
            cap=1,
            stream=Stream.OPTION,
        )


def test_every_record_of_one_plan_carries_the_same_correlation_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """N drops and a summary are one decision, and say so.

    A 2s Markets poll and a 400ms tick can both re-plan, so drop lines from
    different plans interleave in the log with nothing to attribute them.
    """
    with caplog.at_level(logging.WARNING, logger="corollary.engine.stream"):
        plan = plan_subscriptions(
            singles(3),
            cap=1,
            stream=Stream.OPTION,
            at=AT,
            correlation_id=CORRELATION_ID,
        )

    assert plan.correlation_id == CORRELATION_ID
    assert len(caplog.records) == len(plan.dropped) + 1
    assert [record.correlation_id for record in caplog.records] == [
        CORRELATION_ID
    ] * len(caplog.records)


def test_the_correlation_id_is_required() -> None:
    with pytest.raises(TypeError):
        plan_subscriptions(  # type: ignore[call-arg]
            singles(1), at=AT, cap=1, stream=Stream.OPTION
        )


def test_an_empty_correlation_id_is_refused() -> None:
    """A field that correlates nothing is the untraceable plan, with a name."""
    with pytest.raises(ValueError, match="correlation id"):
        plan_subscriptions(
            singles(1),
            at=AT,
            correlation_id="",
            cap=EQUITY_STREAM_SYMBOL_CAP,
            stream=Stream.OPTION,
        )


# --------------------------------------------------------------------------
# Refusals -- a malformed request is a caller bug, not a silent no-op
# --------------------------------------------------------------------------


def test_a_negative_cap_is_refused() -> None:
    with pytest.raises(ValueError, match="cap"):
        plan_for(singles(1), cap=-1)


def test_a_unit_with_no_symbols_is_refused() -> None:
    with pytest.raises(ValueError, match="no symbols"):
        plan_for([contract_unit("pos-0", [])])


def test_an_empty_symbol_is_refused() -> None:
    with pytest.raises(ValueError, match="empty symbol"):
        plan_for([contract_unit("pos-0", ["AAA", ""])])


# --------------------------------------------------------------------------
# The default the helper cannot see -- and why there is no longer one
# --------------------------------------------------------------------------


def test_the_cap_is_required_because_neither_stream_is_the_default_one() -> None:
    """``cap`` has no default at all, and that is the fix for this bug's shape.

    Every other test here goes through :func:`plan_for`, which declares its own
    ``cap`` default and *always forwards it explicitly* -- so no test in this
    file exercises the signature's own default, and a wrong one would leave the
    whole suite green.

    It used to default to the single ``STREAM_SYMBOL_CAP``, and the direction
    that costs money is now *downward*: an option-stream caller who omits
    ``cap`` would silently plan 200 contracts against a budget of 30, drop 170
    of them with ``no_room``, and report held positions as unstreamed while
    the option socket sat 170 quotes under its real limit. Held contracts
    marking at a last known price is the exact failure the module opens by
    naming.

    A default is a hidden choice of *which stream*, and there is no defensible
    answer, so the parameter is required and the choice is made at the call
    site where the stream is known.
    """
    with pytest.raises(TypeError):
        plan_subscriptions(  # type: ignore[call-arg]
            singles(2), at=AT, correlation_id=CORRELATION_ID, stream=Stream.OPTION
        )


def test_no_unqualified_cap_name_survives() -> None:
    """The bug's habitat, asserted gone rather than remembered.

    One module-level ``STREAM_SYMBOL_CAP`` is what let a single number stand
    for two different budgets. Re-adding it as an alias would compile, pass
    every other test here, and restore the ambiguity, so the absence is
    pinned.
    """
    assert not hasattr(stream_module, "STREAM_SYMBOL_CAP")
    assert "STREAM_SYMBOL_CAP" not in stream_module.__all__
    assert EQUITY_STREAM_SYMBOL_CAP == 30
    assert OPTION_STREAM_QUOTE_CAP == 200


# --------------------------------------------------------------------------
# The second budget: the option stream's 200 quotes
# --------------------------------------------------------------------------

#: ``max_concurrent_positions`` from CLAUDE.md rule 4 -- the book this account
#: actually runs, not a hypothetical large one.
BOOK_POSITIONS = 8


def full_ordinary_book() -> tuple[list[SubscriptionUnit], list[SubscriptionUnit]]:
    """Eight grouped four-leg positions, split the way the streams are.

    32 option symbols and the 8 underlyings behind them: the *full but
    ordinary* book the module's docstring reasons about, built once so both
    budgets are measured against the same thing.
    """
    roots = [chr(ord("A") + index) * 3 for index in range(BOOK_POSITIONS)]
    return [condor(root) for root in roots], [underlying_unit(root) for root in roots]


def test_a_full_ordinary_book_fits_both_budgets_whole() -> None:
    """The behaviour change the two-budget correction exists for.

    Planned against one budget of thirty this book dropped held contracts and
    surfaced *"N symbols not streamed"* for slots that were never contended.
    Planned against the two real budgets it drops nothing, and both streams
    finish with most of their capacity unspent.
    """
    contracts, underlyings = full_ordinary_book()

    options = plan_for(contracts, cap=OPTION_STREAM_QUOTE_CAP, stream=Stream.OPTION)
    equities = plan_for(
        underlyings, cap=EQUITY_STREAM_SYMBOL_CAP, stream=Stream.EQUITY
    )

    assert len(options.subscribed) == BOOK_POSITIONS * 4 == 32
    assert options.dropped == ()
    assert options.not_streamed == 0
    assert options.message is None
    assert options.spare_capacity == OPTION_STREAM_QUOTE_CAP - 32 == 168

    assert len(equities.subscribed) == BOOK_POSITIONS == 8
    assert equities.dropped == ()
    assert equities.not_streamed == 0
    assert equities.message is None
    assert equities.spare_capacity == EQUITY_STREAM_SYMBOL_CAP - 8 == 22


def test_the_shared_pool_this_replaces_can_no_longer_even_be_asked_for() -> None:
    """The bug's own shape, refused at the door.

    The old arithmetic put all forty of a full book's symbols -- 32 contracts
    and the 8 underlyings behind them -- into one budget of thirty. That call
    is what this test used to make. It cannot be made any more: one plan is
    one socket, and a list holding both kinds of unit is a caller bug now
    rather than a shortage, which is the strongest form the correction takes.
    """
    contracts, underlyings = full_ordinary_book()

    with pytest.raises(ValueError, match="option stream"):
        plan_for([*contracts, *underlyings], cap=EQUITY_STREAM_SYMBOL_CAP)


def test_the_single_budget_this_replaces_would_have_dropped_held_contracts() -> None:
    """What the wrong number cost, pinned so the correction cannot be undone.

    Forty symbols in a budget of thirty, which is what an ordinary full book
    was measured against: ten over, and **twelve** unstreamed rather than ten,
    because a unit is refused whole and spending stops at the first refusal.
    The prefix cut lands on a position *contract* -- the tier that must never
    go unmarked -- and two slots are stranded behind it. Every one of those
    twelve had an unused option slot waiting for it.

    The forty are all option symbols here, since the real mix of contracts and
    underlyings is now two calls and the test above pins that. The allocation
    is byte-identical to the one the two real budgets run; only the number
    handed to it differs, which is the whole claim of this step.
    """
    roots = [chr(ord("A") + index) * 3 for index in range(BOOK_POSITIONS)]
    spreads = [condor(root) for root in roots]
    tail = singles(BOOK_POSITIONS, "ZZZ")
    assert sum(len(unit.symbols) for unit in [*spreads, *tail]) == 40

    shared = plan_for([*spreads, *tail], cap=EQUITY_STREAM_SYMBOL_CAP)

    assert len(shared.subscribed) == 28
    assert shared.spare_capacity == 2
    assert shared.not_streamed == 12
    assert shared.message == "12 symbols not streamed"
    assert {dropped.priority for dropped in shared.dropped} == {
        SubscriptionPriority.POSITION_CONTRACT
    }


def test_exactly_at_the_option_cap_nothing_is_dropped() -> None:
    """The boundary at the second cap. A limit rejecting its own value is off by one."""
    plan = plan_for(singles(OPTION_STREAM_QUOTE_CAP), cap=OPTION_STREAM_QUOTE_CAP)

    assert len(plan.subscribed) == OPTION_STREAM_QUOTE_CAP
    assert plan.dropped == ()
    assert plan.not_streamed == 0
    assert plan.spare_capacity == 0


def test_one_over_the_option_cap_drops_exactly_one_and_says_so() -> None:
    plan = plan_for(singles(OPTION_STREAM_QUOTE_CAP + 1), cap=OPTION_STREAM_QUOTE_CAP)

    assert len(plan.subscribed) == OPTION_STREAM_QUOTE_CAP
    assert [dropped.key for dropped in plan.dropped] == [
        f"pos-{OPTION_STREAM_QUOTE_CAP}"
    ]
    assert plan.dropped[0].rule is DropRule.NO_ROOM
    assert plan.dropped[0].cap == OPTION_STREAM_QUOTE_CAP
    assert plan.not_streamed == 1
    assert plan.message == "1 symbol not streamed"


def test_a_spread_is_dropped_whole_at_the_option_cap_too() -> None:
    """All-or-nothing is a property of the unit, not of the number beside it."""
    plan = plan_for(
        [*singles(OPTION_STREAM_QUOTE_CAP - 2), condor()],
        cap=OPTION_STREAM_QUOTE_CAP,
    )

    assert len(plan.subscribed) == OPTION_STREAM_QUOTE_CAP - 2
    assert [dropped.key for dropped in plan.dropped] == ["condor-BBB"]
    assert plan.dropped[0].rule is DropRule.NO_ROOM
    assert plan.spare_capacity == 2


def test_a_spread_filling_the_last_option_slots_exactly_is_admitted() -> None:
    """The permitting half of the same boundary."""
    plan = plan_for(
        [*singles(OPTION_STREAM_QUOTE_CAP - 4), condor()],
        cap=OPTION_STREAM_QUOTE_CAP,
    )

    assert len(plan.subscribed) == OPTION_STREAM_QUOTE_CAP
    assert plan.dropped == ()
    assert plan.spare_capacity == 0


# --------------------------------------------------------------------------
# A unit belongs to one stream -- mixing them cannot be all-or-nothing
# --------------------------------------------------------------------------


def test_a_unit_mixing_option_and_equity_symbols_is_refused() -> None:
    """Half admitted and half dropped is the three-legs-live failure, again.

    Two budgets are two calls, so a unit whose symbols land in both cannot be
    answered yes-or-no by either one. It is a caller bug, refused like an
    empty unit rather than silently split down the middle.
    """
    with pytest.raises(ValueError, match="two streams"):
        plan_for([recommendation_unit("rec-1", [occ("AAA", 100), "AAA"])])


def test_the_mixed_unit_refusal_does_not_depend_on_the_order_of_the_symbols() -> None:
    with pytest.raises(ValueError, match="two streams"):
        plan_for([recommendation_unit("rec-1", ["AAA", occ("AAA", 100)])])


def test_a_mixed_unit_is_refused_before_any_allocation_happens() -> None:
    """Validation precedes the budget, so a cap of zero still raises.

    A unit that would have been dropped anyway is still a caller bug, and
    finding out only when the book is small enough to contend is how this
    lands in production instead of in a test.
    """
    with pytest.raises(ValueError, match="two streams"):
        plan_for([contract_unit("pos-0", [occ("AAA", 100), "AAA"])], cap=0)


def test_the_honest_constructors_cannot_build_a_mixed_unit() -> None:
    """``contract_unit`` and ``underlying_unit`` satisfy the rule by construction."""
    contracts, underlyings = full_ordinary_book()

    options = plan_for(contracts, cap=OPTION_STREAM_QUOTE_CAP, stream=Stream.OPTION)
    equities = plan_for(
        underlyings, cap=EQUITY_STREAM_SYMBOL_CAP, stream=Stream.EQUITY
    )

    assert options.dropped == ()
    assert equities.dropped == ()


def test_an_adjusted_contract_root_is_still_an_option_symbol() -> None:
    """``AAPL1`` deliverables are not 100 shares, and are not equity tickers either."""
    adjusted = "AAPL1261218C00150000"

    plan = plan_for([contract_unit("pos-0", [adjusted])], cap=1)

    assert plan.subscribed == (adjusted,)
    with pytest.raises(ValueError, match="two streams"):
        plan_for([contract_unit("pos-1", [adjusted, "AAPL"])], cap=2)


# --------------------------------------------------------------------------
# The viewport hint -- the lowest tier, and it can never be anything else
# --------------------------------------------------------------------------


def test_markets_visible_is_the_lowest_priority_there_is() -> None:
    assert SubscriptionPriority.MARKETS_VISIBLE > SubscriptionPriority.RECOMMENDED_TRADE
    assert max(SubscriptionPriority) is SubscriptionPriority.MARKETS_VISIBLE
    assert SubscriptionPriority.MARKETS_VISIBLE.label == "markets_visible"


def test_a_viewport_row_can_never_evict_a_held_contract() -> None:
    """Rule 4's principle, applied to a stream budget rather than a limit.

    The list is the client's, so a client that could outrank a position
    contract could make a held position mark stale by scrolling. The unit is
    ordered last whatever order the caller sends it in.
    """
    held = occ("AAA", 100)
    on_screen = occ("ZZZ", 500)

    plan = plan_for(
        [markets_visible_unit(on_screen), contract_unit("pos-0", [held])], cap=1
    )

    assert plan.subscribed == (held,)
    assert [dropped.key for dropped in plan.dropped] == [on_screen]
    assert plan.dropped[0].priority is SubscriptionPriority.MARKETS_VISIBLE


def test_the_viewport_hint_sits_behind_a_phase_4_recommendation_too() -> None:
    """The equity socket's ladder: a position's underlying, then the
    underlying a Phase 4 recommendation names, then a row on screen. The
    contract tier is absent because a contract is an option symbol, and the
    other call plans those."""
    plan = plan_for(
        [
            markets_visible_unit("ZZZ"),
            recommendation_unit("rec-1", ["REC"]),
            underlying_unit("AAA"),
        ],
        stream=Stream.EQUITY,
    )

    assert plan.subscribed == ("AAA", "REC", "ZZZ")


# --------------------------------------------------------------------------
# Which socket this plan is for -- stated, not inferred from the cap
# --------------------------------------------------------------------------


def test_the_stream_is_required_because_a_default_is_a_silent_choice_of_socket() -> None:
    """Same reasoning as ``cap``, one field over: neither stream is the default.

    A plan carries two things about the socket it is for -- how many slots it
    has and which socket it is -- and a default for either is a silent choice.
    The cap being wrong under-spends a budget; the stream being wrong admits
    symbols the socket will never quote at all.
    """
    with pytest.raises(TypeError):
        plan_subscriptions(  # type: ignore[call-arg]
            singles(2), at=AT, correlation_id=CORRELATION_ID, cap=1
        )


def test_the_plan_says_which_socket_it_is_for() -> None:
    options = plan_for(singles(1), stream=Stream.OPTION)
    equities = plan_for([underlying_unit("AAA")], stream=Stream.EQUITY)

    assert options.stream is Stream.OPTION
    assert equities.stream is Stream.EQUITY


def test_option_units_filed_under_the_equity_stream_are_refused() -> None:
    """The failure this parameter exists for: a whole list on the wrong socket.

    Five four-leg spreads handed to the equity budget fit inside thirty slots
    and were *admitted* -- twenty OCC symbols returned as the stock socket's
    subscription list, with ``not_streamed == 0`` and no banner. Alpaca's
    equity stream never quotes them, so every leg marks at its last known
    price and the plan affirmatively says nothing is missing. That is worse
    than the mixed unit refused above, which at least emits drop records.
    """
    contracts = [condor(chr(ord("A") + index) * 3) for index in range(5)]

    with pytest.raises(ValueError, match="equity stream"):
        plan_subscriptions(
            contracts,
            at=AT,
            correlation_id=CORRELATION_ID,
            cap=EQUITY_STREAM_SYMBOL_CAP,
            stream=Stream.EQUITY,
        )


def test_equity_units_filed_under_the_option_stream_are_refused() -> None:
    """The likelier form is the two lists swapped, and it is caught both ways."""
    with pytest.raises(ValueError, match="option stream"):
        plan_subscriptions(
            [underlying_unit("AAA"), underlying_unit("BBB")],
            at=AT,
            correlation_id=CORRELATION_ID,
            cap=OPTION_STREAM_QUOTE_CAP,
            stream=Stream.OPTION,
        )


def test_the_wrong_stream_is_refused_before_any_allocation_happens() -> None:
    """Validation precedes the budget, exactly as it does for a mixed unit.

    A cap of zero would have dropped everything anyway, and finding out only
    when the book is small enough to contend is how this reaches production.
    """
    with pytest.raises(ValueError, match="equity stream"):
        plan_subscriptions(
            singles(1),
            at=AT,
            correlation_id=CORRELATION_ID,
            cap=0,
            stream=Stream.EQUITY,
        )


def test_a_wrongly_filed_unit_is_named_along_with_its_symbols() -> None:
    """Rule 8's shape for a caller bug: which unit, which symbols, which socket."""
    with pytest.raises(ValueError) as excinfo:
        plan_for([contract_unit("pos-7", [occ("AAA", 100)])], stream=Stream.EQUITY)

    message = str(excinfo.value)
    assert "pos-7" in message
    assert occ("AAA", 100) in message


def test_an_adjusted_root_is_still_refused_on_the_equity_stream() -> None:
    """``AAPL1`` is an option symbol whatever its deliverable is."""
    with pytest.raises(ValueError, match="equity stream"):
        plan_for(
            [contract_unit("pos-0", ["AAPL1261218C00150000"])], stream=Stream.EQUITY
        )


def test_every_drop_record_names_the_socket_that_ran_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule 8's inputs include *which* budget, not just how big it was.

    Two plans share one correlation id, so without this the only way to tell
    an option drop from an equity one is to recognise 30 versus 200 -- which
    is inference from a number, and it collapses entirely if the two caps ever
    coincide.
    """
    with caplog.at_level(logging.WARNING, logger="corollary.engine.stream"):
        plan_subscriptions(
            [underlying_unit("AAA"), underlying_unit("BBB")],
            at=AT,
            correlation_id=CORRELATION_ID,
            cap=1,
            stream=Stream.EQUITY,
        )

    labelled = {
        getattr(record, "event", ""): getattr(record, "stream", None)
        for record in caplog.records
    }
    assert labelled == {
        "stream_subscription_dropped": "equity",
        "stream_subscription_budget_exceeded": "equity",
    }


def test_the_option_socket_labels_its_own_drops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="corollary.engine.stream"):
        plan_for(singles(2), cap=1, stream=Stream.OPTION)

    assert [getattr(record, "stream", None) for record in caplog.records] == [
        "option",
        "option",
    ]
