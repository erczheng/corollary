"""The ledger's refusals, persisted -- decision 14 at step 8.

``IngestResult`` already carries every refusal a pass made, and until this
module's subject existed that was where they ended: the Activity page could
say lifetime P&L was short a trade only for as long as the process that
noticed lasted. Decision 14 is why that is not good enough -- *"a gap with a
stated cause is a decision the reader can agree with; a gap without one is
indistinguishable from a bug, and the reader's only honest response is to stop
trusting the number."*

**The load-bearing test in this file is
``test_a_refusal_that_books_on_the_next_pass_leaves_no_row_behind``.** An
in-memory refusal cannot go stale, because it is rebuilt from current matcher
logic every pass. A persisted one can, and a leftover row claiming a gap that
no longer exists is a confident wrong reason on a money figure -- which
decision 14 rates as *worse* than an admitted gap. So the table only earns its
place if a refusal is a statement about the pass that wrote it.

The other four things a persistence boundary can get wrong, one test each:

* **Rule 8** -- the rule, the inputs and the timestamp, on every row.
* **Rule 6** -- Alpaca puts the account number inside prose, and a secret in a
  committed database row outlives a secret in a log.
* **Idempotency** -- ingestion runs on startup and on an interval; a second
  identical pass must leave the same rows, not a second copy of them.
* **What must not be stored at all** -- a cash journal declining to be a fill
  is the design working, not a gap, and nineteen of those per pass would make
  the count this table exists to state mean nothing.
"""

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.data.providers.interface import (
    ContractStatus,
    OptionContract,
    ProviderError,
)
from corollary.db.models import RealizedTrade, RejectionRecord
from corollary.db.session import session_scope
from corollary.engine.execution.interface import Activity, PositionIntent
from corollary.engine.ingest import (
    IngestRefusal,
    IngestRule,
    IngestService,
    _fingerprint,
)
from corollary.engine.ledger import LedgerRejection, RejectionRule
from corollary.instruments import OptionType
from corollary.wire import ERROR_BODY_MAX, REDACTED, STORED_DETAIL_MAX

from .ledger_support import at, contract, fill, option_event
from .test_ingest import (
    NVDA_OTM_CALL,
    PAPER,
    FakeBroker,
    FakeProvider,
    an_order,
    expiry_history,
    expiry_orders,
    standard_terms,
)
from .test_ingest import engine as engine  # noqa: F401  -- a fixture, reused

#: A paper account number in Alpaca's shape, so ``_ACCOUNT_NUMBER`` really
#: matches it. Rule 6: obviously fake, and no key material anywhere.
FAKE_ACCOUNT = "PA0EXAMPLE00"

#: Stands in for a credential the way ``app.state.secret_values`` would supply
#: one. Deliberately not key-shaped: substitution is literal and does not care,
#: and rule 6 forbids a real key in a test as firmly as in a log line. Long
#: enough that a half of it is unmistakably a fragment rather than a word.
SECRET = "not-a-real-key-000000000"


def rejection_rows(engine: Engine) -> list[RejectionRecord]:
    with Session(engine) as session:
        return list(
            session.scalars(select(RejectionRecord).order_by(RejectionRecord.id))
        )


def trade_count(engine: Engine) -> int:
    with Session(engine) as session:
        return len(list(session.scalars(select(RealizedTrade))))


def service(
    engine: Engine,
    provider: FakeProvider,
    *,
    broker: FakeBroker | None = None,
    secrets: Sequence[str] = (),
) -> IngestService:
    """A service over the expiry history, which needs one multiplier to book.

    A fresh service per call on purpose: ``_cursor`` and ``_terms`` are
    in-memory, so a second one is a **restart**, which is the case the table
    exists for. ``honour_since_id=False`` makes the restart re-pull everything,
    which is what a cold start does.
    """
    return IngestService(
        broker=broker
        or FakeBroker(expiry_history(), expiry_orders(), honour_since_id=False),
        provider=provider,
        engine=engine,
        account=PAPER,
        secrets=secrets,
    )


class PoisonedProvider(FakeProvider):
    """A contracts endpoint that fails with the account number in the message.

    Alpaca embeds it in free text -- *"CAT fee for proceed of 15 trades on
    <date> by PA..."* -- so this is the shape of the real hazard, not an
    invented one. A field-name redactor never fires on it.
    """

    async def option_contracts(
        self,
        underlying: str,
        *,
        expiration_lte: date | None = None,
        expiration_gte: date | None = None,
        strike_gte: Decimal | None = None,
        strike_lte: Decimal | None = None,
        option_type: OptionType | None = None,
        include_adjusted: bool = False,
        show_deliverables: bool = False,
        status: ContractStatus = ContractStatus.ACTIVE,
    ) -> list[OptionContract]:
        raise ProviderError(
            f"contracts endpoint returned 403 for account {FAKE_ACCOUNT} "
            f"while asking about {underlying}"
        )


@pytest.mark.asyncio
async def test_a_refusal_that_books_on_the_next_pass_leaves_no_row_behind(
    engine: Engine,
) -> None:
    """The whole reason a table is defensible rather than a liability.

    Pass one has no contract terms, so the expiry cannot be booked: ingestion
    refuses the symbol and the matcher refuses the movement. Pass two is a
    restart against a provider that answers, and the trade books. The gap is
    gone, so the row claiming it must be gone too -- a persisted refusal that
    outlives its cause is a confident wrong reason on a money figure.
    """
    first = await service(engine, FakeProvider()).run()
    assert first.refusals, "pass one must refuse the symbol with no terms"
    assert trade_count(engine) == 0

    rules = {row.rule for row in rejection_rows(engine)}
    assert IngestRule.CONTRACT_TERMS_UNAVAILABLE.value in rules
    assert RejectionRule.UNKNOWN_MULTIPLIER.value in rules

    provider = FakeProvider(standard_terms([NVDA_OTM_CALL]))
    second = await service(engine, provider).run()

    assert second.refusals == ()
    assert trade_count(engine) == 1
    assert rejection_rows(engine) == []


