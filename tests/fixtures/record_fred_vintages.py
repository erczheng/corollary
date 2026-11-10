"""Record FRED release-day vintages into ``tests/fixtures/fred/`` (U6a).

U6's pre-flight probe. Run by hand, never by the test suite, with the key in
the process environment only -- supplied by the launcher::

    uv run --env-file <path-to>/.env python tests/fixtures/record_fred_vintages.py
    uv run --env-file <path-to>/.env python tests/fixtures/record_fred_vintages.py --overwrite
    uv run --env-file <path-to>/.env python tests/fixtures/record_fred_vintages.py --overwrite EXHOSLUSM495S

Series IDs after the flag restrict the run to those series.

**The question it settles** (spec ``2026-11-09-phase-3-steps-5-7-remainder``,
*Not verified* and R7): in a ``/series/observations`` response with
``realtime_start = realtime_end = release_date`` (``output_type`` 1), does
each observation's ``realtime_start`` say when that value first became valid,
or is it clipped to the requested ``realtime_start``?

For each of ``CPIAUCSL`` (release 10), ``PAYEMS`` (50), ``BOPGSTB`` (51) and
``EXHOSLUSM495S`` (291) it records, as ``p3_vintage_<series>_<label>.json``:

* ``release_dates`` -- ``/release/dates`` for the release, newest first. The
  release date ``D`` is the latest one strictly before today (Eastern); ``P``
  is the one before it.
* ``vintagedates`` -- ``/series/vintagedates`` from ``P - 1`` to today: the
  days FRED itself says the series changed.
* ``release_day`` -- the vintage on ``D`` (``realtime_start = realtime_end = D``).
* ``day_before`` -- the vintage on ``D - 1``: requested before posting.
* ``window`` -- ``realtime_start = D - 1``, ``realtime_end = D``. With
  ``output_type`` 1 a value that changed on ``D`` splits into two real-time
  periods, which is what tells "clipped" from "per-value".
* ``since_prior`` -- ``realtime_start = P - 1``, ``realtime_end = D``: the same
  across a whole release cycle, so a value first posted on ``P`` shows where
  its own real-time period starts.

**When ``/release/dates`` has fewer than two past dates** (release 291 answered
zero rows on 2026-11-09), more are recorded before ``D`` is picked:

* ``release_dates_all`` -- the same with ``include_release_dates_with_no_data=true``.
* ``release_dates_dated`` -- that again with ``realtime_start`` 120 days back
  and ``realtime_end`` today, because FRED's refusal for a series outside
  ALFRED says to set the real-time period to today.
* ``series`` -- ``/series``, whose ``notes`` carry the copyright terms.
* ``vintagedates_recent`` -- ``/series/vintagedates``, newest first, undated.
* ``releases_dates_window`` -- the all-releases ``/releases/dates`` for the
  last 31 days, with the provider's own :func:`release_dates_params`: the
  endpoint the calendar reads, which carried release 291 when the per-release
  one did not.

* ``releases_dates_forward`` -- the same for the next 31 days, to see whether
  FRED lists the release only while it is still scheduled.

``D`` and ``P`` come from the first of those date sources holding two past
dates. Failing that, a single date from ``releases_dates_window`` is ``D``;
failing that, :data:`KNOWN_RELEASE_DAYS`. ``P`` is then a nominal
``D - 31 days``: a series FRED keeps no vintages of has no prior release to
find. Once ``D`` is known such a series also gets ``current``: the
observations with no real-time period at all, which is what FRED serves for a
series it keeps no vintages of.

Every observation request carries ``observation_start`` six months before
``D``'s month, which holds at least three observations of a monthly series.

**A non-200 is recorded, not refused.** ``EXHOSLUSM495S`` is NAR-copyrighted
and may refuse a vintage query; the status and the body are the finding.

Safety, enforced rather than intended -- the same as ``record_fred.py``:

* **The key never reaches disk or the terminal.** FRED takes it only in the
  query string, so the request URL carries it: this script never prints or
  saves a URL. The envelope records the path and the params *without*
  ``api_key``; the body is passed through the provider's own
  ``api_key=`` redaction before it is written, and the text as written is
  scanned for the key -- a hit aborts with nothing more written. A transport
  error is reported by class name only, because ``httpx`` quotes the URL.
* **GET only.** ``tests/test_hard_rules.py`` globs ``record_*.py`` into its
  write-verb guard, so this file is in scope by construction.
* **Nothing here loads ``.env``.** The launcher's ``--env-file`` puts the key
  in the process environment; this reads that and nothing else.
* **Nothing is half-overwritten.** Every target name is known before the first
  request, and an existing one refuses the whole run unless ``--overwrite``.

``tests/data/providers/test_fred_vintage_fixtures.py`` checks what it writes.
"""

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from corollary.data.providers.fred import (  # noqa: E402
    FRED_BASE_URL,
    FredCredentials,
    _redact_query_key,
    release_dates_params,
)

