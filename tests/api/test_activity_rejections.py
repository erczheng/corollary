"""Why the ledger is incomplete -- decision 14's reporting half.

``ActivityStats.notBooked`` says *how many* closings the lifetime figures are
missing and ``notBookedSymbols`` says *which contracts*. Neither says **why**,
and decision 14 is explicit that a gap without a stated cause "is
indistinguishable from a bug, and the reader's only honest response is to stop
trusting the number". ``8c-1`` persisted the refusals; this is the surface that
serves them.

Three things this module pins that go wrong silently.

**Rule 8 is the whole point.** A refusal records the rule, the inputs and the
timestamp. A read surface that drops any one of the three leaves the row in the
database and the reader no better off than with the bare count, so every rule
either vocabulary can emit is asserted to reach the wire *with its cause*,
iterated from the enums themselves.

That last claim used to be written as *"adding a member to either one fails
here"* and it was **false**, which is worse than absent -- the audit of step 8d
caught it. ``refusal_groups`` passes ``row.rule`` through as an opaque string
with no whitelist anywhere, so a new enum member is seeded by ``every_rule``
and served unchanged, and the test goes green having proved only that the read
path did not drop it. What the iteration really buys is the *converse*: if
anybody ever adds a rule whitelist to the read path and it goes stale, or
renames a rule on the way out, this fails. The rule a *page* cannot render is
not guarded here at all, and neither is a third ``source`` value: see
:func:`test_every_rule_either_vocabulary_can_emit_reaches_the_wire`.

**Rule 6 reaches a read path.** ``detail`` and ``inputs`` are free text and are
redacted on the way *in*; they are redacted again on the way *out*, against
the environment's own credentials, which the writer does not pass. Redaction
runs **before** truncation, because the other order cuts a secret in half and
keeps the half -- the exact bug step 8d part 1 shipped and audit caught.

**Money lives inside the row without a money column.** ``inputs`` carries
stringified ``strike``, ``multiplier``, ``net_amount`` and ``paired_price`` as
TEXT inside a JSON blob, where neither ``Money``'s refusing comparator nor
``guard_money_sql`` can see it. ``'10' < '9.5'`` as text and the opposite as
``Decimal``, so nothing here may ask SQL a question about those values.
"""

import ast
import io
import logging
import tokenize
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.api.app import create_app
from corollary.api.deps import ServiceRegistry
from corollary.api.routes.activity import LedgerGap, unexplained_symbols
from corollary.api.schemas import LedgerRefusalGroup
from corollary.db.models import Fill, RealizedTrade, RejectionRecord
from corollary.engine.ingest import IngestRule
from corollary.engine.ledger import BY_DESIGN_RULES, RejectionRule

MODULE = (
    Path(__file__).resolve().parents[2]
    / "corollary"
    / "api"
    / "routes"
    / "activity.py"
)

GME1 = "GME1261016C00003000"
IWM = "IWM260918P00280000"
AAPL = "AAPL261218C00230000"

T0 = datetime(2026, 9, 10, 13, 11, 25, 217000, tzinfo=timezone.utc)

#: The credential the app is built with, so redaction is observable. Not a
#: real key shape on purpose -- rule 6 forbids one in a test as much as in a
#: log line, and literal substitution does not care what the value looks like.
SECRET = "not-a-real-key-000000000"


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def add_rejection(
    session: Session,
    *,
    source: str = "ingest",
    rule: str = IngestRule.CONTRACT_TERMS_UNAVAILABLE.value,
    symbol: str | None = GME1,
    order_id: str | None = None,
    activity_ids: Sequence[str] | None = None,
    detail: str = "the contracts endpoint did not carry this symbol",
    inputs: Mapping[str, str] | None = None,
    when: datetime | None = None,
    first_seen: datetime | None = None,
    activity_at: datetime | None = None,
    account: str = "paper",
    correlation_id: str = "corr-1",
    fingerprint: str | None = None,
) -> None:
    """One stored refusal.

    ``fingerprint`` defaults to something derived from the fields that decide
    identity, the way ``IngestService`` derives it -- a digest there, a
    readable join here, because the test only needs two refusals never to
    collide.
    """
    ids = sorted(activity_ids or ["20260910131125217::a9d576c2"])
    moment = when or at(0)
    session.add(
        RejectionRecord(
            account=account,
            source=source,
            rule=rule,
            fingerprint=fingerprint or f"{source}|{rule}|{symbol}|{order_id}|{ids}",
            symbol=symbol,
            order_id=order_id,
            activity_ids=list(ids),
            detail=detail,
            inputs=dict(inputs or {"symbol": str(symbol)}),
            at=moment,
            first_seen=first_seen or moment,
            activity_at=activity_at,
            correlation_id=correlation_id,
        )
    )


