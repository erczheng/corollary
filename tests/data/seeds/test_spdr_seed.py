"""The SPDR holdings seed loader -- spec decision 6.

The seed is one CSV the owner rebuilds by hand each quarter. Everything below
runs on synthetic seeds written inline: none of these weights is State
Street's, and none of them is meant to look like it.
"""

from __future__ import annotations

import logging
import random
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from corollary.data import seeds
from corollary.data.seeds import (
    SEED_PATH,
    SPDR_SECTORS,
    SeedError,
    SpdrHolding,
    SpdrSeed,
    format_spdr_seed,
    load_spdr_seed,
    normalize_symbol,
    parse_spdr_seed,
)

#: Six synthetic holdings per fund, weights as text so exactness is visible.
#: XLP carries two ties: COST/WMT at the top and CL/MO across the fifth place.
HOLDINGS: dict[str, list[tuple[str, str]]] = {
    "XLB": [("LIN", "20"), ("SHW", "10"), ("APD", "8"), ("ECL", "7"), ("NEM", "6"), ("FCX", "5")],
    "XLC": [("META", "22"), ("GOOGL", "12"), ("GOOG", "10"), ("NFLX", "6"), ("TMUS", "5"), ("DIS", "4")],
    "XLE": [("XOM", "23"), ("CVX", "17"), ("COP", "8"), ("EOG", "5"), ("WMB", "4.5"), ("SLB", "4")],
    "XLF": [("BRK.B", "12"), ("JPM", "10"), ("V", "8"), ("MA", "7"), ("BAC", "4"), ("WFC", "3.5")],
    "XLI": [("GE", "6"), ("CAT", "5.5"), ("RTX", "4.5"), ("UBER", "4"), ("HON", "3.5"), ("UNP", "3")],
    "XLK": [("NVDA", "15"), ("MSFT", "14.123456789012345678"), ("AAPL", "13"), ("AVGO", "6"), ("ORCL", "3"), ("PLTR", "2.5")],
    "XLP": [("COST", "10"), ("WMT", "10"), ("PG", "9"), ("KO", "6"), ("MO", "4"), ("CL", "4")],
    "XLRE": [("PLD", "9"), ("WELL", "8"), ("AMT", "8.5"), ("EQIX", "7"), ("SPG", "5"), ("O", "4.5")],
    "XLU": [("NEE", "12"), ("SO", "8"), ("DUK", "7.5"), ("CEG", "7"), ("VST", "5"), ("AEP", "4.5")],
    "XLV": [("LLY", "12"), ("JNJ", "8"), ("ABBV", "7"), ("UNH", "5"), ("ABT", "4.5"), ("MRK", "4")],
    "XLY": [("AMZN", "22"), ("TSLA", "16"), ("HD", "7"), ("MCD", "4"), ("BKNG", "4"), ("TJX", "3.5")],
}

AS_OF = date(2026, 9, 22)


def _lines(holdings: dict[str, list[tuple[str, str]]] = HOLDINGS, as_of: str = "2026-09-22") -> list[str]:
    out = [f"as_of,{as_of}", "etf,sector,symbol,weight"]
    for etf, rows in holdings.items():
        out.extend(f"{etf},{SPDR_SECTORS[etf]},{symbol},{weight}" for symbol, weight in rows)
    return out


