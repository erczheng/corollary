"""``scripts/build_spdr_seed.py`` -- the hand-run SPDR seed builder (spec decision 6).

Driven end to end against ``tests/fixtures/ssga/``, eleven **synthetic** files in
the shape of State Street's daily holdings sheets. The xlsx path is exercised by
converting those same CSVs to minimal workbooks here, so both input forms must
produce byte-identical seeds.

Nothing here touches the network, and nothing writes the committed seed: every
output goes to ``tmp_path``.
"""

from __future__ import annotations

import ast
import csv
import importlib.util
import shutil
import sys
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from xml.sax.saxutils import escape

import pytest

from corollary.data.seeds import SPDR_SECTORS, load_spdr_seed

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "build_spdr_seed.py"
FIXTURES = REPO / "tests" / "fixtures" / "ssga"


def _fixture(etf: str) -> Path:
    return FIXTURES / f"synthetic-holdings-daily-us-en-{etf.lower()}.csv"


@pytest.fixture()
def build(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("build_spdr_seed", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "build_spdr_seed", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def inputs(tmp_path: Path) -> Path:
    """A private copy of the eleven fixture files, safe to edit per test."""
    target = tmp_path / "downloads"
    target.mkdir()
    for etf in SPDR_SECTORS:
        shutil.copy(_fixture(etf), target / _fixture(etf).name)
    shutil.copy(FIXTURES / "README.md", target / "README.md")  # not a candidate: ignored
    return target


def _run(build: ModuleType, inputs: Path, output: Path, *extra: str) -> int:
    code: int = build.main(["--input-dir", str(inputs), "--output", str(output), *extra])
    return code


# -- xlsx conversion ----------------------------------------------------------

_NUMERIC_COLUMNS = {4, 6}  # Weight and Shares Held, as SSGA stores them


def _col(index: int) -> str:
    return chr(ord("A") + index)


def _csv_to_xlsx(source: Path, target: Path) -> None:
    """A minimal workbook: shared strings for text, numeric cells for weights,
    one inline string, and blank rows omitted from the XML entirely -- which is
    how Excel writes them, and which the reader must fill back in."""
    with source.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    shared: list[str] = []
    xml_rows: list[str] = []
    header_seen = False
    for r, row in enumerate(rows, start=1):
        cells: list[str] = []
        for c, value in enumerate(row):
            if value == "":
                continue
            ref = f"{_col(c)}{r}"
            if header_seen and c in _NUMERIC_COLUMNS and _is_number(value):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            elif value.startswith("Holdings are subject"):
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
            else:
                shared.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
        if row and row[0] == "Name":
            header_seen = True
        if cells:
            xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    sst = "".join(f"<si><t>{escape(s)}</t></si>" for s in shared)
    with zipfile.ZipFile(target, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("_rels/.rels", f'<?xml version="1.0"?><Relationships xmlns="{pkg}"><Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", f'<?xml version="1.0"?><workbook xmlns="{ns}" xmlns:r="{rel}"><sheets><sheet name="holdings" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0"?><Relationships xmlns="{pkg}"><Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="{rel}/sharedStrings" Target="sharedStrings.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<?xml version="1.0"?><sst xmlns="{ns}">{sst}</sst>')
        z.writestr("xl/worksheets/sheet1.xml", f'<?xml version="1.0"?><worksheet xmlns="{ns}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>')


def _is_number(value: str) -> bool:
    try:
        Decimal(value)
    except ArithmeticError:
        return False
    return True


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path.name}"
    path.write_text(text.replace(old, new), encoding="utf-8", newline="")


# -- end to end ---------------------------------------------------------------


def test_eleven_csv_files_build_a_seed_the_loader_accepts(build: ModuleType, inputs: Path, tmp_path: Path) -> None:
    out = tmp_path / "seed" / "spdr_holdings.csv"
    assert _run(build, inputs, out) == 0
    seed = load_spdr_seed(out)
    assert seed is not None
    assert seed.as_of == date(2026, 9, 22)
    assert set(seed.leaders()) == set(SPDR_SECTORS)
    assert len(seed.symbols()) == 66  # six equities per fund
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[:2] == ["as_of,2026-09-22", "etf,sector,symbol,weight"]
    # the cash row, the futures row and the footer are gone
    for absent in ("US DOLLAR", ",-,", "IXEZ6", "Past performance", "SYNTHETIC"):
        assert absent not in text
    assert seed.sector_of("BRK.B") == "Financials"
    assert seed.leaders()["XLK"] == ("NVDA", "MSFT", "AAPL", "AVGO", "ORCL")
    assert seed.leaders()["XLP"] == ("COST", "WMT", "PG", "KO", "CL")
    assert "XLK,Technology,MSFT,22.123456\n" in text  # the exact text, not a float's repr


def test_rows_are_written_in_a_deterministic_order(build: ModuleType, inputs: Path, tmp_path: Path) -> None:
    """Fund order, then heaviest first, then symbol -- so a quarterly refresh
    diffs as a set of weight changes, not a reshuffle."""
    out = tmp_path / "a.csv"
    assert _run(build, inputs, out) == 0
    body = out.read_text(encoding="utf-8").splitlines()[2:]
    xlp = [line for line in body if line.startswith("XLP,")]
    assert xlp == [
        "XLP,Consumer Staples,COST,25",
        "XLP,Consumer Staples,WMT,25",
        "XLP,Consumer Staples,PG,20",
        "XLP,Consumer Staples,KO,14",
        "XLP,Consumer Staples,CL,8",
        "XLP,Consumer Staples,MO,8",
    ]
    assert [line.split(",")[0] for line in body] == sorted((line.split(",")[0] for line in body))


def test_xlsx_inputs_build_the_same_seed_as_csv(build: ModuleType, inputs: Path, tmp_path: Path) -> None:
    from_csv = tmp_path / "from_csv.csv"
    assert _run(build, inputs, from_csv) == 0
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    for i, etf in enumerate(sorted(SPDR_SECTORS)):
        src = inputs / _fixture(etf).name
        if i % 2 == 0:
            _csv_to_xlsx(src, mixed / src.with_suffix(".xlsx").name)
        else:
            shutil.copy(src, mixed / src.name)
    from_mixed = tmp_path / "from_mixed.csv"
    assert _run(build, mixed, from_mixed) == 0
    assert from_mixed.read_bytes() == from_csv.read_bytes()


def test_a_file_that_is_not_a_workbook_is_refused_by_name(build: ModuleType, inputs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    xlk = inputs / _fixture("XLK").name
    xlk.rename(xlk.with_suffix(".xlsx"))  # CSV bytes under an .xlsx name
    out = tmp_path / "out.csv"
    assert _run(build, inputs, out) != 0
    assert "xlk.xlsx" in capsys.readouterr().err
    assert not out.exists()


# -- refusals -----------------------------------------------------------------


def test_an_as_of_disagreement_is_refused_naming_the_files(build: ModuleType, inputs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _edit(inputs / _fixture("XLU").name, "As of 22-Sep-2026", "As of 19-Sep-2026")
    out = tmp_path / "out.csv"
    assert _run(build, inputs, out) != 0
    err = capsys.readouterr().err
    assert "synthetic-holdings-daily-us-en-xlu.csv" in err
    assert "2026-09-19" in err and "2026-09-22" in err
    assert not out.exists()


def test_a_missing_fund_file_is_refused_by_name(build: ModuleType, inputs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (inputs / _fixture("XLRE").name).unlink()
    out = tmp_path / "out.csv"
    assert _run(build, inputs, out) != 0
    err = capsys.readouterr().err
    assert "XLRE" in err
    assert not out.exists()


def test_two_files_for_one_fund_are_refused(build: ModuleType, inputs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = inputs / _fixture("XLK").name
    shutil.copy(src, inputs / "holdings-daily-us-en-xlk (1).csv")
    out = tmp_path / "out.csv"
    assert _run(build, inputs, out) != 0
    assert "XLK" in capsys.readouterr().err
    assert not out.exists()


def test_an_existing_seed_is_not_overwritten_without_the_flag(build: ModuleType, inputs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "spdr_holdings.csv"
    out.write_text("keep me\n", encoding="utf-8")
    assert _run(build, inputs, out) != 0
    assert "--overwrite" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == "keep me\n"
    assert _run(build, inputs, out, "--overwrite") == 0
    assert load_spdr_seed(out) is not None


def test_a_failed_build_leaves_an_existing_seed_untouched(build: ModuleType, inputs: Path, tmp_path: Path) -> None:
    out = tmp_path / "spdr_holdings.csv"
    out.write_text("keep me\n", encoding="utf-8")
    (inputs / _fixture("XLB").name).unlink()
    assert _run(build, inputs, out, "--overwrite") != 0
    assert out.read_text(encoding="utf-8") == "keep me\n"
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["spdr_holdings.csv"]


def test_a_file_for_the_wrong_fund_is_refused(build: ModuleType, inputs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A download saved under the wrong name must not file XLF's holdings as XLK's."""
    _edit(inputs / _fixture("XLK").name, "Ticker Symbol:,XLK", "Ticker Symbol:,XLF")
    assert _run(build, inputs, tmp_path / "out.csv") != 0
    err = capsys.readouterr().err
    assert "xlk.csv" in err and "XLF" in err


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ("Name,Ticker,Identifier,SEDOL,Weight", "Name,Symbol,Identifier,SEDOL,Pct", "header"),
        ("Holdings:,As of 22-Sep-2026", "Holdings:,sometime", "as-of"),
        ("Ticker Symbol:,XLK\n", "", "Ticker Symbol"),
        ("NVIDIA CORP,NVDA,SYN000000,S000000,24,", "NVIDIA CORP,NVDA,SYN000000,S000000,twenty-four,", "twenty-four"),
        ("NVIDIA CORP,NVDA,SYN000000,S000000,24,", "NVIDIA CORP,NVDA,SYN000000,S000000,-24,", "-24"),
        ("NVIDIA CORP,NVDA,SYN000000,S000000,24,", "NVIDIA CORP,NVDA,SYN000000,S000000,100.5,", "100.5"),
        ("APPLE INC,AAPL,", "MICROSOFT CORP,MSFT,", "MSFT"),
    ],
    ids=[
        "no-header-row", "unparseable-as-of", "no-ticker-line", "weight-not-a-number", "negative-weight",
        "weight-over-100", "duplicate-symbol",
    ],
)
def test_an_unexpected_file_is_refused_by_name(
    build: ModuleType, inputs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], old: str, new: str, expect: str
) -> None:
    _edit(inputs / _fixture("XLK").name, old, new)
    out = tmp_path / "out.csv"
    assert _run(build, inputs, out) != 0
    err = capsys.readouterr().err
    assert "synthetic-holdings-daily-us-en-xlk.csv" in err
    assert expect in err
    assert not out.exists()


def test_holdings_resuming_after_a_blank_row_are_refused(build: ModuleType, inputs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A stray blank row inside the table would otherwise truncate a fund silently."""
    _edit(inputs / _fixture("XLK").name, "BROADCOM INC,", "\nBROADCOM INC,")
    assert _run(build, inputs, tmp_path / "out.csv") != 0
    err = capsys.readouterr().err
    assert "xlk.csv" in err and "AVGO" in err


def test_a_fund_with_too_few_holdings_is_refused(build: ModuleType, inputs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = inputs / _fixture("XLB").name
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith(("SHERWIN", "AIR PRODUCTS"))]
    path.write_text("".join(kept), encoding="utf-8", newline="")
    assert _run(build, inputs, tmp_path / "out.csv") != 0
    assert "xlb.csv" in capsys.readouterr().err


def _set_weights(path: Path, weights: list[str]) -> None:
    """Replace the Weight cell of the fixture's six equity rows, in file order."""
    lines = path.read_text(encoding="utf-8").split("\n")
    remaining = list(weights)
    for i, line in enumerate(lines):
        parts = line.split(",")
        if len(parts) == 8 and parts[2].startswith("SYN"):
            parts[4] = remaining.pop(0)
            lines[i] = ",".join(parts)
    assert remaining == [], f"{path.name} has fewer equity rows than {weights}"
    path.write_text("\n".join(lines), encoding="utf-8", newline="")


LIN_30 = "LINDE PLC,LIN,SYN000000,S000000,30,"


@pytest.mark.parametrize(("lin", "total"), [("20", "90"), ("40", "110")], ids=["sum-90", "sum-110"])
def test_a_fund_summing_to_the_edge_of_the_band_is_accepted(build: ModuleType, inputs: Path, tmp_path: Path, lin: str, total: str) -> None:
    """XLB's other five equities sum to 70; LIN makes up the rest. 90 and 110
    are inside the band."""
    _edit(inputs / _fixture("XLB").name, LIN_30, LIN_30.replace(",30,", f",{lin},"))
    out = tmp_path / "out.csv"
    assert _run(build, inputs, out) == 0
    assert f"XLB,Materials,LIN,{lin}\n" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize(("lin", "total"), [("19.99", "89.99"), ("40.01", "110.01")], ids=["sum-89.99", "sum-110.01"])
def test_a_fund_summing_outside_the_band_is_refused_naming_the_fund_and_its_sum(
    build: ModuleType, inputs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], lin: str, total: str
) -> None:
    """A read that stops early, or a column that is not percent of fund, shows
    as a fund that does not add up -- even with five or more equities read."""
    _edit(inputs / _fixture("XLB").name, LIN_30, LIN_30.replace(",30,", f",{lin},"))
    out = tmp_path / "out.csv"
    assert _run(build, inputs, out) != 0
    err = capsys.readouterr().err
    assert "synthetic-holdings-daily-us-en-xlb.csv" in err
    assert "XLB" in err and total in err
    assert not out.exists()


def test_a_single_weight_of_exactly_100_is_accepted(build: ModuleType, inputs: Path, tmp_path: Path) -> None:
    """100 is the per-weight ceiling, inclusive; the fund still sums inside the band."""
    _set_weights(inputs / _fixture("XLK").name, ["100", "1", "1", "1", "1", "1"])
    out = tmp_path / "out.csv"
    assert _run(build, inputs, out) == 0
    assert "XLK,Technology,NVDA,100\n" in out.read_text(encoding="utf-8")


def test_weights_stored_as_fractions_are_refused_not_rescaled(
    build: ModuleType, inputs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A percent-formatted Weight cell stores 0.24 for 24% in the xlsx, while a
    CSV saved from the same sheet says 24%. Read as-is that fund is 100x lighter
    than the others, and ``sector_of`` compares weights across funds. The script
    says so and stops; it never multiplies by 100 on the owner's behalf."""
    csv_path = inputs / _fixture("XLK").name
    _set_weights(csv_path, ["0.24", "0.22123456", "0.20", "0.14", "0.10876544", "0.09"])
    xlsx_path = csv_path.with_suffix(".xlsx")
    _csv_to_xlsx(csv_path, xlsx_path)
    csv_path.unlink()
    out = tmp_path / "out.csv"
    assert _run(build, inputs, out) != 0
    err = capsys.readouterr().err
    assert xlsx_path.name in err
    assert "XLK" in err and "1.00000000" in err
    assert "fraction" in err and "percent-formatted" in err
    assert "save it as CSV" in err and "not rescale" in err
    assert not out.exists()


def test_a_percent_sign_csv_reads_as_percent(build: ModuleType, inputs: Path, tmp_path: Path) -> None:
    """Excel writes a percent-formatted cell to CSV as ``24%``; that is percent."""
    _set_weights(inputs / _fixture("XLK").name, ["24%", "22.123456%", "20%", "14%", "10.876544%", "9%"])
    from_pct = tmp_path / "pct.csv"
    assert _run(build, inputs, from_pct) == 0
    assert "XLK,Technology,MSFT,22.123456\n" in from_pct.read_text(encoding="utf-8")


def test_rows_sort_by_exact_weight_beyond_28_digits(build: ModuleType, inputs: Path, tmp_path: Path) -> None:
    """Negating a Decimal rounds to the context's 28 digits, which made these two
    weights tie and sort by symbol. The heavier one must come first."""
    path = inputs / _fixture("XLB").name
    _edit(path, LIN_30, LIN_30.replace(",30,", ",25.0000000000000000000000000001,"))
    _edit(path, "SHERWIN WILLIAMS CO/THE,SHW,SYN000001,S000001,20,", "SHERWIN WILLIAMS CO/THE,SHW,SYN000001,S000001,25.0000000000000000000000000002,")
    out = tmp_path / "out.csv"
    assert _run(build, inputs, out) == 0
    xlb = [line.split(",")[2] for line in out.read_text(encoding="utf-8").splitlines() if line.startswith("XLB,")]
    assert xlb[:2] == ["SHW", "LIN"]


def test_class_share_spellings_normalise_to_the_dot_form(build: ModuleType, inputs: Path, tmp_path: Path) -> None:
    _edit(inputs / _fixture("XLF").name, ",BRK.B,", ",BRK/B,")
    out = tmp_path / "out.csv"
    assert _run(build, inputs, out) == 0
    assert "XLF,Financials,BRK.B,22\n" in out.read_text(encoding="utf-8")


def test_a_cp1252_export_is_read(build: ModuleType, inputs: Path, tmp_path: Path) -> None:
    """Excel's "CSV" (not "CSV UTF-8") on Windows writes cp1252; the fund name's
    registered-trademark sign is the byte that trips a UTF-8 read."""
    path = inputs / _fixture("XLV").name
    path.write_bytes(path.read_text(encoding="utf-8").encode("cp1252"))
    assert _run(build, inputs, tmp_path / "out.csv") == 0


# -- the script's reach -------------------------------------------------------


def test_the_script_imports_nothing_that_can_reach_a_network() -> None:
    """It reads eleven local files and writes one. No HTTP client, no socket,
    no vendor SDK, no MCP client.

    ``tests/test_hard_rules.py`` covers only part of that for ``scripts/*.py``:
    its vendor-SDK guard (``test_the_vendor_sdk_is_imported_nowhere``) walks
    every script, but its MCP guard
    (``test_no_module_imports_or_invokes_an_mcp_client``) scans ``corollary/``
    alone. So this allowlist is what keeps an MCP client, an HTTP client and a
    socket out of this script. Standard-library roots are allowed whole; from
    ``corollary`` only ``corollary.data.seeds`` itself -- any other module of
    ours could pull ``httpx`` in behind a harmless-looking name.
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    stdlib: set[str] = set()
    ours: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module, "a relative import in a standalone script"
            names = [node.module]
        for name in names:
            root = name.split(".")[0]
            if root == "corollary":
                ours.add(name)
            else:
                stdlib.add(root)
    assert stdlib <= {
        "__future__", "argparse", "csv", "io", "re", "sys", "os", "posixpath", "zipfile", "dataclasses",
        "datetime", "decimal", "pathlib", "typing", "xml", "collections",
    }, stdlib
    assert ours == {"corollary.data.seeds"}, ours