@pytest.mark.asyncio
async def test_every_row_carries_the_rule_the_inputs_and_a_timestamp(
    engine: Engine,
) -> None:
    """Rule 8, at a persistence boundary rather than a log one.

    ``at`` is when the refusal was recorded and is never null; the vendor's
    own stamp is ``activity_at`` and may legitimately be. Confusing the two is
    how a row that most needs writing becomes the one that cannot be written.
    """
    result = await service(engine, FakeProvider()).run()
    rows = rejection_rows(engine)
    assert rows

    for row in rows:
        assert row.rule
        assert row.detail
        assert row.at.tzinfo is not None
        assert row.at.utcoffset() == timezone.utc.utcoffset(None)
        assert row.correlation_id == result.correlation_id
        assert row.account == PAPER
        assert row.source in ("ledger", "ingest")
        assert row.inputs == {} or all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in row.inputs.items()
        )

    booked = [row for row in rows if row.source == "ingest"]
    assert booked, "ingestion's own refusals belong in the table too"
    assert any(row.inputs for row in rows), "rule 8's inputs, on something"
    assert any(row.symbol == NVDA_OTM_CALL for row in rows)
    assert any(row.activity_ids for row in rows)


@pytest.mark.asyncio
async def test_vendor_free_text_is_redacted_before_it_reaches_the_database(
    engine: Engine,
) -> None:
    """Rule 6. A secret in a log is bad; a secret in a committed row is worse.

    Field-name redaction cannot see inside prose, so the pass is a substring
    one and it runs on the way *in*, not on the way out -- a redactor on the
    read path leaves the row itself holding the number.

    Two halves, because either alone can pass while being wrong. The
    end-to-end half proves a real provider failure carrying an account number
    reaches no column of any row. The boundary half proves the substitution
    actually *ran*: ``vendor_detail`` bounds a detail at 300 characters and
    these refusals are longer than that, so a vendor message appended to the
    end of one is cut off rather than redacted -- which leaves the row safe
    and the redaction unproven, exactly the vacuous pass this half rules out.
    """
    passed = service(engine, PoisonedProvider())
    await passed.run()
    rows = rejection_rows(engine)
    assert rows

    written = " ".join(
        text
        for row in rows
        for text in [row.detail, *(str(value) for value in row.inputs.values())]
    )
    assert FAKE_ACCOUNT not in written

    values = passed._rejection_values(
        source="ingest",
        rule=IngestRule.CONTRACT_TERMS_FETCH_FAILED.value,
        symbol=NVDA_OTM_CALL,
        order_id=None,
        activity_ids=(),
        detail=f"the endpoint answered 403 for account {FAKE_ACCOUNT}",
        inputs={
            "description": (
                f"CAT fee for proceed of 15 trades on 2026-09-10 by {FAKE_ACCOUNT}"
            )
        },
        activity_at=None,
        at=datetime(2026, 9, 13, tzinfo=timezone.utc),
        correlation=passed.account,
    )
    assert FAKE_ACCOUNT not in values["detail"]
    assert REDACTED in values["detail"]
    assert FAKE_ACCOUNT not in values["inputs"]["description"]
    assert REDACTED in values["inputs"]["description"]


class KeyLeakingProvider(FakeProvider):
    """A contracts endpoint whose 403 body echoes the credential it refused.

    Long on purpose. ``_refuse_terms`` writes some 366 characters of our own
    prose and then appends *"The contracts endpoint did not answer: ..."*, so a
    vendor body of this size runs the composed detail past
    ``STORED_DETAIL_MAX`` -- which is the only condition under which the order
    of redaction and truncation can be observed at all.

    The credential is repeated rather than placed once because the exact
    offset of the cut is a function of our own prose, and prose gets edited. A
    run of copies means the bound lands inside one of them wherever the
    sentence above it moves to.
    """

    async def option_contracts(
        self,
        underlying: str,
        *,
        expiration_lte: date | None = None,
        expiration_gte: date | None = None,
        strike_gte: Decimal | None = None,
        strike_lte: Decimal | None = None,
        option_type: OptionType | None = None,
        include_adjusted: bool = False,
        show_deliverables: bool = False,
        status: ContractStatus = ContractStatus.ACTIVE,
    ) -> list[OptionContract]:
        raise ProviderError(
            f"contracts endpoint returned 403 while asking about {underlying}, "
            f"echoing the request: " + " ".join([f"key={SECRET}"] * 40)
        )


@pytest.mark.asyncio
async def test_a_terms_refusal_names_the_root_it_asked_about(
    engine: Engine,
) -> None:
    """Rule 8's *inputs*, on the one axis the two terms rules were missing.

    ``ledger_rejection`` has columns for the symbol, the order and the
    activities and none for an underlying, so anything the refusal knows about
    the ticker has to travel in ``inputs``. ``_refuse_settlement`` and
    ``_refuse_ambiguous_session`` both put it there; the terms rules recorded
    it on ``IngestRefusal.underlying`` and then dropped it at the row, which
    left the reader of an ``unverified_deliverable`` gap unable to tell a
    ``GME1`` adjustment from an endpoint that simply failed.

    The key is ``root_symbol``, not ``underlying``: what is known here is the
    OCC root, and on an adjusted contract those are different strings. The
    settlement rules have the contract in hand and carry the real underlying
    under its own name.
    """
    await service(engine, FakeProvider()).run()

    terms = [
        row
        for row in rejection_rows(engine)
        if row.rule
        in (
            IngestRule.CONTRACT_TERMS_UNAVAILABLE.value,
            IngestRule.CONTRACT_TERMS_FETCH_FAILED.value,
        )
    ]
    assert terms, "the pass with no provider terms must refuse the symbol"
    for row in terms:
        assert row.inputs["root_symbol"] == "NVDA"
        assert row.symbol == NVDA_OTM_CALL


