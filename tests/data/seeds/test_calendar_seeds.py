"""The calendar's committed seeds: central-bank decision dates and release times.

Spec decision 8 (one central-bank seed per year, a missing year said aloud on
the panel) and Q3 (a hand-kept per-release ET time table beside FRED's dates).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from corollary.data.seeds import SeedError
from corollary.data.seeds.calendar_seeds import (
    Bank,
    Coverage,
    GapKind,
    banks_without_seed,
    load_central_bank_seed,
    load_econ_release_times,
    parse_central_bank_seed,
    parse_econ_release_times,
    seed_gaps,
)

ET = ZoneInfo("America/New_York")

_COVERAGE_HEADER = "bank,coverage,covers_from,retrieved,date_source_url,time_source_url,note"
_EVENT_HEADER = "bank,meeting_start,date,local_time,timezone,source_url,note"
_URL = "https://example.invalid/schedule"


def _seed_text(
    *,
    year: int = 2027,
    coverage: list[str] | None = None,
    events: list[str] | None = None,
) -> str:
    """A small, valid central-bank seed; tests override one part to break it."""
    if coverage is None:
        coverage = [
            f"FOMC,full,,2026-10-10,{_URL},{_URL},",
            f"ECB,unpublished,,2026-10-10,{_URL},,ECB has not published {year} dates",
            f"BOE,full,,2026-10-10,{_URL},,",
            f"BOJ,full,,2026-10-10,{_URL},,",
        ]
    if events is None:
        events = [
            f"FOMC,{year}-01-26,{year}-01-27,14:00,America/New_York,{_URL},",
            f"BOE,{year}-02-04,{year}-02-04,,Europe/London,{_URL},",
            f"BOJ,{year}-01-21,{year}-01-22,,Asia/Tokyo,{_URL},",
        ]
    lines = [f"year,{year}", _COVERAGE_HEADER, *coverage, _EVENT_HEADER, *events]
    return "\n".join(lines) + "\n"


# --- the committed files ----------------------------------------------------


@pytest.mark.parametrize("year", [2026, 2027])
def test_committed_central_bank_seeds_parse(year: int) -> None:
    seed = load_central_bank_seed(year)
    assert seed is not None
    assert seed.year == year
    # Every bank is accounted for in the coverage block -- none silently absent.
    assert {c.bank for c in seed.coverage} == set(Bank)
    assert all(d.date.year == year for d in seed.decisions)
    assert all(d.source_url.startswith("https://") for d in seed.decisions)


def test_committed_fomc_rows_carry_14_00_et() -> None:
    for year in (2026, 2027):
        seed = load_central_bank_seed(year)
        assert seed is not None
        fomc = [d for d in seed.decisions if d.bank is Bank.FOMC]
        assert len(fomc) == 8
        for decision in fomc:
            assert decision.at is not None
            at_et = decision.at.astimezone(ET)
            assert at_et.time() == time(14, 0)
            assert at_et.date() == decision.date


def test_committed_fomc_2026_dates_are_the_feds() -> None:
    seed = load_central_bank_seed(2026)
    assert seed is not None
    assert [d.date for d in seed.decisions if d.bank is Bank.FOMC] == [
        date(2026, 1, 28),
        date(2026, 3, 18),
        date(2026, 4, 29),
        date(2026, 6, 17),
        date(2026, 7, 29),
        date(2026, 9, 16),
        date(2026, 10, 28),
        date(2026, 12, 9),
    ]


def test_committed_ecb_2026_is_partial_and_says_from_when() -> None:
    gaps = seed_gaps(2026)
    ecb = [g for g in gaps if g.bank is Bank.ECB]
    assert len(ecb) == 1
    assert ecb[0].kind is GapKind.PARTIAL
    assert ecb[0].covers_from == date(2026, 10, 10)
    # Partial coverage is a gap to report, but the bank does have a seed.
    assert Bank.ECB not in {g.bank for g in banks_without_seed(2026)}


def test_committed_2027_has_every_bank() -> None:
    assert banks_without_seed(2027) == ()


def test_date_only_banks_carry_no_instant() -> None:
    seed = load_central_bank_seed(2026)
    assert seed is not None
    boj = [d for d in seed.decisions if d.bank is Bank.BOJ]
    assert boj and all(d.at is None for d in boj)
    assert all(d.local_time is None for d in boj)


def test_committed_econ_release_times_parse() -> None:
    times = load_econ_release_times()
    assert 10 <= len(times.rows) <= 20
    cpi = times.by_id(10)
    assert cpi is not None
    assert cpi.release_name == "Consumer Price Index"
    assert cpi.et_time == time(8, 30)
    assert times.by_id(50) is not None and times.by_id(50).et_time == time(8, 30)  # type: ignore[union-attr]
    assert all(row.time_source_url.startswith("https://") for row in times.rows)


# --- ET conversion across DST boundaries ------------------------------------


@pytest.mark.parametrize(
    ("day", "expected_et", "expected_utc"),
    [
        # 18 Mar 2027: the US is on EDT (from 14 Mar), the EU still on CET
        # (until 28 Mar). 14:15 CET = 13:15Z = 09:15 EDT -- an hour later in
        # ET than the usual 08:15.
        (date(2027, 3, 18), time(9, 15), time(13, 15)),
        # 29 Oct 2026: the EU is back on CET (25 Oct), the US still on EDT
        # (until 1 Nov). Same one-hour shift.
        (date(2026, 10, 29), time(9, 15), time(13, 15)),
        # 10 Jun 2027: both on summer time. 14:15 CEST = 12:15Z = 08:15 EDT.
        (date(2027, 6, 10), time(8, 15), time(12, 15)),
        # 17 Dec 2026: both on winter time. 14:15 CET = 13:15Z = 08:15 EST.
        (date(2026, 12, 17), time(8, 15), time(13, 15)),
    ],
)
def test_ecb_conversion_across_dst_boundaries(day: date, expected_et: time, expected_utc: time) -> None:
    seed = load_central_bank_seed(day.year)
    assert seed is not None
    [decision] = [d for d in seed.decisions if d.bank is Bank.ECB and d.date == day]
    assert decision.local_time == time(14, 15)
    assert decision.at is not None
    assert decision.at.tzinfo is UTC
    assert decision.at.time() == expected_utc
    assert decision.at.astimezone(ET).time() == expected_et


def test_bank_local_time_converts_through_zoneinfo() -> None:
    # A London noon in the US-EDT/UK-GMT gap (US DST from 8 Mar 2026, UK from
    # 29 Mar 2026): 12:00 GMT = 12:00Z = 08:00 EDT.
    text = _seed_text(
        year=2026,
        coverage=[
            f"FOMC,unpublished,,2026-10-10,{_URL},,not under test",
            f"ECB,unpublished,,2026-10-10,{_URL},,not under test",
            f"BOE,full,,2026-10-10,{_URL},{_URL},",
            f"BOJ,unpublished,,2026-10-10,{_URL},,not under test",
        ],
        events=[f"BOE,2026-03-19,2026-03-19,12:00,Europe/London,{_URL},"],
    )
    seed = parse_central_bank_seed(text)
    [decision] = seed.decisions
    assert decision.at == datetime(2026, 3, 19, 12, 0, tzinfo=UTC)
    assert decision.at_et() == datetime(2026, 3, 19, 8, 0, tzinfo=ET)


def test_econ_release_instant_is_et_on_the_given_day() -> None:
    times = parse_econ_release_times(
        "release_id,release_name,et_time,fred_url,time_source_url,note\n"
        f"10,Consumer Price Index,08:30,{_URL},{_URL},\n"
    )
    # 08:30 EST in January is 13:30Z; 08:30 EDT in July is 12:30Z.
    assert times.at(10, date(2027, 1, 13)) == datetime(2027, 1, 13, 13, 30, tzinfo=UTC)
    assert times.at(10, date(2027, 7, 14)) == datetime(2027, 7, 14, 12, 30, tzinfo=UTC)
    assert times.at(999, date(2027, 7, 14)) is None


# --- missing banks and missing years are reported, not empty ----------------


def test_unpublished_bank_is_reported() -> None:
    seed = parse_central_bank_seed(_seed_text())
    gaps = seed.gaps()
    assert [(g.bank, g.kind) for g in gaps] == [(Bank.ECB, GapKind.UNPUBLISHED)]
    assert "ECB has not published 2027 dates" in gaps[0].reason
    assert not any(d.bank is Bank.ECB for d in seed.decisions)


def test_year_with_no_file_reports_every_bank(tmp_path: Path) -> None:
    assert load_central_bank_seed(2028, directory=tmp_path) is None
    gaps = banks_without_seed(2028, directory=tmp_path)
    assert [g.bank for g in gaps] == list(Bank)
    assert all(g.kind is GapKind.NO_FILE for g in gaps)
    assert all("2028" in g.reason for g in gaps)


def test_banks_without_seed_reads_unpublished_from_file(tmp_path: Path) -> None:
    (tmp_path / "central_banks_2027.csv").write_text(_seed_text(), encoding="utf-8")
    gaps = banks_without_seed(2027, directory=tmp_path)
    assert [(g.bank, g.kind) for g in gaps] == [(Bank.ECB, GapKind.UNPUBLISHED)]


# --- a malformed row rejects the whole file ---------------------------------


@pytest.mark.parametrize(
    ("coverage", "events", "match"),
    [
        # A bank missing from the coverage block is a silent absence.
        (
            [
                f"FOMC,full,,2026-10-10,{_URL},{_URL},",
                f"BOE,full,,2026-10-10,{_URL},,",
                f"BOJ,full,,2026-10-10,{_URL},,",
            ],
            None,
            "ECB",
        ),
        # A bank listed twice in coverage.
        (
            [
                f"FOMC,full,,2026-10-10,{_URL},{_URL},",
                f"FOMC,full,,2026-10-10,{_URL},{_URL},",
                f"ECB,full,,2026-10-10,{_URL},,",
                f"BOE,full,,2026-10-10,{_URL},,",
                f"BOJ,full,,2026-10-10,{_URL},,",
            ],
            None,
            "twice",
        ),
        # An unknown coverage word.
        (
            [
                f"FOMC,mostly,,2026-10-10,{_URL},{_URL},",
                f"ECB,full,,2026-10-10,{_URL},,",
                f"BOE,full,,2026-10-10,{_URL},,",
                f"BOJ,full,,2026-10-10,{_URL},,",
            ],
            None,
            "coverage",
        ),
        # "from" needs a covers_from date.
        (
            [
                f"FOMC,from,,2026-10-10,{_URL},{_URL},",
                f"ECB,full,,2026-10-10,{_URL},,",
                f"BOE,full,,2026-10-10,{_URL},,",
                f"BOJ,full,,2026-10-10,{_URL},,",
            ],
            None,
            "covers_from",
        ),
        # An unpublished bank must say why.
        (
            [
                f"FOMC,full,,2026-10-10,{_URL},{_URL},",
                f"ECB,unpublished,,2026-10-10,{_URL},,",
                f"BOE,full,,2026-10-10,{_URL},,",
                f"BOJ,full,,2026-10-10,{_URL},,",
            ],
            None,
            "note",
        ),
    ],
)
def test_malformed_coverage_rejects_the_file(
    coverage: list[str], events: list[str] | None, match: str
) -> None:
    with pytest.raises(SeedError, match=match):
        parse_central_bank_seed(_seed_text(coverage=coverage, events=events))


_GOOD_FOMC = f"FOMC,2027-01-26,2027-01-27,14:00,America/New_York,{_URL},"


@pytest.mark.parametrize(
    ("bad_row", "match"),
    [
        (f"FOMC,2027-01-26,2027-02-30,14:00,America/New_York,{_URL},", "date"),
        (f"FOMC,2027-01-26,2027-01-27,2pm,America/New_York,{_URL},", "time"),
        (f"FOMC,2027-01-26,2027-01-27,25:00,America/New_York,{_URL},", "time"),
        (f"FOMC,2027-01-26,2027-01-27,14:00,US/Eastern-ish,{_URL},", "timezone"),
        # The right zone for the bank, never another one.
        (f"FOMC,2027-01-26,2027-01-27,14:00,Europe/London,{_URL},", "timezone"),
        (f"FED,2027-01-26,2027-01-27,14:00,America/New_York,{_URL},", "bank"),
        (f"FOMC,2027-01-26,2026-12-09,14:00,America/New_York,{_URL},", "2027"),
        (f"FOMC,2027-01-28,2027-01-27,14:00,America/New_York,{_URL},", "meeting_start"),
        ("FOMC,2027-01-26,2027-01-27,14:00,America/New_York,,", "source_url"),
        (f"FOMC,2027-01-26,2027-01-27,14:00,America/New_York,{_URL}", "fields"),
        # Rows for a bank whose coverage says it has not published.
        (f"ECB,2027-02-03,2027-02-04,14:15,Europe/Berlin,{_URL},", "unpublished"),
        # The same decision twice.
        (_GOOD_FOMC, "twice"),
    ],
)
def test_one_malformed_event_rejects_the_file(bad_row: str, match: str) -> None:
    events = [
        _GOOD_FOMC,
        f"BOE,2027-02-04,2027-02-04,,Europe/London,{_URL},",
        bad_row,
    ]
    with pytest.raises(SeedError, match=match):
        parse_central_bank_seed(_seed_text(events=events), source="seed.csv")


@pytest.mark.parametrize("coverage_word", ["full", "from"])
def test_covered_bank_with_no_decision_rows_rejects_the_file(coverage_word: str) -> None:
    # Audit finding 1: BoJ stated as covered, every BoJ row lost (a bad merge,
    # a hand edit). Accepted, the panel would go quietly empty for BoJ with no
    # gap reported -- exactly what decision 8 forbids.
    covers_from = "2027-01-01" if coverage_word == "from" else ""
    note = "partial" if coverage_word == "from" else ""
    coverage = [
        f"FOMC,full,,2026-10-10,{_URL},{_URL},",
        f"ECB,unpublished,,2026-10-10,{_URL},,ECB has not published 2027 dates",
        f"BOE,full,,2026-10-10,{_URL},,",
        f"BOJ,{coverage_word},{covers_from},2026-10-10,{_URL},,{note}",
    ]
    events = [
        f"FOMC,2027-01-26,2027-01-27,14:00,America/New_York,{_URL},",
        f"BOE,2027-02-04,2027-02-04,,Europe/London,{_URL},",
    ]
    with pytest.raises(SeedError, match=r"BOJ.*no decision rows"):
        parse_central_bank_seed(_seed_text(coverage=coverage, events=events), source="seed.csv")


def test_timed_row_needs_the_banks_time_source() -> None:
    # Audit finding 2: a displayed announcement time with no cited source.
    # BoE's coverage row has an empty time_source_url.
    events = [
        f"FOMC,2027-01-26,2027-01-27,14:00,America/New_York,{_URL},",
        f"BOE,2027-02-04,2027-02-04,07:00,Europe/London,{_URL},",
        f"BOJ,2027-01-21,2027-01-22,,Asia/Tokyo,{_URL},",
    ]
    with pytest.raises(SeedError, match=r"BOE.*time_source_url"):
        parse_central_bank_seed(_seed_text(events=events))


def test_untimed_row_on_a_bank_that_cites_a_time_source_rejects_the_file() -> None:
    # The converse: FOMC's coverage cites a 14:00 source, so a blank time is a
    # dropped cell, not a date-only decision.
    events = [
        f"FOMC,2027-01-26,2027-01-27,,America/New_York,{_URL},",
        f"BOE,2027-02-04,2027-02-04,,Europe/London,{_URL},",
        f"BOJ,2027-01-21,2027-01-22,,Asia/Tokyo,{_URL},",
    ]
    with pytest.raises(SeedError, match=r"FOMC.*local_time"):
        parse_central_bank_seed(_seed_text(events=events))


def test_committed_rows_are_timed_exactly_when_the_bank_cites_a_time_source() -> None:
    for year in (2026, 2027):
        seed = load_central_bank_seed(year)
        assert seed is not None
        for decision in seed.decisions:
            cited = seed.coverage_of(decision.bank).time_source_url is not None
            assert (decision.local_time is not None) is cited, decision


def test_event_before_covers_from_rejects_the_file() -> None:
    coverage = [
        f"FOMC,full,,2026-10-10,{_URL},{_URL},",
        f"ECB,from,2027-06-01,2026-10-10,{_URL},{_URL},partial",
        f"BOE,full,,2026-10-10,{_URL},,",
        f"BOJ,full,,2026-10-10,{_URL},,",
    ]
    events = [f"ECB,2027-03-17,2027-03-18,14:15,Europe/Berlin,{_URL},"]
    with pytest.raises(SeedError, match="covers_from"):
        parse_central_bank_seed(_seed_text(coverage=coverage, events=events))


def test_nonexistent_local_time_rejects_the_file() -> None:
    # 02:30 on 14 Mar 2027 does not exist in New York (clocks jump 02:00 -> 03:00).
    events = [f"FOMC,2027-03-13,2027-03-14,02:30,America/New_York,{_URL},"]
    with pytest.raises(SeedError, match="does not exist"):
        parse_central_bank_seed(_seed_text(events=events))


def test_malformed_header_rejects_the_file() -> None:
    text = _seed_text().replace("year,2027", "year,twenty")
    with pytest.raises(SeedError, match="year"):
        parse_central_bank_seed(text)
    text = _seed_text().replace(_EVENT_HEADER, "bank,date")
    with pytest.raises(SeedError):
        parse_central_bank_seed(text)


def test_file_year_must_match_the_requested_year(tmp_path: Path) -> None:
    (tmp_path / "central_banks_2028.csv").write_text(_seed_text(year=2027), encoding="utf-8")
    with pytest.raises(SeedError, match="2028"):
        load_central_bank_seed(2028, directory=tmp_path)


@pytest.mark.parametrize(
    ("bad_row", "match"),
    [
        (f"x,Consumer Price Index,08:30,{_URL},{_URL},", "release_id"),
        (f"10,,08:30,{_URL},{_URL},", "release_name"),
        (f"10,Consumer Price Index,8.30,{_URL},{_URL},", "time"),
        (f"10,Consumer Price Index,08:30,{_URL},,", "time_source_url"),
        (f"10,Consumer Price Index,08:30,{_URL},{_URL}", "fields"),
        (f"50,Employment Situation,08:30,{_URL},{_URL},", "twice"),
    ],
)
def test_one_malformed_release_row_rejects_the_file(bad_row: str, match: str) -> None:
    text = (
        "release_id,release_name,et_time,fred_url,time_source_url,note\n"
        f"50,Employment Situation,08:30,{_URL},{_URL},\n"
        f"{bad_row}\n"
    )
    with pytest.raises(SeedError, match=match):
        parse_econ_release_times(text)


def test_g17_annual_revision_limitation_is_stated() -> None:
    # Audit finding 3: the Fed issues G.17's annual revision at noon, not
    # 09:15, and FRED's release dates do not say which date is the revision.
    # The table cannot know, so it must say so rather than encode a guess.
    times = load_econ_release_times()
    g17 = times.by_id(13)
    assert g17 is not None
    assert g17.et_time == time(9, 15)
    assert "annual revision" in g17.note and "noon" in g17.note
    assert "not distinguished" in g17.note
    # Every day gets the monthly time -- the limitation, stated, not hidden.
    assert times.at(13, date(2027, 3, 17)) == datetime(2027, 3, 17, 13, 15, tzinfo=UTC)


def test_coverage_enum_values_are_the_file_words() -> None:
    assert {c.value for c in Coverage} == {"full", "from", "unpublished"}
