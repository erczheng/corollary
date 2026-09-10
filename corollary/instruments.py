"""OCC option symbols — parsing, formatting, and adjusted-root detection.

Vendor-neutral on purpose. OCC symbology is an industry standard, not an
Alpaca convention, so this sits outside ``data/providers/`` and the next
provider gets it for free.

The format is underlying root + ``YYMMDD`` + ``C``/``P`` + an 8-digit strike
in thousandths. ``AAPL241220C00150000`` is the AAPL $150 call expiring
20 Dec 2024.

**Why this module exists at all rather than being three lines inline.** Two
things downstream depend on getting it exactly right:

* The strike is a **money** value and must arrive as an exact ``Decimal``.
  ``int(digits) / 1000`` in floats gives ``280.49999999999994`` for an
  IWM 280.5 put, and a payoff curve struck a fraction of a cent off is a
  payoff curve for a contract that does not exist.
* The root is how an **adjusted contract** is detected. After a split or a
  special dividend, OCC issues a modified root — ``AAPL1`` — and the
  deliverable is no longer 100 shares. Sizing that assumes a 100 multiplier
  computes max loss wrong on those, which is precisely the failure CLAUDE.md
  rule 4 exists to prevent. The authoritative comparison is
  ``root_symbol != underlying_symbol`` from ``/v2/options/contracts``; this
  module supplies the same answer from the symbol alone, which is what the
  chain endpoint leaves you with — its snapshots are keyed by symbol and
  carry no contract metadata at all.
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

__all__ = [
    "OCC_STRIKE_SCALE",
    "OccSymbol",
    "OptionType",
    "format_occ_symbol",
    "is_adjusted_root",
    "parse_occ_symbol",
]

#: The strike field is an integer number of thousandths of a dollar.
OCC_STRIKE_SCALE = Decimal(1000)

#: Root, then the fixed 15-character tail. The root is 1-6 characters
#: beginning with a letter, which admits the ``AAPL1`` adjusted form and
#: rejects a symbol that is all digits.
_OCC_RE = re.compile(
    r"^(?P<root>[A-Z][A-Z0-9]{0,5})"
    r"(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
    r"(?P<right>[CP])"
    r"(?P<strike>\d{8})$"
)

#: OCC's two-digit year. There is no 20th-century option chain to worry
#: about, and Alpaca's options history starts February 2024 regardless.
_CENTURY = 2000


class OptionType(StrEnum):
    """Call or put.

    The values are Alpaca's wire strings (``"call"`` / ``"put"``) so a
    response field constructs the enum directly. That is a convenience, not a
    coupling — every option vendor spells these the same way.
    """

    CALL = "call"
    PUT = "put"


#: The single character in the symbol, spelled out. A mapping rather than a
#: conditional so the two directions (here and ``format_occ_symbol``) read as
#: the same fact stated twice.
_RIGHT_TO_TYPE = {"C": OptionType.CALL, "P": OptionType.PUT}


@dataclass(frozen=True, slots=True)
class OccSymbol:
    """The five facts an OCC symbol encodes.

    ``symbol`` is the canonical upper-cased spelling, so a value parsed from a
    lower-cased input can be used as a dictionary key against a response.
    """

    symbol: str
    root: str
    expiration: date
    option_type: OptionType
    strike: Decimal


def parse_occ_symbol(symbol: str) -> OccSymbol:
    """Parse an OCC symbol, or raise ``ValueError``.

    Raises rather than returning ``None``. A symbol this app cannot read is a
    contract it cannot size, and the whole-strategy-rejection principle from
    the indicator whitelist applies for the same reason: partial evaluation of
    something you do not understand is worse than stopping.
    """
    candidate = symbol.strip().upper()
    match = _OCC_RE.match(candidate)
    if match is None:
        raise ValueError(
            f"{symbol!r} is not an OCC option symbol. Expected a root of 1-6 "
            "characters, then YYMMDD, then C or P, then an 8-digit strike in "
            "thousandths — e.g. AAPL241220C00150000."
        )

    year = _CENTURY + int(match["yy"])
    try:
        expiration = date(year, int(match["mm"]), int(match["dd"]))
    except ValueError as exc:
        raise ValueError(
            f"{symbol!r} carries an impossible expiration date: {exc}"
        ) from exc

    return OccSymbol(
        symbol=candidate,
        root=match["root"],
        expiration=expiration,
        option_type=_RIGHT_TO_TYPE[match["right"]],
        strike=Decimal(int(match["strike"])) / OCC_STRIKE_SCALE,
    )


def format_occ_symbol(
    root: str, expiration: date, option_type: OptionType, strike: Decimal
) -> str:
    """Build an OCC symbol. The inverse of :func:`parse_occ_symbol`.

    Refuses a strike finer than a thousandth of a dollar rather than rounding
    it. Rounding here would silently name a *different* contract, and the
    caller would get a clean 404 from the vendor with no clue why.
    """
    scaled = strike * OCC_STRIKE_SCALE
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"strike {strike} cannot be written as an OCC symbol: the field "
            "holds thousandths of a dollar and this needs more precision"
        )
    thousandths = int(scaled)
    if not 0 <= thousandths <= 99_999_999:
        raise ValueError(f"strike {strike} is outside the 8-digit OCC field")
    return (
        f"{root.upper()}"
        f"{expiration:%y%m%d}"
        f"{'C' if option_type is OptionType.CALL else 'P'}"
        f"{thousandths:08d}"
    )


def is_adjusted_root(root: str, underlying_symbol: str) -> bool:
    """True when the contract's deliverable is not the standard 100 shares.

    The test is inequality, not a prefix check. ``AAPL1`` differs from
    ``AAPL`` and is adjusted; a prefix test would either call every ``AAPL``
    contract adjusted or, run the other way, quietly accept ``BRK`` as an
    unadjusted ``B``.

    Case is normalised because the two strings reach this function from
    different places — one from an OCC symbol, one from a JSON field — and
    they are not guaranteed to agree on it.
    """
    return root.upper() != underlying_symbol.upper()