@pytest.mark.risk
def test_a_credential_astride_the_stored_bound_is_redacted_before_the_cut(
    engine: Engine,
) -> None:
    """The bug step 8d part 1 shipped, at the boundary that produced it.

    Both bounds are real and they are 64 characters apart: this method cuts at
    ``STORED_DETAIL_MAX`` and the read path re-redacts at
    ``STORED_DETAIL_MAX + 64``. Neither is wrong alone. Together, with the
    credentials passed only to the *reader*, a key straddling offset 1024 is
    committed in halves -- and redaction is literal substitution, so the
    reader matches nothing and serves the surviving prefix. The audit of step
    8c-2 demonstrated exactly that with the first ten characters of a key.

    Constructed rather than sampled: the secret starts half its length before
    the bound, so a cut-first implementation keeps precisely its first half,
    and that half is what is asserted absent. The truncation notice is
    asserted present too -- without it the detail was never long enough to
    cut and every other assertion here is vacuous.
    """
    writer = service(engine, FakeProvider(), secrets=(SECRET,))
    half = len(SECRET) // 2
    astride = "x" * (STORED_DETAIL_MAX - half) + SECRET + " and a tail after it"

    values = writer._rejection_values(
        source="ingest",
        rule=IngestRule.CONTRACT_TERMS_FETCH_FAILED.value,
        symbol=NVDA_OTM_CALL,
        order_id=None,
        activity_ids=(),
        detail=astride,
        inputs={"request": astride},
        activity_at=None,
        at=datetime(2026, 9, 13, tzinfo=timezone.utc),
        correlation="corr-astride",
    )

    for text in (str(values["detail"]), values["inputs"]["request"]):
        assert "characters truncated" in text, "the bound was never reached"
        assert SECRET not in text
        # The half a truncate-first writer commits, which no later
        # substitution can find.
        assert SECRET[:half] not in text
        assert REDACTED in text


@pytest.mark.risk
@pytest.mark.asyncio
async def test_no_fragment_of_a_credential_survives_a_real_pass(
    engine: Engine,
) -> None:
    """The same rule end to end, through ``run`` rather than one method.

    The unit above pins the ordering; this pins that the ordering is reached
    from a pass an operator can actually cause -- a contracts endpoint
    answering 403 with a long body, which is the case
    :meth:`_refuse_terms` composes its longest detail from.

    Asserted on **fragments**, not on the whole value: a leak here is by
    definition a piece of a key, and ``SECRET not in text`` is the assertion
    that passes while the first ten characters are being served.
    """
    await service(engine, KeyLeakingProvider(), secrets=(SECRET,)).run()
    rows = rejection_rows(engine)
    assert rows

    written = " ".join(
        text
        for row in rows
        for text in [row.detail, *(str(value) for value in row.inputs.values())]
    )
    assert any(
        "characters truncated" in row.detail for row in rows
    ), "no row reached the bound, so this asserts nothing about the ordering"
    assert SECRET not in written
    for length in range(8, len(SECRET) + 1):
        assert SECRET[:length] not in written, length
    assert REDACTED in written


@pytest.mark.asyncio
async def test_a_second_identical_pass_leaves_the_same_rows(engine: Engine) -> None:
    """Ingestion runs on an interval and re-pulls an overlapping window.

    Ids are compared, not just counts: "the same rows" has to mean the rows
    were left alone, not that a delete-and-reinsert happened to land on the
    same values under new keys.
    """
    await service(engine, FakeProvider()).run()
    before = [(row.id, row.rule, row.fingerprint) for row in rejection_rows(engine)]
    assert before

    await service(engine, FakeProvider()).run()
    after = [(row.id, row.rule, row.fingerprint) for row in rejection_rows(engine)]
    assert after == before


@pytest.mark.asyncio
async def test_a_by_design_decline_is_not_stored(engine: Engine) -> None:
    """A cash journal is not a fill, and that is the design, not a gap.

    The matcher declines one on every pass, logs it at info rather than
    warning, and nineteen such rows per pass would make the count this table
    exists to state -- *how many trades are missing* -- mean nothing at all.
    """
    history = [*expiry_history(), option_event("JNLC", "", 0, when=at(15, 0))]
    broker = FakeBroker(history, expiry_orders(), honour_since_id=False)
    provider = FakeProvider(standard_terms([NVDA_OTM_CALL]))

    result = await service(engine, provider, broker=broker).run()

    declined = {
        rejection.rule
        for rejection in result.rejections
        if rejection.rule is RejectionRule.NOT_A_LEDGER_ACTIVITY
    }
    assert declined, "the matcher still declines it, and still says so"
    assert rejection_rows(engine) == []


@pytest.mark.asyncio
async def test_a_refusal_the_pass_never_looked_at_is_left_alone(
    engine: Engine,
) -> None:
    """Conservatism where the cursor is going, not only where it is.

    ``IngestService._cursor`` is in memory today, so every pass re-examines
    the whole history and could clear anything. The spec's own justification
    for the ``fill`` table -- *"page_size maxes at 100 and re-fetching all
    history per request is untenable"* -- says that cursor gets persisted
    eventually, and a pass that looked at ten activities must not delete a
    refusal about an eleventh it never saw.
    """
    stale = RejectionRecord(
        account=PAPER,
        source="ledger",
        rule=RejectionRule.UNKNOWN_MULTIPLIER.value,
        fingerprint="an-older-pass",
        symbol="AAPL261218C00250000",
        order_id=None,
        activity_ids=["20260101000000000::not-in-this-history"],
        detail="a refusal from a pass whose activities this one never pulled",
        inputs={"multiplier": "unknown"},
        at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
        activity_at=None,
        correlation_id="an-older-pass",
    )
    with Session(engine) as session:
        session.add(stale)
        session.commit()

    provider = FakeProvider(standard_terms([NVDA_OTM_CALL]))
    await service(engine, provider).run()

    kept = rejection_rows(engine)
    assert [row.fingerprint for row in kept] == ["an-older-pass"]


