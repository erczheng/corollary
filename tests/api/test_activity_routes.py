"""The ledger surface: the realized-trade table, and the header cards above it.

Two rules decide this module, and both are about arithmetic that SQL is not
allowed to do.

**Decision 11.** ``pnl`` is a ``Money`` column, which is TEXT on SQLite, so
``SUM``, ``AVG``, ``MIN``, ``MAX`` and ``ORDER BY`` over it raise
``MoneyComparisonError`` rather than answering lexicographically. Lifetime
realized P&L, average win, average loss and the win counts are therefore
Python folds over **every** loaded ``realized_trade`` row. A capped window
would make four figures labelled "lifetime" mean something narrower than the
word, so the *table* is what pages.

**Open question 4, resolved.** An adjusted root reaching an ``OPEXC``/
``OPASN`` branch is refused and produces no ``realized_trade`` row. The
figures above are then *known-incomplete*, and the API has to say so -- hence
``ActivityStats.notBooked``. A number that is quietly missing a trade is the
failure this whole module exists to avoid.

The router is mounted here rather than assumed: ``api/app.py`` belongs to a
different dispatch, and a test that only passes once somebody else edits a
file is a test that cannot fail for its own reason.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.api.app import create_app
from corollary.api.deps import ServiceRegistry
from corollary.api.routes.activity import router as activity_router
from corollary.db.models import Fill, RealizedTrade

MODULE = (
    Path(__file__).resolve().parents[2]
    / "corollary"
    / "api"
    / "routes"
    / "activity.py"
)

#: The one round trip this paper account really has: an IWM 280 put bought at
#: 8.21 and sold at 8.14 on a 100 multiplier, realizing -$7.00.
IWM = "IWM260918P00280000"
AAPL = "AAPL261218C00230000"
GME1 = "GME1261016C00003000"

T0 = datetime(2026, 9, 10, 13, 11, 25, 217000, tzinfo=timezone.utc)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def add_fill(
    session: Session,
    *,
    activity_id: str,
    symbol: str = IWM,
    side: str = "buy",
    intent: str | None = "buy_to_open",
    qty: int = 1,
    price: str | None = "8.21",
    when: datetime,
    account: str = "paper",
    order_id: str | None = "ord-1",
    group_id: str | None = None,
) -> None:
    session.add(
        Fill(
            account=account,
            activity_id=activity_id,
            order_id=order_id,
            group_id=group_id,
            symbol=symbol,
            side=side,
            position_intent=intent,
            qty=qty,
            # ``None`` is a state the column really holds: an exercise whose
            # deliverable the ledger could not verify has no price it can
            # stand behind. Never ``Decimal(0)``, which is a price.
            price=None if price is None else Decimal(price),
            at=when,
        )
    )


def add_trade(
    session: Session,
    *,
    symbol: str = IWM,
    opened_at: datetime,
    closed_at: datetime,
    qty: int = 1,
    open_price: str = "8.21",
    close_price: str = "8.14",
    pnl: str = "-7.00",
    pnl_pct: str | None = "-0.8526",
    close_kind: str = "fill",
    account: str = "paper",
) -> None:
    session.add(
        RealizedTrade(
            account=account,
            symbol=symbol,
            opened_at=opened_at,
            closed_at=closed_at,
            qty=qty,
            open_price=Decimal(open_price),
            close_price=Decimal(close_price),
            pnl=Decimal(pnl),
            pnl_pct=None if pnl_pct is None else Decimal(pnl_pct),
            close_kind=close_kind,
        )
    )


def seed(engine: Engine, populate: Callable[[Session], None]) -> None:
    with Session(engine) as session:
        populate(session)
        session.commit()


def rows(session: Session, account: str) -> tuple[list[Fill], list[RealizedTrade]]:
    """One book's stored ledger, for the tests that exercise the pure folds."""
    fills = list(
        session.scalars(
            select(Fill).where(Fill.account == account).order_by(Fill.at, Fill.id)
        )
    )
    trades = list(
        session.scalars(
            select(RealizedTrade).where(RealizedTrade.account == account)
        )
    )
    return fills, trades


