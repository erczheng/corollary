"""Record one vintage window **through** ``FredProvider.vintage_window`` (carried fix P2).

``record_fred_vintages.py`` (U6a) recorded its windows with hand-built params
that did not send ``output_type`` or ``units``. The provider now sends both
explicitly (``vintage_window_params``) and :func:`parse_vintage_window`
refuses a body whose echo differs, so that strict check had never been run
against what FRED returns for the query the provider actually makes. This
records exactly that: the provider's own request, its own params, FRED's
verbatim body.

Run by hand, never by the test suite, with the key in the process
environment only -- supplied by the launcher::

    uv run --env-file <path-to>/.env python tests/fixtures/record_fred_provider_vintage.py
    uv run --env-file <path-to>/.env python tests/fixtures/record_fred_provider_vintage.py --overwrite

It records ``CPIAUCSL`` for release day 2026-10-14 (release 10) as
``fred/p3_provider_vintage_cpiaucsl_window.json``. Being a vintage, it answers
the same whenever it is run, so the figure must equal the U6a recording's.
The file is written **whatever the provider concluded** -- a refusal of the
echo is the finding, not a reason to keep no evidence -- and the outcome and
the echoed ``output_type``/``units`` are printed.

Safety, the same rules as ``record_fred_vintages.py``:

* **The key never reaches disk or the terminal.** The envelope's params are
  :func:`vintage_window_params`' (no ``api_key``); the body is passed through
  the provider's ``api_key=`` redaction, and the text as written is scanned
  for the key -- a hit aborts with nothing written. A provider error is
  printed after the same redaction.
* **GET only**, through the provider's one metered ``_send``. This file is in
  ``tests/test_hard_rules.py``'s ``record_*.py`` write-verb guard.
* **Nothing here loads ``.env``.** The launcher's ``--env-file`` does.
* **No overwrite** unless ``--overwrite`` is passed.

``tests/data/test_econ_values.py`` checks what it writes.
"""

import asyncio
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Final

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from corollary.data.providers.fred import (  # noqa: E402
    FredError,
    FredProvider,
    VintageFrequency,
    _redact_query_key,
    vintage_window_params,
)

OUTPUT_DIR: Final = Path(__file__).resolve().parent / "fred"
TARGET: Final = OUTPUT_DIR / "p3_provider_vintage_cpiaucsl_window.json"
SERIES_ID: Final = "CPIAUCSL"
RELEASE_DAY: Final = date(2026, 10, 14)
FREQUENCY: Final[VintageFrequency] = "monthly"
PATH: Final = "/series/observations"


async def record(overwrite: bool) -> None:
    if TARGET.exists() and not overwrite:
        raise SystemExit(f"{TARGET.name} exists; pass --overwrite to replace it")

    captured: list[httpx.Response] = []

    async def keep(response: httpx.Response) -> None:
        await response.aread()
        captured.append(response)

    client = httpx.AsyncClient(timeout=30.0, event_hooks={"response": [keep]})
    try:
        async with FredProvider.from_env(client=client) as provider:
            secrets = tuple(provider.secrets)
            try:
                answer = await provider.vintage_window(
                    SERIES_ID, RELEASE_DAY, frequency=FREQUENCY
                )
                outcome = f"accepted: {type(answer).__name__}"
            except FredError as exc:
                outcome = f"refused: {type(exc).__name__}: {_redact_query_key(str(exc), secrets)}"
    finally:
        await client.aclose()

    if len(captured) != 1:
        raise SystemExit(f"ABORTED: expected one response, saw {len(captured)}")
    response = captured[0]
    text = _redact_query_key(response.text, secrets).strip()
    parsed: Any
    try:
        parsed = json.loads(text)
        body_text = text  # verbatim, so every value keeps FRED's own text
    except json.JSONDecodeError:
        parsed = None
        body_text = json.dumps(text)
    head = json.dumps(
        {
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "path": PATH,
            "params": vintage_window_params(SERIES_ID, RELEASE_DAY, FREQUENCY),
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type"),
        },
        indent=2,
    )
    payload = head[: head.rstrip().rindex("}")].rstrip() + f',\n  "body": {body_text}\n}}\n'
    for secret in secrets:
        if secret.lower() in payload.lower():
            raise SystemExit(f"ABORTED at {TARGET.name}: the recording contains the key")
    json.loads(payload)  # the file must parse as written
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(payload, encoding="utf-8")

    print(f"{TARGET.name}: {PATH} HTTP {response.status_code}")
    print(f"  provider: {outcome}")
    if isinstance(parsed, dict):
        print(f"  echoed output_type: {parsed.get('output_type')!r}")
        print(f"  echoed units: {parsed.get('units')!r}")
        rows = parsed.get("observations")
        if isinstance(rows, list):
            print(f"  {len(rows)} observations, count {parsed.get('count')!r}")


def main() -> None:
    args = sys.argv[1:]
    if args not in ([], ["--overwrite"]):
        raise SystemExit(f"unknown arguments {args!r}; expected [--overwrite]")
    asyncio.run(record(overwrite=bool(args)))


if __name__ == "__main__":
    main()
