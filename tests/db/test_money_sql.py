"""Money in SQL — the operations that must never return a plausible wrong number.

``Money`` stores an exact decimal as TEXT on SQLite, because SQLite has no
exact decimal storage class and a NUMERIC-affinity column would convert
``'7.5'`` to an IEEE double on the way in. That keeps the *value* exact and
breaks every *comparison*: SQLite compares TEXT lexicographically, so against
the five seeded ceilings (7, 20, 8, 25, 40) the database will happily answer

    MAX(value)                  -> '8'          (the true answer is 40)
    MIN(value)                  -> '20'         (the true answer is 7)
    WHERE value > '10'          -> all five     (including 7 and 8)
    WHERE value <= '8'          -> all five     (including 20, 25 and 40)
    ORDER BY value ASC          -> 20, 25, 40, 7, 8

``test_the_storage_layer_really_is_lexicographic`` pins those five answers as
raw SQL, so the danger stays documented in executable form. Every other test
here proves the ORM refuses to ask the question at all.

The concrete failure this prevents is Phase 6's::

    select(RiskLimit).where(RiskLimit.value < computed_risk)

which approves a 35%-of-account trade against a 40% ceiling and reports the
7% per-trade limit as unbreached, because ``'7' > '10'`` is true as text.
Every line of it reads as correct code, which is exactly why it has to raise
rather than be discouraged in a comment.
"""

# Tagged ``risk``, nearly all of it. Every refusal below stops SQLite
# answering a question about a risk ceiling with a plausible wrong number --
# rule 4 failing silently, which is the failure mode the ``Money`` type exists
# to make impossible. The permits are tagged too, for the reason CLAUDE.md
# gives limits: a guard that also blocked reading and writing a ceiling would
# leave the risk manager unable to load the numbers it enforces.
#
# Left bare: ``test_count_is_still_allowed`` and
# ``test_is_null_is_still_allowed``. Both are about the guard's *breadth*, and
# neither asks anything about a value -- counting rows and testing for NULL
# have no representation problem to get wrong.

from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from corollary.db.models import RiskLimit
from corollary.db.seed import risk_limits, seed
from corollary.db.types import MoneyComparisonError


@pytest.fixture
def seeded(session: Session) -> Session:
    seed(session)
    session.commit()
    return session


# --------------------------------------------------------------------- #
# The five measurements, as raw SQL: the wrong answers are real
# --------------------------------------------------------------------- #


@pytest.mark.risk
def test_the_storage_layer_really_is_lexicographic(seeded: Session) -> None:
    """Raw SQL, deliberately bypassing every guard below.

    If this test ever starts failing, the storage representation changed and
    the guards may no longer be needed. Until then they are.
    """
    assert seeded.execute(text("SELECT MAX(value) FROM risk_limit")).scalar_one() == "8"
    assert seeded.execute(text("SELECT MIN(value) FROM risk_limit")).scalar_one() == "20"
    assert (
        seeded.execute(
            text("SELECT COUNT(*) FROM risk_limit WHERE value > '10'")
        ).scalar_one()
        == 5
    )
    assert (
        seeded.execute(
            text("SELECT COUNT(*) FROM risk_limit WHERE value <= '8'")
        ).scalar_one()
        == 5
    )
    assert seeded.execute(
        text("SELECT value FROM risk_limit ORDER BY value ASC")
    ).scalars().all() == ["20", "25", "40", "7", "8"]


# --------------------------------------------------------------------- #
# ... and the ORM refuses to ask any of them
# --------------------------------------------------------------------- #


@pytest.mark.risk
def test_greater_than_raises_instead_of_returning_every_row(seeded: Session) -> None:
    with pytest.raises(MoneyComparisonError):
        seeded.execute(select(RiskLimit.key).where(RiskLimit.value > Decimal("10")))


@pytest.mark.risk
def test_less_than_or_equal_raises_instead_of_returning_every_row(
    seeded: Session,
) -> None:
    with pytest.raises(MoneyComparisonError):
        seeded.execute(select(RiskLimit.key).where(RiskLimit.value <= Decimal("8")))


@pytest.mark.risk
def test_the_phase_six_ceiling_query_raises(seeded: Session) -> None:
    """The exact shape from the audit: a ceiling check that reads as correct."""
    computed_risk = Decimal("35")
    with pytest.raises(MoneyComparisonError):
        seeded.execute(select(RiskLimit).where(RiskLimit.value < computed_risk))


@pytest.mark.risk
def test_less_than_raises() -> None:
    with pytest.raises(MoneyComparisonError):
        RiskLimit.value < Decimal("1")


@pytest.mark.risk
def test_less_than_or_equal_raises() -> None:
    with pytest.raises(MoneyComparisonError):
        RiskLimit.value <= Decimal("1")


@pytest.mark.risk
def test_greater_than_raises() -> None:
    with pytest.raises(MoneyComparisonError):
        RiskLimit.value > Decimal("1")


@pytest.mark.risk
def test_greater_than_or_equal_raises() -> None:
    with pytest.raises(MoneyComparisonError):
        RiskLimit.value >= Decimal("1")


@pytest.mark.risk
def test_between_raises() -> None:
    with pytest.raises(MoneyComparisonError):
        RiskLimit.value.between(Decimal("1"), Decimal("2"))


@pytest.mark.risk
def test_equality_raises_because_seven_is_different_text_from_seven_point_zero() -> None:
    """The two Decimals are equal; the two strings are not."""
    with pytest.raises(MoneyComparisonError):
        RiskLimit.value == Decimal("7.0")