def add_unbooked_close(
    session: Session,
    *,
    symbol: str = IWM,
    qty: int = 1,
    account: str = "paper",
    when: datetime | None = None,
) -> None:
    """A closing fill with no ``realized_trade`` row behind it.

    One closing short of its P&L, which is exactly what
    :attr:`ActivityStats.notBooked` counts -- the *other* side of the join
    :func:`unexplained_symbols` computes. Seeded as a fill and no trade
    rather than by stubbing the fold, because the route reads both tables and
    the thing under test is whether those two reads meet.
    """
    session.add(
        Fill(
            account=account,
            activity_id=f"unbooked::{symbol}::{qty}",
            order_id="ord-close",
            group_id=None,
            symbol=symbol,
            side="sell",
            position_intent="sell_to_close",
            qty=qty,
            price=Decimal("8.14"),
            at=when or at(0),
        )
    )


def add_booked_close(
    session: Session,
    *,
    symbol: str = IWM,
    account: str = "paper",
) -> None:
    """A closing fill the realized-trade table accounts for in full."""
    moment = at(0)
    session.add(
        Fill(
            account=account,
            activity_id=f"booked::{symbol}",
            order_id="ord-booked",
            group_id=None,
            symbol=symbol,
            side="sell",
            position_intent="sell_to_close",
            qty=1,
            price=Decimal("8.14"),
            at=moment,
        )
    )
    session.add(
        RealizedTrade(
            account=account,
            symbol=symbol,
            opened_at=at(-10),
            closed_at=moment,
            qty=1,
            open_price=Decimal("8.21"),
            close_price=Decimal("8.14"),
            pnl=Decimal("-7.00"),
            pnl_pct=Decimal("-0.8526"),
            close_kind="fill",
        )
    )


def seed(engine: Engine, populate: Callable[[Session], None]) -> None:
    with Session(engine) as session:
        populate(session)
        session.commit()


#: Every rule that can reach the table: the matcher's, less the two it never
#: stores, plus ingestion's. Derived from the enums so a new member is a
#: failing test rather than a rule the page cannot name.
STORED_LEDGER_RULES: tuple[RejectionRule, ...] = tuple(
    rule for rule in RejectionRule if rule not in BY_DESIGN_RULES
)
ALL_RULES: tuple[tuple[str, str], ...] = tuple(
    [("ledger", rule.value) for rule in STORED_LEDGER_RULES]
    + [("ingest", rule.value) for rule in IngestRule]
)


def every_rule(session: Session) -> None:
    """One refusal per rule either vocabulary can emit."""
    for index, (source, rule) in enumerate(ALL_RULES):
        add_rejection(
            session,
            source=source,
            rule=rule,
            symbol=GME1,
            detail=f"{rule} refused this contract",
            inputs={"rule_index": str(index)},
            activity_ids=[f"act-{index}"],
            when=at(index),
        )


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------


def build(registry: ServiceRegistry, db_engine: Engine) -> FastAPI:
    return create_app(registry=registry, db_engine=db_engine, secrets=[SECRET])


def get(
    registry: ServiceRegistry, db_engine: Engine, path: str = "/api/activity/rejections"
) -> dict[str, object]:
    with TestClient(build(registry, db_engine)) as client:
        response = client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


