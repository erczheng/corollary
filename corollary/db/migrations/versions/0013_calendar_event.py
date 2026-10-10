"""calendar_event: the News calendar's rows -- vendor, seed and manual

Phase 3 step 7 (decisions 7, 8 and 9, Q3, Q15). One table for every calendar
producer:

* ``finnhub`` writes ``earnings`` and ``ipo``; ``alpaca`` writes ``dividend``;
  ``fred`` writes ``economic``; ``seed`` writes ``central-bank``. Each upserts
  on UNIQUE ``(source, kind, vendor_id)``, so a re-fetch updates in place and a
  rescheduled event moves rather than duplicating.
* ``manual`` writes ``geopolitical`` (decision 9): no ``vendor_id``, removal is
  a soft delete through ``deleted_at``, and none of it reaches ``audit_log``.

``kind`` and ``source`` are CHECK-constrained and paired, so a typo does not
create a kind and a producer cannot write another's. ``date`` is the Eastern
session and ``at`` the UTC instant or NULL -- separate, because a date-only
event stored as a midnight instant groups under the previous day in New York.
``estimate``/``prior``/``actual`` are ``Money`` TEXT (signed plain decimals);
``estimate`` is NULL on an economic release (Q3: consensus unavailable).
``session`` is an earnings report's ``bmo``/``amc``/``dmh`` (decision 7), NULL
when unknown and NULL on every other kind; it is not a time, so ``at`` stays
NULL beside it.

``exchange``, ``shares``, ``price_low``, ``price_high`` and ``ipo_status`` are
Q15's IPO fields, NULL on every other kind. ``price_low <= price_high`` is not
a CHECK (``Money`` TEXT compares lexicographically); ``CalendarEventInput``
enforces it.

**Amended in-branch (unit 7.2b-F).** The IPO columns were added to this
revision after it was committed on ``phase3-step7``, rather than in a 0014 of
their own. That is correct only because 0013 has not merged anywhere -- no
database outside this branch's tests has ever run it -- and Q22 renumbers it
at merge regardless. Once it has merged, a change here is a new revision.

**The number is provisional (Q22).** Steps 5 and 7 each add a revision on
``0012``. Whichever merges second renumbers its revision to the next free
number and re-points ``down_revision`` at the head it lands on; no merge
revision.

The downgrade drops the table, discarding manual rows with it.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A copy, not an import, for the reason every earlier revision states: a
# migration is a frozen record of the schema on the day.
_MONEY_TEXT_WIDTH = 40


def _money_shape(column: str, *, nullable: bool = False) -> str:
    """A signed plain decimal string: ``-7``, ``0.00``, ``123.45``. See 0002."""
    body = (
        f"(CASE WHEN substr({column}, 1, 1) = '-' "
        f"THEN substr({column}, 2) ELSE {column} END)"
    )
    shape = (
        f"typeof({column}) = 'text' "
        f"AND length({column}) BETWEEN 1 AND {_MONEY_TEXT_WIDTH} "
        f"AND {body} GLOB '[0-9]*' "
        f"AND NOT {body} GLOB '*[^0-9.]*' "
        f"AND NOT {body} GLOB '*.' "
        f"AND length({body}) - length(replace({body}, '.', '')) <= 1"
    )
    if nullable:
        return f"{column} IS NULL OR ({shape})"
    return shape


def upgrade() -> None:
    op.create_table(
        "calendar_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=True),
        sa.Column("estimate", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True),
        sa.Column("prior", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True),
        sa.Column("actual", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("session", sa.String(length=8), nullable=True),
        sa.Column("exchange", sa.String(length=64), nullable=True),
        sa.Column("shares", sa.Integer(), nullable=True),
        sa.Column("price_low", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True),
        sa.Column("price_high", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True),
        sa.Column("ipo_status", sa.String(length=16), nullable=True),
        sa.Column("vendor_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('earnings', 'economic', 'central-bank', 'dividend', "
            "'geopolitical', 'ipo')",
            name="ck_calendar_event_kind",
        ),
        sa.CheckConstraint(
            "source IN ('finnhub', 'alpaca', 'fred', 'seed', 'manual')",
            name="ck_calendar_event_source",
        ),
        sa.CheckConstraint(
            "(kind = 'earnings' AND source = 'finnhub') OR "
            "(kind = 'economic' AND source = 'fred') OR "
            "(kind = 'central-bank' AND source = 'seed') OR "
            "(kind = 'dividend' AND source = 'alpaca') OR "
            "(kind = 'geopolitical' AND source = 'manual') OR "
            "(kind = 'ipo' AND source = 'finnhub')",
            name="ck_calendar_event_kind_source",
        ),
        sa.CheckConstraint(
            "(source = 'manual' AND vendor_id IS NULL) OR "
            "(source <> 'manual' AND vendor_id IS NOT NULL AND vendor_id <> '')",
            name="ck_calendar_event_vendor_id",
        ),
        sa.CheckConstraint("trim(title) <> ''", name="ck_calendar_event_title"),
        sa.CheckConstraint(
            "ticker IS NULL OR (ticker <> '' AND ticker = upper(ticker))",
            name="ck_calendar_event_ticker",
        ),
        sa.CheckConstraint(
            "kind NOT IN ('earnings', 'dividend') OR ticker IS NOT NULL",
            name="ck_calendar_event_ticker_required",
        ),
        sa.CheckConstraint(
            "kind <> 'economic' OR estimate IS NULL",
            name="ck_calendar_event_economic_estimate",
        ),
        sa.CheckConstraint(
            "session IS NULL OR session IN ('bmo', 'amc', 'dmh')",
            name="ck_calendar_event_session",
        ),
        sa.CheckConstraint(
            "session IS NULL OR kind = 'earnings'",
            name="ck_calendar_event_session_earnings_only",
        ),
        sa.CheckConstraint(
            _money_shape("estimate", nullable=True), name="ck_calendar_event_estimate"
        ),
        sa.CheckConstraint(
            _money_shape("prior", nullable=True), name="ck_calendar_event_prior"
        ),
        sa.CheckConstraint(
            _money_shape("actual", nullable=True), name="ck_calendar_event_actual"
        ),
        sa.CheckConstraint(
            "kind = 'ipo' OR (exchange IS NULL AND shares IS NULL AND price_low IS NULL "
            "AND price_high IS NULL AND ipo_status IS NULL)",
            name="ck_calendar_event_ipo_fields",
        ),
        sa.CheckConstraint(
            "exchange IS NULL OR trim(exchange) <> ''", name="ck_calendar_event_exchange"
        ),
        sa.CheckConstraint(
            "shares IS NULL OR (typeof(shares) = 'integer' AND shares > 0)",
            name="ck_calendar_event_shares",
        ),
        sa.CheckConstraint(
            _money_shape("price_low", nullable=True), name="ck_calendar_event_price_low"
        ),
        sa.CheckConstraint(
            _money_shape("price_high", nullable=True), name="ck_calendar_event_price_high"
        ),
        sa.CheckConstraint(
            "(price_low IS NULL) = (price_high IS NULL)", name="ck_calendar_event_price_pair"
        ),
        sa.CheckConstraint(
            "ipo_status IS NULL OR ipo_status IN ('expected', 'filed', 'priced', 'withdrawn')",
            name="ck_calendar_event_ipo_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "kind", "vendor_id", name="uq_calendar_event_vendor_key"),
    )
    op.create_index("ix_calendar_event_date", "calendar_event", ["date"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_calendar_event_date", table_name="calendar_event")
    op.drop_table("calendar_event")
