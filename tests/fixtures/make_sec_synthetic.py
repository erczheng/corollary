"""Generate ``tests/fixtures/sec_synthetic/*.json`` -- SYNTHETIC, deterministic, offline.

Run from the repo root::

    uv run python tests/fixtures/make_sec_synthetic.py

This makes **no request of any kind**: it imports nothing but the standard
library's ``json`` and ``pathlib``, and writes only the three JSON files in
``sec_synthetic/``. It is a generator, not a recorder -- the name is
``make_*`` rather than ``record_*`` so the recorder guards in
``tests/test_hard_rules.py`` do not apply, and it has no need of them. The two
XML files beside the JSON are hand-written and not produced here.

Every value is invented except XLK's seriesId ``S000006415`` and the trust's
CIK. Re-running it reproduces the committed files byte for byte, LF line
endings included.
"""

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent / "sec_synthetic"
TRUST = 1064641
NOTE = (
    "SYNTHETIC: hand-built for tests/data/providers/test_sec_provider.py. Not a recording. "
    "Shapes follow tests/fixtures/record_sec.py's assumptions; the live recording must confirm them. "
    "Series ids other than XLK's S000006415 are invented."
)
SECTOR = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
COLUMNS = ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument")


def _write(name: str, payload: Any) -> None:
    # write_bytes, not write_text: on Windows write_text emits CRLF, and the
    # committed fixtures are LF.
    (OUT / name).write_bytes((json.dumps(payload, indent=1) + "\n").encode("utf-8"))


def main() -> None:
    series = {t: f"S0000064{i:02d}" for i, t in enumerate(SECTOR, start=10)}
    series["XLK"] = "S000006415"
    series["XLF"] = "S000006409"  # keep the invented set collision-free with XLK's real id
    premium = {f"{t}P": f"S00009{i:04d}" for i, t in enumerate(SECTOR, start=1)}
    premium_first = {"XLKP": "S000000001"}  # the decoy that sorts first by seriesId
    premium.update(premium_first)

    rows: list[list[Any]] = [
        [9999999, "S000099999", "C000099999", "XLK"],  # another registrant's XLK, listed first
        [2110, "S000009184", "C000024954", "LACAX"],
    ]
    for t in sorted(premium):
        rows.append([TRUST, premium[t], "C0009" + premium[t][-5:], t])
    for i, t in enumerate(SECTOR):
        rows.append([TRUST, series[t], f"C0000{17000 + i}", t])
    _write(
        "company_tickers_mf.synthetic.json",
        {"_synthetic": NOTE, "fields": ["cik", "seriesId", "classId", "symbol"], "data": rows},
    )

    # Submissions: the Premium decoy's accession sorts first; 22 NPORT-P for
    # 2026-06-30, one NPORT-P/A for XLE's series that quarter, two older-quarter
    # NPORT-P, other forms, and one unreadable NPORT-P row.
    recent: dict[str, list[str]] = {k: [] for k in COLUMNS}
    accession_series: dict[str, str] = {}

    def add(acc: str, filed: str, rep: str, form: str, doc: str = "primary_doc.xml") -> None:
        for key, value in zip(COLUMNS, (acc, filed, rep, form, doc)):
            recent[key].append(value)

    add("0001752724-26-000001", "2026-08-27", "2026-06-30", "NPORT-P")
    accession_series["0001752724-26-000001"] = premium_first["XLKP"]
    n = 2
    for t in sorted(premium):
        if t == "XLKP":
            continue
        acc = f"0001752724-26-{n:06d}"
        n += 1
        add(acc, "2026-08-27", "2026-06-30", "NPORT-P")
        accession_series[acc] = premium[t]
    add("0000950123-26-011111", "2026-09-02", "", "485BPOS", "d123.htm")
    for t in reversed(SECTOR):
        acc = f"0001752724-26-{n:06d}"
        n += 1
        add(acc, "2026-08-28", "2026-06-30", "NPORT-P")
        accession_series[acc] = series[t]
    add("0001752724-26-000900", "2026-09-15", "2026-06-30", "NPORT-P/A")
    accession_series["0001752724-26-000900"] = series["XLE"]
    add("0001752724-26-000901", "2026-08-28", "not-a-date", "NPORT-P")
    add("0001752724-26-000500", "2026-05-29", "2026-03-31", "NPORT-P")
    add("0001752724-26-000501", "2026-05-29", "2026-03-31", "NPORT-P")
    add("0001752724-26-000600", "2026-03-01", "2025-12-31", "N-CSR", "ncsr.htm")
    _write(
        "submissions_CIK0001064641.synthetic.json",
        {
            "_synthetic": NOTE,
            "cik": str(TRUST),
            "name": "SELECT SECTOR SPDR TRUST",
            "filings": {"recent": recent, "files": []},
        },
    )
    _write(
        "accession_series.synthetic.json",
        {
            "_synthetic": NOTE
            + " Which series each synthetic accession's document reports (submissions carry no series).",
            "sector": series,
            "premium": premium,
            "accessions": accession_series,
        },
    )
    forms = recent["form"]
    print(len(forms), "submissions rows;", sum(f == "NPORT-P" for f in forms), "NPORT-P")


if __name__ == "__main__":
    main()