# --------------------------------------------------------------------------
# Rule 8: the rule, the inputs, the timestamp -- none of the three lost
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_every_rule_either_vocabulary_can_emit_reaches_the_wire(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """Rule 8: a stored refusal the page cannot name is a gap with no cause.

    **What this does and does not close.** Both sides are iterated from the
    enums, so nothing in the read path may drop, rename or merge a rule that
    exists today -- a whitelist added here and left stale fails, which is the
    realistic regression. It does **not** fail when a member is *added* to
    either enum: ``rule`` is an opaque string on the wire, so a new one is
    seeded and served with no further ceremony. Nor would a third ``source``
    value fail here, since ``ALL_RULES`` names the two sources itself rather
    than reading a vocabulary; ``_SOURCE_FOR`` is what refuses one, and the
    column's ``ck_ledger_rejection_source`` is what makes it unreachable
    through this application.
    """
    seed(db_engine, every_rule)

    body = get(registry, db_engine)

    groups = body["groups"]
    assert isinstance(groups, list)
    served = {(group["source"], group["rule"]) for group in groups}
    assert served == set(ALL_RULES)
    assert body["total"] == len(ALL_RULES)


@pytest.mark.risk
def test_a_stored_rule_is_served_with_no_second_vocabulary_check(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """The read path serves the rule it finds. Filtering is the writer's job.

    This slot held a test named for the two by-design declines --
    ``NOT_A_LEDGER_ACTIVITY`` and ``NOT_AN_OPTION`` -- asserting that neither
    is served. It could not fail. ``STORED_LEDGER_RULES`` is ``RejectionRule``
    *less* ``BY_DESIGN_RULES``, so ``every_rule`` never seeded one and the
    loop compared a constant with itself; deleting the writer's own
    ``if rejection.rule in BY_DESIGN_RULES: continue`` -- the exact disaster
    it was named for -- left it green. **The real guard is
    ``tests/engine/test_ingest_rejections.py::test_a_by_design_decline_is_not_stored``**,
    which is where the skip lives.

    What this layer can state honestly is the other half, and it is worth
    stating: there is **no** second filter here. A by-design row, which the
    writer never creates, is served rather than hidden. A read path that
    dropped rows by rule would make a committed refusal invisible with
    nothing to say so, and two vocabularies of "what counts" would drift
    apart -- so nineteen ``FEE`` rows a pass is a bug in the writer, and this
    surface is where you would see it rather than the place to suppress it.
    """
    # Deterministic pick from a frozenset: the set has two members and the
    # test must not depend on which one hashes first.
    by_design = sorted(BY_DESIGN_RULES, key=lambda rule: rule.value)[0]

    def stored_anyway(session: Session) -> None:
        add_rejection(session, source="ledger", rule=by_design.value)

    seed(db_engine, stored_anyway)

    body = get(registry, db_engine)

    assert body["total"] == 1
    assert body["groups"][0]["rule"] == by_design.value  # type: ignore[index]


@pytest.mark.risk
def test_a_refusal_carries_its_rule_its_inputs_and_its_timestamp(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """Rule 8, field by field. All three, or the surface is not worth having."""

    def one(session: Session) -> None:
        add_rejection(
            session,
            source="ledger",
            rule=RejectionRule.UNVERIFIED_DELIVERABLE.value,
            symbol=GME1,
            order_id="ord-7",
            activity_ids=["act-b", "act-a"],
            detail="root GME1 is not the underlying GME, so the deliverable "
            "is not multiplier shares",
            inputs={"multiplier": "100", "root_symbol": "GME1"},
            when=at(3),
            first_seen=at(1),
            activity_at=at(2),
            correlation_id="corr-abc",
        )

    seed(db_engine, one)

    body = get(registry, db_engine)

    groups = body["groups"]
    assert isinstance(groups, list)
    (group,) = groups
    assert group["rule"] == "unverified_deliverable"
    assert group["source"] == "ledger"
    assert group["count"] == 1
    assert group["symbols"] == [GME1]
    (refusal,) = group["refusals"]
    assert refusal["rule"] == "unverified_deliverable"
    # Keys stay exactly as the rule spelled them. Camel-casing an
    # *input's* name would misreport what the rule was applied to.
    assert refusal["inputs"] == {"multiplier": "100", "root_symbol": "GME1"}
    assert refusal["symbol"] == GME1
    assert refusal["orderId"] == "ord-7"
    assert refusal["activityIds"] == ["act-a", "act-b"]
    assert "deliverable" in refusal["detail"]
    assert refusal["correlationId"] == "corr-abc"
    assert datetime.fromisoformat(refusal["at"]) == at(3)
    assert datetime.fromisoformat(refusal["firstSeen"]) == at(1)
    assert datetime.fromisoformat(refusal["activityAt"]) == at(2)


def test_an_activity_with_no_usable_timestamp_still_has_one(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """``activityAt`` is nullable and ``at`` is not, deliberately.

    An activity arriving with no usable timestamp is itself something the
    matcher refuses, so rule 8's timestamp cannot be the vendor's.
    """

    def one(session: Session) -> None:
        add_rejection(session, activity_at=None, when=at(4))

    seed(db_engine, one)

    (refusal,) = get(registry, db_engine)["groups"][0]["refusals"]  # type: ignore[index]

    assert refusal["activityAt"] is None
    assert datetime.fromisoformat(refusal["at"]) == at(4)


# --------------------------------------------------------------------------
# Shape: grouped by rule, because the question is "why", not "which rows"
# --------------------------------------------------------------------------


def test_groups_are_most_affected_first_and_deterministic(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    def mixed(session: Session) -> None:
        for index, symbol in enumerate((GME1, IWM, AAPL)):
            add_rejection(
                session,
                rule=IngestRule.CONTRACT_TERMS_UNAVAILABLE.value,
                symbol=symbol,
                activity_ids=[f"terms-{index}"],
                when=at(index),
            )
        add_rejection(
            session,
            source="ledger",
            rule=RejectionRule.UNVERIFIED_DELIVERABLE.value,
            symbol=GME1,
            activity_ids=["deliv-0"],
            when=at(9),
        )

    seed(db_engine, mixed)

    first = get(registry, db_engine)["groups"]
    second = get(registry, db_engine)["groups"]

    assert [(group["rule"], group["count"]) for group in first] == [  # type: ignore[index,union-attr]
        ("contract_terms_unavailable", 3),
        ("unverified_deliverable", 1),
    ]
    assert first == second


def test_a_rule_names_each_affected_symbol_once_and_in_order(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """Two refusals of one contract are one contract in the symbol list."""

    def repeated(session: Session) -> None:
        for index, symbol in enumerate((IWM, GME1, IWM)):
            add_rejection(
                session, symbol=symbol, activity_ids=[f"a-{index}"], when=at(index)
            )

    seed(db_engine, repeated)

    (group,) = get(registry, db_engine)["groups"]  # type: ignore[misc]

    assert group["symbols"] == [GME1, IWM]
    assert group["count"] == 3


def test_a_refusal_about_no_contract_is_not_a_symbol(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """``missing_symbol`` and the ``mleg`` rules name an order, or nothing."""

    def unattributed(session: Session) -> None:
        add_rejection(
            session,
            source="ingest",
            rule=IngestRule.MLEG_ORDER_HAS_NO_LEGS.value,
            symbol=None,
            order_id="ord-mleg",
        )

    seed(db_engine, unattributed)

    (group,) = get(registry, db_engine)["groups"]  # type: ignore[misc]

    assert group["symbols"] == []
    assert group["count"] == 1
    assert group["refusals"][0]["symbol"] is None
    assert group["refusals"][0]["orderId"] == "ord-mleg"


def test_refusals_within_a_rule_are_newest_first(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    def three(session: Session) -> None:
        for index in (0, 2, 1):
            add_rejection(
                session, activity_ids=[f"a-{index}"], when=at(index), symbol=IWM
            )

    seed(db_engine, three)

    (group,) = get(registry, db_engine)["groups"]  # type: ignore[misc]

    served = [datetime.fromisoformat(row["at"]) for row in group["refusals"]]
    assert served == [at(2), at(1), at(0)]
    assert datetime.fromisoformat(group["latestAt"]) == at(2)
    assert datetime.fromisoformat(group["firstSeen"]) == at(0)


def test_a_book_with_no_refusals_reads_as_zero(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """A gap with no recorded cause is zero causes, never an error.

    ``notBooked`` can be non-zero while this is empty -- the two are computed
    from different tables and a refusal that named no subject is logged rather
    than stored. The page must say the cause was not recorded; it must not see
    a 500 and conclude the engine is broken.

    Asserted as the **whole document**, keys included, because the reader of
    this response is a generated client: a field that is absent when there is
    nothing to put in it is a different contract from a field that is empty,
    and ``unexplainedSymbols`` arriving only on the unhappy path would make
    every client treat its absence as "nothing unexplained" by accident. The
    three answers are empty for three different reasons -- no stored refusal,
    no group to put it in, and no fill to be short of a realized trade.

    The other half of "ordinary" is that nothing is logged, which is
    :func:`test_a_book_with_no_refusals_logs_nothing` below; ``get`` asserts
    the 200 this docstring is about.
    """
    body = get(registry, db_engine)

    assert body == {"total": 0, "groups": [], "unexplainedSymbols": []}


def test_the_other_book_is_not_served(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    def both(session: Session) -> None:
        add_rejection(session, account="paper", symbol=IWM)
        add_rejection(session, account="cash", symbol=GME1)

    seed(db_engine, both)

    body = get(registry, db_engine)

    assert body["total"] == 1
    assert body["groups"][0]["symbols"] == [IWM]  # type: ignore[index]


# --------------------------------------------------------------------------
# The join: a gap with no stored cause is named, never inferred
# --------------------------------------------------------------------------


def group(rule: str, *symbols: str) -> LedgerRefusalGroup:
    """A group carrying nothing but the symbols the join reads."""
    return LedgerRefusalGroup(
        source="ledger",
        rule=rule,
        count=len(symbols),
        symbols=sorted(symbols),
        latest_at=at(0),
        first_seen=at(0),
        refusals=[],
    )


@pytest.mark.risk
def test_a_gap_no_stored_refusal_accounts_for_is_named_unexplained(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """Decision 14's floor: say the cause was not recorded.

    A closing fill with no realized trade and no stored refusal is the case
    the whole surface exists to be honest about. Zero groups and zero total
    are both true, and on their own they read as *nothing to explain* --
    which is the opposite of what happened. The contract has to be named.
    """
    seed(db_engine, lambda session: add_unbooked_close(session, symbol=IWM))

    body = get(registry, db_engine)

    assert body["total"] == 0
    assert body["groups"] == []
    assert body["unexplainedSymbols"] == [IWM]


@pytest.mark.risk
def test_a_refusal_about_another_contract_does_not_explain_this_gap(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """The join is by symbol, not by existence.

    A stored refusal about GME1 says nothing about a gap on IWM. Treating
    *any* refusal as covering *any* gap is how a page names the wrong
    contract, and it is the failure mode of reading only `total`.
    """

    def gap_and_an_unrelated_cause(session: Session) -> None:
        add_unbooked_close(session, symbol=IWM)
        add_rejection(session, symbol=GME1)

    seed(db_engine, gap_and_an_unrelated_cause)

    body = get(registry, db_engine)

    assert body["total"] == 1
    assert body["groups"][0]["symbols"] == [GME1]  # type: ignore[index]
    assert body["unexplainedSymbols"] == [IWM]


@pytest.mark.risk
def test_a_gap_a_stored_refusal_names_is_not_unexplained(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """The happy path: a cause the reader can agree with, so nothing to add."""

    def gap_with_its_cause(session: Session) -> None:
        add_unbooked_close(session, symbol=IWM)
        add_rejection(session, symbol=IWM)

    seed(db_engine, gap_with_its_cause)

    body = get(registry, db_engine)

    assert body["total"] == 1
    assert body["unexplainedSymbols"] == []


def test_a_booked_close_is_no_gap_and_so_nothing_is_unexplained(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """A complete ledger has nothing to explain even with fills in it.

    Without this, a route that named every closed contract would pass the
    three tests above and cry wolf on every book that is fine.
    """
    seed(db_engine, lambda session: add_booked_close(session, symbol=IWM))

    body = get(registry, db_engine)

    assert body["unexplainedSymbols"] == []


@pytest.mark.risk
def test_the_join_is_a_sorted_set_difference_whatever_order_it_is_given() -> None:
    """Pure, and deterministic: the same two sides, the same document.

    ``groups`` arrives most-affected-first and the gap arrives sorted, and
    neither order may reach the answer -- a client diffing two polls must see
    a change only when the set changed.
    """
    gap = LedgerGap(count=3, symbols=(AAPL, GME1, IWM))
    groups = [group("unverified_deliverable", GME1), group("no_open_lot")]

    assert unexplained_symbols(gap, groups) == sorted([AAPL, IWM])
    assert unexplained_symbols(gap, list(reversed(groups))) == sorted([AAPL, IWM])
    # A group naming no contract explains no contract. `symbols` is empty
    # whenever a rule refused an order or a bare activity, which is most of
    # the ingestion vocabulary.
    assert unexplained_symbols(gap, [group("no_open_lot")]) == sorted(
        [AAPL, GME1, IWM]
    )
    assert unexplained_symbols(LedgerGap(count=0, symbols=()), groups) == []


# --------------------------------------------------------------------------
# Money: TEXT inside a JSON blob, and SQL must not be asked about it
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_input_money_is_served_verbatim_and_orders_as_decimal_not_as_text(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """``'10' < '9.5'`` as text, and the other way as ``Decimal``.

    ``inputs`` has no money *column*, so nothing refuses a lexicographic
    comparison on the way past -- the row has to reach the reader exactly as
    stored, and the ordering the surface promises has to be one the database
    can answer correctly. Here that is ``at``, a timestamp; the money is
    carried, never sorted on.
    """

    def three(session: Session) -> None:
        for activity_id, strike, minute in (
            ("ten", "10", 0),
            ("nine-fifty", "9.5", 1),
            ("hundred", "100", 2),
        ):
            add_rejection(
                session,
                source="ledger",
                rule=RejectionRule.UNVERIFIED_DELIVERABLE.value,
                symbol=GME1,
                activity_ids=[activity_id],
                inputs={"strike": strike},
                when=at(minute),
            )

    seed(db_engine, three)

    (group,) = get(registry, db_engine)["groups"]  # type: ignore[misc]

    strikes = [row["inputs"]["strike"] for row in group["refusals"]]
    # Newest first, which is the ordering this surface states -- and an order
    # no sort over the money could have produced. Text ascending is
    # ['10', '100', '9.5'] and descending its reverse; Decimal ascending is
    # ['9.5', '10', '100'] and descending its reverse. The four money answers
    # are four different documents, which is why none of them may be the one
    # served, and why SQLite answering lexicographically with no raise would
    # be invisible.
    assert strikes == ["100", "9.5", "10"]
    assert sorted(strikes) == ["10", "100", "9.5"]
    assert [str(value) for value in sorted(Decimal(text) for text in strikes)] == [
        "9.5",
        "10",
        "100",
    ]


def code_of(path: Path) -> str:
    """A module's tokens with every string and comment dropped.

    A plain substring search over the file would be defeated by the module's
    own prose: ``activity.py`` deliberately *names* the query it refuses to
    write -- ``json_extract(inputs, '$.strike')`` -- so that the next reader
    knows which one. Tokenising means the check reads what the interpreter
    would run, which is the thing the assertion is actually about.
    """
    text = path.read_text(encoding="utf-8")
    parts: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type in (tokenize.NAME, tokenize.OP, tokenize.NUMBER):
            parts.append(token.string)
    return "".join(parts)


def sqlalchemy_imports(path: Path) -> set[str]:
    """Every name a module binds out of SQLAlchemy, whatever it renames it to.

    ``ast`` rather than tokens, because an alias defeats a *name* check and
    nothing else: ``from sqlalchemy import func as f`` binds ``f``, so a
    module that goes on to call ``f.max(...)`` reads clean to any search for
    ``func.`` -- the auditor of step 8d found exactly that hole. The
    **imported** name cannot be hidden, so that is what is read.

    ``import sqlalchemy as sa`` is caught by the same pass: a plain ``import``
    of anything under ``sqlalchemy`` contributes the dotted module name, which
    is not in the allowlist either.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "sqlalchemy":
                bound |= {alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            bound |= {
                alias.name
                for alias in node.names
                if alias.name.split(".")[0] == "sqlalchemy"
            }
    return bound


#: What ``activity.py`` may bind out of SQLAlchemy. An **allowlist**, because
#: the evasions are open-ended and a denylist looks shut while being open:
#: ``func`` under an alias, ``text`` around any SQL at all, ``cast`` to
#: ``Numeric`` -- which on SQLite is a *float*, so it fails rule "Decimal for
#: money" on the way to answering the wrong question anyway -- and whichever
#: SQL-building name nobody has thought of yet. This module needs three names.
#: A fourth is a deliberate edit with this test in front of it, and the
#: question to answer before extending the list is whether the name can be
#: made to compare ``inputs`` or ``detail`` inside the database.
SQLALCHEMY_NAMES: frozenset[str] = frozenset({"Select", "select", "Session"})


@pytest.mark.risk
def test_the_module_asks_sql_no_question_about_the_inputs_blob() -> None:
    """Structural, because the bug is silent when it lands.

    The model's own docstring says it: a future
    ``WHERE json_extract(inputs, '$.strike') > ...`` gets SQLite's
    lexicographic answer with no raise. "Do not write that query."

    **Subscript as well as attribute**, which is the form the idiom actually
    takes and the one this gate missed until step 8d's audit compiled it::

        .where(RejectionRecord.inputs["strike"].as_string() > "10")
        -> WHERE JSON_EXTRACT(ledger_rejection.inputs, ?) > ?

    ``code_of`` drops every string, so that tokenises to
    ``...inputs[].as_string()>...`` -- which the old list's
    ``RejectionRecord.inputs.`` did not contain, nor ``json_extract``, since
    the SQL is emitted by the dialect rather than written. ``ORDER BY`` on the
    blob was caught and ``WHERE`` was not, and ``WHERE`` is the one that drops
    rows: a ``?minStrike=10`` filter asking SQLite lexicographically keeps the
    $9.50 refusal and loses the $100 one, with the suite green.

    The JSON accessors are listed by **suffix** rather than by column, because
    the type of the left-hand side is what makes them available: they exist on
    a ``JSON`` comparator and nowhere else, so naming one at all in this
    module means a question was asked of the blob.
    """
    code = code_of(MODULE)

    for forbidden in (
        "json_extract",
        # Attribute access, and the subscript form the audit compiled.
        "RejectionRecord.inputs.",
        "RejectionRecord.inputs[",
        "RejectionRecord.detail[",
        "order_by(RejectionRecord.inputs",
        "order_by(RejectionRecord.detail",
        # The JSON comparator's accessors, every one of which turns a blob
        # member into something SQL will happily compare.
        ".as_string(",
        ".as_float(",
        ".as_integer(",
        ".as_numeric(",
        ".as_boolean(",
        ".as_json(",
        # A raw fragment, where no comparator of ours is consulted at all.
        # The tokeniser drops the SQL itself, so the call is what is visible.
        "text(",
        "func.",
    ):
        assert forbidden not in code, forbidden


@pytest.mark.risk
def test_the_module_imports_nothing_that_can_build_raw_sql() -> None:
    """The half a token search cannot do: an alias.

    ``from sqlalchemy import func as f`` is not a hypothetical evasion -- it
    is what a reader reaches for when the gate above rejects ``func.``, and it
    leaves the gate looking shut. So the names are checked at the import,
    where renaming is the thing being read rather than the thing hiding.

    Fails loudly and names the extra import, because "add it to the allowlist"
    is sometimes the right answer and "why is this test failing" never is.
    """
    bound = sqlalchemy_imports(MODULE)

    assert bound <= SQLALCHEMY_NAMES, (
        "activity.py binds "
        f"{sorted(bound - SQLALCHEMY_NAMES)} out of SQLAlchemy. If it cannot "
        "be made to compare `inputs` or `detail` inside the database, add it "
        "to SQLALCHEMY_NAMES; if it can, this is the query decision 14's "
        "money paragraph forbids."
    )


# --------------------------------------------------------------------------
# Rule 6: redact, then truncate -- never the other way round
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_a_credential_in_a_stored_detail_never_reaches_the_response(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """The writer redacts against the account-number shape and no secrets.

    ``IngestService`` calls ``vendor_detail`` with no ``secrets``, so a
    credential echoed by a vendor error body is in the row. Rule 6 says it is
    not in the response.
    """

    def leaky(session: Session) -> None:
        add_rejection(
            session,
            detail=f"the contracts endpoint answered 403 for key {SECRET}",
            inputs={"request": f"GET /v2/options/contracts?key={SECRET}"},
        )

    seed(db_engine, leaky)

    body = get(registry, db_engine)

    assert SECRET not in str(body)
    (refusal,) = body["groups"][0]["refusals"]  # type: ignore[index]
    assert "<redacted>" in refusal["detail"]
    assert "<redacted>" in refusal["inputs"]["request"]


@pytest.mark.risk
def test_redaction_runs_before_truncation(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """Truncating first cuts a secret in half and keeps the half.

    Step 8d part 1 shipped exactly this and audit caught it: redaction is
    literal substitution, so a cut secret matches nothing. The row here is
    longer than the response bound and carries the credential astride it.
    """
    prefix = "a" * 1075

    def long_and_leaky(session: Session) -> None:
        add_rejection(session, detail=f"{prefix}{SECRET}")

    seed(db_engine, long_and_leaky)

    body = get(registry, db_engine)

    text = str(body)
    assert SECRET not in text
    # A prefix long enough that no ordinary prose contains it. Present only if
    # the cut happened first.
    assert SECRET[:13] not in text
    (refusal,) = body["groups"][0]["refusals"]  # type: ignore[index]
    assert refusal["detail"].endswith("<redacted>")


@pytest.mark.risk
def test_the_log_line_names_the_rules_and_never_the_free_text(
    registry: ServiceRegistry,
    db_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule 8 on the read path, and rule 6 on the log's ``extra``.

    ``detail`` and ``inputs`` are the two free-text fields and neither is
    re-emitted here: a log record's ``extra`` bypasses the response scrubber
    entirely, which is the second bug 8d part 1 shipped.
    """

    def leaky(session: Session) -> None:
        add_rejection(
            session,
            detail=f"403 for key {SECRET}",
            inputs={"request": SECRET},
        )

    seed(db_engine, leaky)

    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.activity"):
        get(registry, db_engine)

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "activity_rejections_served"
    ]
    assert records, [record.message for record in caplog.records]
    record = records[0]
    assert SECRET not in record.getMessage()
    assert SECRET not in str(record.__dict__)
    assert getattr(record, "rules") == {"ingest/contract_terms_unavailable": 1}
    assert getattr(record, "account") == "paper"


def test_a_book_with_no_refusals_logs_nothing(
    registry: ServiceRegistry,
    db_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing to explain is not a warning."""
    with caplog.at_level(logging.WARNING, logger="corollary.api.routes.activity"):
        get(registry, db_engine)

    assert [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "activity_rejections_served"
    ] == []


# --------------------------------------------------------------------------
# The account boundary -- why `broker: BrokerDep` is in the signature
# --------------------------------------------------------------------------


def test_cash_without_live_keys_is_a_409_and_never_papers_causes(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """A book with no credentials is refused with a stated reason.

    The dependency is depended on for the boundary it enforces and never
    called. Drop it and this answers 200 with paper's refusals under the
    heading of the cash account -- the same misreport as serving paper's
    balance, one table further down.
    """

    def paper_only(session: Session) -> None:
        add_rejection(session, account="paper", symbol=IWM)

    seed(db_engine, paper_only)

    with TestClient(build(registry, db_engine)) as client:
        response = client.get("/api/activity/rejections?account=cash")

    assert response.status_code == 409
    assert "not substituted" in response.json()["error"]["message"]
    assert IWM not in response.text


def test_an_unknown_account_is_a_422(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    with TestClient(build(registry, db_engine)) as client:
        response = client.get("/api/activity/rejections?account=margin")

    assert response.status_code == 422


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------


def test_an_unmigrated_database_says_what_to_run(clientless_app: FastAPI) -> None:
    """The existing convention: 503 with a stated remedy, never a 500."""
    with TestClient(clientless_app, raise_server_exceptions=False) as client:
        response = client.get("/api/activity/rejections")

    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "database_unavailable"
    assert "alembic upgrade head" in body["message"]


def test_health_still_answers_with_no_rejection_table(
    clientless_app: FastAPI,
) -> None:
    with TestClient(clientless_app, raise_server_exceptions=False) as client:
        assert client.get("/api/health").status_code == 200
