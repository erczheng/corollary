"""Acknowledgement reconciliation, and re-planning at a corrected cap -- pure.

``engine/stream.py`` decides what *we* will not stream. These two additions
cover the other direction: what the **server** did not stream, which it says
in its ``subscription`` message, and what to do when it says the cap we
planned against was wrong.

Both are here rather than in the vendor client because both are arithmetic
over a :class:`~corollary.engine.stream.SubscriptionPlan`, and a plan that
silently streams fewer symbols than it was handed is the failure that module
exists to prevent, arriving from the server instead of from our own sums.
"""

from datetime import datetime, timezone

import pytest

from corollary.engine.stream import (
    DropRule,
    Stream,
    SubscriptionPriority,
    contract_unit,
    plan_subscriptions,
    reconcile_acknowledgement,
    replan_at_cap,
    requested_units,
    underlying_unit,
)

T0 = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def test_a_full_acknowledgement_leaves_nothing_missing() -> None:
    plan = plan_subscriptions(
        [underlying_unit("AAPL"), underlying_unit("NVDA")],
        at=T0,
        correlation_id="cid-1",
        cap=30,
        stream=Stream.EQUITY,
    )
    ack = reconcile_acknowledgement(
        plan, channel="quotes", acknowledged=["AAPL", "NVDA"], at=T0
    )
    assert ack.absent == ()
    assert ack.not_streamed == 0
    assert ack.message is None


def test_a_symbol_subscribed_and_not_acknowledged_counts_as_not_streamed() -> None:
    """The server took one of the two. The figure has to say so.

    This is U2's whole point: a stream that accepts fewer symbols than it was
    handed, and says so only in a message nobody reconciles, is a position
    marking at a last known price with a banner that reads *"0 symbols not
    streamed"*.
    """
    plan = plan_subscriptions(
        [underlying_unit("AAPL"), underlying_unit("NVDA")],
        at=T0,
        correlation_id="cid-1",
        cap=30,
        stream=Stream.EQUITY,
    )
    ack = reconcile_acknowledgement(
        plan, channel="quotes", acknowledged=["AAPL"], at=T0
    )
    assert ack.absent == ("NVDA",)
    assert ack.not_streamed == 1
    assert ack.message == "1 symbol not streamed"


def test_absentees_add_to_the_plans_own_dropped_figure() -> None:
    """Two causes, one number. The reader's question is *"is anything unmarked?"*"""
    plan = plan_subscriptions(
        [underlying_unit("AAPL"), underlying_unit("NVDA"), underlying_unit("TSLA")],
        at=T0,
        correlation_id="cid-1",
        cap=2,
        stream=Stream.EQUITY,
    )
    assert plan.not_streamed == 1  # TSLA never fit
    ack = reconcile_acknowledgement(
        plan, channel="quotes", acknowledged=["AAPL"], at=T0
    )
    assert ack.absent == ("NVDA",)
    assert ack.not_streamed == 2
    assert ack.message == "2 symbols not streamed"


def test_an_acknowledgement_we_never_asked_for_is_reported_not_ignored() -> None:
    """A symbol the server streams and we never requested is a state mismatch.

    It costs a slot in the budget we are metering, so it cannot be dropped on
    the floor -- and it is the shape a stale subscription from a previous
    connection would take.
    """
    plan = plan_subscriptions(
        [underlying_unit("AAPL")],
        at=T0,
        correlation_id="cid-1",
        cap=30,
        stream=Stream.EQUITY,
    )
    ack = reconcile_acknowledgement(
        plan, channel="quotes", acknowledged=["AAPL", "MSFT"], at=T0
    )
    assert ack.surplus == ("MSFT",)
    assert ack.not_streamed == 0