@pytest.mark.asyncio
async def test_the_other_book_is_never_touched(engine: Engine) -> None:
    """One service is one account, and a paper pass reconciles paper only.

    The same reason ``flatten()`` may not reach into the other book: a row
    under ``cash`` describes money this service's credentials cannot even see.
    """
    other = RejectionRecord(
        account="cash",
        source="ingest",
        rule=IngestRule.CONTRACT_TERMS_UNAVAILABLE.value,
        fingerprint="the-cash-book",
        symbol=NVDA_OTM_CALL,
        order_id=None,
        activity_ids=[],
        detail="the cash book's own gap",
        inputs={},
        at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
        activity_at=None,
        correlation_id="the-cash-book",
    )
    with Session(engine) as session:
        session.add(other)
        session.commit()

    provider = FakeProvider(standard_terms([NVDA_OTM_CALL]))
    await service(engine, provider).run()

    rows = rejection_rows(engine)
    assert [(row.account, row.fingerprint) for row in rows] == [
        ("cash", "the-cash-book")
    ]


# --------------------------------------------------------------------------
# What the fingerprint is, and what it must not be
# --------------------------------------------------------------------------


def test_the_fingerprint_is_the_refusals_identity_not_its_evidence() -> None:
    """Two passes holding different amounts of history, one refusal.

    An ingest refusal names *every* held activity for the symbol it refuses.
    That set is a property of how much history the pass happened to pull, not
    of the refusal, so hashing it made the digest move between passes -- and a
    moved digest is an append, not an upsert: ``UNIQUE (account, fingerprint)``
    cannot catch a duplicate whose keys differ by construction.

    The identity of an ingest refusal is its rule and the **contract** (or the
    order) it concerns. A ledger rejection is the other shape: it names
    exactly one activity, and that activity *is* the subject -- two refusals
    of the same rule about two different fills are two missing trades.
    """
    rule = IngestRule.CONTRACT_TERMS_UNAVAILABLE.value

    three = _fingerprint("ingest", rule, ["a", "b", "c"], None, NVDA_OTM_CALL)
    two = _fingerprint("ingest", rule, ["a", "b"], None, NVDA_OTM_CALL)
    none_at_all = _fingerprint("ingest", rule, [], None, NVDA_OTM_CALL)
    assert three == two == none_at_all

    other = _fingerprint("ingest", rule, ["a"], None, "AAPL261218C00250000")
    assert other != three

    mleg = IngestRule.MLEG_ORDER_HAS_NO_LEGS.value
    assert _fingerprint("ingest", mleg, [], "order-1", None) != _fingerprint(
        "ingest", mleg, [], "order-2", None
    )

    booked = RejectionRule.UNKNOWN_MULTIPLIER.value
    assert _fingerprint("ledger", booked, ["a"], None, NVDA_OTM_CALL) != _fingerprint(
        "ledger", booked, ["b"], None, NVDA_OTM_CALL
    )


@pytest.mark.asyncio
async def test_a_pass_holding_less_history_updates_the_row_it_finds(
    engine: Engine,
) -> None:
    """Restart, *different* history, refusal still true -- exactly one row.

    The case the mechanism exists for, and the one nothing exercised: replaying
    an identical history makes the fingerprints match trivially. Here pass one
    holds three activities for the symbol and pass two holds two, and both
    refuse it for the same reason. Two rows would tell the Activity page that
    two trades are missing where the truth is one, and nothing would
    distinguish the zombie -- ``at`` is only refreshed on reassertion, so the
    stale row keeps its old stamp and sorts below the fresh one.
    """
    longer = [
        *expiry_history(),
        fill(NVDA_OTM_CALL, "buy", 1, "1.30", when=at(14, 30), order_id="order-extra"),
    ]
    first = await service(
        engine,
        FakeProvider(),
        broker=FakeBroker(longer, expiry_orders(), honour_since_id=False),
    ).run()
    assert first.rejections_written

    def terms_rows() -> list[RejectionRecord]:
        return [
            row
            for row in rejection_rows(engine)
            if row.source == "ingest"
            and row.rule == IngestRule.CONTRACT_TERMS_UNAVAILABLE.value
            and row.symbol == NVDA_OTM_CALL
        ]

    before = terms_rows()
    assert len(before) == 1
    assert len(before[0].activity_ids) == 3

    second = await service(engine, FakeProvider()).run()
    assert second.refusals, "pass two still has no terms for the symbol"

    after = terms_rows()
    assert [row.id for row in after] == [before[0].id]
    assert len(after[0].activity_ids) == 2, "the evidence is stored, and it moved"


@pytest.mark.asyncio
async def test_first_seen_records_when_the_gap_opened(engine: Engine) -> None:
    """``at`` says *still true as of*; ``first_seen`` says *since when*.

    Without the second the table cannot say how long a gap has persisted,
    which is exactly what made a stale duplicate indistinguishable from a real
    second gap.
    """
    pass_one = service(engine, FakeProvider())
    result = await pass_one.run()
    opened = {row.fingerprint: row.first_seen for row in rejection_rows(engine)}
    assert opened
    assert all(row.first_seen == row.at for row in rejection_rows(engine))

    later = datetime(2026, 12, 25, tzinfo=timezone.utc)
    with session_scope(engine) as session:
        pass_one._write_rejections(
            session,
            result.rejections,
            result.refusals,
            correlation="a-later-pass",
            at=later,
            examined_activities=set(),
            examined_orders=set(),
            examined_symbols=set(),
        )

    for row in rejection_rows(engine):
        assert row.at == later, "reassertion refreshes 'still true as of'"
        assert row.first_seen == opened[row.fingerprint], "and never 'since when'"


