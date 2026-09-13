"""The four ledger tables — ``fill``, ``realized_trade``, ``mleg_group``, ``mleg_leg``.

Three groups of tests matter more than the rest.

* **The money columns come back as exact ``Decimal``, asserted on the type.**
  Same reasoning as ``test_risk_limit_value_round_trips_as_decimal``: a float
  that happens to compare equal is still the bug. What is new here is that
  ``pnl`` is **signed** — a loss is negative — which is the one thing
  ``risk_limit``'s ``ck_risk_limit_value`` would have rejected outright.
* **``pnl`` refuses to be ordered, compared or aggregated in SQL.** That is
  the guard rail, not a limitation: ``Money`` is TEXT on SQLite, so
  ``ORDER BY pnl`` sorts ``-7`` after ``120`` and ``MIN(pnl)`` answers with
  whichever loss happens to start with the earliest character. "Biggest
  loser" is a Python sort over loaded rows, and the tests below prove the
  wrong version raises rather than answering.
* **``activity_id`` UNIQUE is what makes re-ingestion safe.** Ingestion runs
  on startup and on an interval; it is idempotent on that column and on
  nothing else.

Everything asserted here goes through the ORM against a ``create_all``
schema. The same schema built by ``alembic upgrade head`` is checked in
``tests/db/test_migrations.py``, which is where the models and the migration
are held to each other.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corollary.db.models import (
    ACCOUNT_MODES,
    CLOSE_KINDS,
    FILL_SIDES,
    POSITION_INTENTS,
    Fill,
    MlegGroup,
    MlegLeg,
    RealizedTrade,
)
from corollary.db.types import MoneyComparisonError

#: A real activity id, from the probe recorded in the design spec. Composite:
#: a 17-digit timestamp, ``::``, then a UUID. Not a bare UUID, and the column
#: has to be wide enough for the whole thing.
REAL_ACTIVITY_ID = "20260910131125598::68cda3e9-1e1f-4c3f-9d6a-7b2a0c5e4d31"

#: The round trip the probe recorded: IWM 280 put bought at 8.21, sold at
#: 8.14, one contract, 100 multiplier — a $7.00 loss. The arithmetic the FIFO
#: matcher must reproduce, and the negative number this schema must store.
IWM_PUT = "IWM261120P00280000"

OPENED_AT = datetime(2026, 9, 10, 13, 31, 25, tzinfo=timezone.utc)
CLOSED_AT = datetime(2026, 9, 10, 17, 4, 2, tzinfo=timezone.utc)


def a_fill(**overrides: object) -> Fill:
    """A well-formed opening fill, so each test overrides only what it probes."""
    fields: dict[str, object] = {
        "account": "paper",
        "activity_id": REAL_ACTIVITY_ID,
        "order_id": "68cda3e9-1e1f-4c3f-9d6a-7b2a0c5e4d31",
        "group_id": None,
        "symbol": IWM_PUT,
        "side": "buy",
        "position_intent": "buy_to_open",
        "qty": 1,
        "price": Decimal("8.21"),
        "at": OPENED_AT,
    }
    fields.update(overrides)
    return Fill(**fields)


def a_trade(**overrides: object) -> RealizedTrade:
    """The probe's losing round trip, as the matcher would emit it."""
    fields: dict[str, object] = {
        "account": "paper",
        "symbol": IWM_PUT,
        "opened_at": OPENED_AT,
        "closed_at": CLOSED_AT,
        "qty": 1,
        "open_price": Decimal("8.21"),
        "close_price": Decimal("8.14"),
        "pnl": Decimal("-7.00"),
        "pnl_pct": Decimal("-0.85"),
        "close_kind": "fill",
    }
    fields.update(overrides)
    return RealizedTrade(**fields)


def a_group(**overrides: object) -> MlegGroup:
    fields: dict[str, object] = {
        "account": "paper",
        "order_id": "7f1c9a52-0b64-4d2e-8a11-3c6f5e9d0a77",
        "opened_at": OPENED_AT,
        "net_price": Decimal("1.35"),
    }
    fields.update(overrides)
    return MlegGroup(**fields)


# --------------------------------------------------------------------- #
# The vocabulary — one spelling, shared with the frontend and the broker
# --------------------------------------------------------------------- #


