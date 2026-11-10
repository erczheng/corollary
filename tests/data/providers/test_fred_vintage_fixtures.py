"""The U6a vintage fixtures parse, and carry no key.

``tests/fixtures/record_fred_vintages.py`` records them by hand, keyed, once.
This file replays nothing over HTTP: it reads the files and checks two things
about every one of them --

* **It parses.** Every 200 from ``/series/observations`` goes through
  :func:`corollary.data.providers.fred.parse_observations`, the parser the
  engine itself uses, so a value FRED sent that the engine could not read
  fails here rather than in U6c. ``count`` must equal the rows returned: the
  recorder takes one page, and a truncated vintage would be a partial answer
  read as a whole one.
* **It carries no key.** No ``api_key=`` with a value other than the
  ``<redacted>`` marker, and no ``api_key`` in the recorded params.

It also pins the one vendor fact U6a exists to settle, as data rather than as
prose, so U6c builds on a test rather than on a note: what each observation's
``realtime_start`` says in an ``output_type=1`` vintage. The reading, recorded
2026-11-09:

* FRED **clips** ``realtime_start`` and ``realtime_end`` to the requested
  window. A single-day vintage (``realtime_start = realtime_end = D``) stamps
  every row ``D``, so it cannot say which value is new.
* A two-day window ``D-1 .. D`` can. A value valid on both days is one row
  spanning both; a value new or revised on ``D`` starts on ``D``; a revised one
  also leaves its old value in a row ending on ``D-1``.
* ``EXHOSLUSM495S`` (NAR, release 291) is not in ALFRED. Every dated query is
  a 400 with FRED's own message, and only its current values are served.
"""

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest

from corollary.data.providers.fred import MISSING_VALUE, parse_observations
from tests.fixtures.record_fred_vintages import KNOWN_RELEASE_DAYS

FIXTURE_DIR: Final = Path(__file__).resolve().parents[2] / "fixtures" / "fred"
PREFIX: Final = "p3_vintage_"
ET: Final = ZoneInfo("America/New_York")

#: The four series U6's pre-flight names, by release.
SERIES: Final = {
    "cpiaucsl": "CPIAUCSL",
    "payems": "PAYEMS",
    "bopgstb": "BOPGSTB",
    "exhoslusm495s": "EXHOSLUSM495S",
}

#: The four observation vintages recorded per series.
VINTAGE_LABELS: Final = ("release_day", "day_before", "window", "since_prior")

#: The three series FRED answered for. ``EXHOSLUSM495S`` is asserted
#: separately, because its answer is the finding.
ANSWERED: Final = ("cpiaucsl", "payems", "bopgstb")

#: What ``tests/fixtures/record_fred_vintages.py`` records only for a release
#: whose ``/release/dates`` came back short -- on 2026-11-09, release 291 alone.
FALLBACK_LABELS: Final = (
    "release_dates_all",
    "release_dates_dated",
    "series",
    "vintagedates_recent",
    "releases_dates_window",
    "releases_dates_forward",
    "current",
)

#: FRED's refusal of every vintage query on ``EXHOSLUSM495S``, verbatim.
NOT_IN_ALFRED: Final = (
    "Bad Request.  The series does not exist in ALFRED but may exist in FRED.  "
    "Try setting realtime_start and realtime_end to today's date or removing the "
    "realtime_start and realtime_end variables."
)

#: ``api_key=`` followed by anything but the redaction marker.
_UNREDACTED_KEY: Final = re.compile(r"(?i)api_key=(?!<redacted>)")


def fixture_paths() -> list[Path]:
    return sorted(FIXTURE_DIR.glob(f"{PREFIX}*.json"))


def observation_200_paths() -> list[Path]:
    """The fixtures holding a 200 from ``/series/observations``."""
    chosen = []
    for path in fixture_paths():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if loaded["path"] == "/series/observations" and loaded["status_code"] == 200:
            chosen.append(path)
    return chosen


def envelope(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    )
    return loaded


