"""Does Finnhub ``/calendar/earnings`` silently cap its rows? Counts only.

Run by hand, never by the test suite, and **only through the launcher** so no
agent ever reads ``.env`` (rule 6)::

    uv run --env-file <path-to>/.env python scripts/probe_earnings_truncation.py
    uv run --env-file <path-to>/.env python scripts/probe_earnings_truncation.py --overwrite

The question (fix 1 of the calendar risks): the earnings job fetches one
window -- today .. today + ``EARNINGS_WINDOW`` (21 days) -- and
``replace_window`` withdraws every stored row the response no longer lists. If
Finnhub caps a response in peak season, real rows are withdrawn. So this:

1. fetches the job's real window in one request;
2. fetches the same window one day at a time, and one week at a time;
3. fetches a few wider windows (42 and 63 days) to look for a ceiling;
4. compares the full-window count with the sum of the per-day counts, by
   distinct ``(symbol, year, quarter)`` key as well as by raw rows.

Only counts are printed and only counts are saved -- no row, no header, no
URL with a token (the key travels in a header, never the query). The fixture
is ``tests/fixtures/finnhub/p7_earnings_truncation_counts.json``; an existing
one is committed evidence and is refused unless ``--overwrite`` is passed.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final
from zoneinfo import ZoneInfo

from probe_phase3 import FINNHUB, FIXTURES, Http, finnhub_headers, leaked, say

EARNINGS_WINDOW_DAYS: Final = 21  # corollary.data.calendar_finnhub.EARNINGS_WINDOW
WIDER_WINDOWS: Final = (42, 63)
FIXTURE: Final = FIXTURES / "finnhub" / "p7_earnings_truncation_counts.json"


def _rows(http: Http, start: date, end: date) -> list[Any] | None:
    reply = http.get(
        f"{FINNHUB}/calendar/earnings",
        {"from": start.isoformat(), "to": end.isoformat()},
        finnhub_headers(),
    )
    if reply is None:
        say(f"  {start}..{end}: no reply")
        return None
    if reply.status != 200 or not isinstance(reply.body, dict):
        say(f"  {start}..{end}: status {reply.status}")
        return None
    rows = reply.body.get("earningsCalendar")
    if not isinstance(rows, list):
        say(f"  {start}..{end}: no earningsCalendar list")
        return None
    return rows


def _keys(rows: list[Any]) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for row in rows:
        if isinstance(row, dict):
            keys.add((str(row.get("symbol")), str(row.get("year")), str(row.get("quarter"))))
    return keys


def _dates(rows: list[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        if isinstance(row, dict):
            day = str(row.get("date"))
            out[day] = out.get(day, 0) + 1
    return dict(sorted(out.items()))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if FIXTURE.exists() and not args.overwrite:
        say(f"{FIXTURE.name} exists and is committed evidence; pass --overwrite to replace it")
        return 2

    http = Http()
    try:
        today = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
        end = today + timedelta(days=EARNINGS_WINDOW_DAYS)
        say(f"job window {today}..{end}")

        full = _rows(http, today, end)
        if full is None:
            return 1
        full_keys = _keys(full)
        full_dates = _dates(full)
        say(f"full window: {len(full)} rows, {len(full_keys)} distinct keys")

        per_day: dict[str, int] = {}
        per_day_keys: set[tuple[str, str, str]] = set()
        per_day_rows = 0
        day = today
        while day <= end:
            rows = _rows(http, day, day)
            if rows is None:
                return 1
            per_day[day.isoformat()] = len(rows)
            per_day_rows += len(rows)
            per_day_keys |= _keys(rows)
            say(f"  {day}: {len(rows)} (full window had {full_dates.get(day.isoformat(), 0)})")
            day += timedelta(days=1)

        per_week: dict[str, int] = {}
        per_week_keys: set[tuple[str, str, str]] = set()
        week_start = today
        while week_start <= end:
            week_end = min(week_start + timedelta(days=6), end)
            rows = _rows(http, week_start, week_end)
            if rows is None:
                return 1
            per_week[f"{week_start}..{week_end}"] = len(rows)
            per_week_keys |= _keys(rows)
            say(f"  week {week_start}..{week_end}: {len(rows)}")
            week_start = week_end + timedelta(days=1)

        wider: dict[str, int] = {}
        for days in WIDER_WINDOWS:
            rows = _rows(http, today, today + timedelta(days=days))
            if rows is None:
                return 1
            wider[f"{days}d"] = len(rows)
            say(f"  wider {days}d: {len(rows)} rows, {len(_keys(rows))} distinct keys")

        missing_from_full = len(per_day_keys - full_keys)
        say(f"sum of per-day rows {per_day_rows}, distinct {len(per_day_keys)}")
        say(f"sum of per-week distinct {len(per_week_keys)}")
        say(f"keys in per-day but not in full window: {missing_from_full}")
        say(f"keys in full window but not per-day: {len(full_keys - per_day_keys)}")
        say(f"max per-day count {max(per_day.values())}")

        record = {
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "endpoint": "/calendar/earnings",
            "note": "counts only; no row, no key. Probe: scripts/probe_earnings_truncation.py",
            "job_window": {"from": today.isoformat(), "to": end.isoformat()},
            "full_window": {"rows": len(full), "distinct_keys": len(full_keys)},
            "full_window_rows_by_date": full_dates,
            "per_day": per_day,
            "per_day_sum": {"rows": per_day_rows, "distinct_keys": len(per_day_keys)},
            "per_week": per_week,
            "per_week_distinct_keys": len(per_week_keys),
            "wider_windows": wider,
            "keys_missing_from_full_window": missing_from_full,
            "requests": dict(http.requests),
            "throttled": dict(http.throttled),
        }
        text = json.dumps(record, indent=2) + "\n"
        problems = leaked(text)
        if problems:
            say(f"refusing to save: leak scan flagged {problems}")
            return 1
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        with open(FIXTURE, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        say(f"saved {FIXTURE.name}")
        return 0
    finally:
        http.close()


if __name__ == "__main__":
    raise SystemExit(main())
