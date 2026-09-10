"""OCC option symbols, and the adjusted-contract detection that rides on them.

The adjusted case is the one with money behind it. After a split or special
dividend, OCC issues a modified root (``AAPL1``) and the deliverable is no
longer 100 shares — so a max-loss computed with a hardcoded 100 multiplier is
wrong, which is the failure CLAUDE.md rule 4 exists to prevent.
"""

from datetime import date
from decimal import Decimal

import pytest

from corollary.instruments import (
    OptionType,
    format_occ_symbol,
    is_adjusted_root,
    parse_occ_symbol,
)


def test_the_claude_md_worked_example() -> None:
    """AAPL241220C00150000 is the AAPL $150 call expiring 20 Dec 2024."""
    parsed = parse_occ_symbol("AAPL241220C00150000")
    assert parsed.root == "AAPL"
    assert parsed.expiration == date(2024, 12, 20)
    assert parsed.option_type is OptionType.CALL
    assert parsed.strike == Decimal("150")


def test_a_symbol_from_the_live_account() -> None:
    parsed = parse_occ_symbol("NVDA260911C00205000")
    assert parsed.root == "NVDA"
    assert parsed.expiration == date(2026, 9, 11)
    assert parsed.option_type is OptionType.CALL
    assert parsed.strike == Decimal("205")


def test_a_put_parses_as_a_put() -> None:
    assert parse_occ_symbol("SPY261218P00600000").option_type is OptionType.PUT


def test_a_fractional_strike_keeps_its_thousandths_exactly() -> None:
    """Strike is 8 digits x1000, so 00007500 is $7.50 and not 7500."""
    assert parse_occ_symbol("F260116C00007500").strike == Decimal("7.5")
    assert parse_occ_symbol("IWM260320P00280500").strike == Decimal("280.5")


def test_the_strike_is_a_decimal_and_never_a_float() -> None:
    strike = parse_occ_symbol("SPY261218P00600250").strike
    assert isinstance(strike, Decimal)
    assert strike == Decimal("600.25")
    # 600.25 happens to be representable in binary; 0.001 increments are not.
    assert parse_occ_symbol("AAPL261218C00000001").strike == Decimal("0.001")


def test_a_short_root_parses() -> None:
    parsed = parse_occ_symbol("F260116C00012000")
    assert parsed.root == "F"


def test_an_adjusted_root_parses_and_is_detected() -> None:
    parsed = parse_occ_symbol("AAPL1241220C00150000")
    assert parsed.root == "AAPL1"
    assert is_adjusted_root(parsed.root, "AAPL") is True


def test_an_ordinary_root_is_not_adjusted() -> None:
    assert is_adjusted_root("AAPL", "AAPL") is False


def test_adjusted_detection_is_case_insensitive_but_not_prefix_based() -> None:
    """``AAPL1`` is adjusted; ``AAPL`` under a lower-cased underlying is not.

    A prefix test would call every ``AAPL`` contract adjusted, and a naive
    ``startswith`` in the other direction would miss nothing but would also
    call ``BRK`` an adjusted ``B``.
    """
    assert is_adjusted_root("AAPL", "aapl") is False
    assert is_adjusted_root("aapl1", "AAPL") is True
    assert is_adjusted_root("BRK", "B") is True


@pytest.mark.parametrize(
    "symbol",
    [
        "",
        "AAPL",
        "AAPL241220C0015000",  # 7 strike digits
        "AAPL241220C001500000",  # 9 strike digits
        "AAPL241220X00150000",  # not C or P
        "AAPL241320C00150000",  # month 13
        "AAPL240230C00150000",  # 30 February
        "241220C00150000",  # no root
        "AAPL241220C0015000O",  # letter in the strike
        "TOOLONGX241220C00150000",  # root over six characters
    ],
)
def test_a_malformed_symbol_is_rejected_rather_than_guessed(symbol: str) -> None:
    with pytest.raises(ValueError):
        parse_occ_symbol(symbol)


def test_none_and_whitespace_are_rejected() -> None:
    with pytest.raises(ValueError):
        parse_occ_symbol("   ")


def test_format_round_trips() -> None:
    symbol = format_occ_symbol("AAPL", date(2024, 12, 20), OptionType.CALL, Decimal("150"))
    assert symbol == "AAPL241220C00150000"
    assert parse_occ_symbol(symbol).strike == Decimal("150")


def test_format_round_trips_a_fractional_strike() -> None:
    symbol = format_occ_symbol("IWM", date(2026, 3, 20), OptionType.PUT, Decimal("280.5"))
    assert symbol == "IWM260320P00280500"


def test_format_refuses_a_strike_it_cannot_represent() -> None:
    """The OCC field is thousandths. A tenth of a cent has nowhere to go."""
    with pytest.raises(ValueError):
        format_occ_symbol("AAPL", date(2024, 12, 20), OptionType.CALL, Decimal("150.0001"))


def test_parsing_is_case_insensitive_on_the_right_side() -> None:
    parsed = parse_occ_symbol("aapl241220c00150000")
    assert parsed.root == "AAPL"
    assert parsed.option_type is OptionType.CALL
    assert parsed.symbol == "AAPL241220C00150000"


def test_option_type_values_match_alpacas_wire_strings() -> None:
    """``type`` on /v2/options/contracts is literally 'call' or 'put'."""
    assert OptionType.CALL.value == "call"
    assert OptionType.PUT.value == "put"
    assert OptionType("call") is OptionType.CALL