OUTPUT_DIR: Final = Path(__file__).resolve().parent / "fred"
PREFIX: Final = "p3_vintage"
ET: Final = ZoneInfo("America/New_York")

#: ``(release_id, series_id)``, as R7's seed table pairs them.
RELEASES: Final[tuple[tuple[int, str], ...]] = (
    (10, "CPIAUCSL"),
    (50, "PAYEMS"),
    (51, "BOPGSTB"),
    (291, "EXHOSLUSM495S"),
)

VINTAGE_LABELS: Final = ("release_day", "day_before", "window", "since_prior")
BASE_LABELS: Final = ("release_dates", "vintagedates", *VINTAGE_LABELS)
FALLBACK_LABELS: Final = (
    "release_dates_all",
    "release_dates_dated",
    "series",
    "vintagedates_recent",
    "releases_dates_window",
    "releases_dates_forward",
    "current",
)

#: The last resort for ``D``, used only when every FRED date source above is
#: empty. ``EXHOSLUSM495S``: 2026-10-13 is release 291's row in
#: ``p3_release_dates_forward.json`` (recorded 2026-10-10, when FRED still
#: listed it), and ``/series`` gives ``last_updated`` 2026-10-13 09:07:56-05.
#: Recorded on 2026-11-09, no FRED dates endpoint lists release 291 at all.
KNOWN_RELEASE_DAYS: Final = {"EXHOSLUSM495S": date(2026, 10, 13)}

#: ``releases_dates_window``'s width, and the nominal gap to ``P`` when only
#: ``D`` is known. Thirty days was one page of 842 rows on 2026-09-24.
WINDOW_DAYS: Final = 31

#: How far back ``release_dates_dated`` reaches.
DATED_LOOKBACK_DAYS: Final = 120

#: How many release dates to ask for, newest first. Two past ones are needed.
RELEASE_DATES_LIMIT: Final = 12

#: Months of observations before ``D``'s month. Six holds at least three
#: observations of a monthly series even with a two-month publication lag.
OBSERVATION_MONTHS: Final = 6

#: A dev tool's pause between requests, well under FRED's 120/min.
PAUSE_SECONDS: Final = 0.6


def first_of_month_before(day: date, months: int) -> date:
    year, month = day.year, day.month - months
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def target(series_id: str, label: str) -> Path:
    return OUTPUT_DIR / f"{PREFIX}_{series_id.lower()}_{label}.json"


