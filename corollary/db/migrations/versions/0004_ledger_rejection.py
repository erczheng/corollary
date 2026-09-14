"""ledger_rejection: the refusals outlive the process that made them

One table, and decision 14 is the whole reason for it: *"A gap with a stated
cause is a decision the reader can agree with; a gap without one is
indistinguishable from a bug, and the reader's only honest response is to stop
trusting the number."* The matcher and ingestion both refuse work they cannot
do correctly — a contract whose terms never arrived, an exercise with no
settlement price, an adjusted deliverable that invalidates the strike — and
until this revision every one of those refusals lived only in an
``IngestResult`` and a log line. The Activity page could say lifetime P&L was
short a trade for exactly as long as the process that noticed stayed up.

**Why a table rather than handing ``IngestResult`` to the API**, which the
design spec leaned towards: ``IngestService._cursor`` is in memory today, so
every restart re-pulls the whole history and re-derives every refusal. That is
the property the in-memory answer depends on, and it is temporary — this
schema's own ``fill`` table exists because *"page_size maxes at 100 and
re-fetching all history per request is untenable"*, so the cursor gets
persisted eventually. On that day the *count* of unbooked trades survives the
restart and the *reason* silently stops surviving, which is the worst of the
three possible outcomes. A table does not depend on an unrelated module
staying as it is.

**No money column, and that is not an oversight.** A refusal is the absence of
a figure; if it could state one it would not be a refusal. Nothing here is
``Money``/VARCHAR(40) and nothing here is NUMERIC — the question this table
answers is *how many trades are missing*, which is a count.

"No money column" is true; *"no money in the row"* is not. ``inputs`` is a
JSON blob and several rules put stringified money inside it — ``strike``,
``net_amount``, ``paired_price``, ``multiplier``. Those are TEXT inside a
blob, where neither ``Money``'s refusing comparator nor ``guard_money_sql``
can see them, so a future ``WHERE json_extract(inputs, '$.strike') > …``
gets SQLite's **lexicographic** answer with no raise. Load the row and
compare as ``Decimal`` in Python; do not write that query.

**Two enums, one ``rule`` column.** ``source`` says which vocabulary the value
came from: ``corollary.engine.ledger.RejectionRule`` for ``ledger``,
``corollary.engine.ingest.IngestRule`` for ``ingest``. Neither is translated
into the other, because a refusal to *fetch* and a refusal to *book* are
different failures with different remedies. ``rule`` deliberately carries no
CHECK listing today's members: the Python enums are the authority, and a
constraint here would turn adding a rule into a migration for no gain in
safety. ``source`` does carry one — two values, both structural.

**``fingerprint`` is what makes a stored refusal safe.** A persisted refusal
can go stale in a way an in-memory one cannot: rebuild the ledger, book what
was previously refused, and a leftover row claims a gap that no longer exists
— a confident wrong reason on a money figure, which decision 14 rates as
worse than an admitted gap. The writer therefore reconciles rather than
appends, upserting on ``(account, fingerprint)`` and clearing rows whose
subject it re-examined and no longer refuses. The UNIQUE is what stops two
passes double-counting one gap; the digest is what keeps the key
index-sized.

What the digest covers is the refusal's **identity** — its rule, and the
contract or order it concerns — and never the activity ids it happens to
name. Those are evidence: an ingest refusal lists every held activity for its
symbol, which is a property of how much history the pass pulled rather than of
the refusal, so hashing them made the key move between passes and turned the
upsert into an append. The ids are stored in ``activity_ids`` and left out of
the key.

A refusal that names **no** subject at all is therefore not storable, and the
writer skips it with a WARNING rather than inventing one: every subject-less
refusal of one rule digests identically, so two genuinely different gaps would
collapse into a single row and the count would *under*-state.

**Rule 6 has one stated hole at this boundary.** ``wire._ACCOUNT_NUMBER``
matches an Alpaca *paper* account number (``PA`` plus ten characters); a live
account number is bare digits and is deliberately uncovered, because no
digit-run rule can tell one from a quantity or an epoch. ``account`` here
admits ``cash``. This table did not create that gap, but it moves the
consequence from a log line that rotates away to a committed row that does
not.

**Three timestamps, and ``at`` is the one rule 8 means.** ``at`` is when the
refusal was recorded and is NOT NULL; ``activity_at`` is the vendor's own
stamp and is nullable, because an activity arriving with no usable timestamp
is *itself* one of the things the matcher refuses. A NOT NULL column holding
the vendor's stamp could not be written for exactly the rows that most need
writing. ``first_seen`` is the third: ``at`` is refreshed every time a pass
re-asserts the refusal, so on its own the table can say *still true as of* and
cannot say *since when* — and how long a gap has persisted is exactly what
tells a reader whether it is new or has been ignored for a month. All three
are UTC, like every other timestamp in this schema.

``detail`` and ``inputs`` hold free text, and rule 6 reaches a persistence
boundary here for the first time: Alpaca embeds the account number in prose a
field-name redactor cannot see inside, so the writer runs ``vendor_detail``'s
substring pass on both *before* they reach these columns. A secret in a log is
bad; a secret in a committed database row outlives the log.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copies, not imports, for the reason 0001, 0002 and 0003 all state: a
# migration is a frozen record of the schema on the day, and one that imported
# live constants would rewrite its own history the moment one changed.

_ACCOUNT_MODES = ("paper", "cash")
_REJECTION_SOURCES = ("ledger", "ingest")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    op.create_table(
        "ledger_rejection",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account", sa.String(length=8), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("rule", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        # JSON, not a second table: a ledger rejection names exactly one
        # activity and an ingest refusal may name a symbol's whole history,
        # and nothing queries by id — the reconcile loads the account's rows
        # and matches in Python, the way the fill upsert does.
        sa.Column("activity_ids", sa.JSON(), nullable=False),
        # 1280 = STORED_DETAIL_MAX (1024, the scrubber's storage bound) plus
        # room for the truncation notice it appends on top. The log's bound is
        # 300 and is a different number for a stated reason: a stored refusal
        # is mostly our own prose, and its *last* clause is the one that says
        # whether a position merely lacks terms or has ended with its P&L
        # permanently missing.
        sa.Column("detail", sa.String(length=1280), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        # Two of the three timestamps answer different questions: `at` is
        # refreshed on every reassertion (*still true as of*), `first_seen`
        # never is (*since when*). Without the second the table cannot say how
        # long a gap has persisted, and a stale row is indistinguishable from
        # a fresh one.
        sa.Column("first_seen", sa.DateTime(), nullable=False),
        sa.Column("activity_at", sa.DateTime(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            _in_list("account", _ACCOUNT_MODES), name="ck_ledger_rejection_account"
        ),
        sa.CheckConstraint(
            _in_list("source", _REJECTION_SOURCES), name="ck_ledger_rejection_source"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account", "fingerprint", name="uq_ledger_rejection_fingerprint"
        ),
    )
    # "What is missing from this book, most recent first" — the whole read
    # pattern. Orderable, unlike the Money and ActivityId columns elsewhere in
    # this schema, because it is a timestamp.
    op.create_index(
        "ix_ledger_rejection_account_at", "ledger_rejection", ["account", "at"]
    )


def downgrade() -> None:
    """Drops the table, and with it every stated cause it held.

    Losing the reasons is the actual cost of going back, and it is worth
    naming: the refusals themselves are recomputed on the next pass, because
    the ledger is rebuilt from ``fill`` and the broker's history rather than
    from this table. Nothing else reads it, so nothing downstream breaks —
    the terminal simply goes back to a count with no reason beside it.
    """
    op.drop_index("ix_ledger_rejection_account_at", table_name="ledger_rejection")
    op.drop_table("ledger_rejection")
