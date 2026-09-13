"""``engine/stream.py`` -- who gets one of the thirty websocket slots.

The cap is Alpaca's; the consequence is Corollary's. Every option contract is
its own stream symbol, so eight grouped multi-leg positions reach 32 symbols
before a single underlying is counted. Something has to lose, and these tests
pin *which* something, that it is the same something every poll for the same
book, and that nothing is ever dropped quietly.

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

from corollary.engine.stream import (
    STREAM_SYMBOL_CAP,
    DropRule,
    SubscriptionPlan,
    SubscriptionPriority,
    SubscriptionUnit,
    contract_unit,
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
    cap: int = STREAM_SYMBOL_CAP,
    at: datetime = AT,
    correlation_id: str = CORRELATION_ID,
) -> SubscriptionPlan:
    """:func:`plan_subscriptions` with the audit arguments filled in.

    ``at`` and ``correlation_id`` are required of every caller, and repeating
    the same two literals in thirty calls would bury the thing each test is
    actually about. The tests that are *about* those two arguments call
    :func:`plan_subscriptions` directly, so the requirement itself is still
    exercised rather than papered over here.
    """
    return plan_subscriptions(
        units, cap=cap, at=at, correlation_id=correlation_id
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
    assert plan.spare_capacity == STREAM_SYMBOL_CAP - 5


def test_exactly_at_the_cap_nothing_is_dropped() -> None:
    """The boundary. A limit that rejects at its own value is off by one."""
    plan = plan_for(singles(STREAM_SYMBOL_CAP))

    assert len(plan.subscribed) == STREAM_SYMBOL_CAP
    assert plan.dropped == ()
    assert plan.not_streamed == 0
    assert plan.spare_capacity == 0


def test_one_over_the_cap_drops_exactly_one_and_says_so() -> None:
    plan = plan_for(singles(STREAM_SYMBOL_CAP + 1))

    assert len(plan.subscribed) == STREAM_SYMBOL_CAP
    assert [dropped.key for dropped in plan.dropped] == [f"pos-{STREAM_SYMBOL_CAP}"]
    assert plan.dropped[0].rule is DropRule.NO_ROOM
    assert plan.not_streamed == 1
    assert plan.message == "1 symbol not streamed"


def test_the_tail_that_drops_is_the_lowest_priority_one() -> None:
    """Contracts outrank underlyings, which outrank Phase 4 recommendations."""
    plan = plan_for(
        [
            *singles(3),
            underlying_unit("AAA"),
            recommendation_unit("rec-1", [occ("ZZZ", 500)]),
        ],
        cap=4,
    )

    assert plan.subscribed == (
        occ("AAA", 100),
        occ("AAA", 101),
        occ("AAA", 102),
        "AAA",
    )
    assert [dropped.key for dropped in plan.dropped] == ["rec-1"]
    assert plan.dropped[0].priority is SubscriptionPriority.RECOMMENDED_TRADE


def test_subscribed_symbols_come_back_in_priority_order() -> None:
    plan = plan_for(
        [
            recommendation_unit("rec-1", ["REC"]),
            underlying_unit("AAA"),
            contract_unit("pos-0", [occ("AAA", 100)]),
        ]
    )

    assert plan.subscribed == (occ("AAA", 100), "AAA", "REC")


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
    pre = contract_unit("pre", ["S0", "S1"])
    wide = contract_unit("wide", ["S0", "S1", "S2", "S3", "S4"])

    plan = plan_for([pre, wide], cap=4)

    assert [unit.key for unit in plan.admitted] == ["pre"]
    dropped = plan.dropped[0]
    assert dropped.key == "wide"
    assert dropped.rule is DropRule.EXCEEDS_CAP
    assert dropped.width == 5
    # The situational figure is still reported; it is just not the classifier.
    assert dropped.required == 3
    assert dropped.remaining == 2
    assert plan.dropped_symbols == ("S2", "S3", "S4")


def test_a_unit_that_would_fit_an_empty_stream_is_no_room_not_exceeds_cap() -> None:
    """The other direction, at the boundary: width == cap is transient.

    Same shape as the test above -- a unit sharing symbols with an admitted
    one -- but this one is exactly as wide as the whole budget. Drop ``pre``
    and it fits, so the shortage really does clear and ``no_room`` is the
    honest rule.
    """
    pre = contract_unit("pre", ["S0", "S1"])
    wide = contract_unit("wide", ["S0", "S2", "S3", "S4"])

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
    plan = plan_for(singles(STREAM_SYMBOL_CAP + 25))

    assert len(plan.dropped) == 25
    assert plan.not_streamed == 25


# --------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------


def test_an_underlying_requested_twice_consumes_one_slot() -> None:
    plan = plan_for(
        [underlying_unit("AAA"), underlying_unit("AAA"), underlying_unit("BBB")]
    )

    assert plan.subscribed == ("AAA", "BBB")
    assert plan.dropped == ()


def test_dedup_across_tiers_does_not_disturb_priority_order() -> None:
    """A recommendation naming a held contract is free, and stays last."""
    held = occ("AAA", 100)
    plan = plan_for(
        [
            contract_unit("pos-0", [held]),
            underlying_unit("AAA"),
            recommendation_unit("rec-1", [held, "AAA", occ("AAA", 105)]),
        ],
        cap=3,
    )

    assert plan.subscribed == (held, "AAA", occ("AAA", 105))
    assert plan.dropped == ()


def test_a_symbol_already_streaming_is_not_counted_as_not_streamed() -> None:
    """The UI's N is distinct symbols missing, not dropped-unit arithmetic."""
    held = occ("AAA", 100)
    plan = plan_for(
        [
            contract_unit("pos-0", [held]),
            recommendation_unit("rec-1", [held, "NEW"]),
        ],
        cap=1,
    )

    assert plan.subscribed == (held,)
    assert plan.dropped[0].key == "rec-1"
    # `held` is streaming; only NEW is missing.
    assert plan.not_streamed == 1
    assert plan.dropped_symbols == ("NEW",)