# --------------------------------------------------------------------------
# What may clear a row, and what may not
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_row_naming_a_symbol_this_pass_never_saw_is_left_alone(
    engine: Engine,
) -> None:
    """``symbol`` is a subject, not decoration.

    A row with no activity ids and no order id used to be "vacuously
    re-examined" -- deletable by any pass at all. It is not: the pass below
    knows nothing but NVDA, and a live refusal about AAPL erased by it turns a
    stated gap back into an unexplained one.

    This is one step away, not hypothetical: ``LedgerRejection.activity_id``
    and ``_reject``'s parameter both default to ``None``, so the first
    whole-symbol matcher refusal lands straight in this branch. The account
    filter is what saves the row in ``test_the_other_book_is_never_touched``;
    here the row is under **paper**, the same book the pass is reconciling.
    """
    elsewhere = RejectionRecord(
        account=PAPER,
        source="ingest",
        rule=IngestRule.CONTRACT_TERMS_UNAVAILABLE.value,
        fingerprint="a-symbol-this-pass-knows-nothing-about",
        symbol="AAPL261218C00250000",
        order_id=None,
        activity_ids=[],
        detail="no terms for a contract whose activities this pass never pulled",
        inputs={},
        at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
        activity_at=None,
        correlation_id="an-older-pass",
    )
    with Session(engine) as session:
        session.add(elsewhere)
        session.commit()

    provider = FakeProvider(standard_terms([NVDA_OTM_CALL]))
    result = await service(engine, provider).run()

    assert result.rejections_cleared == 0
    assert [row.fingerprint for row in rejection_rows(engine)] == [
        "a-symbol-this-pass-knows-nothing-about"
    ]


@pytest.mark.asyncio
async def test_a_refusal_naming_no_subject_at_all_is_not_stored(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """It cannot be attributed, so a row for it cannot inform the reader.

    Every subject-less refusal of one rule hashes to the same digest, so two
    genuinely different gaps would collapse into one row and the count would
    *under*-state -- worse than the over-statement a duplicate causes, because
    nothing about it looks wrong. Rule 8 still holds: it is logged, with its
    rule and its inputs.
    """
    nameless = LedgerRejection(
        rule=RejectionRule.UNKNOWN_MULTIPLIER,
        detail="a refusal that names neither an activity, an order nor a symbol",
        activity_id=None,
        symbol=None,
        at=None,
        inputs={"multiplier": "unknown"},
    )
    pass_one = service(engine, FakeProvider())
    with caplog.at_level(logging.WARNING, logger="corollary.engine.ingest"):
        with session_scope(engine) as session:
            written, cleared = pass_one._write_rejections(
                session,
                [nameless],
                [],
                correlation="a-pass",
                at=datetime(2026, 9, 13, tzinfo=timezone.utc),
                examined_activities=set(),
                examined_orders=set(),
                examined_symbols=set(),
            )

    assert (written, cleared) == (0, 0)
    assert rejection_rows(engine) == []

    unattributable = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "ingest_rejection_unattributable"
    ]
    assert len(unattributable) == 1
    assert unattributable[0].rule == RejectionRule.UNKNOWN_MULTIPLIER.value
    assert unattributable[0].inputs == {"multiplier": "unknown"}


@pytest.mark.asyncio
async def test_a_pass_that_clears_and_writes_shares_no_key_between_them(
    engine: Engine,
) -> None:
    """The unit of work emits every INSERT before any DELETE.

    That is safe only because a row is deleted exactly when the pass did *not*
    re-derive its fingerprint, so no inserted row can carry a deleted row's
    key. It is an invariant rather than an accident, and an unasserted one
    would fail as an ``IntegrityError`` that rolls the **whole** ingestion pass
    back -- ``fill`` rows included, since ``_write_rejections`` shares the
    transaction. So: one pass that deletes and inserts in the same flush.

    **This test cannot exercise a collision, and is not a guard against one.**
    The invariant is the *absence* of one and it is structural:
    ``desired.pop(row.fingerprint, None)`` removes a re-derived key from the
    insert set **before** the insert loop runs, so no deleted row's key can
    still be in ``desired``. No fixture can drive the two into conflict while
    that shape holds, and only an edit to the ``pop``/``reasserted`` structure
    itself could break it -- which this test would not catch. What it does is
    show the two halves happening in one flush at all, so the arrangement is
    exercised rather than merely reasoned about.
    """
    superseded = RejectionRecord(
        account=PAPER,
        source="ingest",
        rule=IngestRule.CONTRACT_TERMS_UNAVAILABLE.value,
        fingerprint="a-digest-no-pass-will-ever-derive-again",
        symbol=NVDA_OTM_CALL,
        order_id=None,
        activity_ids=[],
        detail="the same symbol, under a key this pass does not produce",
        inputs={},
        at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
        activity_at=None,
        correlation_id="an-older-pass",
    )
    with Session(engine) as session:
        session.add(superseded)
        session.commit()

    result = await service(engine, FakeProvider()).run()

    assert result.rejections_written and result.rejections_cleared
    fingerprints = [row.fingerprint for row in rejection_rows(engine)]
    assert "a-digest-no-pass-will-ever-derive-again" not in fingerprints
    assert len(fingerprints) == len(set(fingerprints))
    assert result.fills_written, "the rest of the pass committed with it"


