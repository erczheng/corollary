"""Build ``corollary/data/seeds/spdr_holdings.csv`` from State Street's holdings files.

Spec decision 6. Run **by hand**, quarterly, against the eleven Select Sector
SPDR daily-holdings files the owner downloaded. This script reads local files
and writes one; it opens no network connection, loads no ``.env`` and holds no
key. A scheduled download of State Street's site was rejected in the spec
because its terms were never checked.

Usage::

    uv run python scripts/build_spdr_seed.py --input-dir <folder> [--overwrite]

``<folder>`` holds one file per fund -- XLB, XLC, XLE, XLF, XLI, XLK, XLP,
XLRE, XLU, XLV, XLY -- each named so the fund ticker is a separate word of the
file name, as State Street's own are (``holdings-daily-us-en-xlk.xlsx``). Each
may be the ``.xlsx`` as downloaded or a ``.csv`` saved from it in Excel.

What it refuses, rather than guesses at (each names the file):

* a fund with no file, or with two;
* a file whose ``Ticker Symbol:`` line names a different fund;
* a file with no ``As of`` date above the table, or files that disagree on it
  -- all eleven must be downloaded the same day;
* no header row carrying ``Ticker`` and ``Weight`` columns;
* a weight that is not a number, a negative one, one over 100, a symbol twice
  in one fund, holdings that resume after a blank row, fewer than five
  equities in a fund;
* a fund whose equity weights do not sum to between 90 and 110 -- a read that
  stopped early, or a Weight column that is not percent of fund. A sum near 1
  means the column holds fractions (a percent-formatted cell stores ``0.1412``
  for 14.12%), and that gets its own message: the script never rescales, since
  guessing the unit wrong would file a stock under the wrong sector wherever
  two funds hold it (``SpdrSeed.sector_of`` compares weights across funds);
* an existing seed, unless ``--overwrite`` is passed.

It never writes a partial seed: the whole seed is built and validated by
``corollary.data.seeds`` in memory first, then written to a temporary file and
moved into place.

**The layout it expects was written from memory and is unverified against a
real download** -- a few ``Label:`` lines, a table, a blank row, footer
disclaimers. Every assumption fails loudly with the file name rather than
producing a quiet wrong answer, and the summary printed on success lists every
row dropped as not-an-equity so the owner can check it before committing.

What it drops, and prints: rows with no ticker (``-``), cash and futures lines
(by name), zero-weight rows, and anything whose ticker is not an equity symbol
of the form ``AAPL`` or ``BRK.B``. Class shares are normalised to the dot form
(``BRK/B`` -> ``BRK.B``), which is how Alpaca, Finnhub and this codebase write
them. Weights are the exact decimal text in the file -- never a float. That is
exact to the *file*, not to the fund's true weight: an ``.xlsx`` stores the
full value while a CSV saved from it in Excel writes what the cell displays, so
the same download can give different precision in the two forms.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import posixpath
import re
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, Sequence
from xml.etree import ElementTree

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from corollary.data.seeds import (  # noqa: E402 -- after the sys.path line above
    EQUITY_SYMBOL_RE,
    LEADERS_PER_FUND,
    SEED_PATH,
    SPDR_FUNDS,
    SPDR_SECTORS,
    SeedError,
    SpdrHolding,
    SpdrSeed,
    format_spdr_seed,
    normalize_symbol,
    parse_spdr_seed,
)

#: Input files the script will read.
SUFFIXES: Final = (".xlsx", ".csv")

#: A fund with fewer equities than this was not read as expected: the smallest
#: Select Sector SPDR holds a couple of dozen names, and the consensus roll-up
#: needs five leaders from each.
MIN_HOLDINGS: Final = LEADERS_PER_FUND

#: No holding can be more than the whole fund.
MAX_WEIGHT: Final = Decimal("100")

#: A fund's equity weights, summed, must land inside this band, inclusive.
#: Cash and futures lines account for the gap below 100; a sum well outside it
#: means the table was read short or the column is not percent of fund.
WEIGHT_SUM_BAND: Final = (Decimal("90"), Decimal("110"))

#: The same band divided by 100: a sum inside it means the Weight column holds
#: fractions of 1, which is how an ``.xlsx`` stores a percent-formatted cell.
FRACTION_SUM_BAND: Final = (Decimal("0.9"), Decimal("1.1"))

#: Tickers that mean "no ticker" in State Street's sheets.
_NO_TICKER: Final = frozenset({"", "-", "--"})

#: Names of lines that are not equities: cash, futures, the sweep fund.
_NON_EQUITY_NAME: Final = re.compile(r"\b(CASH|US DOLLAR|FUTURES?|FUT|MONEY MARKET)\b", re.IGNORECASE)

#: The ``As of`` date's spellings. State Street writes the first.
_DATE_FORMATS: Final = ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%d %b %Y")

_AS_OF_RE: Final = re.compile(r"\bas\s+of\b[:\s]*(.*)$", re.IGNORECASE)


class BuildError(Exception):
    """A refusal. The message names the file and says what was expected."""


@dataclass
class FundFile:
    """One fund's file, read."""

    etf: str
    path: Path
    as_of: date
    holdings: list[SpdrHolding] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