class Recorder:
    def __init__(self, client: httpx.Client, secret: str) -> None:
        self._client = client
        self._secret = secret

    def get(self, out: Path, path: str, params: dict[str, str]) -> tuple[int, Any]:
        """One GET, written to ``out`` whatever its status. Returns (status, parsed body)."""
        time.sleep(PAUSE_SECONDS)
        try:
            response = self._client.get(
                f"{FRED_BASE_URL}{path}",
                params={**params, "api_key": self._secret},
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            # The message quotes the URL, key included. Class name only.
            raise SystemExit(
                f"ABORTED at {out.name}: transport error ({type(exc).__name__})"
            )
        text = _redact_query_key(response.text, (self._secret,)).strip()
        parsed: Any
        try:
            parsed = json.loads(text)
            body_text = text  # verbatim, so every value keeps FRED's own text
        except json.JSONDecodeError:
            parsed = None
            body_text = json.dumps(text)  # a non-JSON body, kept as one string
        head = json.dumps(
            {
                "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "path": path,
                "params": params,
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type"),
            },
            indent=2,
        )
        payload = head[: head.rstrip().rindex("}")].rstrip() + f',\n  "body": {body_text}\n}}\n'
        if self._secret.lower() in payload.lower():
            raise SystemExit(f"ABORTED at {out.name}: the recording contains the key")
        json.loads(payload)  # the file must parse as written
        out.write_text(payload, encoding="utf-8")
        size = ""
        if isinstance(parsed, dict):
            for key in ("observations", "release_dates", "vintage_dates"):
                if isinstance(parsed.get(key), list):
                    size = f", {len(parsed[key])} {key}"
        print(f"  {out.name}: {path} HTTP {response.status_code}{size}")
        return response.status_code, parsed


def past_release_dates(body: Any, today: date) -> list[date]:
    """Release dates strictly before ``today``, oldest first."""
    if not isinstance(body, dict) or not isinstance(body.get("release_dates"), list):
        return []
    days = {date.fromisoformat(str(row["date"])) for row in body["release_dates"]}
    return sorted(d for d in days if d < today)


def past_vintage_dates(body: Any, today: date) -> list[date]:
    """Vintage dates strictly before ``today``, oldest first."""
    if not isinstance(body, dict) or not isinstance(body.get("vintage_dates"), list):
        return []
    days = {date.fromisoformat(str(day)) for day in body["vintage_dates"]}
    return sorted(d for d in days if d < today)


def fallback_dates(recorder: Recorder, release_id: int, series_id: str, today: date) -> list[date]:
    """Past dates for a release whose default ``/release/dates`` came back short."""
    status, body = recorder.get(
        target(series_id, "release_dates_all"),
        "/release/dates",
        {
            "release_id": str(release_id),
            "file_type": "json",
            "include_release_dates_with_no_data": "true",
            "sort_order": "desc",
            "limit": str(RELEASE_DATES_LIMIT),
        },
    )
    dstatus, dbody = recorder.get(
        target(series_id, "release_dates_dated"),
        "/release/dates",
        {
            "release_id": str(release_id),
            "file_type": "json",
            "include_release_dates_with_no_data": "true",
            "realtime_start": (today - timedelta(days=DATED_LOOKBACK_DAYS)).isoformat(),
            "realtime_end": today.isoformat(),
            "sort_order": "desc",
            "limit": str(RELEASE_DATES_LIMIT),
        },
    )
    recorder.get(
        target(series_id, "series"),
        "/series",
        {"series_id": series_id, "file_type": "json"},
    )
    vstatus, vbody = recorder.get(
        target(series_id, "vintagedates_recent"),
        "/series/vintagedates",
        {
            "series_id": series_id,
            "file_type": "json",
            "sort_order": "desc",
            "limit": str(RELEASE_DATES_LIMIT),
        },
    )
    sources = (
        ("release_dates_all", past_release_dates(body, today) if status == 200 else []),
        ("release_dates_dated", past_release_dates(dbody, today) if dstatus == 200 else []),
        ("vintagedates_recent", past_vintage_dates(vbody, today) if vstatus == 200 else []),
    )
    for label, past in sources:
        if len(past) >= 2:
            print(f"  D and P from {label}")
            return past
    wstatus, wbody = recorder.get(
        target(series_id, "releases_dates_window"),
        "/releases/dates",
        {
            k: str(v)
            for k, v in release_dates_params(
                today - timedelta(days=WINDOW_DAYS), today, offset=0
            ).items()
        },
    )
    recorder.get(
        target(series_id, "releases_dates_forward"),
        "/releases/dates",
        {
            k: str(v)
            for k, v in release_dates_params(
                today, today + timedelta(days=WINDOW_DAYS), offset=0
            ).items()
        },
    )
    past = []
    rows = wbody.get("release_dates") if wstatus == 200 and isinstance(wbody, dict) else None
    if isinstance(rows, list) and isinstance(wbody, dict) and wbody.get("count") == len(rows):
        mine = {"release_dates": [r for r in rows if r.get("release_id") == release_id]}
        past = past_release_dates(mine, today)
    else:
        print("  ! releases_dates_window is not one complete 200 page; not used")
    if len(past) >= 2:
        print("  D and P from releases_dates_window")
        return past
    if len(past) == 1:
        release_day, source = past[0], "releases_dates_window"
    elif series_id in KNOWN_RELEASE_DAYS:
        release_day, source = KNOWN_RELEASE_DAYS[series_id], "KNOWN_RELEASE_DAYS"
    else:
        return []
    nominal = release_day - timedelta(days=WINDOW_DAYS)
    print(f"  D from {source}; P is nominal ({nominal})")
    return [nominal, release_day]


def main() -> None:
    args = sys.argv[1:]
    overwrite = bool(args) and args[0] == "--overwrite"
    wanted = set(args[1:] if overwrite else args)
    known = {series_id for _, series_id in RELEASES}
    if any(a.startswith("-") for a in wanted) or not wanted <= known:
        raise SystemExit(
            f"unknown arguments {args!r}; expected [--overwrite] [SERIES_ID ...] from {sorted(known)}"
        )
    releases = [(r, s) for r, s in RELEASES if not wanted or s in wanted]
    targets = [
        target(series_id, label)
        for _, series_id in releases
        for label in (*BASE_LABELS, *FALLBACK_LABELS)
    ]
    existing = [p.name for p in targets if p.exists()]
    if existing and not overwrite:
        raise SystemExit(f"{existing} exist; pass --overwrite to replace them")

    secret = FredCredentials.from_env().api_key
    today = datetime.now(ET).date()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30.0) as client:
        recorder = Recorder(client, secret)
        for release_id, series_id in releases:
            print(f"release {release_id} / {series_id}")
            status, body = recorder.get(
                target(series_id, "release_dates"),
                "/release/dates",
                {
                    "release_id": str(release_id),
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": str(RELEASE_DATES_LIMIT),
                },
            )
            past = past_release_dates(body, today) if status == 200 else []
            fell_back = len(past) < 2
            if fell_back:
                print(f"  ! fewer than two past release dates for {release_id}")
                past = fallback_dates(recorder, release_id, series_id, today)
            if len(past) < 2:
                print(f"  ! no two past dates for {series_id} from any source; vintages skipped")
                continue
            release_day, prior = past[-1], past[-2]
            day_before = release_day - timedelta(days=1)
            print(f"  D = {release_day}, P = {prior}")

            recorder.get(
                target(series_id, "vintagedates"),
                "/series/vintagedates",
                {
                    "series_id": series_id,
                    "file_type": "json",
                    "realtime_start": (prior - timedelta(days=1)).isoformat(),
                    "realtime_end": today.isoformat(),
                    "sort_order": "asc",
                },
            )
            observation_start = first_of_month_before(release_day, OBSERVATION_MONTHS)
            windows = {
                "release_day": (release_day, release_day),
                "day_before": (day_before, day_before),
                "window": (day_before, release_day),
                "since_prior": (prior - timedelta(days=1), release_day),
            }
            if fell_back:
                recorder.get(
                    target(series_id, "current"),
                    "/series/observations",
                    {
                        "series_id": series_id,
                        "file_type": "json",
                        "observation_start": observation_start.isoformat(),
                    },
                )
            for label in VINTAGE_LABELS:
                start, end = windows[label]
                recorder.get(
                    target(series_id, label),
                    "/series/observations",
                    {
                        "series_id": series_id,
                        "file_type": "json",
                        "realtime_start": start.isoformat(),
                        "realtime_end": end.isoformat(),
                        "observation_start": observation_start.isoformat(),
                    },
                )


if __name__ == "__main__":
    main()
