"""``ledger_rejection``: the constraints, at the database rather than in prose.

The writer in ``engine/ingest.py`` is what keeps this table honest — it
reconciles against each pass rather than appending — and
``tests/engine/test_ingest_rejections.py`` is where that behaviour is proved.
What is tested here is the floor underneath it: what the schema refuses even
if a future writer, or a second one, gets it wrong.

Two rows for one refusal is the failure that matters. The table exists to
answer *how many trades are missing*, and a duplicate does not make that
number fuzzy — it makes it wrong, in the direction of alarm.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corollary.db.models import RejectionRecord
from corollary.engine.ingest import IngestRule
from corollary.engine.ledger import RejectionRule

AT = datetime(2026, 9, 13, 14, 30, tzinfo=timezone.utc)


def a_row(**overrides: object) -> RejectionRecord:
    values: dict[str, object] = {
        "account": "paper",
        "source": "ledger",
        "rule": RejectionRule.UNVERIFIED_DELIVERABLE.value,
        "fingerprint": "0" * 64,
        "symbol": "GME1261016C00003000",
        "order_id": None,
        "activity_ids": ["20261016000000000::an-assignment"],
        "detail": "the deliverable is not 100 shares, so the strike is not a price",
        "inputs": {"root_symbol": "GME1", "underlying_symbol": "GME"},
        "at": AT,
        "first_seen": AT,
        "activity_at": None,
        "correlation_id": "a-pass",
    }
    values.update(overrides)
    return RejectionRecord(**values)


def test_a_row_round_trips_with_its_json_columns_intact(session: Session) -> None:
    """The two JSON columns come back as a list and a dict, not as text.

    ``activity_ids`` is what the writer reads to decide whether a pass
    re-examined a row's subject, and a string that merely looks like a list
    would make that check silently answer "no" forever.
    """
    session.add(a_row())
    session.commit()
    session.expunge_all()

    row = session.query(RejectionRecord).one()
    assert row.activity_ids == ["20261016000000000::an-assignment"]
    assert row.inputs == {"root_symbol": "GME1", "underlying_symbol": "GME"}
    assert row.at == AT
    assert row.first_seen == AT
    assert row.activity_at is None


def test_a_row_cannot_be_written_without_saying_when_the_gap_opened(
    session: Session,
) -> None:
    """``first_seen`` is NOT NULL, because *since when* is not optional.

    ``at`` is refreshed on every reassertion, so a row without ``first_seen``
    can only say *still true as of* — and a stale duplicate then looks exactly
    like a gap that opened today. The writer sets it on insert and never
    updates it; the column is what stops a second writer forgetting to.
    """
    session.add(a_row(first_seen=None))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_one_refusal_cannot_become_two_rows(session: Session) -> None:
    """``UNIQUE (account, fingerprint)`` — the count is the whole point."""
    session.add(a_row())
    session.commit()

    session.add(a_row(detail="the same refusal, said differently"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_the_same_fingerprint_in_the_other_book_is_a_different_row(
    session: Session,
) -> None:
    """The UNIQUE is per account, because the two books refuse separately.

    A paper pass and a cash pass can refuse the same contract for the same
    reason, and those are two gaps in two P&L figures, not one.
    """
    session.add(a_row())
    session.add(a_row(account="cash"))
    session.commit()

    assert session.query(RejectionRecord).count() == 2


def test_a_book_that_is_not_a_book_is_refused(session: Session) -> None:
    session.add(a_row(account="margin"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_a_source_outside_the_two_vocabularies_is_refused(session: Session) -> None:
    """Caught in Python before the INSERT, and by a CHECK if it ever is not.

    ``rule`` deliberately has no such list — the enums are the authority and a
    constraint would make adding a rule a migration. ``source`` is different:
    it says *which* enum, and a third value means a third vocabulary nobody
    has read.
    """
    with pytest.raises(ValueError, match="is not one of"):
        a_row(source="scanner")


def test_both_vocabularies_are_storable(session: Session) -> None:
    """One table, two enums, no third spelling in between."""
    session.add(a_row())
    session.add(
        a_row(
            source="ingest",
            rule=IngestRule.SETTLEMENT_PRICE_UNAVAILABLE.value,
            fingerprint="1" * 64,
        )
    )
    session.commit()

    rules = {row.rule for row in session.query(RejectionRecord)}
    assert rules == {
        RejectionRule.UNVERIFIED_DELIVERABLE.value,
        IngestRule.SETTLEMENT_PRICE_UNAVAILABLE.value,
    }
