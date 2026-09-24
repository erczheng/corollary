"""Hand-kept seed files, and the loader for the SPDR holdings seed (spec decision 6).

``spdr_holdings.csv`` is built from State Street's eleven Select Sector SPDR
holdings files by ``scripts/build_spdr_seed.py``, which the owner runs by hand,
quarterly, against files the owner downloaded. Nothing here downloads it and
nothing schedules it: a scrape of State Street's site was rejected because its
terms were never checked.

One seed does three jobs:

* **ticker -> sector** for the news filter (:meth:`SpdrSeed.sector_of`);
* **the ~500-name universe** for the composite's breadth and strength
  components (:meth:`SpdrSeed.symbols`) -- the eleven funds together are the
  S&P 500;
* **the consensus roll-up's leaders**, the top five holdings of each fund by
  weight (:meth:`SpdrSeed.leaders`) -- 55 names.

The file format is plain CSV with the as-of date on its own first row, so the
date travels with the data rather than being lost in a code constant::

    as_of,2026-09-22
    etf,sector,symbol,weight
    XLK,Technology,NVDA,14.51
    ...

Weights are percentages of fund, kept as the exact decimal text the source
wrote -- :class:`~decimal.Decimal` from the file to the caller, no float on the
path. "Exact" is a property of the file, not of the fund's true weight: the
build script's inputs may be an ``.xlsx`` or a CSV saved from it, and those
can carry different precision for the same holding.

**Before the owner builds it, the seed does not exist**, and
:func:`load_spdr_seed` answers ``None`` -- logged once per path -- rather than
raising. A consumer renders that as a stated absence. A file that exists but is
malformed raises :class:`SeedError`: a half-read seed would silently shrink the
universe and misfile news, which is worse than no seed.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Final, Mapping

logger = logging.getLogger(__name__)

#: The eleven Select Sector SPDRs and the sector label each files under.
#:
#: The labels are the frontend's (``web/src/lib/mockData.ts``'s consensus
#: rows), not GICS's own spellings -- ``Technology``, not ``Information
#: Technology`` -- so a sector string from this seed matches the one the News
#: page already filters and renders by. ``MARKET`` stories file under
#: ``Macro`` (``MACRO_SECTOR`` in ``types.ts``) and a ticker outside the seed
#: under ``Other``; neither is a fund, so neither is here.
SPDR_SECTORS: Final[Mapping[str, str]] = MappingProxyType(
    {
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
)

#: The funds in their one canonical order (alphabetical). Leaders are returned
#: in it and a cross-fund tie in :meth:`SpdrSeed.sector_of` resolves by it.
SPDR_FUNDS: Final[tuple[str, ...]] = tuple(sorted(SPDR_SECTORS))

#: Where the committed seed lives -- next to this file, inside the package.
SEED_PATH: Final[Path] = Path(__file__).resolve().parent / "spdr_holdings.csv"

#: The label of the first row, whose second field is the as-of date.
AS_OF_LABEL: Final = "as_of"

#: The column header, the file's second row.
SEED_COLUMNS: Final[tuple[str, str, str, str]] = ("etf", "sector", "symbol", "weight")

#: Decision 6: the panel warns once the seed is *more than* this many days old.
STALE_AFTER_DAYS: Final = 100

#: How many leaders per fund the consensus roll-up takes (decision 6).
LEADERS_PER_FUND: Final = 5

#: An equity symbol in the form Alpaca and the rest of this codebase write it:
#: 1-6 capitals, optionally a class suffix after a dot (``BRK.B``). The same
#: shape as ``api/routes/markets.py``'s ``_SYMBOL_RE``. An adjusted OCC root
#: (``AAPL1``) is a property of a contract, never of a stock, and fails here.
#:
#: Always ``fullmatch``: with ``re.match``, a ``$`` anchor also matches just
#: before a trailing newline, so ``"AAPL\n"`` passed. ``\A``/``\Z`` keep the
#: pattern strict even for a caller that forgets.
EQUITY_SYMBOL_RE: Final = re.compile(r"\A[A-Z]{1,6}(\.[A-Z])?\Z")

#: A class share spelled with any common separator: ``BRK.B``, ``BRK/B``,
#: ``BRK-B``, ``BRK B``.
_CLASS_SHARE_RE: Final = re.compile(r"\A([A-Z]{1,6})[./\- ]([A-Z])\Z")


class SeedError(ValueError):
    """The seed file exists but cannot be trusted. The message names the file."""


def normalize_symbol(raw: str) -> str:
    """Upper-case and trim, and write a class share with a dot: ``BRK/B`` -> ``BRK.B``.

    Alpaca, Finnhub and this codebase (``api/routes/markets.py``,
    ``engine/runtime.py``) all write class shares as ``BRK.B``. Anything else is
    returned trimmed and upper-cased but otherwise untouched, so a caller can
    still see that ``-`` or ``CASH_USD`` is not a symbol.
    """
    text = raw.strip().upper()
    match = _CLASS_SHARE_RE.fullmatch(text)
    if match is not None:
        return f"{match.group(1)}.{match.group(2)}"
    return text


@dataclass(frozen=True)
class SpdrHolding:
    """One fund's holding of one stock. ``weight`` is percent of fund."""

    etf: str
    sector: str
    symbol: str
    weight: Decimal