def _text(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def _write(tmp_path: Path, text: str, name: str = "spdr_holdings.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="")
    return path


@pytest.fixture()
def seed() -> SpdrSeed:
    return parse_spdr_seed(_text(_lines()))


# -- the fund list -----------------------------------------------------------


def test_the_eleven_funds_map_to_the_frontends_sector_labels() -> None:
    """The labels are ``web/src/lib/mockData.ts``'s consensus sectors, not GICS's
    own spellings: ``Technology``, not ``Information Technology``."""
    assert dict(SPDR_SECTORS) == {
        "XLB": "Materials",
        "XLC": "Communication Services",
        "XLE": "Energy",
        "XLF": "Financials",
        "XLI": "Industrials",
        "XLK": "Technology",
        "XLP": "Consumer Staples",
        "XLRE": "Real Estate",
        "XLU": "Utilities",
        "XLV": "Health Care",
        "XLY": "Consumer Discretionary",
    }


def test_the_default_path_is_the_package_file() -> None:
    assert SEED_PATH.name == "spdr_holdings.csv"
    assert SEED_PATH.parent == Path(seeds.__file__).resolve().parent


def test_the_committed_seed_is_absent_or_valid() -> None:
    """Until the owner builds it the seed does not exist, and that is ``None``.
    Once it does, it must load, and a real one is the S&P 500: a count far
    under ~500 means a parse stopped early, not a small index."""
    loaded = load_spdr_seed()
    if loaded is not None:
        assert len(loaded.symbols()) >= 450
        assert set(loaded.leaders()) == set(SPDR_SECTORS)


# -- absent and malformed ----------------------------------------------------


def test_an_absent_seed_is_none_and_is_logged_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "nope.csv"
    with caplog.at_level(logging.WARNING, logger="corollary.data.seeds"):
        assert load_spdr_seed(path) is None
        assert load_spdr_seed(path) is None
    records = [r for r in caplog.records if getattr(r, "event", None) == "spdr_seed_missing"]
    assert len(records) == 1
    assert getattr(records[0], "path") == str(path)


def _replace(lines: list[str], index: int, new: str | None) -> list[str]:
    out = list(lines)
    if new is None:
        del out[index]
    else:
        out[index] = new
    return out


MALFORMED: dict[str, list[str]] = {
    "empty file": [],
    "no as-of row": _lines()[1:],
    "as-of not a date": _replace(_lines(), 0, "as_of,22-Sep-2026"),
    "as-of label wrong": _replace(_lines(), 0, "asof,2026-09-22"),
    "as-of row has extra fields": _replace(_lines(), 0, "as_of,2026-09-22,x"),
    "column header wrong": _replace(_lines(), 1, "etf,sector,ticker,weight"),
    "a row with three fields": _replace(_lines(), 2, "XLB,Materials,LIN"),
    "a row with five fields": _replace(_lines(), 2, "XLB,Materials,LIN,20,x"),
    "unknown fund": _replace(_lines(), 2, "SPY,Materials,LIN,20"),
    "sector disagrees with fund": _replace(_lines(), 2, "XLB,Energy,LIN,20"),
    "lower-case symbol": _replace(_lines(), 2, "XLB,Materials,lin,20"),
    "adjusted-root style symbol": _replace(_lines(), 2, "XLB,Materials,LIN1,20"),
    "weight not a number": _replace(_lines(), 2, "XLB,Materials,LIN,twenty"),
    "weight infinite": _replace(_lines(), 2, "XLB,Materials,LIN,Infinity"),
    "weight NaN": _replace(_lines(), 2, "XLB,Materials,LIN,NaN"),
    "weight zero": _replace(_lines(), 2, "XLB,Materials,LIN,0"),
    "weight negative": _replace(_lines(), 2, "XLB,Materials,LIN,-1"),
    "same symbol twice in one fund": _replace(_lines(), 3, "XLB,Materials,LIN,10"),
    "a fund missing": [line for line in _lines() if not line.startswith("XLU,")],
    "header only": _lines()[:2],
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_a_malformed_seed_raises(case: str, tmp_path: Path) -> None:
    path = _write(tmp_path, _text(MALFORMED[case]) if MALFORMED[case] else "")
    with pytest.raises(SeedError) as excinfo:
        load_spdr_seed(path)
    assert str(path) in str(excinfo.value)


def test_a_seed_error_is_a_value_error() -> None:
    assert issubclass(SeedError, ValueError)


# -- round trip and exactness ------------------------------------------------


def test_a_valid_seed_round_trips(tmp_path: Path) -> None:
    text = _text(_lines())
    loaded = load_spdr_seed(_write(tmp_path, text))
    assert loaded is not None
    assert loaded.as_of == AS_OF
    assert format_spdr_seed(loaded) == text
    assert parse_spdr_seed(format_spdr_seed(loaded)) == loaded


def test_weights_are_exact_decimals(seed: SpdrSeed) -> None:
    msft = next(row for row in seed.rows if row.symbol == "MSFT")
    assert isinstance(msft.weight, Decimal)
    assert msft.weight == Decimal("14.123456789012345678")
    assert "14.123456789012345678" in format_spdr_seed(seed)


def test_a_seed_with_a_byte_order_mark_loads(tmp_path: Path) -> None:
    """Notepad and Excel's "CSV UTF-8" write a BOM. Read as plain UTF-8 it would
    glue itself to ``as_of`` and fail the first row."""
    path = tmp_path / "spdr_holdings.csv"
    path.write_bytes(b"\xef\xbb\xbf" + _text(_lines()).encode("utf-8"))
    loaded = load_spdr_seed(path)
    assert loaded is not None and loaded.as_of == AS_OF


def test_a_symbol_with_a_trailing_newline_is_refused(tmp_path: Path) -> None:
    """``$`` matches just before a trailing newline, so ``re.match`` with ``^...$``
    accepted ``"LIN\\n"``. A quoted CSV field can carry one."""
    path = _write(tmp_path, _text(_replace(_lines(), 2, 'XLB,Materials,"LIN\n",20')))
    with pytest.raises(SeedError, match="equity symbol"):
        load_spdr_seed(path)
    rows = tuple(
        SpdrHolding(etf, SPDR_SECTORS[etf], symbol + ("\n" if symbol == "LIN" else ""), Decimal(weight))
        for etf, held in HOLDINGS.items()
        for symbol, weight in held
    )
    with pytest.raises(SeedError, match="equity symbol"):
        SpdrSeed(as_of=AS_OF, rows=rows)


def test_leaders_sort_by_exact_weight_beyond_28_digits() -> None:
    """Negating a Decimal rounds to the context's 28 digits, so ``-w`` made these
    two weights tie and fall back to symbol order. The heavier one leads."""
    holdings = {etf: list(rows) for etf, rows in HOLDINGS.items()}
    holdings["XLB"] = [("LIN", "25.0000000000000000000000000001"), ("SHW", "25.0000000000000000000000000002")] + holdings["XLB"][2:]
    exact = parse_spdr_seed(_text(_lines(holdings)))
    assert exact.leaders()["XLB"][:2] == ("SHW", "LIN")


def test_a_crlf_seed_loads(tmp_path: Path) -> None:
    """Excel and Windows editors write CRLF; the file must not care."""
    loaded = load_spdr_seed(_write(tmp_path, "\r\n".join(_lines()) + "\r\n"))
    assert loaded is not None and loaded.as_of == AS_OF


def test_the_seed_is_frozen(seed: SpdrSeed) -> None:
    with pytest.raises(AttributeError):
        seed.as_of = date(2020, 1, 1)  # type: ignore[misc]


def test_a_seed_cannot_be_constructed_invalid() -> None:
    """The invariants live on the type, so the build script cannot write a seed
    the loader would refuse."""
    rows = tuple(SpdrHolding("XLB", "Materials", "LIN", Decimal("1")) for _ in range(1))
    with pytest.raises(SeedError, match="XLC"):
        SpdrSeed(as_of=AS_OF, rows=rows)


# -- lookups -----------------------------------------------------------------


def test_leaders_are_the_top_five_by_weight(seed: SpdrSeed) -> None:
    leaders = seed.leaders()
    assert list(leaders) == sorted(SPDR_SECTORS)
    assert leaders["XLK"] == ("NVDA", "MSFT", "AAPL", "AVGO", "ORCL")
    assert leaders["XLRE"] == ("PLD", "AMT", "WELL", "EQIX", "SPG")
    assert leaders["XLF"][0] == "BRK.B"
    assert sum(len(v) for v in leaders.values()) == 55


def test_leader_ties_break_by_symbol(seed: SpdrSeed) -> None:
    # COST/WMT tie at 10, CL/MO tie at 4 across the cut: alphabetical wins.
    assert seed.leaders()["XLP"] == ("COST", "WMT", "PG", "KO", "CL")


def test_leaders_do_not_depend_on_row_order() -> None:
    lines = _lines()
    body = lines[2:]
    for seed_value in range(5):
        shuffled = list(body)
        random.Random(seed_value).shuffle(shuffled)
        other = parse_spdr_seed(_text(lines[:2] + shuffled))
        assert other.leaders() == parse_spdr_seed(_text(lines)).leaders()
        assert other.leaders(per_fund=3) == parse_spdr_seed(_text(lines)).leaders(per_fund=3)


def test_leaders_per_fund_is_honoured_and_bounded(seed: SpdrSeed) -> None:
    assert seed.leaders(per_fund=1)["XLK"] == ("NVDA",)
    assert len(seed.leaders(per_fund=50)["XLK"]) == 6
    for bad in (0, -1):
        with pytest.raises(ValueError):
            seed.leaders(per_fund=bad)


def test_sector_of(seed: SpdrSeed) -> None:
    assert seed.sector_of("NVDA") == "Technology"
    assert seed.sector_of("BRK.B") == "Financials"
    assert seed.sector_of("ZZZZ") is None
    assert seed.sector_of("MARKET") is None


def test_sector_of_normalises_the_query(seed: SpdrSeed) -> None:
    assert seed.sector_of(" nvda ") == "Technology"
    assert seed.sector_of("BRK/B") == "Financials"
    assert seed.sector_of("BRK-B") == "Financials"


def test_symbols_is_the_universe(seed: SpdrSeed) -> None:
    symbols = seed.symbols()
    assert isinstance(symbols, frozenset)
    assert len(symbols) == 66
    assert {"BRK.B", "GOOG", "GOOGL"} <= symbols


def test_a_symbol_in_two_funds_takes_the_sector_where_it_weighs_more() -> None:
    """Reclassifications leave a name in two funds for a rebalance. That must not
    crash: the name counts once in the universe, can lead in both funds, and
    files under the fund where its weight is larger."""
    holdings = {etf: list(rows) for etf, rows in HOLDINGS.items()}
    holdings["XLY"].append(("NFLX", "30"))  # already in XLC at 6
    two = parse_spdr_seed(_text(_lines(holdings)))
    assert two.sector_of("NFLX") == "Consumer Discretionary"
    assert "NFLX" in two.leaders()["XLY"] and "NFLX" in two.leaders()["XLC"]
    assert len(two.symbols()) == 66


def test_a_symbol_in_two_funds_at_equal_weight_takes_the_first_fund() -> None:
    holdings = {etf: list(rows) for etf, rows in HOLDINGS.items()}
    holdings["XLY"].append(("NFLX", "6"))
    two = parse_spdr_seed(_text(_lines(holdings)))
    assert two.sector_of("NFLX") == "Communication Services"  # XLC sorts before XLY


def test_age_and_staleness(seed: SpdrSeed) -> None:
    assert seed.age_days(AS_OF) == 0
    assert seed.age_days(date(2026, 12, 31)) == 100
    assert seed.is_stale(date(2026, 12, 31)) is False  # "more than 100 days"
    assert seed.is_stale(date(2027, 1, 1)) is True
    assert seed.age_days(date(2026, 9, 21)) == -1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BRK.B", "BRK.B"),
        ("BRK/B", "BRK.B"),
        ("BRK-B", "BRK.B"),
        ("BRK B", "BRK.B"),
        (" brk.b ", "BRK.B"),
        ("BF.B", "BF.B"),
        ("AAPL", "AAPL"),
        ("-", "-"),
        ("", ""),
        ("CASH_USD", "CASH_USD"),
    ],
)
def test_normalize_symbol(raw: str, expected: str) -> None:
    assert normalize_symbol(raw) == expected