# --------------------------------------------------------------------------
# What a stored detail is allowed to lose
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stored_detail_keeps_the_clause_that_states_the_severity(
    engine: Engine,
) -> None:
    """One scrubber, two limits -- and the log's limit is not storage's.

    ``ERROR_BODY_MAX`` bounds what a *vendor* error body may write into a log
    line. Applied to a stored refusal it cut 185 characters that are not the
    vendor's tail at all but our own closing sentence, and deterministically:
    whenever a fetch failure was present, the clause separating *"a contract I
    still hold has no terms yet"* from *"a position ended and its P&L is
    permanently gone"* was the part that went.
    """
    await service(engine, PoisonedProvider()).run()
    rows = [
        row
        for row in rejection_rows(engine)
        if row.rule == IngestRule.CONTRACT_TERMS_FETCH_FAILED.value
    ]
    assert rows, "a failing contracts endpoint is a fetch failure, not an absence"

    detail = rows[0].detail
    assert "The contracts endpoint did not answer" in detail
    assert "missing from lifetime P&L rather than merely delayed" in detail
    assert len(detail) > ERROR_BODY_MAX
    assert FAKE_ACCOUNT not in detail
    assert REDACTED in detail


def test_nothing_survives_the_longer_bound_that_could_not_survive_the_shorter(
    engine: Engine,
) -> None:
    """A longer cut is a different cut, so rule 6 is re-proved at it.

    Redaction runs before truncation at both bounds. The hazard the order
    guards against is the same one either way: truncating first can slice an
    identifier in half and keep the half, which is worth no less to whoever
    reads the row.

    **The identifier has to straddle the bound for this test to mean
    anything.** Put wholly past it -- which is how this test was first written
    -- truncation alone removes it, both assertions pass with the substitution
    deleted outright, and the test proving the new bound is the one test that
    proves nothing. ``"y " * 509`` puts ``PA`` at index 1018 of a
    1024-character bound, so six of its twelve characters fit: redacting first
    takes all twelve, truncating first stores ``PA0EXA``.

    So the assertion is on that surviving **partial**, not on ``REDACTED``
    being present -- at this position the replacement itself is cut off by the
    bound, and a correct implementation leaves no ``<redacted>`` behind
    either. ``test_a_stored_detail_keeps_the_clause_that_states_the_severity``
    is where the replacement is asserted, on a string that has room for it.

    **The straddle is asserted on the collapsed string, not the given one.**
    ``vendor_detail`` joins on ``split()`` before it measures anything, so the
    bound applies to the collapsed form; the two happen to agree here only
    because ``'y ' * 509`` loses its trailing space and regains one from the
    join. Pinned against the given string, a different filler could move the
    identifier off the bound while this assertion went on passing, and the
    test would quietly stop testing the thing it names.
    """
    straddle = f"{'y ' * 509}{FAKE_ACCOUNT} tail"
    measured = " ".join(straddle.split())
    values = service(engine, FakeProvider())._rejection_values(
        source="ingest",
        rule=IngestRule.CONTRACT_TERMS_FETCH_FAILED.value,
        symbol=NVDA_OTM_CALL,
        order_id=None,
        activity_ids=(),
        detail=straddle,
        inputs={"description": straddle},
        activity_at=None,
        at=datetime(2026, 9, 13, tzinfo=timezone.utc),
        correlation="a-pass",
    )
    detail = str(values["detail"])
    description = values["inputs"]["description"]

    assert measured.index(FAKE_ACCOUNT) < STORED_DETAIL_MAX, "it must straddle"
    assert measured.index(FAKE_ACCOUNT) + len(FAKE_ACCOUNT) > STORED_DETAIL_MAX
    assert FAKE_ACCOUNT not in detail
    assert FAKE_ACCOUNT not in description
    assert "PA0EXA" not in detail, "truncating first would keep the half that fits"
    assert "PA0EXA" not in description
    assert len(detail) < 2 * STORED_DETAIL_MAX


# --------------------------------------------------------------------------
# A symbol is a subject only when it names one
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("", None),
        ("   ", None),
        (f"  {NVDA_OTM_CALL.lower()}  ", NVDA_OTM_CALL),
        (NVDA_OTM_CALL, NVDA_OTM_CALL),
    ],
)
def test_a_stored_symbol_is_canonical_or_absent(
    engine: Engine, given: str, stored: str | None
) -> None:
    """``""`` is not a symbol, and neither is a differently-cased one.

    Both halves are the same hazard reaching the column by different routes.
    ``_identity`` tests the symbol for **truthiness**, so an empty string is
    already no subject as far as the fingerprint is concerned; storing it
    verbatim would leave the column disagreeing with the digest about what the
    row is about, and ``NULL means no subject`` true only by luck. Alpaca
    really does send ``symbol: ""`` on structure rows and ``MISSING_SYMBOL``
    passes it straight through.

    The canonical form is the parser's -- ``parse_occ_symbol`` returns
    ``symbol.strip().upper()`` -- because that is the form the matcher's own
    rejections already carry, via ``contract.symbol``. A row written in one
    form and compared against the other is a row nothing can ever clear.
    """
    values = service(engine, FakeProvider())._rejection_values(
        source="ingest",
        rule=IngestRule.CONTRACT_TERMS_UNAVAILABLE.value,
        symbol=given,
        order_id=None,
        activity_ids=("an-activity",),
        detail="a refusal about a symbol the vendor spelled its own way",
        inputs={},
        activity_at=None,
        at=datetime(2026, 9, 13, tzinfo=timezone.utc),
        correlation="a-pass",
    )

    assert values["symbol"] == stored
    assert values["fingerprint"] == _fingerprint(
        "ingest", IngestRule.CONTRACT_TERMS_UNAVAILABLE.value, (), None, stored
    )


