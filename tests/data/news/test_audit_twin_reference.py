"""Owner decision Q24b (2026-11-11): a twin group is graded from its earliest stamp.

A label's reference publication time is the **earliest** ``published_at``
among every article in its duplicate group -- the canonical row and every
member -- not the stamp of whichever copy the label happens to sit on. A
label on an ungrouped article is unchanged.

Why: Finnhub stamps its Yahoo copy of a Motley Fool story exactly 20 minutes
late, and in 66 of the 92 pairs relinked on 2026-11-10 the late copy is the
canonical one (stored first after downtime), so its 1h window measured
minutes 20 to 80 after the story.

The rule is applied where the runner builds :class:`AuditLabel`, so the
windows, the stored ``reference_at``, decision 25's verdict session and R3's
``label_session`` all follow from one place. All data here is SYNTHETIC.
"""

from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.data.news import audit_runner
from corollary.data.news.audit import AuditLabel, label_session, reference_instant
from corollary.db.models import LabelOutcome
from tests.data.news.test_audit_runner import (
    _ET,
    MONDAY,
    MONDAY_10,
    TUESDAY,
    UTC,
    FakeBars,
    _article,
    _deps,
    _label_on,
    _outcomes_by_label,
    _run,
    _sessions,
    _verdict,
    db_engine,
)

__all__ = ["db_engine"]  # the fixture, re-exported so pytest finds it here

#: The Yahoo copy's lag behind the Motley Fool original, as measured.
_YAHOO_LAG = timedelta(minutes=20)


def _loaded(engine: Engine) -> dict[int, AuditLabel]:
    labels, _, _ = audit_runner._load_labels(_sessions(engine))
    return {label.label_id: label for label in labels}


# ------------------------------------------------------------- the loader


@pytest.mark.parametrize("held_by", ["canonical", "member"])
def test_a_twin_pair_is_graded_from_the_earlier_stamp_whichever_copy_holds_the_label(
    db_engine: Engine, held_by: str
) -> None:
    """The late copy is canonical (stored first); the label reads the early stamp."""
    with Session(db_engine) as session:
        late_canonical = _article(session, at=MONDAY_10 + _YAHOO_LAG)
        early_member = _article(session, at=MONDAY_10, canonical_id=late_canonical)
        holder = late_canonical if held_by == "canonical" else early_member
        label_id = _label_on(session, holder, ticker="AAPL", at=MONDAY_10 + _YAHOO_LAG)
        session.commit()

    label = _loaded(db_engine)[label_id]
    assert label.published_at == MONDAY_10
    assert reference_instant(label) == MONDAY_10


def test_a_three_copy_group_reads_its_earliest_member(db_engine: Engine) -> None:
    earliest = MONDAY_10 - timedelta(minutes=3)
    with Session(db_engine) as session:
        canonical = _article(session, at=MONDAY_10 + _YAHOO_LAG)
        _article(session, at=earliest, canonical_id=canonical)
        later_member = _article(session, at=MONDAY_10 + timedelta(minutes=5), canonical_id=canonical)
        label_id = _label_on(session, later_member, ticker="AAPL", at=MONDAY_10 + _YAHOO_LAG)
        session.commit()

    assert _loaded(db_engine)[label_id].published_at == earliest


def test_an_ungrouped_article_is_unchanged_and_no_group_bleeds_into_it(
    db_engine: Engine,
) -> None:
    alone_at = MONDAY_10 + timedelta(minutes=7)
    with Session(db_engine) as session:
        # A group with an earlier stamp, stored first, beside it.
        canonical = _article(session, at=MONDAY_10 + _YAHOO_LAG)
        _article(session, at=MONDAY_10 - timedelta(hours=1), canonical_id=canonical)
        alone = _article(session, at=alone_at)
        label_id = _label_on(session, alone, ticker="AAPL", at=alone_at)
        session.commit()

    label = _loaded(db_engine)[label_id]
    assert label.published_at == alone_at
    assert label.group_id == alone