def observations_of(sid: str, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = envelope(f"{PREFIX}{sid}_{label}")["body"]["observations"]
    return rows


def test_every_series_has_its_full_set_of_fixtures() -> None:
    names = {path.stem for path in fixture_paths()}
    expected = {
        f"{PREFIX}{sid}_{label}"
        for sid in SERIES
        for label in (*VINTAGE_LABELS, "release_dates", "vintagedates")
    } | {f"{PREFIX}exhoslusm495s_{label}" for label in FALLBACK_LABELS}
    missing = sorted(expected - names)
    assert not missing, f"fixtures not recorded: {missing}"
    unexpected = sorted(names - expected)
    assert not unexpected, f"fixtures nothing here checks by name: {unexpected}"


@pytest.mark.parametrize("sid", SERIES)
def test_every_series_set_is_from_one_release_cycle(sid: str) -> None:
    """A set mixed across two recordings fails here, not in U6c.

    The recorder writes file by file, so an ``--overwrite`` run that aborts
    part-way leaves new date sources beside old vintages. Each series' ``D``
    must therefore be the latest past date its own ``release_dates`` lists --
    or, for a release FRED lists no dates for (291), the recorder's
    ``KNOWN_RELEASE_DAYS`` entry -- and a date its ``vintagedates`` lists
    whenever FRED answered that one. The four vintages must be the windows the
    recorder derives from that one ``D``.
    """
    dated = envelope(f"{PREFIX}{sid}_release_dates")
    recorded_on = datetime.fromisoformat(dated["recorded_at"]).astimezone(ET).date()
    listed = sorted(
        d
        for d in (date.fromisoformat(r["date"]) for r in dated["body"]["release_dates"])
        if d < recorded_on
    )
    release_day = listed[-1] if listed else KNOWN_RELEASE_DAYS[SERIES[sid]]

    windows = {
        label: tuple(
            date.fromisoformat(envelope(f"{PREFIX}{sid}_{label}")["params"][key])
            for key in ("realtime_start", "realtime_end")
        )
        for label in VINTAGE_LABELS
    }
    day_before = release_day - timedelta(days=1)
    assert windows["release_day"] == (release_day, release_day)
    assert windows["day_before"] == (day_before, day_before)
    assert windows["window"] == (day_before, release_day)
    assert windows["since_prior"][1] == release_day

    vintages = envelope(f"{PREFIX}{sid}_vintagedates")
    if vintages["status_code"] == 200:
        assert release_day.isoformat() in vintages["body"]["vintage_dates"]


@pytest.mark.parametrize("path", fixture_paths(), ids=lambda p: p.stem)
def test_no_fixture_carries_a_key(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not _UNREDACTED_KEY.search(text), f"{path.name} carries an api_key value"
    params = json.loads(text)["params"]
    assert "api_key" not in {k.lower() for k in params}


def test_every_answered_vintage_is_a_200_observations_fixture() -> None:
    """The floor under the parametrization below: it cannot shrink to nothing."""
    names = {path.stem for path in observation_200_paths()}
    expected = {f"{PREFIX}{sid}_{label}" for sid in ANSWERED for label in VINTAGE_LABELS}
    assert names == expected | {f"{PREFIX}exhoslusm495s_current"}


@pytest.mark.parametrize("path", observation_200_paths(), ids=lambda p: p.stem)
def test_every_200_observations_fixture_parses_whole(path: Path) -> None:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    series_id = loaded["params"]["series_id"]
    body = loaded["body"]
    parsed = parse_observations(series_id, body)
    assert len(parsed) == body["count"] == len(body["observations"])
    raw_missing = sum(1 for row in body["observations"] if row["value"] == MISSING_VALUE)
    assert sum(1 for o in parsed if o.value is None) == raw_missing


@pytest.mark.parametrize("sid", ANSWERED)
@pytest.mark.parametrize("label", ("release_day", "day_before"))
def test_a_single_day_vintage_stamps_every_row_with_the_requested_day(
    sid: str, label: str
) -> None:
    """**Clipped, not per-value.** The U6a finding, pinned.

    With ``realtime_start = realtime_end = d``, every observation's
    ``realtime_start`` and ``realtime_end`` are ``d`` -- including values
    that are also in the day-before vintage unchanged, so they were not
    first valid on ``d``. A single-day vintage cannot say what is new.
    """
    params = envelope(f"{PREFIX}{sid}_{label}")["params"]
    day = params["realtime_start"]
    assert params["realtime_end"] == day
    rows = observations_of(sid, label)
    assert len(rows) >= 3
    assert {(r["realtime_start"], r["realtime_end"]) for r in rows} == {(day, day)}


@pytest.mark.parametrize("sid", ANSWERED)
def test_the_release_day_vintage_holds_values_already_known_the_day_before(sid: str) -> None:
    """The evidence for clipping: an unchanged prior value, stamped release day."""
    before = {r["date"]: r["value"] for r in observations_of(sid, "day_before")}
    on_day = observations_of(sid, "release_day")
    release_day = envelope(f"{PREFIX}{sid}_release_day")["params"]["realtime_start"]
    unchanged = [r for r in on_day if before.get(r["date"]) == r["value"]]
    assert unchanged, "no value carried over unchanged; clipping is not shown by this pair"
    assert all(r["realtime_start"] == release_day for r in unchanged)


@pytest.mark.parametrize("sid", ANSWERED)
def test_the_release_day_vintage_has_exactly_one_more_observation(sid: str) -> None:
    """The day-before vintage ends one observation earlier: the release's own."""
    before = observations_of(sid, "day_before")
    on_day = observations_of(sid, "release_day")
    new_dates = sorted({r["date"] for r in on_day} - {r["date"] for r in before})
    assert len(new_dates) == 1
    assert new_dates[0] == max(r["date"] for r in on_day)


@pytest.mark.parametrize("sid", ANSWERED)
def test_the_two_day_window_splits_exactly_the_new_and_revised_values(sid: str) -> None:
    """``realtime_start = day_before, realtime_end = release_day``, output_type 1.

    A value valid on both days is one row clipped to ``day_before .. release_day``;
    a value new or revised on release day is a row starting on release day, and
    a revised one also leaves a row ending on ``day_before``. The release-day
    rows are therefore exactly what comparing the two single-day vintages finds.

    Both halves of the split are pinned, because U6c reads both. The rows
    *ending* on release day are the release-day vintage, which is where the
    prior is taken from: without that filter ``PAYEMS`` has two 2026-09-01
    rows, and the superseded 159044 gives a payroll change of +249K where the
    vintage's 159128 gives +165K. The rows ending on ``day_before`` are exactly
    the superseded values, which is how a revision is told from a new value.
    """
    params = envelope(f"{PREFIX}{sid}_window")["params"]
    start, end = params["realtime_start"], params["realtime_end"]
    assert date.fromisoformat(end).toordinal() - date.fromisoformat(start).toordinal() == 1
    rows = observations_of(sid, "window")
    assert {r["realtime_start"] for r in rows} <= {start, end}
    assert {r["realtime_end"] for r in rows} <= {start, end}

    starting_on_day = {(r["date"], r["value"]) for r in rows if r["realtime_start"] == end}
    before = {r["date"]: r["value"] for r in observations_of(sid, "day_before")}
    on_day = {r["date"]: r["value"] for r in observations_of(sid, "release_day")}
    changed = {(d, v) for d, v in on_day.items() if before.get(d) != v}
    assert starting_on_day == changed

    # The release-day vintage is exactly the rows still valid on release day...
    assert {(r["date"], r["value"]) for r in rows if r["realtime_end"] == end} == set(
        on_day.items()
    )
    # ...and the rows that stopped on the day before are exactly the values
    # release day superseded: a revision leaves one, a new value does not.
    assert {(r["date"], r["value"]) for r in rows if r["realtime_end"] == start} == {
        (d, v) for d, v in before.items() if on_day.get(d) != v
    }


@pytest.mark.parametrize("label", (*VINTAGE_LABELS, "vintagedates", "vintagedates_recent"))
def test_exhoslusm495s_refuses_every_vintage_query(label: str) -> None:
    """NAR-copyrighted and outside ALFRED: a dated query is a 400, not data."""
    loaded = envelope(f"{PREFIX}exhoslusm495s_{label}")
    assert loaded["status_code"] == 400
    assert loaded["body"] == {"error_code": 400, "error_message": NOT_IN_ALFRED}


def test_exhoslusm495s_serves_only_its_current_values() -> None:
    """With no real-time period at all FRED answers, stamped with one day."""
    loaded = envelope(f"{PREFIX}exhoslusm495s_current")
    assert loaded["status_code"] == 200
    assert not any(k.startswith("realtime") for k in loaded["params"])
    rows = loaded["body"]["observations"]
    assert len(rows) >= 3
    assert len({(r["realtime_start"], r["realtime_end"]) for r in rows}) == 1


@pytest.mark.parametrize("label", ("release_dates", "release_dates_all", "release_dates_dated"))
def test_release_291_has_no_per_release_dates(label: str) -> None:
    body = envelope(f"{PREFIX}exhoslusm495s_{label}")["body"]
    assert body["count"] == 0 and body["release_dates"] == []


def test_release_291_is_on_the_all_releases_calendar_only_while_scheduled() -> None:
    """Past 291 dates are gone from ``/releases/dates``; future ones are listed."""
    def rows_291(label: str) -> list[str]:
        body = envelope(f"{PREFIX}exhoslusm495s_{label}")["body"]
        assert body["count"] == len(body["release_dates"])  # one complete page
        return [r["date"] for r in body["release_dates"] if r["release_id"] == 291]

    assert rows_291("releases_dates_window") == []
    assert rows_291("releases_dates_forward")
