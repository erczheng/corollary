"""Record FRED's forward release calendar into ``tests/fixtures/fred/``.

Unit 7.2b-R. Run by hand, never by the test suite, with the key in the
process environment only -- supplied by the launcher::

    uv run --env-file <path-to>/.env python tests/fixtures/record_fred.py
    uv run --env-file <path-to>/.env python tests/fixtures/record_fred.py --overwrite

**The suite makes no live calls.** ``tests/data/providers/test_fred_release_dates.py``
and ``tests/data/test_calendar_releases.py`` replay what this captures through
an ``httpx.MockTransport``.

The window is today (Eastern) through today + 30 days, the same width the
step-0 probe used. Recorded on 2026-10-10 that window crosses the 2026-11-01
end of daylight time, which is what lets the mapping tests prove an EDT and an
EST date of one release from real data rather than from a hand-written row.

Safety, enforced rather than intended:

* **The key never reaches disk or the terminal.** FRED takes it only in the
  query string, so the request URL carries it: this script never prints or
  saves a URL. The envelope records the path and the params *without*
  ``api_key``, and the text as written is scanned for the key -- a hit aborts
  with nothing written. A transport error is reported by class name only,
  because ``httpx`` quotes the URL in its message.
* **GET only.** ``tests/test_hard_rules.py`` globs ``record_*.py`` into its
  write-verb guard, so this file is in scope by construction.
* **Nothing here loads ``.env``.** The launcher's ``--env-file`` puts the key
  in the process environment; this reads that and nothing else.
* **The request is the provider's.** Params come from
  :func:`corollary.data.providers.fred.release_dates_params`, so the fixture
  cannot drift from what the code sends, and a test pins that it has not.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from corollary.data.providers.fred import (  # noqa: E402
    FRED_BASE_URL,
    RELEASE_DATES_PAGE_LIMIT,
    FredCredentials,
    release_dates_params,
)

OUTPUT_DIR: Final = Path(__file__).resolve().parent / "fred"
FIXTURE_NAME: Final = "p3_release_dates_forward"
PATH: Final = "/releases/dates"
WINDOW_DAYS: Final = 30
ET: Final = ZoneInfo("America/New_York")


def main() -> None:
    args = sys.argv[1:]
    if args not in ([], ["--overwrite"]):
        raise SystemExit(f"unknown arguments {args!r}; expected nothing or --overwrite")
    out = OUTPUT_DIR / f"{FIXTURE_NAME}.json"
    if out.exists() and args != ["--overwrite"]:
        raise SystemExit(f"{out.name} exists; pass --overwrite to replace it")

    credentials = FredCredentials.from_env()
    secret = credentials.api_key
    start = datetime.now(ET).date()
    end = start + timedelta(days=WINDOW_DAYS)
    params = release_dates_params(start, end, offset=0)
    query = {k: str(v) for k, v in params.items()}

    with httpx.Client(timeout=30.0) as client:
        try:
            response = client.get(
                f"{FRED_BASE_URL}{PATH}",
                params={**query, "api_key": secret},
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            # The message quotes the URL, key included. Class name only.
            raise SystemExit(f"ABORTED: transport error ({type(exc).__name__}); nothing written")

    print(f"{PATH} {start.isoformat()}..{end.isoformat()}: HTTP {response.status_code}")
    text = response.text
    if response.status_code != 200:
        raise SystemExit(f"ABORTED: HTTP {response.status_code} ({len(text)} bytes; body not echoed)")
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        raise SystemExit(f"ABORTED: non-JSON body ({len(text)} bytes; not echoed)")
    count = body.get("count") if isinstance(body, dict) else None
    rows = body.get("release_dates") if isinstance(body, dict) else None
    if not isinstance(rows, list) or count != len(rows):
        raise SystemExit(
            f"ABORTED: count {count!r} is not the {len(rows) if isinstance(rows, list) else '?'} "
            f"rows returned; this recorder captures one complete page of "
            f"{RELEASE_DATES_PAGE_LIMIT} at most. Nothing written."
        )

    envelope_head = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "path": PATH,
        "params": query,
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type"),
    }
    head = json.dumps(envelope_head, indent=2)
    # The body is embedded verbatim so every value keeps FRED's own text.
    payload = head[: head.rstrip().rindex("}")].rstrip() + f',\n  "body": {text.strip()}\n}}\n'
    if secret.lower() in payload.lower():
        raise SystemExit("ABORTED: the recording contains the key. Nothing written.")
    json.loads(payload)  # the file must parse as written
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(payload, encoding="utf-8")
    print(f"  wrote {out.name}: {len(rows)} rows, {len(payload)} bytes")


if __name__ == "__main__":
    main()