def test_the_account_values_are_the_frontend_account_mode_union() -> None:
    """``web/src/lib/types.ts``: ``type AccountMode = 'paper' | 'cash'``.

    Same standard ``AUDIT_CATEGORIES`` already holds itself to — a row
    renders without translation, so there is no mapping table to get wrong.
    """
    assert ACCOUNT_MODES == ("paper", "cash")


def test_side_has_three_values_not_two() -> None:
    """The probe found ``sell_short`` alongside ``sell`` and ``buy``.

    ``sell_short`` opens a short (STO), ``sell`` closes a long (STC), and
    ``buy`` is *both* BTO and BTC — which is exactly why
    ``position_intent`` is a separate column rather than derived from this
    one.
    """
    assert FILL_SIDES == ("buy", "sell", "sell_short")


def test_position_intent_uses_alpacas_own_spelling() -> None:
    assert POSITION_INTENTS == (
        "buy_to_open",
        "buy_to_close",
        "sell_to_open",
        "sell_to_close",
    )


def test_close_kind_covers_the_four_ways_a_position_ends() -> None:
    """An option expiring is the most common one and is not a fill."""
    assert CLOSE_KINDS == ("fill", "expiry", "exercise", "assignment")


# --------------------------------------------------------------------- #
# fill — the raw activity ledger
# --------------------------------------------------------------------- #


def test_fill_round_trips(session: Session) -> None:
    session.add(a_fill())
    session.commit()
    session.expunge_all()

    stored = session.query(Fill).one()
    assert stored.id is not None
    assert stored.account == "paper"
    assert stored.activity_id == REAL_ACTIVITY_ID
    assert stored.order_id == "68cda3e9-1e1f-4c3f-9d6a-7b2a0c5e4d31"
    assert stored.group_id is None
    assert stored.symbol == IWM_PUT
    assert stored.side == "buy"
    assert stored.position_intent == "buy_to_open"
    assert stored.qty == 1
    assert stored.price == Decimal("8.21")
    assert stored.at == OPENED_AT


def test_fill_price_comes_back_as_an_exact_decimal(session: Session) -> None:
    """A float that compares equal is still the bug."""
    session.add(a_fill(price=Decimal("8.21")))
    session.commit()
    session.expunge_all()

    stored = session.query(Fill).one()
    assert type(stored.price) is Decimal
    assert not isinstance(stored.price, float)
    assert str(stored.price) == "8.21"


def test_fill_price_is_stored_as_text(session: Session) -> None:
    """SQLite has no exact-decimal storage class; NUMERIC affinity means REAL."""
    session.add(a_fill(price=Decimal("8.21")))
    session.commit()

    assert (
        session.execute(text("SELECT typeof(price) FROM fill")).scalar_one() == "text"
    )


def test_fill_price_may_be_null_and_null_is_not_zero(session: Session) -> None:
    """The one nullable money column in this schema, and what it means.

    An ``OPEXC``/``OPASN`` settles at intrinsic value against the OCC strike,
    and an adjusted root is exactly the finding that the strike is no longer
    the price the deliverable changes hands at -- which is why the matcher
    already refuses the realized trade. NULL is where that refusal reaches the
    price. Zero would be a *price*: it is what an ``OPEXP`` closes at, and on
    a long it books a total loss.
    """
    session.add(a_fill(price=None))
    session.commit()
    session.expunge_all()

    stored = session.query(Fill).one()
    assert stored.price is None
    assert (
        session.execute(text("SELECT typeof(price) FROM fill")).scalar_one() == "null"
    )


def test_a_nullable_price_column_still_refuses_a_price_that_is_not_a_number(
    session: Session,
) -> None:
    """``ck_fill_price`` learned about NULL and nothing else.

    ``Decimal('NaN')`` *succeeds* on the way back out and then folds lifetime
    P&L to NaN in silence, so the shape CHECK is the only guard -- and
    widening a constraint to admit NULL is the easiest way to accidentally
    admit everything.
    """
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO fill (account, activity_id, symbol, side, qty, price, "
                "at) VALUES ('paper', 'nan-row', :symbol, 'buy', 1, 'NaN', :at)"
            ),
            # The timestamp as text: the CHECK under test fires on `price`
            # long before anything reads this, and binding a datetime through
            # raw SQL trips Python 3.12's deprecated sqlite3 adapter.
            {"symbol": IWM_PUT, "at": OPENED_AT.isoformat(sep=" ")},
        )
        session.commit()