# -- finding the eleven files --------------------------------------------------


def find_inputs(input_dir: Path) -> dict[str, Path]:
    """One file per fund, matched by the fund ticker as a word of the file name."""
    if not input_dir.is_dir():
        raise BuildError(f"input directory {input_dir} does not exist")
    candidates = sorted(
        p
        for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUFFIXES and not p.name.startswith(("~$", "."))
    )
    matches: dict[str, list[Path]] = {etf: [] for etf in SPDR_FUNDS}
    for path in candidates:
        words = set(re.split(r"[^a-z0-9]+", path.stem.lower()))
        funds = [etf for etf in SPDR_FUNDS if etf.lower() in words]
        if len(funds) > 1:
            raise BuildError(f"{path.name}: the name matches more than one fund {funds}")
        if funds:
            matches[funds[0]].append(path)
    missing = [etf for etf, paths in matches.items() if not paths]
    doubled = {etf: paths for etf, paths in matches.items() if len(paths) > 1}
    problems: list[str] = []
    if missing:
        problems.append(
            f"no holdings file for {', '.join(missing)} in {input_dir} -- expected an .xlsx or .csv "
            f"whose name contains the fund ticker as a word, e.g. holdings-daily-us-en-{missing[0].lower()}.xlsx"
        )
    for etf, paths in doubled.items():
        problems.append(f"more than one file for {etf}: {', '.join(p.name for p in paths)} -- keep exactly one")
    if problems:
        raise BuildError("; ".join(problems))
    return {etf: paths[0] for etf, paths in matches.items()}


# -- reading a sheet into rows of text -----------------------------------------


def read_rows(path: Path) -> list[list[str]]:
    """The sheet as rows of cell text, blank rows kept as empty lists."""
    if path.suffix.lower() == ".xlsx":
        return read_xlsx_rows(path)
    return read_csv_rows(path)