def test_a_row_an_older_build_left_with_an_empty_subject_still_clears(
    engine: Engine,
) -> None:
    """An empty string is not a subject this pass failed to re-examine.

    ``_identity`` asks whether the symbol and the order id are *truthy*; this
    predicate used to ask whether they were ``not None``, and the two disagree
    on exactly one value. ``examined_symbols`` is built from truthy symbols, so
    ``""`` was never in it and a row carrying it failed the check on **every**
    pass, forever: the refusal stops being derived, the row survives anyway,
    and the table claims lifetime P&L is short a trade that has in fact been
    booked. Decision 14's own *worse than no table at all*.

    Written as rows an older build left behind, because after the fix above
    the writer cannot produce one: the two halves close different aspects of
    the same hole and both are load-bearing.
    """
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    empty_symbol = RejectionRecord(
        account=PAPER,
        source="ledger",
        rule=RejectionRule.MISSING_SYMBOL.value,
        fingerprint="a-row-whose-symbol-is-the-empty-string",
        symbol="",
        order_id=None,
        activity_ids=["an-activity-this-pass-holds"],
        detail="an OPEXP arrived carrying symbol: '' and could not be booked",
        inputs={},
        at=older,
        first_seen=older,
        activity_at=None,
        correlation_id="an-older-pass",
    )
    empty_order = RejectionRecord(
        account=PAPER,
        source="ingest",
        rule=IngestRule.MLEG_ORDER_HAS_NO_LEGS.value,
        fingerprint="a-row-whose-order-id-is-the-empty-string",
        symbol=None,
        order_id="",
        activity_ids=["another-activity-this-pass-holds"],
        detail="an mleg order arrived with no legs[] and no usable id",
        inputs={},
        at=older,
        first_seen=older,
        activity_at=None,
        correlation_id="an-older-pass",
    )
    with Session(engine) as session:
        session.add_all([empty_symbol, empty_order])
        session.commit()

    with session_scope(engine) as session:
        written, cleared = service(engine, FakeProvider())._write_rejections(
            session,
            [],
            [],
            correlation="a-later-pass",
            at=datetime(2026, 9, 13, tzinfo=timezone.utc),
            examined_activities={
                "an-activity-this-pass-holds",
                "another-activity-this-pass-holds",
            },
            examined_orders=set(),
            examined_symbols=set(),
        )

    assert (written, cleared) == (0, 2)
    assert rejection_rows(engine) == []


@pytest.mark.asyncio
async def test_a_symbol_the_vendor_cased_differently_still_clears_its_row(
    engine: Engine,
) -> None:
    """The pass and the row must agree on what a symbol's name *is*.

    ``examined_symbols`` comes from the activities the vendor sent; a stored
    row's symbol comes, on most rules, from ``parse_occ_symbol`` -- which
    strips and upper-cases. Alpaca sends canonical uppercase today, so a
    padded or lower-cased symbol is latent rather than observed; when it
    arrives the two forms diverge and the row becomes unclearable in exactly
    the way an empty string does, with nothing to show for it.

    The pass below refuses this contract on its own account, which is beside
    the point: what is asserted is that the *older* row went.
    """
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stale = RejectionRecord(
        account=PAPER,
        source="ingest",
        rule=IngestRule.CONTRACT_TERMS_UNAVAILABLE.value,
        fingerprint="a-digest-this-pass-does-not-derive",
        symbol=NVDA_OTM_CALL,
        order_id=None,
        activity_ids=[],
        detail="no terms, recorded under a key this pass will not produce",
        inputs={},
        at=older,
        first_seen=older,
        activity_at=None,
        correlation_id="an-older-pass",
    )
    with Session(engine) as session:
        session.add(stale)
        session.commit()

    shouting = FakeBroker(
        [fill(f" {NVDA_OTM_CALL.lower()} ", "buy", 1, "1.00", when=at(14, 30))],
        [],
        honour_since_id=False,
    )
    result = await service(
        engine, FakeProvider(standard_terms([NVDA_OTM_CALL])), broker=shouting
    ).run()

    assert result.rejections_cleared == 1
    assert "a-digest-this-pass-does-not-derive" not in [
        row.fingerprint for row in rejection_rows(engine)
    ]


# --------------------------------------------------------------------------
# The guard over booked trades reads the same spelling the rows carry
# --------------------------------------------------------------------------

#: The same contract in the spelling a vendor is allowed to send and a stored
#: row can never carry: padded, lower-cased. ``parse_occ_symbol`` strips and
#: upper-cases, so every realized trade is written in the canonical form.
SHOUTED = f" {NVDA_OTM_CALL.lower()} "


def spelled_both_ways() -> dict[str, OptionContract]:
    """Terms answerable under either spelling, so only the *intent* is missing.

    ``_fetch`` matches an answer to a demand by exact symbol, so a contracts
    endpoint that only knew the canonical form would refuse the vendor spelling
    for terms as well -- a second refusal, also carrying the raw symbol, which
    would guard the row for the wrong reason and hide what this test is about.
    Serving both leaves ``UNKNOWN_INTENT`` as the only refusal in the pass.
    """
    canonical = contract(NVDA_OTM_CALL)
    return {
        NVDA_OTM_CALL: canonical,
        SHOUTED: replace(canonical, symbol=SHOUTED, name=SHOUTED),
    }


def short_and_its_close(closing_symbol: str) -> list[Activity]:
    """A short opened and bought back, with the close spelled as asked."""
    return [
        fill(
            NVDA_OTM_CALL,
            "sell_short",
            1,
            "2.00",
            when=at(14, 30),
            order_id="order-open",
        ),
        fill(
            closing_symbol,
            "buy",
            1,
            "1.00",
            when=at(15, 30),
            order_id="order-close",
        ),
    ]


