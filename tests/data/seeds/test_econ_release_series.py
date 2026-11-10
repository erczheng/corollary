"""The econ release-series seed (spec R7, *The seed*) and its joint validation.

``econ_release_series.csv`` says which FRED series carries each release's
figure, how to transform it, and how to display it. It is validated **whole,
together with** ``econ_release_times.csv``: every release has exactly one
series row, and a missing row, an extra row, a duplicate, an unknown
transform or frequency, an unparseable scale or bad decimals refuses **both**
seeds.

Every refusal test below starts from :data:`_BASE_TIMES` / :data:`_BASE_SERIES`,
a pair that :func:`test_baseline_pair_parses` proves valid on its own, and
changes exactly one thing. A rejection test whose baseline is already invalid
proves nothing -- an earlier round shipped those.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import get_args

import pytest

from corollary.data.seeds import SeedError
from corollary.data.seeds.calendar_seeds import (
    ECON_FREQUENCIES,
    ECON_RELEASE_SERIES_COLUMNS,
    ECON_RELEASE_SERIES_PATH,
    ECON_RELEASE_TIMES_PATH,
    ECON_TRANSFORMS,
    EconFrequency,
    EconReleaseSeries,
    EconTransform,
    load_econ_release_seeds,
    load_econ_release_times,
    parse_econ_release_seeds,
)
from corollary.data.providers.fred import VINTAGE_LOOKBACK_MONTHS, VintageFrequency

_URL = "https://example.invalid/schedule"
_TIMES_HEADER = "release_id,release_name,et_time,fred_url,time_source_url,note"
_SERIES_HEADER = "release_id,series_id,transform,scale,unit_label,decimals,frequency"

_BASE_TIMES_ROWS = [
    f"10,Consumer Price Index,08:30,{_URL},{_URL},",
    f"51,U.S. International Trade in Goods and Services,08:30,{_URL},{_URL},",
    f"188,U.S. Import and Export Price Indexes,08:30,{_URL},{_URL},",
]
_BASE_SERIES_ROWS = [
    "10,CPIAUCSL,pct_change,1,% m/m,1,monthly",
    "51,BOPGSTB,level,0.001,$B,1,monthly",
    '188,IR,pct_change,1,"% m/m, NSA",1,monthly',
]


def _text(header: str, rows: list[str]) -> str:
    return "\n".join([header, *rows]) + "\n"


_BASE_TIMES = _text(_TIMES_HEADER, _BASE_TIMES_ROWS)
_BASE_SERIES = _text(_SERIES_HEADER, _BASE_SERIES_ROWS)


def _series_with(index: int, row: str) -> str:
    """The baseline series text with row ``index`` replaced by ``row``."""
    rows = list(_BASE_SERIES_ROWS)
    rows[index] = row
    return _text(_SERIES_HEADER, rows)


def _parse(times: str = _BASE_TIMES, series: str = _BASE_SERIES) -> None:
    parse_econ_release_seeds(times, series, times_source="times.csv", series_source="series.csv")


#: Spec R7's table, verbatim, plus the ``frequency`` column (U6c-fix F8, a
#: deliberate extension of R7's columns). The shipped seed must be exactly this.
_SPEC_TABLE: tuple[EconReleaseSeries, ...] = (
    EconReleaseSeries(10, "CPIAUCSL", "pct_change", Decimal("1"), "% m/m", 1, "monthly"),
    EconReleaseSeries(50, "PAYEMS", "change", Decimal("1"), "K", 0, "monthly"),
    EconReleaseSeries(46, "PPIFIS", "pct_change", Decimal("1"), "% m/m", 1, "monthly"),
    EconReleaseSeries(192, "JTSJOL", "level", Decimal("1"), "K", 0, "monthly"),
    EconReleaseSeries(11, "ECIALLCIV", "pct_change", Decimal("1"), "% q/q", 1, "quarterly"),
    EconReleaseSeries(188, "IR", "pct_change", Decimal("1"), "% m/m, NSA", 1, "monthly"),
    EconReleaseSeries(53, "A191RL1Q225SBEA", "level", Decimal("1"), "% q/q SAAR", 1, "quarterly"),
    EconReleaseSeries(54, "PCEPILFE", "pct_change", Decimal("1"), "% m/m, core PCE", 1, "monthly"),
    EconReleaseSeries(51, "BOPGSTB", "level", Decimal("0.001"), "$B", 1, "monthly"),
    EconReleaseSeries(9, "RSAFS", "pct_change", Decimal("1"), "% m/m", 1, "monthly"),
    EconReleaseSeries(27, "HOUST", "level", Decimal("1"), "K SAAR", 0, "monthly"),
    EconReleaseSeries(97, "HSN1F", "level", Decimal("1"), "K SAAR", 0, "monthly"),
    EconReleaseSeries(229, "TTLCONS", "pct_change", Decimal("1"), "% m/m", 1, "monthly"),
    EconReleaseSeries(13, "INDPRO", "pct_change", Decimal("1"), "% m/m", 1, "monthly"),
    EconReleaseSeries(291, "EXHOSLUSM495S", "level", Decimal("0.000001"), "M SAAR", 2, "monthly"),
)


# --- the shipped pair ---------------------------------------------------------


def test_shipped_pair_loads_and_is_the_spec_table() -> None:
    seeds = load_econ_release_seeds()
    assert len(seeds.series.rows) == 15
    assert seeds.series.rows == _SPEC_TABLE
    assert {row.release_id for row in seeds.series.rows} == {
        row.release_id for row in seeds.times.rows
    }


def test_shipped_scales_are_exact_decimals() -> None:
    seeds = load_econ_release_seeds()
    bop = seeds.series.by_id(51)
    assert bop is not None and bop.series_id == "BOPGSTB"
    assert type(bop.scale) is Decimal
    assert bop.scale == Decimal("0.001")
    assert str(bop.scale) == "0.001"  # the file's text, not a float's nearest value
    ehs = seeds.series.by_id(291)
    assert ehs is not None and str(ehs.scale) == "0.000001"
    assert all(type(row.scale) is Decimal for row in seeds.series.rows)


def test_shipped_unit_labels_with_commas_survive_the_csv() -> None:
    seeds = load_econ_release_seeds()
    ir = seeds.series.by_id(188)
    pce = seeds.series.by_id(54)
    assert ir is not None and ir.unit_label == "% m/m, NSA"
    assert pce is not None and pce.unit_label == "% m/m, core PCE"


def test_times_loader_is_the_joint_loader() -> None:
    # The existing callers (the calendar job, calendar_releases) go through
    # load_econ_release_times; it must see the same validated table.
    assert load_econ_release_times() == load_econ_release_seeds().times
    assert ECON_RELEASE_SERIES_PATH.parent == ECON_RELEASE_TIMES_PATH.parent


def test_transforms_are_exactly_the_three() -> None:
    assert ECON_TRANSFORMS == ("level", "change", "pct_change")
    assert ECON_TRANSFORMS == get_args(EconTransform)
    assert ECON_RELEASE_SERIES_COLUMNS == (
        "release_id",
        "series_id",
        "transform",
        "scale",
        "unit_label",
        "decimals",
        "frequency",
    )


def test_frequencies_are_exactly_the_two_the_fetch_knows() -> None:
    # One Literal: the seed's frequency is what sets the fetch's lookback.
    assert ECON_FREQUENCIES == ("monthly", "quarterly")
    assert ECON_FREQUENCIES == get_args(EconFrequency)
    assert get_args(VintageFrequency) == get_args(EconFrequency)
    assert set(VINTAGE_LOOKBACK_MONTHS) == set(ECON_FREQUENCIES)


def test_shipped_quarterly_rows_are_exactly_eci_and_gdp() -> None:
    rows = load_econ_release_seeds().series.rows
    quarterly = {(row.release_id, row.series_id) for row in rows if row.frequency == "quarterly"}
    assert quarterly == {(11, "ECIALLCIV"), (53, "A191RL1Q225SBEA")}
    monthly = [row for row in rows if row.frequency == "monthly"]
    assert len(monthly) == 13
    assert len(monthly) + len(quarterly) == len(rows)


# --- the baseline every refusal starts from -----------------------------------


def test_baseline_pair_parses() -> None:
    seeds = parse_econ_release_seeds(_BASE_TIMES, _BASE_SERIES)
    assert [row.release_id for row in seeds.times.rows] == [10, 51, 188]
    assert seeds.series.rows == (
        EconReleaseSeries(10, "CPIAUCSL", "pct_change", Decimal("1"), "% m/m", 1, "monthly"),
        EconReleaseSeries(51, "BOPGSTB", "level", Decimal("0.001"), "$B", 1, "monthly"),
        EconReleaseSeries(188, "IR", "pct_change", Decimal("1"), "% m/m, NSA", 1, "monthly"),
    )
    assert all(row.frequency == "monthly" for row in seeds.series.rows)
    assert seeds.series.by_id(999) is None


def test_series_rows_need_not_follow_the_times_order() -> None:
    reordered = _text(_SERIES_HEADER, list(reversed(_BASE_SERIES_ROWS)))
    seeds = parse_econ_release_seeds(_BASE_TIMES, reordered)
    assert [row.release_id for row in seeds.series.rows] == [188, 51, 10]


def test_scale_and_decimals_boundaries_permit() -> None:
    # decimals 0 is the floor and is allowed (PAYEMS); a tiny scale is allowed.
    seeds = parse_econ_release_seeds(
        _BASE_TIMES, _series_with(1, "51,BOPGSTB,level,0.000001,$B,0,monthly")
    )
    bop = seeds.series.by_id(51)
    assert bop is not None
    assert bop.scale == Decimal("0.000001") and bop.decimals == 0


# --- joint refusals: the set of release ids -----------------------------------


def test_missing_series_row_refuses_the_pair() -> None:
    series = _text(_SERIES_HEADER, [_BASE_SERIES_ROWS[0], _BASE_SERIES_ROWS[2]])
    with pytest.raises(SeedError, match=r"times\.csv, line 3: release_id 51 .*no row"):
        _parse(series=series)


def test_extra_series_row_refuses_the_pair() -> None:
    series = _text(_SERIES_HEADER, [*_BASE_SERIES_ROWS, "999,FOO,level,1,K,0,monthly"])
    with pytest.raises(SeedError, match=r"series\.csv, line 5: release_id 999 .*not in"):
        _parse(series=series)


def test_release_added_to_times_without_a_series_row_refuses_the_pair() -> None:
    times = _text(
        _TIMES_HEADER, [*_BASE_TIMES_ROWS, f"50,Employment Situation,08:30,{_URL},{_URL},"]
    )
    with pytest.raises(SeedError, match=r"times\.csv, line 5: release_id 50 .*no row"):
        _parse(times=times)


def test_duplicate_series_row_refuses_the_pair() -> None:
    series = _text(_SERIES_HEADER, [*_BASE_SERIES_ROWS, "10,CPIAUCSL,pct_change,1,% m/m,1,monthly"])
    with pytest.raises(SeedError, match=r"series\.csv, line 5: release_id 10 appears twice"):
        _parse(series=series)


def test_duplicate_with_a_different_series_still_refuses() -> None:
    series = _text(_SERIES_HEADER, [*_BASE_SERIES_ROWS, "10,CPILFESL,pct_change,1,% m/m,1,monthly"])
    with pytest.raises(SeedError, match="twice"):
        _parse(series=series)


# --- per-row refusals ---------------------------------------------------------


@pytest.mark.parametrize("transform", ["yoy", "Level", "pct", "", " level"])
def test_unknown_transform_refuses_the_pair(transform: str) -> None:
    with pytest.raises(SeedError, match=r"series\.csv, line 2: transform"):
        _parse(series=_series_with(0, f"10,CPIAUCSL,{transform},1,% m/m,1,monthly"))


@pytest.mark.parametrize(
    "scale", ["abc", "0", "-1", "NaN", "Infinity", "-0", "0.000", "1e-3", "", " 1", "1_000"]
)
def test_unparseable_scale_refuses_the_pair(scale: str) -> None:
    with pytest.raises(SeedError, match=r"series\.csv, line 3: scale"):
        _parse(series=_series_with(1, f"51,BOPGSTB,level,{scale},$B,1,monthly"))


@pytest.mark.parametrize("decimals", ["-1", "1.5", "", "x", "01", " 1"])
def test_bad_decimals_refuses_the_pair(decimals: str) -> None:
    with pytest.raises(SeedError, match=r"series\.csv, line 4: decimals"):
        _parse(series=_series_with(2, f'188,IR,pct_change,1,"% m/m, NSA",{decimals},monthly'))


def test_decimals_of_four_permits() -> None:
    # The ceiling: R7 stores the figure quantized to 4 places, so 4 is the
    # most precision display can honestly show.
    seeds = parse_econ_release_seeds(
        _BASE_TIMES, _series_with(2, '188,IR,pct_change,1,"% m/m, NSA",4,monthly')
    )
    ir = seeds.series.by_id(188)
    assert ir is not None and ir.decimals == 4


@pytest.mark.parametrize("decimals", ["5", "12"])
def test_decimals_above_four_refuses_the_pair(decimals: str) -> None:
    # Display cannot show precision that was never stored.
    with pytest.raises(SeedError, match=r"series\.csv, line 4: decimals .* at most 4"):
        _parse(series=_series_with(2, f'188,IR,pct_change,1,"% m/m, NSA",{decimals},monthly'))


def test_quarterly_frequency_permits() -> None:
    seeds = parse_econ_release_seeds(
        _BASE_TIMES, _series_with(0, "10,CPIAUCSL,pct_change,1,% q/q,1,quarterly")
    )
    cpi = seeds.series.by_id(10)
    assert cpi is not None and cpi.frequency == "quarterly"
    bop = seeds.series.by_id(51)
    assert bop is not None and bop.frequency == "monthly"


@pytest.mark.parametrize(
    "frequency",
    [
        "weekly",
        "annual",
        "Monthly",
        "QUARTERLY",
        "q",
        "",
        " ",
        " monthly",
        "monthly ",
        "\tquarterly",
    ],
)
def test_unknown_frequency_refuses_the_pair(frequency: str) -> None:
    # As for transform: the fetch's lookback and the figure's period check
    # both come from this value, so a guess is not an option.
    with pytest.raises(SeedError, match=r"series\.csv, line 2: frequency"):
        _parse(series=_series_with(0, f"10,CPIAUCSL,pct_change,1,% m/m,1,{frequency}"))


def test_a_series_file_without_the_frequency_column_refuses_the_pair() -> None:
    # The file as it stood before the column: refused at its header, not
    # read with a frequency guessed for every row.
    old_header = "release_id,series_id,transform,scale,unit_label,decimals"
    old_rows = [row.rsplit(",", 1)[0] for row in _BASE_SERIES_ROWS]
    assert old_rows[0] == "10,CPIAUCSL,pct_change,1,% m/m,1"
    with pytest.raises(SeedError, match=r"series\.csv, line 1: .*frequency"):
        _parse(series=_text(old_header, old_rows))


@pytest.mark.parametrize("label", [" % m/m", "% m/m ", "\t% m/m"])
def test_unit_label_with_surrounding_whitespace_refuses_the_pair(label: str) -> None:
    # Consistent with " 1" being refused for scale: the file's text is the value.
    with pytest.raises(SeedError, match=r"series\.csv, line 2: unit_label .*whitespace"):
        _parse(series=_series_with(0, f'10,CPIAUCSL,pct_change,1,"{label}",1,monthly'))


@pytest.mark.parametrize(
    ("row", "match"),
    [
        ("x,CPIAUCSL,pct_change,1,% m/m,1,monthly", "release_id"),
        ("0,CPIAUCSL,pct_change,1,% m/m,1,monthly", "release_id"),
        ("10,,pct_change,1,% m/m,1,monthly", "series_id"),
        ("10,CPI AUCSL,pct_change,1,% m/m,1,monthly", "series_id"),
        ("10,cpiaucsl,pct_change,1,% m/m,1,monthly", "series_id"),
        ("10,CPIAUCSL,pct_change,1,,1,monthly", "unit_label"),
        ("10,CPIAUCSL,pct_change,1, ,1,monthly", "unit_label"),
        ("10,CPIAUCSL,pct_change,1,% m/m", "fields"),
        # A row without its frequency, as every row was before the column.
        ("10,CPIAUCSL,pct_change,1,% m/m,1", "fields"),
        # An unquoted comma in a unit label splits it into two fields.
        ("10,CPIAUCSL,pct_change,1,% m/m, NSA,1,monthly", "fields"),
    ],
)
def test_one_malformed_series_row_refuses_the_pair(row: str, match: str) -> None:
    with pytest.raises(SeedError, match=rf"series\.csv, line 2: .*{match}"):
        _parse(series=_series_with(0, row))


def test_wrong_series_header_refuses_the_pair() -> None:
    series = _BASE_SERIES.replace(_SERIES_HEADER, "release_id,series_id,transform,scale")
    with pytest.raises(SeedError, match=r"series\.csv, line 1"):
        _parse(series=series)


def test_empty_series_file_refuses_the_pair() -> None:
    with pytest.raises(SeedError, match="empty"):
        _parse(series="")


def test_bad_times_row_refuses_the_pair() -> None:
    # The other direction: a series file that is fine on its own is not
    # returned when its partner is broken.
    times = _BASE_TIMES.replace("188,U.S. Import", "x,U.S. Import")
    with pytest.raises(SeedError, match=r"times\.csv, line 4: release_id"):
        _parse(times=times)


# --- "refuses both", through the loaders --------------------------------------


def _write_pair(tmp_path: Path, times: str, series: str) -> tuple[Path, Path]:
    times_path = tmp_path / "econ_release_times.csv"
    series_path = tmp_path / "econ_release_series.csv"
    times_path.write_text(times, encoding="utf-8")
    series_path.write_text(series, encoding="utf-8")
    return times_path, series_path


def test_loaders_accept_the_baseline_pair(tmp_path: Path) -> None:
    times_path, series_path = _write_pair(tmp_path, _BASE_TIMES, _BASE_SERIES)
    seeds = load_econ_release_seeds(times_path, series_path)
    assert len(seeds.series.rows) == 3
    assert load_econ_release_times(times_path, series_path) == seeds.times


@pytest.mark.parametrize(
    "series",
    [
        _text(_SERIES_HEADER, _BASE_SERIES_ROWS[:2]),  # missing
        _text(_SERIES_HEADER, [*_BASE_SERIES_ROWS, "999,FOO,level,1,K,0,monthly"]),  # extra
        _text(_SERIES_HEADER, [*_BASE_SERIES_ROWS, _BASE_SERIES_ROWS[0]]),  # duplicate
        _series_with(0, "10,CPIAUCSL,yoy,1,% m/m,1,monthly"),  # unknown transform
        _series_with(1, "51,BOPGSTB,level,abc,$B,1,monthly"),  # unparseable scale
        _series_with(2, '188,IR,pct_change,1,"% m/m, NSA",1,weekly'),  # unknown frequency
    ],
    ids=["missing", "extra", "duplicate", "transform", "scale", "frequency"],
)
def test_a_bad_series_seed_refuses_the_times_loader_too(tmp_path: Path, series: str) -> None:
    times_path, series_path = _write_pair(tmp_path, _BASE_TIMES, series)
    with pytest.raises(SeedError, match=r"econ_release_series\.csv|econ_release_times\.csv"):
        load_econ_release_seeds(times_path, series_path)
    with pytest.raises(SeedError, match=r"econ_release_series\.csv|econ_release_times\.csv"):
        load_econ_release_times(times_path, series_path)


def test_a_missing_series_file_refuses_the_times_loader(tmp_path: Path) -> None:
    times_path, series_path = _write_pair(tmp_path, _BASE_TIMES, _BASE_SERIES)
    series_path.unlink()
    with pytest.raises(SeedError, match="missing"):
        load_econ_release_times(times_path, series_path)


def test_a_series_file_with_a_byte_order_mark_loads(tmp_path: Path) -> None:
    # Excel's "CSV UTF-8" prepends one; the times loader already tolerates it.
    times_path, series_path = _write_pair(tmp_path, _BASE_TIMES, _BASE_SERIES)
    series_path.write_text(_BASE_SERIES, encoding="utf-8-sig")
    assert len(load_econ_release_seeds(times_path, series_path).series.rows) == 3