def test_the_session_follows_the_earliest_stamp(db_engine: Engine) -> None:
    """Earliest copy at Monday 15:50 ET (in session); the label's copy at 16:10
    (after the close, so on its own stamp it would be Tuesday's)."""
    in_session = datetime.combine(MONDAY, time(15, 50), tzinfo=_ET).astimezone(UTC)
    after_close = in_session + _YAHOO_LAG
    with Session(db_engine) as session:
        canonical = _article(session, at=after_close)
        _article(session, at=in_session, canonical_id=canonical)
        label_id = _label_on(session, canonical, ticker="AAPL", at=after_close)
        session.commit()

    label = _loaded(db_engine)[label_id]
    assert label_session(after_close) == TUESDAY  # the old, per-copy answer
    assert label_session(reference_instant(label)) == MONDAY


def test_the_future_stamp_guard_reads_the_groups_earliest_stamp(db_engine: Engine) -> None:
    """Decision 1's guard still applies, to the stamp the label is now graded from.

    A copy stamped years ahead (a vendor typo) no longer drags the label to
    ``labeled_at`` when its twin carries an honest stamp; a group whose
    earliest stamp is itself impossibly late still falls back to ``labeled_at``.
    """
    labeled = MONDAY_10 + timedelta(minutes=30)
    typo = datetime(2062, 11, 16, 15, 0, tzinfo=UTC)
    with Session(db_engine) as session:
        bogus = _article(session, at=typo)
        _article(session, at=MONDAY_10, canonical_id=bogus)
        rescued = _label_on(session, bogus, ticker="AAPL", at=labeled)

        all_late = _article(session, at=typo)
        _article(session, at=typo + timedelta(minutes=20), canonical_id=all_late)
        guarded = _label_on(session, all_late, ticker="AAPL", at=labeled)
        session.commit()

    loaded = _loaded(db_engine)
    assert reference_instant(loaded[rescued]) == MONDAY_10
    assert reference_instant(loaded[guarded]) == labeled


# ------------------------------------------------------------- the runner


def test_the_runner_starts_both_windows_at_the_groups_earliest_stamp(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        late_canonical = _article(session, at=MONDAY_10 + _YAHOO_LAG)
        _article(session, at=MONDAY_10, canonical_id=late_canonical)
        label_id = _label_on(session, late_canonical, ticker="AAPL", at=MONDAY_10 + _YAHOO_LAG)
        session.commit()

    _run(_deps(db_engine, bars=FakeBars()))

    with Session(db_engine) as session:
        rows = {
            row.window: row
            for row in session.scalars(select(LabelOutcome).where(LabelOutcome.label_id == label_id))
        }
    assert set(rows) == {"1h", "1d"}
    for row in rows.values():
        assert row.skip_reason is None
        # In session and past the first minute: the price is taken at the
        # reference instant itself, the Fool copy's stamp.
        assert row.reference_at == MONDAY_10
    assert rows["1h"].outcome_at == MONDAY_10 + timedelta(minutes=60)
    # 10:00 ET Monday plus 390 session minutes is 10:00 ET Tuesday.
    assert rows["1d"].outcome_at == datetime.combine(TUESDAY, time(10), tzinfo=_ET).astimezone(UTC)


def test_decision_25_reads_the_verdict_of_the_earliest_stamps_session(db_engine: Engine) -> None:
    """Off-watch: Monday's verdict passes, Tuesday's fails. The label's own copy
    is after Monday's close (Tuesday's session), its twin inside Monday's."""
    in_session = datetime.combine(MONDAY, time(15, 50), tzinfo=_ET).astimezone(UTC)
    after_close = in_session + _YAHOO_LAG
    with Session(db_engine) as session:
        canonical = _article(session, at=after_close)
        _article(session, at=in_session, canonical_id=canonical)
        label_id = _label_on(session, canonical, ticker="XTWIN", at=after_close)
        _verdict(session, "XTWIN", MONDAY, True)
        _verdict(session, "XTWIN", TUESDAY, False)
        session.commit()

    _run(_deps(db_engine, bars=FakeBars()))

    assert _outcomes_by_label(db_engine)[label_id] == [
        ("1d", None, "discovery"),
        ("1h", None, "discovery"),
    ]
    with Session(db_engine) as session:
        stored = session.scalars(
            select(LabelOutcome.reference_at).where(LabelOutcome.label_id == label_id)
        ).all()
    # R3's session count reads the stored reference: Monday's, not Tuesday's.
    assert {label_session(at) for at in stored} == {MONDAY}
    assert all(at.astimezone(_ET).date() == MONDAY for at in stored)

