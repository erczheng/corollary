"""What a risk ceiling is allowed to be — enforced by the server, twice.

CLAUDE.md rule 4: *"The UI displays limits. The engine enforces them. Never
trust a value that arrived from the client."* ``web/src/lib/settings.ts``
``validateRiskLimit`` rejects every value below, but that is the client, and
a client-side check is a courtesy rather than a control.

Each of these five round-tripped without complaint before this was added, and
each one is a distinct way to disable the risk manager:

===================  ==================================================
``Decimal('Inf')``   every ceiling check passes — no limit at all
``Decimal('NaN')``   every comparison against it is False, so likewise
``Decimal('-7')``    every trade is over the ceiling; the bot never trades
``Decimal('0')``     same, and reads as a deliberate "no risk" setting
``Decimal('999999')``  a 999999% ceiling is not a ceiling
===================  ==================================================

Two layers, tested separately because they fail at different moments and a
caller can only reach one of them:

* ``validate_risk_limit`` / the ORM validator — rejects at assignment, with a
  message naming the key and its range.
* the ``ck_risk_limit_value`` CHECK constraint — rejects the same values
  arriving as raw SQL, which is the path that skips Python entirely.
"""

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corollary.db.models import RISK_LIMIT_RANGES, RiskLimit, validate_risk_limit

#: The values the audit round-tripped. Every one disables the risk manager.
DISABLING_VALUES = [
    Decimal("Infinity"),
    Decimal("-Infinity"),
    Decimal("NaN"),
    Decimal("-7"),
    Decimal("0"),
    Decimal("999999"),
]


# --------------------------------------------------------------------- #
# The Python layer
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", DISABLING_VALUES, ids=str)
def test_the_orm_refuses_a_value_that_would_disable_the_risk_manager(
    session: Session, bad: Decimal
) -> None:
    with pytest.raises(ValueError):
        RiskLimit(key="max_risk_per_trade_pct", value=bad)


@pytest.mark.parametrize("bad", DISABLING_VALUES, ids=str)
def test_the_orm_refuses_them_on_an_edit_too(session: Session, bad: Decimal) -> None:
    """Settings edits an existing row; assignment is the common path."""
    row = RiskLimit(key="max_risk_per_trade_pct", value=Decimal("7"))
    session.add(row)
    session.commit()

    with pytest.raises(ValueError):
        row.value = bad


@pytest.mark.parametrize("bad", DISABLING_VALUES, ids=str)
def test_the_orm_refuses_them_whichever_order_the_fields_are_set(bad: Decimal) -> None:
    """``value`` before ``key`` must validate as strictly as ``key`` first."""
    with pytest.raises(ValueError):
        limit = RiskLimit()
        limit.value = bad
        limit.key = "max_risk_per_trade_pct"


@pytest.mark.parametrize("bad", DISABLING_VALUES, ids=str)
def test_an_unknown_key_still_gets_the_universal_rules(bad: Decimal) -> None:
    """No configured range is not the same as no rules.

    A key with no entry in ``RISK_LIMIT_RANGES`` has no per-limit range, but
    a ceiling still has to be a positive, finite, plausibly sized number.
    """
    with pytest.raises(ValueError):
        RiskLimit(key="max_vibes_pct", value=bad)


def test_a_count_must_be_a_whole_number_of_positions() -> None:
    with pytest.raises(ValueError):
        RiskLimit(key="max_concurrent_positions", value=Decimal("8.5"))


def test_a_percentage_may_be_fractional() -> None:
    limit = RiskLimit(key="max_risk_per_trade_pct", value=Decimal("7.5"))
    assert limit.value == Decimal("7.5")


@pytest.mark.parametrize("key,low,high", [(k, r.low, r.high) for k, r in RISK_LIMIT_RANGES.items()])
def test_every_limit_permits_both_ends_of_its_range(
    key: str, low: Decimal, high: Decimal
) -> None:
    """Boundaries are inclusive, and proving that is half of a limit's tests."""
    assert validate_risk_limit(key, low) is None
    assert validate_risk_limit(key, high) is None


@pytest.mark.parametrize("key,low,high", [(k, r.low, r.high) for k, r in RISK_LIMIT_RANGES.items()])
def test_every_limit_rejects_just_outside_its_range(
    key: str, low: Decimal, high: Decimal
) -> None:
    with pytest.raises(ValueError):
        validate_risk_limit(key, low - Decimal("0.001"))
    with pytest.raises(ValueError):
        validate_risk_limit(key, high + Decimal("0.001"))


def test_the_ranges_match_the_settings_page() -> None:
    """The server is authoritative, but it must not silently disagree with
    the control the human is typing into — a field that accepts a value the
    server then rejects is a bug report either way round."""
    assert {k: (r.low, r.high, r.whole) for k, r in RISK_LIMIT_RANGES.items()} == {
        "max_risk_per_trade_pct": (Decimal("1"), Decimal("25"), False),
        "max_daily_loss_pct": (Decimal("1"), Decimal("50"), False),
        "max_concurrent_positions": (Decimal("1"), Decimal("20"), True),
        "max_exposure_per_underlying": (Decimal("5"), Decimal("100"), False),
        "max_net_directional_pct": (Decimal("5"), Decimal("100"), False),
    }


def test_the_seeded_defaults_all_validate() -> None:
    from corollary.db.seed import RISK_LIMIT_DEFAULTS

    for key, value in RISK_LIMIT_DEFAULTS.items():
        assert validate_risk_limit(key, value) is None


def test_the_error_names_the_key_and_the_range() -> None:
    """A rejection nobody can act on is a rejection that gets worked around."""
    with pytest.raises(ValueError) as caught:
        validate_risk_limit("max_risk_per_trade_pct", Decimal("99"))
    message = str(caught.value)
    assert "max_risk_per_trade_pct" in message
    assert "25" in message


# --------------------------------------------------------------------- #
# The CHECK constraint — the layer that survives Python being skipped
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw", ["Infinity", "-Infinity", "NaN", "-7", "0", "0.00", "999999", "", "seven"],
    ids=repr,
)
def test_raw_sql_cannot_write_a_value_that_would_disable_the_risk_manager(
    session: Session, raw: str
) -> None:
    with pytest.raises(IntegrityError):
        session.execute(
            text("INSERT INTO risk_limit (key, value) VALUES ('probe', :v)"),
            {"v": raw},
        )
        session.commit()
    session.rollback()


@pytest.mark.parametrize("raw", ["1", "7", "7.5", "20", "100", "999.999"], ids=repr)
def test_raw_sql_accepts_a_well_formed_positive_ceiling(
    session: Session, raw: str
) -> None:
    session.execute(
        text("INSERT INTO risk_limit (key, value) VALUES ('probe', :v)"),
        {"v": raw},
    )
    session.commit()
    stored = session.get(RiskLimit, "probe")
    assert stored is not None
    assert stored.value == Decimal(raw)


def test_the_check_constraint_keeps_the_column_text(session: Session) -> None:
    """A bare number written by raw SQL would come back as an int or a float.

    TEXT affinity converts it on the way in, and the constraint says so out
    loud so the guarantee does not rest on affinity alone.
    """
    session.execute(text("INSERT INTO risk_limit (key, value) VALUES ('probe', 7)"))
    session.commit()
    storage_class = session.execute(
        text("SELECT typeof(value) FROM risk_limit WHERE key = 'probe'")
    ).scalar_one()
    assert storage_class == "text"