def read_csv_rows(path: Path) -> list[list[str]]:
    """A CSV saved from the sheet. UTF-8 (Excel's "CSV UTF-8"), else cp1252
    (Excel's plain "CSV" on Windows)."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise BuildError(f"{path.name}: neither UTF-8 nor cp1252 text ({exc})") from None
    return [list(row) for row in csv.reader(io.StringIO(text, newline=""))]


def _local(tag: str) -> str:
    """An XML tag without its namespace, so transitional and strict OOXML read alike."""
    return tag.rsplit("}", 1)[-1]


def _xml(zf: zipfile.ZipFile, member: str, path: Path) -> ElementTree.Element:
    try:
        data = zf.read(member)
    except KeyError:
        raise BuildError(f"{path.name}: the workbook has no part {member!r}") from None
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise BuildError(f"{path.name}: part {member!r} is not well-formed XML ({exc})") from None


def _relationships(zf: zipfile.ZipFile, part: str, path: Path) -> list[tuple[str, str, str]]:
    """``(id, type, resolved target)`` for every relationship of ``part``."""
    folder, name = posixpath.split(part)
    rels_part = posixpath.join(folder, "_rels", f"{name}.rels")
    out: list[tuple[str, str, str]] = []
    for element in _xml(zf, rels_part, path):
        if _local(element.tag) != "Relationship":
            continue
        target = element.get("Target", "")
        resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
        out.append((element.get("Id", ""), element.get("Type", ""), resolved))
    return out


def _attr(element: ElementTree.Element, local_name: str) -> str | None:
    for key, value in element.attrib.items():
        if _local(key) == local_name:
            return value
    return None


def _column_index(ref: str) -> int:
    """``"C7"`` -> 2."""
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def read_xlsx_rows(path: Path) -> list[list[str]]:
    """The first worksheet of an ``.xlsx``, read with the standard library.

    Not ``openpyxl``: it returns numeric cells as floats, and a weight is kept
    here as the exact decimal text the workbook stores. The format is a zip of
    XML parts -- the workbook names its first sheet, the sheet's cells are
    numbers written as text, shared-string indices, or inline strings.
    """
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise BuildError(
            f"{path.name}: not an .xlsx workbook (not a zip archive) -- if it is an old .xls or "
            "anything else, open it in Excel and save it as CSV"
        ) from None
    with zf:
        office = [t for _, kind, t in _relationships(zf, "", path) if kind.endswith("/officeDocument")]
        if not office:
            raise BuildError(f"{path.name}: the package names no workbook part")
        workbook_part = office[0]
        workbook = _xml(zf, workbook_part, path)
        sheets = [el for el in workbook.iter() if _local(el.tag) == "sheet"]
        if not sheets:
            raise BuildError(f"{path.name}: the workbook has no sheets")
        first_id = _attr(sheets[0], "id")
        rels = _relationships(zf, workbook_part, path)
        sheet_parts = [t for rid, _, t in rels if rid == first_id]
        if not sheet_parts:
            raise BuildError(f"{path.name}: the first sheet's part could not be resolved")
        shared: list[str] = []
        for _, kind, target in rels:
            if kind.endswith("/sharedStrings"):
                shared = _shared_strings(_xml(zf, target, path))
        sheet = _xml(zf, sheet_parts[0], path)

    cells_by_row: dict[int, dict[int, str]] = {}
    row_number = 0
    for row_el in sheet.iter():
        if _local(row_el.tag) != "row":
            continue
        row_number = int(row_el.get("r", row_number + 1))
        cells: dict[int, str] = {}
        column = -1
        for cell in row_el:
            if _local(cell.tag) != "c":
                continue
            ref = cell.get("r")
            column = _column_index(ref) if ref else column + 1
            cells[column] = _cell_text(cell, shared, path)
        cells_by_row[row_number] = cells
    last = max(cells_by_row, default=0)
    rows: list[list[str]] = []
    for number in range(1, last + 1):
        cells = cells_by_row.get(number, {})
        width = max(cells, default=-1) + 1
        rows.append([cells.get(i, "") for i in range(width)])
    return rows


def _shared_strings(root: ElementTree.Element) -> list[str]:
    """Each ``<si>``'s text: a plain ``<t>``, or the ``<t>`` of each rich-text run.
    Phonetic runs (``<rPh>``) are not the cell's text and are skipped."""
    out: list[str] = []
    for si in root:
        if _local(si.tag) != "si":
            continue
        parts: list[str] = []
        for child in si:
            if _local(child.tag) == "t":
                parts.append(child.text or "")
            elif _local(child.tag) == "r":
                parts.extend(t.text or "" for t in child if _local(t.tag) == "t")
        out.append("".join(parts))
    return out


def _cell_text(cell: ElementTree.Element, shared: list[str], path: Path) -> str:
    kind = cell.get("t", "n")
    value = next((child.text or "" for child in cell if _local(child.tag) == "v"), "")
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            raise BuildError(f"{path.name}: cell {cell.get('r')} points at no shared string") from None
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter() if _local(t.tag) == "t")
    if kind == "b":
        return "TRUE" if value == "1" else "FALSE"
    return value


# -- reading one fund's holdings ------------------------------------------------


