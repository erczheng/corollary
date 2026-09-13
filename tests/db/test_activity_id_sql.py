"""``activity_id`` in SQL — the operations that would silently skip rows.

Design decision 13. ``fill.activity_id`` is composite: a 17-digit timestamp,
``::``, then a UUID. The shape is uniform across every activity type, which is
exactly what makes it look orderable::

    FILL   20260910131125217::a9d576c2-…     real time, 13:11:25.217
    FEE    20260910000000000::a2a0c406-…     zeroed
    JNLC   20260805000000000::4b47d1d0-…     zeroed

**Non-trade rows carry a date-only id with the time zeroed out** — every
``FEE``, every journal, and every ``OPEXP``, ``OPEXC`` and ``OPASN``. So within
one day all of them sort *below* every fill of that day.

Take the ingestion cursor as ``MAX(activity_id)`` and it lands on the day's
last **fill**, with that same day's expiry and assignment rows sitting under
it. The next pull asks for everything newer and never sees them again — not
late, not duplicated, gone. Missing rows become missing realized trades become
a wrong lifetime P&L, with nothing anywhere saying so. The first symptom is a
terminal that believes you never win.

``test_max_activity_id_really_does_hide_the_days_option_events`` measures that
in raw SQL, deliberately outside every guard, so the danger stays documented in
executable form. ``test_ordering_by_activity_id_really_does_invert_a_vertical``
measures the second, independent reason: the id does not sort chronologically
even ignoring the zeroed halves, because stamps repeat and the UUID then breaks
the tie arbitrarily. Every other test here proves the ORM refuses to ask.

The resume cursor is the vendor's own ``since_id`` page token, which
``corollary.engine.ingest`` already takes. Sequencing is ``Fill.at``, per
contract symbol — ``ix_fill_account_at`` and ``ix_fill_symbol``.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import Engine, String, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corollary.db.models import Fill
from corollary.db.types import ActivityIdComparisonError

# --------------------------------------------------------------------- #
# Real ids, from the probe recorded in the design spec
# --------------------------------------------------------------------- #

#: A fill on 2026-09-10 at 13:11:25.217 UTC. The stamp is the real time.
FILL_ID = "20260910131125217::a9d576c2-3f08-4b1e-9d77-1c0a5e6f2b44"

#: A fee on the *same day*, with the time zeroed. Lexicographically this is
#: below every fill of 2026-09-10, which is the whole trap.
FEE_ID = "20260910000000000::a2a0c406-7d51-4e2a-b3c9-6f8d1e0a5b72"

#: An option expiry, same day, same zeroed shape. This is the row whose loss
#: goes missing — an ``OPEXP`` is the most common way an option position ends.
OPEXP_ID = "20260910000000000::c4f10b8e-2a63-4d95-8e07-9b3c1d7a0f56"

#: A journal from five weeks earlier, also zeroed.
JNLC_ID = "20260805000000000::4b47d1d0-6c29-4a83-9f15-2e7b8d0c3a61"

AT = datetime(2026, 9, 10, 13, 11, 25, 217000, tzinfo=timezone.utc)
IWM_PUT = "IWM261120P00280000"


def a_fill(**overrides: object) -> Fill:
    """A well-formed row, so each test overrides only what it probes."""
    fields: dict[str, object] = {
        "account": "paper",
        "activity_id": FILL_ID,
        "order_id": "a9d576c2-3f08-4b1e-9d77-1c0a5e6f2b44",
        "group_id": None,
        "symbol": IWM_PUT,
        "side": "buy",
        "position_intent": "buy_to_open",
        "qty": 1,
        "price": Decimal("8.21"),
        "at": AT,
    }
    fields.update(overrides)
    return Fill(**fields)


@pytest.fixture
def one_day(session: Session) -> Session:
    """One trading day as Alpaca really returns it: a fill and three zeroed rows."""
    session.add(a_fill(activity_id=FILL_ID, at=AT))
    session.add(
        a_fill(
            activity_id=FEE_ID,
            order_id=None,
            symbol="USD",
            position_intent=None,
            at=AT - timedelta(minutes=5),
        )
    )
    session.add(
        a_fill(
            activity_id=OPEXP_ID,
            order_id=None,
            side="sell",
            position_intent=None,
            at=AT + timedelta(hours=7),
        )
    )
    session.add(
        a_fill(
            activity_id=JNLC_ID,
            order_id=None,
            symbol="USD",
            position_intent=None,
            at=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc),
        )
    )
    session.commit()
    return session


# --------------------------------------------------------------------- #
# The two measurements: the wrong answers are real
# --------------------------------------------------------------------- #


def test_max_activity_id_really_does_hide_the_days_option_events(
    one_day: Session,
) -> None:
    """Raw SQL, deliberately bypassing the guard below.

    If this test ever starts failing, the id shape changed and the guard may
    no longer be needed. Until then it is.
    """
    cursor = one_day.execute(text("SELECT MAX(activity_id) FROM fill")).scalar_one()
    assert cursor == FILL_ID  # the day's last *fill*, not the day's last row

    after = (
        one_day.execute(
            text("SELECT activity_id FROM fill WHERE activity_id > :c"),
            {"c": cursor},
        )
        .scalars()
        .all()
    )
    # Nothing is newer, so the next pull asks for nothing — and the expiry,
    # the fee and the journal are never seen again.
    assert after == []

    # The mechanism, stated rather than implied: the expiry is *later* in
    # wall-clock time than the fill, and *lower* as text, because its stamp
    # half is zeroed. Both halves of that sentence have to be true for the
    # trap to exist, so both are asserted.
    rows = one_day.execute(text("SELECT activity_id, at FROM fill")).all()
    times = {activity_id: at for activity_id, at in rows}
    assert times[OPEXP_ID] > times[FILL_ID]
    assert OPEXP_ID < FILL_ID
    assert OPEXP_ID.startswith("20260910000000000")


def test_ordering_by_activity_id_really_does_invert_a_vertical(
    session: Session,
) -> None:
    """The second, independent reason: the composite id is not chronological.

    Only the 17-digit stamp half orders, and **stamps repeat** — the stamp is
    a millisecond and two legs of one spread fill inside the same one. The
    UUID half then breaks the tie arbitrarily. The shape here is the shape the
    real recording has; ``tests/engine/execution/test_alpaca_activities.py``
    holds the recorded measurement itself.
    """
    stamp = "20260910131125438"
    early_leg = f"{stamp}::f9e0d1c2-0000-4000-8000-000000000001"  # sorts second
    late_leg = f"{stamp}::0a1b2c3d-0000-4000-8000-000000000002"  # sorts first
    session.add(
        a_fill(
            activity_id=early_leg,
            symbol="IWM261120P00280000",
            at=datetime(2026, 9, 10, 13, 11, 25, 438263, tzinfo=timezone.utc),
        )
    )
    session.add(
        a_fill(
            activity_id=late_leg,
            symbol="IWM261120P00275000",
            at=datetime(2026, 9, 10, 13, 11, 25, 438268, tzinfo=timezone.utc),
        )
    )
    session.commit()

    by_id = (
        session.execute(text("SELECT activity_id FROM fill ORDER BY activity_id ASC"))
        .scalars()
        .all()
    )
    by_time = (
        session.execute(text("SELECT activity_id FROM fill ORDER BY at ASC"))
        .scalars()
        .all()
    )
    assert by_time == [early_leg, late_leg]
    assert by_id == [late_leg, early_leg]
    assert by_id == list(reversed(by_time))


# --------------------------------------------------------------------- #
# ... and the ORM refuses to ask any of it
# --------------------------------------------------------------------- #


def test_max_raises_instead_of_answering_with_the_days_last_fill(
    one_day: Session,
) -> None:
    """The regression decision 13 names. There is **no carve-out for MAX**.

    An earlier draft proposed one, on the grounds that ``MAX(activity_id)`` is
    a legitimate resume cursor. It is precisely the opposite.
    """
    with pytest.raises(ActivityIdComparisonError):
        one_day.execute(select(func.max(Fill.activity_id))).scalar_one()


def test_min_raises(one_day: Session) -> None:
    with pytest.raises(ActivityIdComparisonError):
        one_day.execute(select(func.min(Fill.activity_id))).scalar_one()


def test_sum_raises(one_day: Session) -> None:
    """SQLite coerces the leading digits to a float and sums them."""
    with pytest.raises(ActivityIdComparisonError):
        one_day.execute(select(func.sum(Fill.activity_id))).scalar_one()


def test_avg_raises(one_day: Session) -> None:
    with pytest.raises(ActivityIdComparisonError):
        one_day.execute(select(func.avg(Fill.activity_id))).scalar_one()


def test_total_raises(one_day: Session) -> None:
    with pytest.raises(ActivityIdComparisonError):
        one_day.execute(select(func.total(Fill.activity_id))).scalar_one()


def test_count_is_still_allowed(one_day: Session) -> None:
    """Counting rows asks nothing about the value."""
    assert one_day.execute(select(func.count(Fill.activity_id))).scalar_one() == 4


def test_order_by_the_bare_column_raises(one_day: Session) -> None:
    """The form people actually write — and not an operator call."""
    with pytest.raises(ActivityIdComparisonError):
        one_day.execute(select(Fill.activity_id).order_by(Fill.activity_id)).all()


def test_legacy_query_order_by_raises(one_day: Session) -> None:
    with pytest.raises(ActivityIdComparisonError):
        one_day.query(Fill).order_by(Fill.activity_id).all()


def test_asc_raises() -> None:
    with pytest.raises(ActivityIdComparisonError):
        Fill.activity_id.asc()


def test_desc_raises() -> None:
    with pytest.raises(ActivityIdComparisonError):
        Fill.activity_id.desc()


def test_greater_than_raises_because_that_is_the_resume_query() -> None:
    """``WHERE activity_id > :cursor`` is the skip, written out."""
    with pytest.raises(ActivityIdComparisonError):
        Fill.activity_id > FILL_ID


def test_greater_than_or_equal_raises() -> None:
    with pytest.raises(ActivityIdComparisonError):
        Fill.activity_id >= FILL_ID


def test_less_than_raises() -> None:
    with pytest.raises(ActivityIdComparisonError):
        Fill.activity_id < FILL_ID


def test_less_than_or_equal_raises() -> None:
    with pytest.raises(ActivityIdComparisonError):
        Fill.activity_id <= FILL_ID


def test_between_raises() -> None:
    with pytest.raises(ActivityIdComparisonError):
        Fill.activity_id.between(JNLC_ID, FILL_ID)


def test_equality_raises_even_though_an_id_has_one_spelling() -> None:
    """Not the ``Money`` reason, and the docstring says which reason it is.

    ``'7'`` and ``'7.0'`` spell one number two ways, which is why ``Money``
    blocks ``==``. An activity id has exactly one spelling, so a point lookup
    would answer correctly. It is blocked anyway: ``==`` and ``>=`` differ by
    one character on the same column, and the guard that permits the first
    teaches that the column is ordinarily comparable. Idempotency does not
    need it — ``uq_fill_activity_id`` is DDL and the match is a Python dict.
    """
    with pytest.raises(ActivityIdComparisonError):
        Fill.activity_id == FILL_ID


def test_inequality_raises() -> None:
    with pytest.raises(ActivityIdComparisonError):
        Fill.activity_id != FILL_ID


def test_in_raises() -> None:
    with pytest.raises(ActivityIdComparisonError):
        Fill.activity_id.in_([FILL_ID, FEE_ID])


def test_not_in_raises() -> None:
    with pytest.raises(ActivityIdComparisonError):
        Fill.activity_id.not_in([FILL_ID])


def test_is_null_is_still_allowed() -> None:
    """NULL asks nothing about the value, exactly as for ``Money``."""
    assert Fill.activity_id.is_(None) is not None
    assert (Fill.activity_id == None) is not None  # noqa: E711


def test_the_message_names_the_rule_and_says_what_to_do_instead() -> None:
    """Rule 8: a rejection records the rule and the inputs, never just 'no'."""
    with pytest.raises(ActivityIdComparisonError) as caught:
        Fill.activity_id > FILL_ID
    message = str(caught.value)
    assert "fill.activity_id" in message
    assert "'>'" in message
    assert "since_id" in message
    assert "Fill.at" in message


def test_the_max_message_names_max_and_says_what_to_do_instead(
    one_day: Session,
) -> None:
    with pytest.raises(ActivityIdComparisonError) as caught:
        one_day.execute(select(func.max(Fill.activity_id))).scalar_one()
    message = str(caught.value)
    assert "MAX()" in message
    assert "since_id" in message


def test_the_order_by_message_names_the_column(one_day: Session) -> None:
    with pytest.raises(ActivityIdComparisonError) as caught:
        one_day.execute(select(Fill.activity_id).order_by(Fill.activity_id)).all()
    message = str(caught.value)
    assert "ORDER BY" in message
    assert "fill.activity_id" in message
    assert "Fill.at" in message


# --------------------------------------------------------------------- #
# Ordinary use is untouched — and so is idempotency
# --------------------------------------------------------------------- #


def test_reading_and_writing_activity_id_is_untouched(one_day: Session) -> None:
    """The guard blocks questions about ordering, not ordinary use."""
    rows = one_day.scalars(select(Fill).where(Fill.account == "paper")).all()
    assert {row.activity_id for row in rows} == {
        FILL_ID,
        FEE_ID,
        OPEXP_ID,
        JNLC_ID,
    }
    assert all(type(row.activity_id) is str for row in rows)


def test_the_unique_constraint_still_rejects_a_duplicate(session: Session) -> None:
    """Idempotent ingestion is this constraint and nothing else.

    It is DDL, so no comparator can weaken it — this test proves the new type
    did not change the DDL out from under it.
    """
    session.add(a_fill())
    session.commit()
    session.add(a_fill(order_id="a-different-order"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_the_ingestion_upsert_shape_still_runs(session: Session) -> None:
    """``IngestService._write_fills``, reproduced exactly.

    It loads the book with ``select(Fill).where(Fill.account == …)`` and
    matches on ``activity_id`` in a **Python dict** — no SQL equality anywhere,
    which is why blocking ``==`` costs it nothing. Running it twice must leave
    the table as the first run did.
    """
    desired = {FILL_ID: 1, FEE_ID: 2, OPEXP_ID: 3}

    def upsert() -> tuple[int, int]:
        existing = {
            row.activity_id: row
            for row in session.scalars(select(Fill).where(Fill.account == "paper"))
        }
        written = updated = 0
        for activity_id, qty in desired.items():
            row = existing.get(activity_id)
            if row is None:
                session.add(
                    a_fill(activity_id=activity_id, qty=qty, order_id=None)
                )
                written += 1
            elif row.qty != qty:
                row.qty = qty
                updated += 1
        session.flush()
        return written, updated

    assert upsert() == (3, 0)
    session.commit()
    assert upsert() == (0, 0)
    session.commit()
    assert session.query(Fill).count() == 3


# --------------------------------------------------------------------- #
# The DDL is byte-identical, so no migration is owed
# --------------------------------------------------------------------- #


def test_the_column_still_emits_varchar_128(engine: Engine) -> None:
    """``String(128)`` before, ``String(128)`` after — 0002 needs no revision."""
    columns = {c["name"]: c for c in inspect(engine).get_columns("fill")}
    activity_id = columns["activity_id"]
    assert str(activity_id["type"]) == "VARCHAR(128)"
    assert activity_id["nullable"] is False


def test_the_create_table_text_is_unchanged(session: Session) -> None:
    sql = session.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='fill'")
    ).scalar_one()
    assert "activity_id VARCHAR(128) NOT NULL" in sql
    assert "UNIQUE (activity_id)" in sql


def test_the_type_is_still_a_string_underneath() -> None:
    """A ``TypeDecorator`` over ``String``, so every dialect emits what it did."""
    column = Fill.__table__.c.activity_id
    assert isinstance(column.type.impl, String)
    assert column.type.impl.length == 128