@pytest.mark.risk
def test_inequality_raises() -> None:
    with pytest.raises(MoneyComparisonError):
        RiskLimit.value != Decimal("7.0")


@pytest.mark.risk
def test_in_raises() -> None:
    with pytest.raises(MoneyComparisonError):
        RiskLimit.value.in_([Decimal("7"), Decimal("20")])


def test_is_null_is_still_allowed() -> None:
    """NULL has no representation problem; only values do."""
    assert RiskLimit.value.is_(None) is not None
    assert (RiskLimit.value == None) is not None  # noqa: E711


@pytest.mark.risk
def test_arithmetic_raises_because_sqlite_would_coerce_to_a_float() -> None:
    with pytest.raises(MoneyComparisonError):
        RiskLimit.value + Decimal("1")


# --------------------------------------------------------------------- #
# ORDER BY and the aggregates — caught at execution, not construction
# --------------------------------------------------------------------- #


@pytest.mark.risk
def test_order_by_the_bare_column_raises(seeded: Session) -> None:
    with pytest.raises(MoneyComparisonError):
        seeded.execute(select(RiskLimit.value).order_by(RiskLimit.value)).all()


@pytest.mark.risk
def test_order_by_desc_raises() -> None:
    with pytest.raises(MoneyComparisonError):
        RiskLimit.value.desc()


@pytest.mark.risk
def test_legacy_query_order_by_raises(seeded: Session) -> None:
    with pytest.raises(MoneyComparisonError):
        seeded.query(RiskLimit).order_by(RiskLimit.value).all()


@pytest.mark.risk
def test_max_raises_instead_of_answering_eight(seeded: Session) -> None:
    with pytest.raises(MoneyComparisonError):
        seeded.execute(select(func.max(RiskLimit.value))).scalar_one()


@pytest.mark.risk
def test_min_raises_instead_of_answering_twenty(seeded: Session) -> None:
    with pytest.raises(MoneyComparisonError):
        seeded.execute(select(func.min(RiskLimit.value))).scalar_one()


@pytest.mark.risk
def test_sum_raises(seeded: Session) -> None:
    """SUM was only ever safe by accident.

    SQLite coerced the text to a number and the result processor happened to
    reject the ``int`` it got back — an error, but for the wrong reason and
    only for values that summed to a whole number.
    """
    with pytest.raises(MoneyComparisonError):
        seeded.execute(select(func.sum(RiskLimit.value))).scalar_one()


@pytest.mark.risk
def test_avg_raises(seeded: Session) -> None:
    with pytest.raises(MoneyComparisonError):
        seeded.execute(select(func.avg(RiskLimit.value))).scalar_one()


def test_count_is_still_allowed(seeded: Session) -> None:
    """Counting rows asks nothing about the value."""
    assert seeded.execute(select(func.count(RiskLimit.value))).scalar_one() == 5


@pytest.mark.risk
def test_selecting_and_updating_money_is_untouched(seeded: Session) -> None:
    """The guard blocks questions about ordering, not ordinary use."""
    row = seeded.get(RiskLimit, "max_risk_per_trade_pct")
    assert row is not None
    row.value = Decimal("6")
    seeded.commit()
    seeded.expunge_all()

    again = seeded.get(RiskLimit, "max_risk_per_trade_pct")
    assert again is not None
    assert again.value == Decimal("6")


# --------------------------------------------------------------------- #
# The supported way to get the right answer
# --------------------------------------------------------------------- #


@pytest.mark.risk
def test_risk_limits_reads_every_ceiling_as_a_decimal(seeded: Session) -> None:
    limits = risk_limits(seeded)
    assert limits == {
        "max_risk_per_trade_pct": Decimal("7"),
        "max_daily_loss_pct": Decimal("20"),
        "max_concurrent_positions": Decimal("8"),
        "max_exposure_per_underlying": Decimal("25"),
        "max_net_directional_pct": Decimal("40"),
    }
    assert all(type(v) is Decimal for v in limits.values())


@pytest.mark.risk
def test_comparing_in_python_gives_the_true_answers(seeded: Session) -> None:
    """The same five questions, answered correctly."""
    limits = risk_limits(seeded)
    values = list(limits.values())

    assert max(values) == Decimal("40")
    assert min(values) == Decimal("7")
    assert sorted(values) == [
        Decimal("7"),
        Decimal("8"),
        Decimal("20"),
        Decimal("25"),
        Decimal("40"),
    ]
    assert sorted(k for k, v in limits.items() if v > Decimal("10")) == [
        "max_daily_loss_pct",
        "max_exposure_per_underlying",
        "max_net_directional_pct",
    ]
    assert sorted(k for k, v in limits.items() if v <= Decimal("8")) == [
        "max_concurrent_positions",
        "max_risk_per_trade_pct",
    ]


@pytest.mark.risk
def test_the_thirty_five_percent_trade_breaches_the_per_trade_ceiling_only(
    seeded: Session,
) -> None:
    """The audit's scenario, answered the supported way.

    A 35%-of-account trade is under the 40% net-directional ceiling and over
    the 7% per-trade ceiling. The SQL version got both backwards.
    """
    computed_risk = Decimal("35")
    limits = risk_limits(seeded)
    breached = sorted(key for key, ceiling in limits.items() if computed_risk > ceiling)
    assert "max_risk_per_trade_pct" in breached
    assert "max_net_directional_pct" not in breached
