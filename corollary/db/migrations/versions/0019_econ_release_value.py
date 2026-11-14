"""econ_release_value and econ_release_fetch: economic first prints, kept apart from the calendar row

**Revises 0018** (R4's void tool), which revises 0017 (R3's
``sentiment_accuracy.sessions``). This revision was written on the
``phase3-rem-econ`` branch as a provisional 0017 while the other Phase 3
remainder branch held 0017 and 0018. Per the spec's Q22 rule
(``2026-11-09-phase-3-steps-5-7-remainder.md``, *Database*: numbers are
assigned at merge, after head ``0016``) it was numbered 0019 when that
branch reached ``master``. The renumber is done:

* this file's name, ``revision`` and ``down_revision``;
* ``tests/db/test_migrations.py`` -- the head assertion
  (``test_there_is_exactly_one_head``) and ``_0019_TABLES`` beside it.
  ``tests/db/test_cash_session_migration.py`` holds no head assertion;
* ``tests/db/test_econ_release_value.py`` finds this revision by its file
  name's ``_econ_release_value.py`` suffix, never a literal id, and asserts
  it revises the revision numbered one before it, so its pin followed the
  renumber unaided (0019 revising 0018); the merge added only a comment and
  an assertion that 0018's ``label_outcome_voided`` survives a downgrade;
* ``EconReleaseValue`` / ``EconReleaseFetch`` in ``corollary/db/models.py``
  name the migration by that suffix, not by number, and needed no edit.

Spec R6 and unit U6d:

* ``econ_release_value`` -- one economic release's first print, primary key
  ``(release_id, release_date)``, which joins to ``calendar_event.vendor_id
  = "<release_id>:<release_date>"``. ``actual`` and ``prior`` are ``Money``
  TEXT (signed plain decimals, the 0002 shape). ``period`` is the
  observation date the figure describes; ``vintage_date`` must equal
  ``release_date``. **Written once, never updated** (Q23c): the store is
  insert-only. ``calendar_event.prior``/``.actual`` are not touched and stay
  NULL on economic rows, because the daily dates refresh rewrites them.
* ``econ_release_fetch`` -- the **last** attempt per release, upserted on
  every attempt: ``attempted_at``, ``outcome`` (CHECK: ``figure``,
  ``not_posted``, ``no_value``, ``refused``, ``error``) and ``detail`` --
  NULL on ``figure``/``not_posted``, required and non-blank on the other
  three, at most 1280 characters. It exists so the value table can stay
  write-once while R8's ``unavailable`` note can still quote the vendor's
  reason. The display state itself is derived on read and stored nowhere.

Neither table has a foreign key to ``calendar_event``: that table's key is
``(source, kind, vendor_id)`` and its rows are soft-deleted and revived by
the refresh, so the join is by value, on read.

Downgrade drops both tables. A first print is re-fetchable from FRED's
vintage, so nothing is lost that cannot be recovered.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copies, not imports, for the reason every earlier revision states: a
# migration is a frozen record of the schema on the day.
# tests/db/test_econ_release_value.py runs every CHECK against both this and
# ``create_all()``, so drift fails a test.
_MONEY_TEXT_WIDTH = 40
_OUTCOMES: tuple[str, ...] = ("figure", "not_posted", "no_value", "refused", "error")
_DETAILED_OUTCOMES: tuple[str, ...] = ("no_value", "refused", "error")
_DETAIL_MAX = 1280


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _money_shape(column: str) -> str:
    """A signed plain decimal string: ``-7``, ``0.00``, ``123.45``. See 0002."""
    body = (
        f"(CASE WHEN substr({column}, 1, 1) = '-' "
        f"THEN substr({column}, 2) ELSE {column} END)"
    )
    return (
        f"typeof({column}) = 'text' "
        f"AND length({column}) BETWEEN 1 AND {_MONEY_TEXT_WIDTH} "
        f"AND {body} GLOB '[0-9]*' "
        f"AND NOT {body} GLOB '*[^0-9.]*' "
        f"AND NOT {body} GLOB '*.' "
        f"AND length({body}) - length(replace({body}, '.', '')) <= 1"
    )


_DETAIL_RULE = (
    f"({_in_list('outcome', _DETAILED_OUTCOMES)} "
    f"AND detail IS NOT NULL AND trim(detail) <> '' "
    f"AND length(detail) <= {_DETAIL_MAX}) "
    f"OR (NOT {_in_list('outcome', _DETAILED_OUTCOMES)} AND detail IS NULL)"
)


def upgrade() -> None:
    op.create_table(
        "econ_release_value",
        sa.Column("release_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("release_date", sa.Date(), nullable=False),
        sa.Column("series_id", sa.String(length=32), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("actual", sa.String(length=_MONEY_TEXT_WIDTH), nullable=False),
        sa.Column("prior", sa.String(length=_MONEY_TEXT_WIDTH), nullable=False),
        sa.Column("unit_label", sa.String(length=32), nullable=False),
        sa.Column("vintage_date", sa.Date(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("release_id > 0", name="ck_econ_release_value_release_id"),
        sa.CheckConstraint("trim(series_id) <> ''", name="ck_econ_release_value_series_id"),
        sa.CheckConstraint(
            "trim(unit_label) <> ''", name="ck_econ_release_value_unit_label"
        ),
        sa.CheckConstraint(_money_shape("actual"), name="ck_econ_release_value_actual"),
        sa.CheckConstraint(_money_shape("prior"), name="ck_econ_release_value_prior"),
        sa.CheckConstraint(
            "vintage_date = release_date", name="ck_econ_release_value_vintage_date"
        ),
        sa.PrimaryKeyConstraint("release_id", "release_date"),
    )
    op.create_table(
        "econ_release_fetch",
        sa.Column("release_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("release_date", sa.Date(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.CheckConstraint("release_id > 0", name="ck_econ_release_fetch_release_id"),
        sa.CheckConstraint(
            _in_list("outcome", _OUTCOMES), name="ck_econ_release_fetch_outcome"
        ),
        sa.CheckConstraint(_DETAIL_RULE, name="ck_econ_release_fetch_detail"),
        sa.PrimaryKeyConstraint("release_id", "release_date"),
    )


def downgrade() -> None:
    op.drop_table("econ_release_fetch")
    op.drop_table("econ_release_value")