def _cell(row: Sequence[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def _filled(row: Sequence[str]) -> list[str]:
    return [c.strip() for c in row if c.strip()]


def _parse_date(text: str) -> date | None:
    candidates = [text.strip()]
    if text.split():
        candidates.append(text.split()[0])
    for candidate in candidates:
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def _find_header(rows: list[list[str]]) -> tuple[int, int, int, int | None]:
    """``(row index, ticker column, weight column, name column or None)``, or -1s."""
    for index, row in enumerate(rows):
        labels = [c.strip().lower() for c in row]
        tickers = [i for i, label in enumerate(labels) if label == "ticker"]
        weights = [i for i, label in enumerate(labels) if re.match(r"^weight\b", label)]
        if tickers and weights:
            names = [i for i, label in enumerate(labels) if label == "name"]
            return index, tickers[0], weights[0], (names[0] if names else None)
    return -1, -1, -1, None


def parse_fund(etf: str, path: Path, rows: list[list[str]]) -> FundFile:
    """One fund's equity holdings. Raises :class:`BuildError` naming ``path``."""
    name = path.name
    header, ticker_col, weight_col, name_col = _find_header(rows)
    if header < 0:
        raise BuildError(f"{name}: no header row with 'Ticker' and 'Weight' columns")

    as_of_dates: set[date] = set()
    fund_ticker: str | None = None
    for row in rows[:header]:
        filled = _filled(row)
        if not filled:
            continue
        if filled[0].lower().rstrip(":").strip() == "ticker symbol":
            fund_ticker = filled[1].upper() if len(filled) > 1 else ""
        match = _AS_OF_RE.search(" ".join(filled))
        if match is not None:
            parsed = _parse_date(match.group(1))
            if parsed is None:
                raise BuildError(f"{name}: cannot read the as-of date from {match.group(0)!r}")
            as_of_dates.add(parsed)
    if fund_ticker is None:
        raise BuildError(f"{name}: no 'Ticker Symbol:' line above the table, so the fund cannot be confirmed as {etf}")
    if fund_ticker != etf:
        raise BuildError(f"{name}: the file's 'Ticker Symbol:' line says {fund_ticker!r}, but its name says {etf}")
    if not as_of_dates:
        raise BuildError(f"{name}: no 'As of' date above the table, so the as-of date cannot be established")
    if len(as_of_dates) > 1:
        raise BuildError(f"{name}: more than one as-of date above the table: {sorted(as_of_dates)}")
    fund = FundFile(etf=etf, path=path, as_of=as_of_dates.pop())

    seen: set[str] = set()
    end = len(rows)
    for index in range(header + 1, len(rows)):
        row = rows[index]
        where = f"{name}, row {index + 1}"
        raw_ticker, raw_weight = _cell(row, ticker_col), _cell(row, weight_col)
        if not raw_ticker and not raw_weight:
            end = index
            break
        label = _cell(row, name_col) or raw_ticker
        symbol = normalize_symbol(raw_ticker)
        if symbol in _NO_TICKER or _NON_EQUITY_NAME.search(label):
            fund.dropped.append(f"{label} [{raw_ticker or 'no ticker'}] {raw_weight} -- not an equity line")
            continue
        weight_text = raw_weight.replace(",", "").rstrip("%").strip()
        try:
            weight = Decimal(weight_text)
        except InvalidOperation:
            raise BuildError(f"{where}: weight {raw_weight!r} for {raw_ticker} is not a number") from None
        if not weight.is_finite():
            raise BuildError(f"{where}: weight {raw_weight!r} for {raw_ticker} is not finite")
        if weight > MAX_WEIGHT:
            raise BuildError(
                f"{where}: weight {raw_weight} for {raw_ticker} is over {MAX_WEIGHT}; "
                "no holding can be more than the whole fund, so the Weight column was not read as percent"
            )
        if not EQUITY_SYMBOL_RE.fullmatch(symbol):
            fund.dropped.append(f"{label} [{raw_ticker}] {raw_weight} -- not an equity symbol")
            continue
        if weight < 0:
            raise BuildError(f"{where}: negative weight {raw_weight} for equity {symbol}")
        if weight == 0:
            fund.dropped.append(f"{label} [{raw_ticker}] {raw_weight} -- zero weight")
            continue
        if symbol in seen:
            raise BuildError(f"{where}: {symbol} appears twice in {etf}")
        seen.add(symbol)
        fund.holdings.append(SpdrHolding(etf=etf, sector=SPDR_SECTORS[etf], symbol=symbol, weight=weight))

    for index in range(end + 1, len(rows)):
        row = rows[index]
        symbol = normalize_symbol(_cell(row, ticker_col))
        weight_text = _cell(row, weight_col).replace(",", "").rstrip("%").strip()
        if EQUITY_SYMBOL_RE.fullmatch(symbol) and _is_decimal(weight_text):
            raise BuildError(
                f"{name}, row {index + 1}: holdings ({symbol}) resume after the blank row at row {end + 1}; "
                "the table has a gap and would be read short"
            )

    if len(fund.holdings) < MIN_HOLDINGS:
        raise BuildError(
            f"{name}: only {len(fund.holdings)} equity holdings read; a Select Sector SPDR holds far more, "
            "so the table was not read as expected"
        )
    _check_weight_sum(fund)
    return fund


def _check_weight_sum(fund: FundFile) -> None:
    """Refuse a fund whose equity weights do not add up to roughly the whole fund.

    Never rescales. A fund left in fractions while the others are in percent
    would be 100x lighter, and ``SpdrSeed.sector_of`` compares a symbol's
    weight across the funds that hold it.
    """
    total = sum((h.weight for h in fund.holdings), Decimal(0))
    low, high = WEIGHT_SUM_BAND
    if low <= total <= high:
        return
    name = fund.path.name
    frac_low, frac_high = FRACTION_SUM_BAND
    if frac_low <= total <= frac_high:
        raise BuildError(
            f"{name}: {fund.etf} equity weights sum to {total}, not about 100 -- the Weight column holds "
            "fractions of 1, not percentages, which is what an .xlsx stores for a percent-formatted cell "
            "(0.1412 for 14.12%). This script does not rescale. Open the file in Excel and save it as CSV, "
            "which writes the percentages as displayed (14.12%), then rerun."
        )
    raise BuildError(
        f"{name}: {fund.etf} equity weights sum to {total}, outside {low}-{high}; the table was read short "
        "or the Weight column is not percent of fund"
    )


def _is_decimal(text: str) -> bool:
    try:
        return Decimal(text).is_finite()
    except InvalidOperation:
        return False


# -- the whole build ------------------------------------------------------------


def build_seed(funds: Sequence[FundFile]) -> SpdrSeed:
    """The seed from eleven read files: one as-of date, rows in fund order,
    heaviest first, ties by symbol -- so a refresh diffs as weight changes."""
    by_date: dict[date, list[str]] = defaultdict(list)
    for fund in funds:
        by_date[fund.as_of].append(fund.path.name)
    if len(by_date) > 1:
        groups = sorted(by_date.items(), key=lambda item: (-len(item[1]), item[0]))
        detail = "; ".join(f"{d.isoformat()}: {', '.join(sorted(names))}" for d, names in groups)
        raise BuildError(f"the files disagree on the as-of date -- download all eleven the same day. {detail}")
    as_of = next(iter(by_date))
    rows: list[SpdrHolding] = []
    for fund in sorted(funds, key=lambda f: SPDR_FUNDS.index(f.etf)):
        # copy_negate, not unary minus: ``-w`` rounds to the context's 28
        # digits and can turn two different weights into a tie.
        rows.extend(sorted(fund.holdings, key=lambda h: (h.weight.copy_negate(), h.symbol)))
    try:
        return SpdrSeed(as_of=as_of, rows=tuple(rows))
    except SeedError as exc:
        raise BuildError(f"the built seed is invalid: {exc}") from None


def build(input_dir: Path, output: Path, *, overwrite: bool) -> tuple[SpdrSeed, list[FundFile]]:
    """Read, validate, and write the seed. Raises :class:`BuildError` on any refusal."""
    if output.exists() and not overwrite:
        raise BuildError(f"existing seed {output} left as it is: pass --overwrite to replace it")
    paths = find_inputs(input_dir)
    funds = [parse_fund(etf, path, read_rows(path)) for etf, path in paths.items()]
    seed = build_seed(funds)
    text = format_spdr_seed(seed)
    if parse_spdr_seed(text, source=str(output)) != seed:
        raise BuildError("the formatted seed does not read back as the seed that was built")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return seed, funds


def _summary(seed: SpdrSeed, funds: Sequence[FundFile], output: Path) -> str:
    lines = [f"wrote {output} -- as of {seed.as_of.isoformat()}"]
    for fund in sorted(funds, key=lambda f: SPDR_FUNDS.index(f.etf)):
        total = sum((h.weight for h in fund.holdings), Decimal(0))
        leaders = ", ".join(seed.leaders()[fund.etf])
        lines.append(
            f"  {fund.etf:<4} {SPDR_SECTORS[fund.etf]:<23} {len(fund.holdings):>3} equities, "
            f"weights sum {total}%  leaders: {leaders}  ({fund.path.name})"
        )
        lines.extend(f"         dropped: {line}" for line in fund.dropped)
    held: dict[str, list[str]] = defaultdict(list)
    for row in seed.rows:
        held[row.symbol].append(row.etf)
    shared = {symbol: etfs for symbol, etfs in held.items() if len(etfs) > 1}
    lines.append(f"  {len(seed.symbols())} distinct symbols across {len(seed.rows)} rows")
    for symbol, etfs in sorted(shared.items()):
        lines.append(f"  note: {symbol} is held by {', '.join(etfs)}; it files under {seed.sector_of(symbol)}")
    lines.append("Review the dropped lines and the diff before committing.")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--input-dir", type=Path, required=True, help="folder holding the eleven downloaded files")
    parser.add_argument("--output", type=Path, default=SEED_PATH, help=f"seed to write (default {SEED_PATH})")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing seed")
    args = parser.parse_args(argv)
    try:
        seed, funds = build(args.input_dir, args.output, overwrite=args.overwrite)
    except BuildError as exc:
        print(f"build_spdr_seed: refusing: {exc}", file=sys.stderr)
        return 1
    print(_summary(seed, funds, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