def round_trip(session: Session) -> None:
    """The account's real history: one IWM round trip, booked."""
    add_fill(
        session,
        activity_id="20260910131125217::a9d576c2",
        side="buy",
        intent="buy_to_open",
        price="8.21",
        when=at(0),
    )
    add_fill(
        session,
        activity_id="20260910131625217::b1e4771f",
        side="sell",
        intent="sell_to_close",
        price="8.14",
        when=at(5),
        order_id="ord-2",
    )
    add_trade(session, opened_at=at(0), closed_at=at(5))


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------


def build(registry: ServiceRegistry, db_engine: Engine) -> FastAPI:
    """The app with the activity router mounted.

    Mounted here because ``api/app.py`` is another dispatch's file. If it has
    already wired the router by the time this runs, mounting a second copy
    would only shadow it with an identical route, so the check keeps the
    OpenAPI document honest either way.
    """
    app = create_app(registry=registry, db_engine=db_engine)
    paths = {getattr(route, "path", None) for route in app.routes}
    if "/api/activity" not in paths:
        app.include_router(activity_router)
    return app


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------


def test_the_ledger_serves_one_row_per_fill_newest_first(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    seed(db_engine, round_trip)

    with TestClient(build(registry, db_engine)) as client:
        body = client.get("/api/activity").json()

    assert [item["action"] for item in body["items"]] == ["STC", "BTO"]
    assert body["total"] == 2


def test_a_closing_fill_carries_the_realized_pnl_of_its_slices(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """Corollary's arithmetic. Alpaca publishes no realized P&L anywhere."""
    seed(db_engine, round_trip)

    with TestClient(build(registry, db_engine)) as client:
        closing = client.get("/api/activity").json()["items"][0]

    assert closing["pnl"] == -7.0
    assert closing["pnlPct"] == -0.8526


def test_an_opening_fill_realizes_nothing(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    seed(db_engine, round_trip)

    with TestClient(build(registry, db_engine)) as client:
        opening = client.get("/api/activity").json()["items"][1]

    assert opening["pnl"] is None
    assert opening["pnlPct"] is None


def test_the_action_comes_from_the_intent_and_not_from_the_side(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """``buy`` is BTO *and* BTC. Only the order's intent separates them.

    Read off ``side`` alone, a buy-to-close renders as an opening trade and
    the row claims a position was opened where one was closed.
    """

    def populate(session: Session) -> None:
        add_fill(
            session,
            activity_id="a",
            side="sell_short",
            intent="sell_to_open",
            price="1.10",
            when=at(0),
        )
        add_fill(
            session,
            activity_id="b",
            side="buy",
            intent="buy_to_close",
            price="0.40",
            when=at(1),
        )

    seed(db_engine, populate)

    with TestClient(build(registry, db_engine)) as client:
        body = client.get("/api/activity").json()

    assert [item["action"] for item in body["items"]] == ["BTC", "STO"]


def test_a_side_that_decides_the_action_still_decides_it_without_an_intent(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """``sell_short`` is STO and ``sell`` is STC, whatever the join did."""

    def populate(session: Session) -> None:
        add_fill(
            session,
            activity_id="a",
            side="sell_short",
            intent=None,
            price="1.10",
            when=at(0),
        )
        add_fill(
            session, activity_id="b", side="sell", intent=None, price="2.20", when=at(1)
        )

    seed(db_engine, populate)

    with TestClient(build(registry, db_engine)) as client:
        body = client.get("/api/activity").json()

    assert [item["action"] for item in body["items"]] == ["STC", "STO"]


def test_a_buy_with_no_intent_is_refused_and_logged(
    registry: ServiceRegistry,
    db_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule 8: the rule, the inputs and the timestamp -- never a guess.

    A ``buy`` whose order never joined could be an open or a close, and the
    row cannot state which. Naming it BTO would report a position opened
    where one may have been closed.
    """

    def populate(session: Session) -> None:
        add_fill(
            session,
            activity_id="unjoined",
            side="buy",
            intent=None,
            price="8.21",
            when=at(0),
        )

    seed(db_engine, populate)

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.activity"):
        with TestClient(build(registry, db_engine)) as client:
            body = client.get("/api/activity").json()

    assert body["items"] == []
    assert body["total"] == 0
    record = next(
        entry
        for entry in caplog.records
        if getattr(entry, "event", None) == "activity_row_refused"
    )
    assert getattr(record, "rule") == "unknown_intent"
    assert getattr(record, "activity_id") == "unjoined"
    assert getattr(record, "side") == "buy"
    assert getattr(record, "at")


def test_a_multi_slice_close_sums_pnl_and_weights_the_percentage(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """One close consuming two lots is two rows in ``realized_trade``.

    The dollar figure adds. The percentage does not: the two slices have
    different bases, so the honest aggregate is weighted by basis -- which is
    ``open_price x qty`` here, because the multiplier is common to both
    slices and cancels out of the ratio.

    Lots of 1 @ 2.00 and 3 @ 4.00, both closed at 5.00 on a 100 multiplier:
    +300 on 200 of basis and +300 on 1200, so +600 realized on 1400 of basis
    -- 42.8571%, not the 87.5% an unweighted mean of 150% and 25% would give.
    """

    def populate(session: Session) -> None:
        add_fill(
            session, activity_id="o1", intent="buy_to_open", price="2.00", when=at(0)
        )
        add_fill(
            session,
            activity_id="o2",
            intent="buy_to_open",
            qty=3,
            price="4.00",
            when=at(1),
        )
        add_fill(
            session,
            activity_id="c1",
            side="sell",
            intent="sell_to_close",
            qty=4,
            price="5.00",
            when=at(2),
        )
        add_trade(
            session,
            opened_at=at(0),
            closed_at=at(2),
            qty=1,
            open_price="2.00",
            close_price="5.00",
            pnl="300.00",
            pnl_pct="150.0000",
        )
        add_trade(
            session,
            opened_at=at(1),
            closed_at=at(2),
            qty=3,
            open_price="4.00",
            close_price="5.00",
            pnl="300.00",
            pnl_pct="25.0000",
        )

    seed(db_engine, populate)

    with TestClient(build(registry, db_engine)) as client:
        closing = client.get("/api/activity").json()["items"][0]

    assert closing["pnl"] == 600.0
    assert closing["pnlPct"] == pytest.approx(42.8571)


def test_the_contract_reads_as_a_contract(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """An OCC symbol is a key, not a label. The table shows the label."""
    seed(db_engine, round_trip)

    with TestClient(build(registry, db_engine)) as client:
        body = client.get("/api/activity").json()

    assert body["items"][0]["contract"] == "IWM $280 Put Sep 18"


# --------------------------------------------------------------------------
# Pagination -- the table pages, the cards do not
# --------------------------------------------------------------------------


def many_fills(count: int) -> Callable[[Session], None]:
    def populate(session: Session) -> None:
        for index in range(count):
            add_fill(
                session,
                activity_id=f"fill-{index:03d}",
                intent="buy_to_open",
                price="1.00",
                when=at(index),
            )

    return populate


def test_total_counts_every_matching_row_not_the_page(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    seed(db_engine, many_fills(40))

    with TestClient(build(registry, db_engine)) as client:
        body = client.get("/api/activity?pageSize=15").json()

    assert len(body["items"]) == 15
    assert body["total"] == 40
    assert body["page"] == 0
    assert body["hasMore"] is True


def test_has_more_is_false_on_the_last_page(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    seed(db_engine, many_fills(40))

    with TestClient(build(registry, db_engine)) as client:
        body = client.get("/api/activity?pageSize=15&page=2").json()

    assert len(body["items"]) == 10
    assert body["hasMore"] is False


def test_the_table_is_paged_on_a_total_order() -> None:
    """The tie-break is asserted on the statement, not on the rows.

    ``at`` alone is not a total order: the composite activity id does not
    sort chronologically -- stamps repeat and the UUID half breaks ties
    arbitrarily -- and ``transaction_time`` does not either at microsecond
    resolution. Page on an order that is only nearly total and one row lands
    on two pages while another lands on none.

    Asserted here rather than by paging tied rows because **that test has no
    teeth**: SQLite answers a fully-tied ``ORDER BY`` in rowid order, so
    dropping the tie-break leaves every behavioural assertion green while
    removing the only thing that guarantees it. Written and watched to fail
    both ways before it was kept.
    """
    from corollary.api.deps import AccountMode
    from corollary.api.routes.activity import fills_query

    statement = str(fills_query(AccountMode.PAPER))
    _, _, ordering = statement.partition("ORDER BY")

    assert "fill.at DESC" in ordering
    assert "fill.id DESC" in ordering


def test_paging_covers_every_row_exactly_once(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """Nine rows, three pages, nine distinct ids. No repeats, no drops."""

    def populate(session: Session) -> None:
        for index in range(9):
            add_fill(
                session,
                activity_id=f"tied-{index}",
                intent="buy_to_open",
                price="1.00",
                when=at(0),
            )

    seed(db_engine, populate)

    with TestClient(build(registry, db_engine)) as client:
        first = client.get("/api/activity?pageSize=3&page=0").json()["items"]
        second = client.get("/api/activity?pageSize=3&page=1").json()["items"]
        third = client.get("/api/activity?pageSize=3&page=2").json()["items"]

    seen = [item["id"] for item in first + second + third]
    assert sorted(seen) == sorted(f"tied-{index}" for index in range(9))


def test_a_page_past_the_end_is_empty_rather_than_an_error(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    seed(db_engine, round_trip)

    with TestClient(build(registry, db_engine)) as client:
        body = client.get("/api/activity?page=9").json()

    assert body["items"] == []
    assert body["total"] == 2
    assert body["hasMore"] is False


# --------------------------------------------------------------------------
# Search and filter -- they combine, they do not replace
# --------------------------------------------------------------------------


def two_underlyings(session: Session) -> None:
    add_fill(
        session, activity_id="iwm", symbol=IWM, intent="buy_to_open", when=at(0)
    )
    add_fill(
        session,
        activity_id="aapl",
        symbol=AAPL,
        intent="buy_to_open",
        price="3.10",
        when=at(1),
    )


def test_search_matches_the_underlying_case_insensitively(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    seed(db_engine, two_underlyings)

    with TestClient(build(registry, db_engine)) as client:
        body = client.get("/api/activity?search=aapl").json()

    assert [item["id"] for item in body["items"]] == ["aapl"]
    assert body["total"] == 1


def test_search_matches_the_occ_symbol_a_user_pasted(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """The label says "IWM $280 Put Sep 18"; the broker says the OCC symbol.

    Somebody pasting the second must not be told there is no such trade.
    """
    seed(db_engine, two_underlyings)

    with TestClient(build(registry, db_engine)) as client:
        body = client.get(f"/api/activity?search={IWM}").json()

    assert [item["id"] for item in body["items"]] == ["iwm"]


def test_search_and_status_combine_rather_than_replace(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    seed(db_engine, two_underlyings)

    with TestClient(build(registry, db_engine)) as client:
        matching = client.get("/api/activity?search=aapl&status=filled").json()
        conflicting = client.get("/api/activity?search=aapl&status=rejected").json()

    assert matching["total"] == 1
    assert conflicting["total"] == 0


def test_an_unknown_status_is_a_422_rather_than_everything(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    seed(db_engine, two_underlyings)

    with TestClient(build(registry, db_engine)) as client:
        assert client.get("/api/activity?status=partial").status_code == 422


# --------------------------------------------------------------------------
# The header cards -- decision 11
# --------------------------------------------------------------------------


def test_lifetime_pnl_is_the_accounts_one_round_trip(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    seed(db_engine, round_trip)

    with TestClient(build(registry, db_engine)) as client:
        stats = client.get("/api/activity/stats").json()

    assert stats["lifetimePnl"] == -7.0
    assert stats["wins"] == 0
    assert stats["losses"] == 1
    assert stats["avgLoss"] == -7.0
    assert stats["avgLossPct"] == -0.8526


def test_the_cards_fold_every_trade_and_not_just_the_first_page(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """Decision 11: "all of them for the header cards, with pagination on the
    table itself." A capped window makes "lifetime" mean something narrower
    than the word.
    """

    def populate(session: Session) -> None:
        for index in range(40):
            add_trade(
                session,
                opened_at=at(index),
                closed_at=at(index + 1),
                pnl="1.00",
                pnl_pct="1.0000",
            )

    seed(db_engine, populate)

    with TestClient(build(registry, db_engine)) as client:
        page = client.get("/api/activity?pageSize=15").json()
        stats = client.get("/api/activity/stats").json()

    assert len(page["items"]) == 0  # no fills seeded; the cards read trades
    assert stats["wins"] == 40
    assert stats["lifetimePnl"] == 40.0


def test_averages_are_null_before_any_trade_of_that_kind(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """An average over zero trades is unknown, not zero. The card is an em
    dash; ``$0.00`` would claim a result that does not exist.
    """
    seed(db_engine, round_trip)

    with TestClient(build(registry, db_engine)) as client:
        stats = client.get("/api/activity/stats").json()

    assert stats["avgWin"] is None
    assert stats["avgWinPct"] is None


def test_an_empty_book_reports_zero_lifetime_and_no_averages(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    with TestClient(build(registry, db_engine)) as client:
        stats = client.get("/api/activity/stats").json()

    assert stats["lifetimePnl"] == 0.0
    assert stats["wins"] == 0
    assert stats["losses"] == 0
    assert stats["avgLoss"] is None


def test_the_fold_beats_the_answer_sql_would_have_given(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """-7 and -41 are the pair that makes the text comparison look right.

    ``Money`` is TEXT on SQLite, so ``'-41' < '-7'`` and ``MIN`` would name
    -41 the worse loss by coincidence of the digits while ``ORDER BY``
    inverts them. Add -410 and the coincidence breaks: lexicographically
    ``'-410' < '-41' < '-7'``, so a text ``MIN`` answers -410 correctly and a
    text ``MAX`` answers -7, but the *average* SQL cannot compute at all.
    Python gets -152.67 and any lexicographic shortcut does not.
    """

    def populate(session: Session) -> None:
        for index, pnl in enumerate(("-7.00", "-41.00", "-410.00")):
            add_trade(
                session,
                opened_at=at(index),
                closed_at=at(index + 1),
                pnl=pnl,
                pnl_pct="-1.0000",
            )

    seed(db_engine, populate)

    with TestClient(build(registry, db_engine)) as client:
        stats = client.get("/api/activity/stats").json()

    assert stats["lifetimePnl"] == -458.0
    assert stats["losses"] == 3
    assert stats["avgLoss"] == pytest.approx(-152.67)


def test_the_module_asks_sql_no_question_about_money() -> None:
    """Structural, because the bug is silent when it lands.

    ``guard_money_sql`` catches ``func.sum`` and a bare ``ORDER BY`` at
    runtime, but only on a code path a test happens to reach. A module that
    never writes one cannot regress into it on a branch nobody exercised.
    """
    source = MODULE.read_text(encoding="utf-8")

    for forbidden in ("func.", "order_by(RealizedTrade.pnl", "Fill.price.desc"):
        assert forbidden not in source, forbidden


# --------------------------------------------------------------------------
# The gap -- open question 4, resolved
# --------------------------------------------------------------------------


def adjusted_exercise(session: Session) -> None:
    """A ``GME1`` exercise: a fill row, and deliberately no realized trade.

    An adjusted root delivers 100 GME **plus** 10 GME.WS while ``multiplier``
    and ``size`` both report 100, so there is no honest dollar figure. The
    matcher refuses, the contracts really are gone, and the page has to say
    the lifetime figure is missing one.
    """
    add_fill(
        session,
        activity_id="gme-open",
        symbol=GME1,
        intent="buy_to_open",
        price="1.20",
        when=at(0),
    )
    add_fill(
        session,
        activity_id="gme-exercise",
        symbol=GME1,
        side="sell",
        intent="sell_to_close",
        price="2.50",
        when=at(5),
        order_id=None,
        group_id="grp-1",
    )


def unpriced_adjusted_exercise(session: Session) -> None:
    """The same ``GME1`` exercise as ingestion now writes it: with no price.

    ``OPEXC`` carries ``net_amount: "0"``, so the close price is synthesised
    as intrinsic value against the OCC strike -- and an adjusted root is
    exactly the finding that the strike is not what the deliverable changes
    hands at. The ledger refuses the trade on that rule, so it refuses the
    price with it.
    """
    add_fill(
        session,
        activity_id="gme-open",
        symbol=GME1,
        intent="buy_to_open",
        price="1.20",
        when=at(0),
    )
    add_fill(
        session,
        activity_id="gme-exercise",
        symbol=GME1,
        side="sell",
        intent="sell_to_close",
        price=None,
        when=at(5),
        order_id=None,
        group_id="grp-1",
    )


def test_an_unverifiable_settlement_shows_no_price_rather_than_an_estimate(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """An estimate in a column of prices paid is worse than an empty cell.

    ``max(close - strike, 0)`` against the unadjusted strike is a number
    nobody can stand behind -- the same class of guess open question 4
    forbade deriving from ``deliverables``, ``size`` or a split factor, one
    column over. The row is on screen because the contracts really left the
    book; the price is absent because it was never established.
    """
    seed(db_engine, unpriced_adjusted_exercise)

    with TestClient(build(registry, db_engine)) as client:
        items = client.get("/api/activity").json()["items"]

    exercise = next(item for item in items if item["action"] == "STC")
    assert exercise["price"] is None
    assert exercise["pnl"] is None
    assert exercise["quantity"] == 1


def test_a_close_with_no_price_is_still_counted_in_the_gap(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """Losing the price must not lose the row out of ``notBooked``.

    The count is what makes a known-incomplete lifetime figure legible, and
    the close it exists for is precisely the one with no price. A join keyed
    on ``(symbol, closed_at, close_price)`` finds nothing for it -- which is
    the right answer, since no trade was booked -- and the quantity test is
    what still counts it.
    """
    seed(db_engine, unpriced_adjusted_exercise)

    with TestClient(build(registry, db_engine)) as client:
        stats = client.get("/api/activity/stats").json()

    assert stats["notBooked"] == 1
    assert stats["notBookedSymbols"] == [GME1]
    assert stats["lifetimePnl"] == 0.0


def test_a_close_with_no_realized_trade_is_counted_rather_than_hidden(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    seed(db_engine, adjusted_exercise)

    with TestClient(build(registry, db_engine)) as client:
        stats = client.get("/api/activity/stats").json()

    assert stats["notBooked"] == 1
    assert stats["lifetimePnl"] == 0.0


def test_the_gap_names_the_contract_so_it_can_be_reconciled(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """A bare count cannot be checked by hand. The symbol can."""
    seed(db_engine, adjusted_exercise)

    with TestClient(build(registry, db_engine)) as client:
        stats = client.get("/api/activity/stats").json()

    assert stats["notBookedSymbols"] == [GME1]


def test_a_fully_booked_ledger_reports_no_gap(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    seed(db_engine, round_trip)

    with TestClient(build(registry, db_engine)) as client:
        stats = client.get("/api/activity/stats").json()

    assert stats["notBooked"] == 0
    assert stats["notBookedSymbols"] == []


def test_an_expiry_at_zero_is_booked_and_is_not_a_gap(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """``OPEXP`` closes at zero -- a price, not an absence.

    Zero is a full loss on a long, and the deliverable question never arises
    because premium times the multiplier is right on any contract. Counting
    it as unbooked would invent a gap out of a trade that booked correctly.
    """

    def populate(session: Session) -> None:
        add_fill(
            session, activity_id="e1", intent="buy_to_open", price="1.00", when=at(0)
        )
        add_fill(
            session,
            activity_id="e2",
            side="sell",
            intent="sell_to_close",
            price="0",
            when=at(5),
            order_id=None,
        )
        add_trade(
            session,
            opened_at=at(0),
            closed_at=at(5),
            open_price="1.00",
            close_price="0",
            pnl="-100.00",
            pnl_pct="-100.0000",
            close_kind="expiry",
        )

    seed(db_engine, populate)

    with TestClient(build(registry, db_engine)) as client:
        stats = client.get("/api/activity/stats").json()

    assert stats["notBooked"] == 0
    assert stats["lifetimePnl"] == -100.0


def test_a_partly_booked_close_is_a_gap_too(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """Closing three contracts when two were open books two, and the third
    is as missing from lifetime P&L as a whole refused close would be.

    Counting only *wholly* unbooked closings would report this ledger as
    complete while a contract's worth of P&L is absent -- the quiet half of
    exactly the failure open question 4 refuses to risk.
    """

    def populate(session: Session) -> None:
        add_fill(
            session,
            activity_id="o",
            intent="buy_to_open",
            qty=2,
            price="1.00",
            when=at(0),
        )
        add_fill(
            session,
            activity_id="c",
            side="sell",
            intent="sell_to_close",
            qty=3,
            price="2.00",
            when=at(5),
        )
        add_trade(
            session,
            opened_at=at(0),
            closed_at=at(5),
            qty=2,
            open_price="1.00",
            close_price="2.00",
            pnl="200.00",
            pnl_pct="100.0000",
        )

    seed(db_engine, populate)

    with TestClient(build(registry, db_engine)) as client:
        stats = client.get("/api/activity/stats").json()

    assert stats["notBooked"] == 1
    assert stats["notBookedSymbols"] == [IWM]


def test_an_opening_fill_is_never_a_gap(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """An open realizes nothing, so there is nothing for it to be missing."""

    def populate(session: Session) -> None:
        add_fill(
            session, activity_id="o", intent="buy_to_open", price="1.00", when=at(0)
        )

    seed(db_engine, populate)

    with TestClient(build(registry, db_engine)) as client:
        stats = client.get("/api/activity/stats").json()

    assert stats["notBooked"] == 0


# --------------------------------------------------------------------------
# The account boundary
# --------------------------------------------------------------------------


def test_the_other_books_rows_are_not_served(
    make_registry: Callable[..., ServiceRegistry], db_engine: Engine
) -> None:
    """Rendering paper's ledger while Cash is live misreports real money."""

    def populate(session: Session) -> None:
        add_fill(
            session,
            activity_id="paper-1",
            intent="buy_to_open",
            when=at(0),
            account="paper",
        )
        add_fill(
            session,
            activity_id="cash-1",
            intent="buy_to_open",
            when=at(1),
            account="cash",
        )
        add_trade(session, opened_at=at(0), closed_at=at(1), account="cash")

    seed(db_engine, populate)

    with TestClient(build(make_registry(live_keys=True), db_engine)) as client:
        paper = client.get("/api/activity").json()
        paper_stats = client.get("/api/activity/stats").json()
        cash = client.get("/api/activity?account=cash").json()
        cash_stats = client.get("/api/activity/stats?account=cash").json()

    assert [item["id"] for item in paper["items"]] == ["paper-1"]
    assert paper_stats["lifetimePnl"] == 0.0
    assert [item["id"] for item in cash["items"]] == ["cash-1"]
    assert cash_stats["lifetimePnl"] == -7.0


@pytest.mark.parametrize("path", ["/api/activity", "/api/activity/stats"])
def test_cash_without_live_keys_is_a_409_and_never_papers_book(
    registry: ServiceRegistry, db_engine: Engine, path: str
) -> None:
    seed(db_engine, round_trip)

    with TestClient(build(registry, db_engine)) as client:
        response = client.get(f"{path}?account=cash")

    assert response.status_code == 409
    assert "not substituted" in response.json()["error"]["message"]


@pytest.mark.parametrize("path", ["/api/activity", "/api/activity/stats"])
def test_an_unknown_account_is_a_422(
    registry: ServiceRegistry, db_engine: Engine, path: str
) -> None:
    with TestClient(build(registry, db_engine)) as client:
        assert client.get(f"{path}?account=margin").status_code == 422


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------


def test_no_float_reaches_the_arithmetic(db_engine: Engine) -> None:
    """``JsonMoney`` is a float only on the wire.

    The fold is a pure function over loaded rows, and in Python mode every
    figure it produces is still an exact ``Decimal`` -- which is what proves
    nothing went through a double on the way to the serializer.
    """
    from corollary.api.routes.activity import ledger_stats

    seed(db_engine, round_trip)

    with Session(db_engine) as session:
        stats = ledger_stats(*rows(session, "paper"))

    assert isinstance(stats.lifetime_pnl, Decimal)
    assert stats.lifetime_pnl == Decimal("-7.00")
    assert isinstance(stats.avg_loss_pct, Decimal)
    assert stats.avg_loss_pct == Decimal("-0.8526")


def test_the_item_fold_is_deterministic(db_engine: Engine) -> None:
    """Same rows in, same rows out. Twice, including the order."""
    from corollary.api.routes.activity import activity_items

    seed(db_engine, round_trip)

    with Session(db_engine) as session:
        fills, trades = rows(session, "paper")
        first = activity_items(fills, trades, correlation_id="one")
        second = activity_items(fills, trades, correlation_id="two")

    assert [item.model_dump() for item in first] == [
        item.model_dump() for item in second
    ]