def test_reconciliation_logs_the_rule_the_inputs_and_the_timestamp(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule 8. A dropped subscription is a rejection, whoever dropped it."""
    plan = plan_subscriptions(
        [underlying_unit("AAPL"), underlying_unit("NVDA")],
        at=T0,
        correlation_id="cid-7",
        cap=30,
        stream=Stream.EQUITY,
    )
    with caplog.at_level("WARNING", logger="corollary.engine.stream"):
        reconcile_acknowledgement(
            plan, channel="quotes", acknowledged=["AAPL"], at=T0
        )
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "stream_subscription_unacknowledged"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.rule == DropRule.NOT_ACKNOWLEDGED.value  # type: ignore[attr-defined]
    assert record.correlation_id == "cid-7"  # type: ignore[attr-defined]
    assert record.stream == Stream.EQUITY.label  # type: ignore[attr-defined]
    assert record.channel == "quotes"  # type: ignore[attr-defined]
    assert record.absent == ["NVDA"]  # type: ignore[attr-defined]
    assert record.at == T0.isoformat()  # type: ignore[attr-defined]


def test_a_reconciliation_that_fits_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plan = plan_subscriptions(
        [underlying_unit("AAPL")],
        at=T0,
        correlation_id="cid-1",
        cap=30,
        stream=Stream.EQUITY,
    )
    with caplog.at_level("WARNING", logger="corollary.engine.stream"):
        reconcile_acknowledgement(
            plan, channel="quotes", acknowledged=["AAPL"], at=T0
        )
    assert caplog.records == []


def test_reconciliation_refuses_a_naive_timestamp() -> None:
    plan = plan_subscriptions(
        [underlying_unit("AAPL")],
        at=T0,
        correlation_id="cid-1",
        cap=30,
        stream=Stream.EQUITY,
    )
    with pytest.raises(ValueError):
        reconcile_acknowledgement(
            plan,
            channel="quotes",
            acknowledged=["AAPL"],
            at=datetime(2026, 9, 14, 13, 30),
        )


def test_reconciliation_refuses_an_unnamed_channel() -> None:
    """Per channel, per the spec. An unnamed one reconciles two books at once."""
    plan = plan_subscriptions(
        [underlying_unit("AAPL")],
        at=T0,
        correlation_id="cid-1",
        cap=30,
        stream=Stream.EQUITY,
    )
    with pytest.raises(ValueError):
        reconcile_acknowledgement(plan, channel="", acknowledged=["AAPL"], at=T0)


# --------------------------------------------------------------------------
# Re-planning at a corrected cap
# --------------------------------------------------------------------------


def test_requested_units_returns_every_unit_the_plan_considered() -> None:
    units = [
        contract_unit("pos-1", ["AAPL241220C00150000"]),
        contract_unit("pos-2", ["NVDA241220C00500000"]),
    ]
    plan = plan_subscriptions(
        units, at=T0, correlation_id="cid-1", cap=1, stream=Stream.OPTION
    )
    assert len(plan.admitted) == 1
    assert len(plan.dropped) == 1
    assert requested_units(plan) == tuple(units)


def test_requested_units_keeps_priority_order() -> None:
    plan = plan_subscriptions(
        [underlying_unit("AAPL"), underlying_unit("NVDA")],
        at=T0,
        correlation_id="cid-1",
        cap=30,
        stream=Stream.EQUITY,
    )
    assert [unit.priority for unit in requested_units(plan)] == [
        SubscriptionPriority.POSITION_UNDERLYING,
        SubscriptionPriority.POSITION_UNDERLYING,
    ]


def test_replanning_at_a_lower_cap_keeps_the_stream_and_drops_the_tail() -> None:
    """A server-corrected cap re-runs the same allocation, not a different one."""
    units = [
        contract_unit("pos-1", ["AAPL241220C00150000"]),
        contract_unit("pos-2", ["NVDA241220C00500000"]),
        contract_unit("pos-3", ["TSLA241220C00250000"]),
    ]
    plan = plan_subscriptions(
        units, at=T0, correlation_id="cid-1", cap=200, stream=Stream.OPTION
    )
    assert plan.not_streamed == 0

    replanned = replan_at_cap(plan, cap=2, at=T0, correlation_id="cid-2")
    assert replanned.stream is Stream.OPTION
    assert replanned.cap == 2
    assert replanned.subscribed == (
        "AAPL241220C00150000",
        "NVDA241220C00500000",
    )
    assert replanned.not_streamed == 1
    assert replanned.correlation_id == "cid-2"


def test_replanning_at_the_same_cap_reproduces_the_plan() -> None:
    """Determinism: same units, same cap, same list. Scanner rule, same reason."""
    units = [underlying_unit("AAPL"), underlying_unit("NVDA")]
    plan = plan_subscriptions(
        units, at=T0, correlation_id="cid-1", cap=30, stream=Stream.EQUITY
    )
    again = replan_at_cap(plan, cap=30, at=T0, correlation_id="cid-1")
    assert again.subscribed == plan.subscribed
    assert again.dropped == plan.dropped


def test_replanning_refuses_to_raise_a_cap() -> None:
    """A correction lowers. A socket that could raise its own cap is not corrected.

    The 405 path exists because the server said we asked for too much. Letting
    the same path *widen* a budget would turn one refusal into a subscribe the
    server truncates silently, which is the failure the budget exists to
    prevent.
    """
    plan = plan_subscriptions(
        [underlying_unit("AAPL")],
        at=T0,
        correlation_id="cid-1",
        cap=30,
        stream=Stream.EQUITY,
    )
    with pytest.raises(ValueError):
        replan_at_cap(plan, cap=31, at=T0, correlation_id="cid-2")
