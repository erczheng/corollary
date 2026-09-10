"""The recorder's serialiser. The recorder itself is never run by the suite.

``dumps_exact`` is the half of the fixture pipeline that decides whether the
files on disk carry Alpaca's digits or Python's ``repr`` of a double. It has a
test because the answer is invisible in the output: a fixture written the
lossy way looks identical for every value that happens to round-trip, which is
almost all of them.
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from record_alpaca import dumps_exact  # noqa: E402

#: Twenty-three significant figures. A double holds about seventeen, so this
#: is a value that cannot survive ``json.loads`` without ``parse_float``.
BEYOND_A_DOUBLE = "205.67753612345678901234"


def test_a_value_no_double_can_hold_survives_the_round_trip() -> None:
    payload = json.loads(
        '{"bars": {"NVDA": [{"vw": ' + BEYOND_A_DOUBLE + "}]}}",
        parse_float=Decimal,
    )
    text = dumps_exact(payload)
    assert BEYOND_A_DOUBLE in text
    assert json.loads(text, parse_float=Decimal) == payload


def test_the_lossy_path_is_what_this_replaces() -> None:
    """Proof the test above is not vacuous.

    ``json.dumps(json.loads(text))`` -- what the recorder used to do -- loses
    the tail of the value silently and writes something that still looks like
    a price.
    """
    lossy = json.dumps(json.loads('{"vw": ' + BEYOND_A_DOUBLE + "}"))
    assert BEYOND_A_DOUBLE not in lossy


@pytest.mark.parametrize(
    "literal",
    [
        "-1.5e-9",  # Decimal renders this as -1.5E-9, which is valid JSON
        "0",
        "0.0",
        "1e2",
        "-0.000001",
        "123456789012345678901234567890",
    ],
)
def test_awkward_numbers_come_back_as_numbers_not_strings(literal: str) -> None:
    """The sentinel must be unwrapped, not left quoted.

    ``json.dumps`` writes a control character as a ``\\u0001`` escape rather
    than as itself, so a pattern matching the raw byte silently matches
    nothing and every number in the fixture becomes a string. Nothing would
    error; the fixtures would just stop being JSON numbers.
    """
    payload = json.loads('{"v": ' + literal + "}", parse_float=Decimal)
    reparsed = json.loads(dumps_exact(payload), parse_float=Decimal)
    assert not isinstance(reparsed["v"], str)
    assert reparsed == payload


def test_a_string_that_looks_like_a_number_stays_a_string() -> None:
    """The trading API sends money as strings; those must not be unquoted."""
    payload = {"avg_entry_price": "8.21", "qty": "2"}
    assert json.loads(dumps_exact(payload)) == payload


def test_an_unserialisable_value_raises_rather_than_being_stringified() -> None:
    with pytest.raises(TypeError):
        dumps_exact({"when": object()})