@pytest.mark.asyncio
async def test_a_booked_trade_survives_the_order_window_rolling_off(
    engine: Engine,
) -> None:
    """A refusal in the vendor spelling must still guard the canonical row.

    ``_write_trades`` deletes a held ``realized_trade`` the rebuilt ledger no
    longer produces, unless its symbol is ``guarded`` -- the set of everything
    this pass could not fully account for. ``RealizedTrade.symbol`` is
    **always** the parser form, because it comes from ``LotMovement.symbol``
    = ``contract.symbol``. The guard was built from the vendor form, and four
    matcher rules reject before a contract is resolved and pass
    ``activity.symbol`` straight through.

    The chain, with only the spelling latent:

    1. A short is closed by a ``buy``. The closing order is still inside the
       window ``orders(status=ALL)`` returns, the intent joins, and the trade
       books under the canonical symbol.
    2. The order ages out. ``buy`` implies no intent on its own, so the close
       is refused with ``UNKNOWN_INTENT``, carrying the vendor spelling, and
       no realized trade is re-derived.
    3. The held row symbol is not in the guard, so the row is **deleted** --
       lifetime P&L loses a trade that really happened, over an orders
       endpoint retention window.

    Both sides are canonicalised now. The remedy can only widen the guard,
    which is the direction to be wrong in: a guard that is too broad leaves a
    row that should have gone, and a reader can see that row; a guard that is
    too narrow erases money and leaves nothing behind.
    """
    provider = FakeProvider(spelled_both_ways())

    joined = FakeBroker(
        short_and_its_close(NVDA_OTM_CALL),
        [
            an_order(
                "order-close", NVDA_OTM_CALL, PositionIntent.BUY_TO_CLOSE, "1.00"
            )
        ],
        honour_since_id=False,
    )
    first = await service(engine, provider, broker=joined).run()

    assert first.trades_written == 1, "the control: the trade has to book first"
    with Session(engine) as session:
        booked = list(session.scalars(select(RealizedTrade)))
    assert [row.symbol for row in booked] == [NVDA_OTM_CALL]

    # The same activities, respelled, and the order gone from the window.
    aged_out = FakeBroker(short_and_its_close(SHOUTED), [], honour_since_id=False)
    second = await service(engine, provider, broker=aged_out).run()

    assert second.refusals == (), "only the intent is missing, not the terms"
    assert [item.rule for item in second.rejections] == [
        RejectionRule.UNKNOWN_INTENT
    ]
    assert [item.symbol for item in second.rejections] == [SHOUTED], (
        "the premise: this rule carries the vendor spelling, not the parser one"
    )

    assert second.trades_removed == 0
    assert trade_count(engine) == 1, (
        "a booked trade must outlive the orders endpoint retention window"
    )


@pytest.mark.asyncio
async def test_a_refusal_whose_only_subject_is_blank_is_not_stored(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """The gate and the value it admits must read the same string.

    ``_identity`` asks whether the symbol is truthy; ``_rejection_values``
    then folds it through ``_canonical_symbol`` before fingerprinting. They
    part on exactly one class of input -- whitespace-only -- and a refusal
    admitted on a subject its row does not carry is stored **subject-less**,
    where every subject-less refusal of one rule digests identically and two
    genuinely different gaps collapse into one row. That under-states, which
    is the one direction of error nothing about the row reveals.

    Not reachable through the rules as they stand: ``MISSING_SYMBOL`` fires
    only on a falsy symbol, so three spaces route to ``NOT_AN_OPTION``, which
    is by design and not stored. It is closed by construction rather than left
    to the vendor habits, which is the same reasoning as the fix it mirrors.
    """
    blank = IngestRefusal(
        rule=IngestRule.CONTRACT_TERMS_UNAVAILABLE,
        detail="a symbol of three spaces names no contract to fetch terms for",
        at=datetime(2026, 9, 13, tzinfo=timezone.utc),
        symbol="   ",
        inputs={"symbol_seen": "three spaces"},
    )
    with caplog.at_level(logging.WARNING, logger="corollary.engine.ingest"):
        with session_scope(engine) as session:
            written, cleared = service(engine, FakeProvider())._write_rejections(
                session,
                [],
                [blank],
                correlation="a-pass",
                at=datetime(2026, 9, 13, tzinfo=timezone.utc),
                examined_activities=set(),
                examined_orders=set(),
                examined_symbols=set(),
            )

    assert (written, cleared) == (0, 0)
    assert rejection_rows(engine) == []

    unattributable = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "ingest_rejection_unattributable"
    ]
    assert len(unattributable) == 1
    assert unattributable[0].rule == IngestRule.CONTRACT_TERMS_UNAVAILABLE.value


def test_a_row_an_older_build_left_in_the_vendor_spelling_still_clears(
    engine: Engine,
) -> None:
    """The mirror of the activity-side test: a non-canonical **row** clears.

    ``_re_examined`` folds both sides, and this is the half the writer can no
    longer produce -- ``_rejection_values`` canonicalises on the way in -- so
    the only population it exists for is rows an older build left behind.
    Untested, the fold could be deleted from this side with every other test
    still green, and those rows would go back to being unclearable: the
    refusal stops being derived, the row survives anyway, and the table claims
    lifetime P&L is short a trade that has in fact been booked.

    ``UNKNOWN_INTENT`` because it is one of the four rules that carry
    ``activity.symbol`` verbatim, which is how such a row came to exist.
    """
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    shouted = RejectionRecord(
        account=PAPER,
        source="ledger",
        rule=RejectionRule.UNKNOWN_INTENT.value,
        fingerprint="a-row-written-before-the-symbol-was-folded",
        symbol=SHOUTED,
        order_id=None,
        activity_ids=["an-activity-this-pass-holds"],
        detail="a buy with no order joined, recorded in the vendor spelling",
        inputs={},
        at=older,
        first_seen=older,
        activity_at=None,
        correlation_id="an-older-pass",
    )
    with Session(engine) as session:
        session.add(shouted)
        session.commit()

    with session_scope(engine) as session:
        written, cleared = service(engine, FakeProvider())._write_rejections(
            session,
            [],
            [],
            correlation="a-later-pass",
            at=datetime(2026, 9, 13, tzinfo=timezone.utc),
            examined_activities={"an-activity-this-pass-holds"},
            examined_orders=set(),
            examined_symbols={NVDA_OTM_CALL},
        )

    assert (written, cleared) == (0, 1)
    assert rejection_rows(engine) == []