def test_a_repeated_symbol_inside_one_unit_costs_one_slot() -> None:
    plan = plan_for([contract_unit("pos-0", ["AAA", "AAA"])], cap=1)

    assert plan.subscribed == ("AAA",)
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
                contract_unit("pos-A", ["X"]),
                contract_unit("pos-B", ["Y"]),
                contract_unit("pos-C", ["X"]),
            ],
            cap=1,
        )

    assert plan.subscribed == ("X",)
    assert [unit.key for unit in plan.admitted] == ["pos-A", "pos-C"]
    assert [dropped.key for dropped in plan.dropped] == ["pos-B"]
    assert plan.dropped_symbols == ("Y",)
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
        underlying_unit("AAA"),
        underlying_unit("BBB"),
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
    forward = plan_for([underlying_unit("AAA"), underlying_unit("BBB")], cap=1)
    reverse = plan_for([underlying_unit("BBB"), underlying_unit("AAA")], cap=1)

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
            singles(2), cap=1, at=AT, correlation_id=CORRELATION_ID
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
        singles(2), cap=1, at=eastern, correlation_id=CORRELATION_ID
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
        )


def test_the_timestamp_is_required() -> None:
    """No default, because a rule-8 record with no timestamp is incomplete."""
    with pytest.raises(TypeError):
        plan_subscriptions(singles(1), correlation_id=CORRELATION_ID)  # type: ignore[call-arg]


def test_every_record_of_one_plan_carries_the_same_correlation_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """N drops and a summary are one decision, and say so.

    A 2s Markets poll and a 400ms tick can both re-plan, so drop lines from
    different plans interleave in the log with nothing to attribute them.
    """
    with caplog.at_level(logging.WARNING, logger="corollary.engine.stream"):
        plan = plan_subscriptions(
            singles(3), cap=1, at=AT, correlation_id=CORRELATION_ID
        )

    assert plan.correlation_id == CORRELATION_ID
    assert len(caplog.records) == len(plan.dropped) + 1
    assert [record.correlation_id for record in caplog.records] == [
        CORRELATION_ID
    ] * len(caplog.records)


def test_the_correlation_id_is_required() -> None:
    with pytest.raises(TypeError):
        plan_subscriptions(singles(1), at=AT)  # type: ignore[call-arg]


def test_an_empty_correlation_id_is_refused() -> None:
    """A field that correlates nothing is the untraceable plan, with a name."""
    with pytest.raises(ValueError, match="correlation id"):
        plan_subscriptions(singles(1), at=AT, correlation_id="")


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
# The default the helper cannot see
# --------------------------------------------------------------------------


def test_omitting_cap_entirely_falls_back_to_the_one_named_constant() -> None:
    """The ``cap`` default is ``STREAM_SYMBOL_CAP``, pinned without ``plan_for``.

    Every other test here goes through :func:`plan_for`, which declares its own
    ``cap`` default and *always forwards it explicitly* -- so no other test in
    this file ever invokes :func:`plan_subscriptions` with ``cap`` omitted, and
    the signature's own default is unpinned. A mutation to ``cap: int = 3``
    leaves the whole suite green.

    The direction that costs money is upward. Suppose a later edit makes the
    default plan-aware and leaves it bound at, say, 50: a caller that omits
    ``cap`` -- which the signature advertises as optional -- plans fifty symbols
    on a Basic stream. This module reports ``dropped == ()`` and ``message is
    None``, the transport subscribes fifty, Alpaca silently truncates at thirty,
    and twenty contracts mark at their last known price while the UI states
    affirmatively that nothing is missing. That is the silent staleness this
    module's opening paragraph exists to prevent, reached by a green suite.

    So this asserts the default twice: the number the plan reports, and the
    behaviour it produces. The second survives ``cap`` ever ceasing to be a
    field on the plan.
    """
    plan = plan_subscriptions(
        singles(STREAM_SYMBOL_CAP + 1), at=AT, correlation_id=CORRELATION_ID
    )

    assert plan.cap == STREAM_SYMBOL_CAP
    assert len(plan.subscribed) == STREAM_SYMBOL_CAP
    assert len(plan.dropped) == 1
    assert plan.not_streamed == 1