def test_fill_rejects_a_float_price(session: Session) -> None:
    from sqlalchemy.exc import StatementError

    session.add(a_fill(price=8.21))
    with pytest.raises(StatementError) as caught:
        session.commit()
    assert isinstance(caught.value.orig, TypeError)


def test_the_composite_activity_id_fits(session: Session) -> None:
    """It is a timestamp concatenated with a UUID, not a bare UUID.

    Sized for the real thing, and the string-sortability of that prefix is
    what makes the column usable as a resume cursor.
    """
    session.add(a_fill())
    session.commit()

    stored = session.execute(text("SELECT activity_id FROM fill")).scalar_one()
    assert stored == REAL_ACTIVITY_ID
    assert "::" in stored


def test_activity_id_rejects_a_duplicate(session: Session) -> None:
    """Idempotent ingestion is this constraint and nothing else.

    Ingestion runs on startup and on an interval, re-pulling an overlapping
    window every time. Without the UNIQUE, the second pass doubles every
    fill and the matcher books every position twice.
    """
    session.add(a_fill())
    session.commit()
    session.add(a_fill(order_id="a-different-order"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_two_fills_of_the_same_order_are_fine(session: Session) -> None:
    """A partial fill is several activities against one order id."""
    session.add(a_fill(activity_id=REAL_ACTIVITY_ID, qty=1))
    session.add(a_fill(activity_id=REAL_ACTIVITY_ID.replace("598", "599"), qty=2))
    session.commit()
    assert session.query(Fill).count() == 2


def test_fill_rejects_an_unknown_account(session: Session) -> None:
    session.add(a_fill(account="live"))
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("side", FILL_SIDES)
def test_fill_accepts_every_real_side(session: Session, side: str) -> None:
    session.add(a_fill(side=side))
    session.commit()
    assert session.query(Fill).one().side == side


def test_fill_rejects_an_unknown_side(session: Session) -> None:
    session.add(a_fill(side="short"))
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("intent", POSITION_INTENTS)
def test_fill_accepts_every_real_position_intent(
    session: Session, intent: str
) -> None:
    session.add(a_fill(position_intent=intent))
    session.commit()
    assert session.query(Fill).one().position_intent == intent


def test_fill_rejects_an_unknown_position_intent(session: Session) -> None:
    session.add(a_fill(position_intent="open"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_position_intent_is_null_when_the_order_join_failed(session: Session) -> None:
    """NULL means "not known", and that is a state the broker really produces.

    ``position_intent`` lives on the order, not the fill, so the join can
    fail; a non-trade activity has no ``order_id`` to join through at all.
    Guessing here would defeat the column — ``buy`` being both BTO and BTC
    is the precise ambiguity it exists to resolve.
    """
    session.add(a_fill(order_id=None, position_intent=None))
    session.commit()
    session.expunge_all()

    stored = session.query(Fill).one()
    assert stored.position_intent is None
    assert stored.order_id is None


def test_fill_carries_the_group_id_a_non_trade_activity_links_through(
    session: Session,
) -> None:
    """Fee attribution for expiry, assignment and exercise runs through it.

    A non-trade activity has no ``order_id``, so ``group_id`` is the only
    linkage it gets. A table without this column cannot attribute those fees
    at all.
    """
    session.add(a_fill(order_id=None, group_id="1b2c3d4e-5f60-4711-8899-aabbccddeeff"))
    session.commit()
    session.expunge_all()

    assert session.query(Fill).one().group_id == "1b2c3d4e-5f60-4711-8899-aabbccddeeff"


@pytest.mark.parametrize("bad_qty", [0, -1, -2])
def test_fill_rejects_a_qty_that_is_not_a_positive_count(
    session: Session, bad_qty: int
) -> None:
    """``fill.qty`` stores the **normalised**, unsigned convention.

    A non-trade activity arrives with a *signed* ``qty`` and no ``side`` at
    all; ingestion turns the sign into a side and keeps the magnitude. A
    negative value in this column means that normalisation did not happen,
    which is how a matcher books an exercise backwards.
    """
    session.add(a_fill(qty=bad_qty))
    with pytest.raises(IntegrityError):
        session.commit()


def test_fill_rejects_a_fractional_qty(session: Session) -> None:
    """SQLite stores 1.5 in an INTEGER column as a REAL, unconverted.

    Options trade in whole contracts. Integer affinity alone does not
    enforce that, so the CHECK asserts the storage class.
    """
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO fill (account, activity_id, symbol, side, qty, price, at) "
                "VALUES ('paper', 'x::y', 'AAPL', 'buy', 1.5, '1', "
                "'2026-09-10 13:31:25')"
            )
        )
        session.commit()
    session.rollback()


def test_fill_timestamps_come_back_utc_aware(session: Session) -> None:
    eastern = datetime(2026, 9, 10, 9, 31, 25, tzinfo=ZoneInfo("America/New_York"))
    session.add(a_fill(at=eastern))
    session.commit()
    session.expunge_all()

    stored = session.query(Fill).one()
    assert stored.at == eastern
    assert stored.at.utcoffset() == timedelta(0)
    assert stored.at.hour == 13  # 09:31 EDT is 13:31 UTC


# --------------------------------------------------------------------- #
# realized_trade — the matcher's output, and a signed pnl
# --------------------------------------------------------------------- #


def test_realized_trade_round_trips(session: Session) -> None:
    session.add(a_trade())
    session.commit()
    session.expunge_all()

    stored = session.query(RealizedTrade).one()
    assert stored.id is not None
    assert stored.account == "paper"
    assert stored.symbol == IWM_PUT
    assert stored.opened_at == OPENED_AT
    assert stored.closed_at == CLOSED_AT
    assert stored.qty == 1
    assert stored.open_price == Decimal("8.21")
    assert stored.close_price == Decimal("8.14")
    assert stored.pnl == Decimal("-7.00")
    assert stored.pnl_pct == Decimal("-0.85")
    assert stored.close_kind == "fill"


def test_a_negative_pnl_round_trips_as_an_exact_decimal(session: Session) -> None:
    """The one value ``risk_limit``'s CHECK would have rejected outright.

    ``ck_risk_limit_value`` requires the text to start with a digit, because
    a negative ceiling silently stops the bot trading. ``pnl`` is signed and
    a loss is the normal case, so copying that constraint here would reject
    every losing trade.
    """
    session.add(a_trade(pnl=Decimal("-7.00")))
    session.commit()
    session.expunge_all()

    stored = session.query(RealizedTrade).one()
    assert type(stored.pnl) is Decimal
    assert not isinstance(stored.pnl, float)
    assert stored.pnl == Decimal("-7.00")
    assert str(stored.pnl) == "-7.00"
    assert stored.pnl < 0


def test_a_negative_pnl_is_stored_as_signed_text(session: Session) -> None:
    session.add(a_trade(pnl=Decimal("-7.00")))
    session.commit()

    row = session.execute(
        text("SELECT typeof(pnl), pnl FROM realized_trade")
    ).one()
    assert row[0] == "text"
    assert row[1] == "-7.00"


@pytest.mark.parametrize("raw", ["-7", "-7.00", "0", "0.00", "-0.07", "123.45", "7"])
def test_the_money_shape_check_admits_a_leading_minus(
    session: Session, raw: str
) -> None:
    """Every legitimate money value, including the signed and zero ones.

    Zero is admitted deliberately: an ``OPEXP`` closes at zero and a scratch
    trade nets zero. ``risk_limit`` rejects zero because a zero ceiling
    rejects every trade; nothing of the kind is true of a P&L.
    """
    session.add(a_trade(pnl=Decimal(raw)))
    session.commit()
    session.expunge_all()
    assert session.query(RealizedTrade).one().pnl == Decimal(raw)


@pytest.mark.parametrize(
    "raw",
    ["Infinity", "-Infinity", "NaN", "1E+2", "", "7.", "+7", "--7", "7.5.5", " 7", "-"],
    ids=repr,
)
def test_raw_sql_cannot_write_a_money_value_that_is_not_a_plain_decimal(
    session: Session, raw: str
) -> None:
    """``Money.process_result_value`` calls ``Decimal(text)`` on the way out.

    ``Decimal('NaN')`` succeeds. A NaN then makes every Python comparison
    False, so a "biggest loser" sort over loaded rows silently reorders
    around it and lifetime P&L becomes NaN. The shape CHECK is what stops a
    string like that reaching the column via raw SQL.
    """
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO realized_trade "
                "(account, symbol, opened_at, closed_at, qty, open_price, "
                " close_price, pnl, pnl_pct, close_kind) "
                "VALUES ('paper', 'AAPL', '2026-09-10 13:31:25', "
                "'2026-09-10 17:04:02', 1, '8.21', '8.14', :v, NULL, 'fill')"
            ),
            {"v": raw},
        )
        session.commit()
    session.rollback()