@dataclass(frozen=True)
class SpdrSeed:
    """The whole seed: its as-of date and every holding row.

    Validated on construction, so the build script cannot produce -- and the
    loader cannot return -- a seed that breaks these invariants: every fund
    present, every sector the fund's own, every symbol well formed, every
    weight a finite positive decimal, no symbol twice within one fund.

    **A symbol may appear in more than one fund.** It happens for a rebalance
    around a GICS reclassification. It counts once in :meth:`symbols`, can lead
    in both funds, and :meth:`sector_of` files it under the fund where its
    weight is larger -- on a tie, the fund first in :data:`SPDR_FUNDS`.
    """

    as_of: date
    rows: tuple[SpdrHolding, ...]
    _sector_by_symbol: Mapping[str, str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        seen: set[tuple[str, str]] = set()
        for row in self.rows:
            problem = _row_problem(row)
            if problem is not None:
                raise SeedError(f"{row.etf} {row.symbol!r}: {problem}")
            key = (row.etf, row.symbol)
            if key in seen:
                raise SeedError(f"{row.etf} {row.symbol!r}: appears twice in {row.etf}")
            seen.add(key)
        present = {row.etf for row in self.rows}
        missing = [etf for etf in SPDR_FUNDS if etf not in present]
        if missing:
            raise SeedError(f"no holdings for {missing}: the seed must cover all eleven funds")

        best: dict[str, SpdrHolding] = {}
        for row in self.rows:
            current = best.get(row.symbol)
            if current is None or _heavier(row, current):
                best[row.symbol] = row
        sectors = MappingProxyType({symbol: row.sector for symbol, row in best.items()})
        object.__setattr__(self, "_sector_by_symbol", sectors)

    def sector_of(self, ticker: str) -> str | None:
        """The sector label for ``ticker``, or ``None`` if no fund holds it."""
        return self._sector_by_symbol.get(normalize_symbol(ticker))

    def symbols(self) -> frozenset[str]:
        """Every symbol any fund holds -- the ~500-name universe."""
        return frozenset(self._sector_by_symbol)

    def leaders(self, per_fund: int = LEADERS_PER_FUND) -> dict[str, tuple[str, ...]]:
        """Each fund's top ``per_fund`` holdings by weight, heaviest first.

        Ties break by symbol, alphabetically, so the answer never depends on
        row order. Keys come in :data:`SPDR_FUNDS` order. A fund with fewer
        holdings than ``per_fund`` returns all of them.
        """
        if per_fund < 1:
            raise ValueError(f"per_fund must be at least 1, not {per_fund}")
        out: dict[str, tuple[str, ...]] = {}
        for etf in SPDR_FUNDS:
            held = sorted(
                (row for row in self.rows if row.etf == etf),
                # copy_negate, not unary minus: ``-w`` rounds to the context's
                # 28 digits and can turn two different weights into a tie.
                key=lambda row: (row.weight.copy_negate(), row.symbol),
            )
            out[etf] = tuple(row.symbol for row in held[:per_fund])
        return out

    def age_days(self, today: date) -> int:
        """Days from the as-of date to ``today``. Negative if ``today`` is earlier."""
        return (today - self.as_of).days

    def is_stale(self, today: date) -> bool:
        """Decision 6's warning: *more than* :data:`STALE_AFTER_DAYS` old."""
        return self.age_days(today) > STALE_AFTER_DAYS


def _heavier(row: SpdrHolding, current: SpdrHolding) -> bool:
    """Does ``row`` beat ``current`` for a symbol held in two funds?"""
    if row.weight != current.weight:
        return row.weight > current.weight
    return SPDR_FUNDS.index(row.etf) < SPDR_FUNDS.index(current.etf)


def parse_spdr_seed(text: str, source: str = "<string>") -> SpdrSeed:
    """Parse the seed's text. Raises :class:`SeedError` naming ``source`` and the line."""
    reader = csv.reader(io.StringIO(text, newline=""))
    records = [(reader.line_num, record) for record in reader]

    def fail(line: int, message: str) -> SeedError:
        return SeedError(f"{source}, line {line}: {message}")

    if not records:
        raise SeedError(f"{source}: empty file")
    line, first = records[0]
    if len(first) != 2 or first[0] != AS_OF_LABEL:
        raise fail(line, f"the first row must be '{AS_OF_LABEL},YYYY-MM-DD', not {first!r}")
    try:
        as_of = date.fromisoformat(first[1])
    except ValueError:
        raise fail(line, f"as-of {first[1]!r} is not an ISO date YYYY-MM-DD") from None
    if len(records) < 2 or tuple(records[1][1]) != SEED_COLUMNS:
        got = records[1][1] if len(records) > 1 else None
        raise fail(2, f"the second row must be {','.join(SEED_COLUMNS)!r}, not {got!r}")

    rows: list[SpdrHolding] = []
    for line, record in records[2:]:
        if len(record) != len(SEED_COLUMNS):
            raise fail(line, f"expected {len(SEED_COLUMNS)} fields, got {len(record)}: {record!r}")
        etf, sector, symbol, weight_text = record
        try:
            weight = Decimal(weight_text)
        except InvalidOperation:
            raise fail(line, f"weight {weight_text!r} is not a decimal") from None
        row = SpdrHolding(etf, sector, symbol, weight)
        problem = _row_problem(row)
        if problem is not None:
            raise fail(line, problem)
        rows.append(row)
    if not rows:
        raise SeedError(f"{source}: no holdings rows")
    try:
        return SpdrSeed(as_of=as_of, rows=tuple(rows))
    except SeedError as exc:
        raise SeedError(f"{source}: {exc}") from None


def _row_problem(row: SpdrHolding) -> str | None:
    """What is wrong with one row on its own, or ``None``.

    The one per-row check, used by :class:`SpdrSeed` on construction and by
    :func:`parse_spdr_seed` so a bad row is reported with its line number.
    """
    if row.etf not in SPDR_SECTORS:
        return f"{row.etf!r} is not one of the eleven funds {list(SPDR_FUNDS)}"
    if row.sector != SPDR_SECTORS[row.etf]:
        return f"sector {row.sector!r} is not {row.etf}'s sector {SPDR_SECTORS[row.etf]!r}"
    if not EQUITY_SYMBOL_RE.fullmatch(row.symbol):
        return f"{row.symbol!r} is not an equity symbol of the form AAPL or BRK.B"
    if not isinstance(row.weight, Decimal) or not row.weight.is_finite() or row.weight <= 0:
        return f"weight {row.weight!r} is not a finite positive Decimal"
    return None


def format_spdr_seed(seed: SpdrSeed) -> str:
    """The seed as file text: as-of row, header, then the rows as given.

    Weights are written in fixed-point notation from the exact ``Decimal`` --
    never through a float, never in exponent form.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([AS_OF_LABEL, seed.as_of.isoformat()])
    writer.writerow(SEED_COLUMNS)
    for row in seed.rows:
        writer.writerow([row.etf, row.sector, row.symbol, format(row.weight, "f")])
    return buffer.getvalue()


#: Paths already reported missing, so a caller asking per article logs once.
_MISSING_LOGGED: set[str] = set()


def load_spdr_seed(path: Path | None = None) -> SpdrSeed | None:
    """Load the seed from ``path`` (default :data:`SEED_PATH`).

    ``None`` if the file does not exist -- the state before the owner first
    builds it -- logged once per path. Raises :class:`SeedError` if it exists
    and is malformed.
    """
    target = SEED_PATH if path is None else path
    if not target.exists():
        key = str(target)
        if key not in _MISSING_LOGGED:
            _MISSING_LOGGED.add(key)
            logger.warning(
                "SPDR holdings seed not found at %s; sector lookup, the universe and leaders are unavailable",
                key,
                extra={
                    "event": "spdr_seed_missing",
                    "path": key,
                    "remedy": "run scripts/build_spdr_seed.py against the eleven downloaded holdings files",
                },
            )
        return None
    try:
        # utf-8-sig: Notepad and Excel's "CSV UTF-8" prepend a byte-order mark,
        # which plain utf-8 would glue onto the first field.
        text = target.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SeedError(f"{target}: not UTF-8 text ({exc})") from None
    return parse_spdr_seed(text, source=str(target))