def test_pnl_pct_is_null_when_the_cost_basis_is_zero(session: Session) -> None:
    """A percentage of nothing is not zero percent.

    ``pnl_pct`` denominates on ``open_price × qty × multiplier``. Where that
    is zero the percentage has no value, and NULL says so rather than
    reporting a flat trade.
    """
    session.add(a_trade(pnl_pct=None))
    session.commit()
    session.expunge_all()
    assert session.query(RealizedTrade).one().pnl_pct is None


@pytest.mark.parametrize("kind", CLOSE_KINDS)
def test_realized_trade_accepts_every_close_kind(session: Session, kind: str) -> None:
    session.add(a_trade(close_kind=kind))
    session.commit()
    assert session.query(RealizedTrade).one().close_kind == kind


def test_realized_trade_rejects_an_unknown_close_kind(session: Session) -> None:
    session.add(a_trade(close_kind="closed"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_realized_trade_rejects_an_unknown_account(session: Session) -> None:
    session.add(a_trade(account="margin"))
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("bad_qty", [0, -1])
def test_realized_trade_rejects_a_qty_that_is_not_a_positive_count(
    session: Session, bad_qty: int
) -> None:
    session.add(a_trade(qty=bad_qty))
    with pytest.raises(IntegrityError):
        session.commit()


# --------------------------------------------------------------------- #
# pnl in SQL — the guard rail, and why it is one
# --------------------------------------------------------------------- #


@pytest.fixture
def three_trades(session: Session) -> Session:
    """A win, a small loss and the biggest loss. Text-sorted, ``120`` is last."""
    session.add(a_trade(symbol="AAPL", pnl=Decimal("120"), pnl_pct=Decimal("14.6")))
    session.add(a_trade(symbol="NVDA", pnl=Decimal("-7"), pnl_pct=Decimal("-0.85")))
    session.add(a_trade(symbol="AMD", pnl=Decimal("-410"), pnl_pct=Decimal("-9.9")))
    session.commit()
    return session


def test_the_storage_layer_really_does_sort_a_loss_wrongly(
    three_trades: Session,
) -> None:
    """Raw SQL, deliberately bypassing every guard below.

    ``pnl`` is TEXT, so ``ORDER BY pnl`` is lexicographic: ``'-410' <
    '-7' < '120'`` as strings, which puts the *biggest* loss first only by
    coincidence of the digits, and ``MIN`` picks by first character. Change
    the numbers to −7 and −41 and the order inverts. If this test ever
    starts failing, the storage representation changed and the guards below
    may no longer be needed. Until then they are.
    """
    assert three_trades.execute(
        text("SELECT pnl FROM realized_trade ORDER BY pnl ASC")
    ).scalars().all() == ["-410", "-7", "120"]
    assert (
        three_trades.execute(
            text("SELECT MAX(pnl) FROM realized_trade")
        ).scalar_one()
        == "120"
    )
    # A filter is worse than a sort, because it answers with a number rather
    # than an order. All three rows -- including the -410 loss -- report as
    # "better than -10", because '-4' > '-1' as text. So a screen for trades
    # that lost more than $10 returns every trade on the books and calls the
    # worst loss acceptable.
    assert (
        three_trades.execute(
            text("SELECT COUNT(*) FROM realized_trade WHERE pnl > '-10'")
        ).scalar_one()
        == 3
    )


def test_order_by_pnl_raises_rather_than_answering(three_trades: Session) -> None:
    """"Biggest loser" is the obvious query to write, and it must not work.

    The spec is explicit: ``pnl`` is signed, so this is a Python sort over
    the loaded rows. It raises rather than returning a plausible wrong
    ordering, because a plausible wrong ordering is what gets shipped.
    """
    with pytest.raises(MoneyComparisonError):
        three_trades.execute(
            select(RealizedTrade.symbol).order_by(RealizedTrade.pnl)
        ).all()


def test_min_pnl_raises_rather_than_naming_the_wrong_loser(
    three_trades: Session,
) -> None:
    with pytest.raises(MoneyComparisonError):
        three_trades.execute(select(func.min(RealizedTrade.pnl))).scalar_one()


def test_sum_pnl_raises_rather_than_answering_lifetime_pnl(
    three_trades: Session,
) -> None:
    """Lifetime realized P&L is a Python fold, not ``SUM``.

    Which is the second-order consequence worth knowing before step 7:
    every lifetime figure on the Activity page — total P&L, average win,
    average loss, win rate — loads rows and folds them in Python, so the
    page's row budget is a real constraint rather than an optimisation.
    """
    with pytest.raises(MoneyComparisonError):
        three_trades.execute(select(func.sum(RealizedTrade.pnl))).scalar_one()


def test_avg_pnl_raises(three_trades: Session) -> None:
    with pytest.raises(MoneyComparisonError):
        three_trades.execute(select(func.avg(RealizedTrade.pnl))).scalar_one()


def test_filtering_for_losers_in_sql_raises() -> None:
    """``where(pnl < 0)`` reads as correct and is not."""
    with pytest.raises(MoneyComparisonError):
        RealizedTrade.pnl < Decimal("0")


def test_ordering_pnl_pct_and_the_prices_raises_too() -> None:
    """Every money column, not only the signed one."""
    for column in (
        RealizedTrade.pnl_pct,
        RealizedTrade.open_price,
        RealizedTrade.close_price,
        Fill.price,
        MlegGroup.net_price,
    ):
        with pytest.raises(MoneyComparisonError):
            column.desc()


def test_counting_and_reading_rows_is_untouched(three_trades: Session) -> None:
    """The guard blocks questions about ordering, not ordinary use."""
    assert (
        three_trades.execute(select(func.count(RealizedTrade.id))).scalar_one() == 3
    )


def test_the_python_fold_gives_the_true_answers(three_trades: Session) -> None:
    """The supported way to ask all three questions."""
    trades = three_trades.scalars(select(RealizedTrade)).all()
    pnls = [t.pnl for t in trades]

    assert sum(pnls) == Decimal("-297")
    assert min(pnls) == Decimal("-410")
    worst = min(trades, key=lambda t: t.pnl)
    assert worst.symbol == "AMD"
    assert all(type(p) is Decimal for p in pnls)


# --------------------------------------------------------------------- #
# mleg_group + mleg_leg — the grouping evidence
# --------------------------------------------------------------------- #


def test_mleg_group_round_trips_with_its_legs(session: Session) -> None:
    group = a_group()
    group.legs = [
        MlegLeg(
            symbol="AMD261120P00470000",
            ratio=1,
            side="sell_short",
            position_intent="sell_to_open",
        ),
        MlegLeg(
            symbol="AMD261120P00460000",
            ratio=1,
            side="buy",
            position_intent="buy_to_open",
        ),
    ]
    session.add(group)
    session.commit()
    session.expunge_all()

    stored = session.query(MlegGroup).one()
    assert stored.id is not None
    assert stored.account == "paper"
    assert stored.order_id == "7f1c9a52-0b64-4d2e-8a11-3c6f5e9d0a77"
    assert stored.opened_at == OPENED_AT
    assert type(stored.net_price) is Decimal
    assert stored.net_price == Decimal("1.35")

    legs = {leg.symbol: leg for leg in stored.legs}
    assert set(legs) == {"AMD261120P00470000", "AMD261120P00460000"}
    assert legs["AMD261120P00470000"].side == "sell_short"
    assert legs["AMD261120P00470000"].position_intent == "sell_to_open"
    assert legs["AMD261120P00470000"].ratio == 1
    assert all(leg.group_id == stored.id for leg in stored.legs)


def test_mleg_group_order_id_rejects_a_duplicate(session: Session) -> None:
    """One parent mleg order proposes exactly one group.

    Which also makes re-ingestion idempotent for groups, and supplies the
    index the grouper's lookup-by-order_id needs.
    """
    session.add(a_group())
    session.commit()
    session.add(a_group(opened_at=CLOSED_AT))
    with pytest.raises(IntegrityError):
        session.commit()


def test_mleg_group_net_price_may_be_unknown(session: Session) -> None:
    """Direction comes from the net price, so an absent one means "unknown".

    NULL rather than a substituted number: a group whose direction cannot be
    established must not be labelled debit or credit, exactly as
    ``risk_limit_value`` returns ``None`` rather than inventing a ceiling.
    """
    session.add(a_group(net_price=None))
    session.commit()
    session.expunge_all()
    assert session.query(MlegGroup).one().net_price is None


def test_mleg_group_rejects_an_unknown_account(session: Session) -> None:
    session.add(a_group(account="live"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_leg_needs_a_group_that_exists(session: Session) -> None:
    """``PRAGMA foreign_keys=ON`` is set on every connection.

    An orphan leg is grouping evidence pointing at nothing, which is worse
    than no evidence.
    """
    session.add(
        MlegLeg(
            group_id=999,
            symbol="AMD261120P00470000",
            ratio=1,
            side="buy",
            position_intent="buy_to_open",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_a_group_deletes_its_legs(session: Session) -> None:
    group = a_group()
    group.legs = [
        MlegLeg(
            symbol="AMD261120P00470000",
            ratio=1,
            side="buy",
            position_intent="buy_to_open",
        )
    ]
    session.add(group)
    session.commit()

    session.delete(group)
    session.commit()
    assert session.query(MlegLeg).count() == 0


def test_a_group_cannot_hold_the_same_symbol_twice(session: Session) -> None:
    """Rejected loudly rather than merged.

    Leg ratios must be in simplest form — the GCD across a group's
    ``ratio_qty`` values is 1 — so two legs on one symbol would have
    combined into one. A duplicate is a parse error, and the composite
    primary key is what makes it one.
    """
    group = a_group()
    session.add(group)
    session.commit()

    session.add(
        MlegLeg(
            group_id=group.id,
            symbol="AMD261120P00470000",
            ratio=1,
            side="buy",
            position_intent="buy_to_open",
        )
    )
    session.commit()
    session.add(
        MlegLeg(
            group_id=group.id,
            symbol="AMD261120P00470000",
            ratio=2,
            side="sell_short",
            position_intent="sell_to_open",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("bad_ratio", [0, -1])
def test_a_leg_ratio_must_be_a_positive_count(
    session: Session, bad_ratio: int
) -> None:
    group = a_group()
    session.add(group)
    session.commit()

    session.add(
        MlegLeg(
            group_id=group.id,
            symbol="AMD261120P00470000",
            ratio=bad_ratio,
            side="buy",
            position_intent="buy_to_open",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_fractional_leg_ratio_is_a_parse_error_not_a_spread(
    session: Session,
) -> None:
    """SQLite would store 1.5 in an INTEGER column as a REAL, unconverted."""
    group = a_group()
    session.add(group)
    session.commit()

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO mleg_leg (group_id, symbol, ratio, side, "
                "position_intent) VALUES (:g, 'AMD261120P00470000', 1.5, "
                "'buy', 'buy_to_open')"
            ),
            {"g": group.id},
        )
        session.commit()
    session.rollback()


def test_a_leg_rejects_an_unknown_side(session: Session) -> None:
    group = a_group()
    session.add(group)
    session.commit()

    session.add(
        MlegLeg(
            group_id=group.id,
            symbol="AMD261120P00470000",
            ratio=1,
            side="short",
            position_intent="buy_to_open",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_leg_rejects_an_unknown_position_intent(session: Session) -> None:
    group = a_group()
    session.add(group)
    session.commit()

    session.add(
        MlegLeg(
            group_id=group.id,
            symbol="AMD261120P00470000",
            ratio=1,
            side="buy",
            position_intent="open",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_leg_requires_a_position_intent(session: Session) -> None:
    """Unlike ``fill.position_intent``, and the asymmetry is deliberate.

    A leg is read off the mleg order object itself, where
    ``position_intent`` is always present. A fill's intent comes from a join
    to that order, and the join can fail.
    """
    group = a_group()
    session.add(group)
    session.commit()

    session.add(
        MlegLeg(
            group_id=group.id,
            symbol="AMD261120P00470000",
            ratio=1,
            side="buy",
            position_intent=None,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_leg_carries_no_account_of_its_own(session: Session) -> None:
    """It inherits the book from its group.

    A denormalised copy is a second answer to "which book is this in", and
    two answers is how a cash leg ends up under a paper group.
    """
    assert not hasattr(MlegLeg, "account")
