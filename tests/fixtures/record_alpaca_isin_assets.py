"""Record Alpaca's active-equity rows for the 29 tickers OpenFIGI names.

Run by hand, never by the test suite, and **only through the launcher** --
nothing here loads ``.env`` (rule 6)::

    uv run --env-file <path-to>/.env python tests/fixtures/record_alpaca_isin_assets.py

Phase 3 spec Q17 accepts an OpenFIGI ISIN -> ticker answer only when the
ticker is in *today's* Alpaca asset directory (``accept_mapping`` in
``corollary/data/seeds/isin.py``). The OpenFIGI recording in
``tests/fixtures/openfigi/`` names 29 tickers; this records the directory
half of that evidence, so the real 2026-06-30 snapshot can be rebuilt in a
test from recorded inputs only.

**One request, through the provider.** It calls
:meth:`AlpacaProvider.active_equities` once -- ``GET /v2/assets?status=active
&asset_class=us_equity`` on the paper trading host, a read -- and makes no
request of its own. The raw rows are observed through an ``httpx`` response
event hook on the client handed to the provider, which reads the body the
provider is already receiving; nothing else is put on the wire.

What it enforces before anything is written:

* **Trimmed.** Only the rows for :data:`ISIN_TICKERS` are kept, sorted by
  symbol, with the full row count (``trimmed_from``) and the provider's
  decoded directory size beside them. A ticker absent from the active list
  is recorded under ``absent`` -- never dropped silently, never invented.
* **No float.** The body is parsed at ``parse_float=Decimal`` and written by
  ``record_alpaca.dumps_exact``; any float in the kept rows aborts.
* **No secret.** The rows go through ``record_alpaca.scrub``, then the text
  is checked for the Alpaca key header's prefix (``APCA``) and for the value
  of every environment variable whose name contains ``KEY``, ``SECRET``,
  ``TOKEN`` or ``AGENT`` (eight characters or more). A hit aborts with
  nothing written. Names and counts are printed; values never are.
* **No overwrite.** An existing fixture refuses the run before any request.
"""

import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import httpx

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from corollary.data.providers.alpaca import AlpacaProvider  # noqa: E402
from corollary.data.providers.interface import AssetDirectory  # noqa: E402
from record_alpaca import dumps_exact, scrub  # noqa: E402

OUTPUT: Final = Path(__file__).resolve().parent / "alpaca" / "p4_assets_active_isin_tickers.json"
RECORDER: Final = "tests/fixtures/record_alpaca_isin_assets.py"
REQUEST: Final = "/v2/assets?status=active&asset_class=us_equity"

#: The 29 tickers ``tests/fixtures/openfigi/`` names for spec Q17's ISIN-only
#: lines, in Q17's order (XLB, XLF, XLI, XLK, XLP, XLV, XLY).
#: ``tests/data/seeds/test_isin_resolver.py`` pins the same set as
#: ``LIVE_TICKERS``.
ISIN_TICKERS: Final[tuple[str, ...]] = (
    "AMCR", "CRH", "LIN", "SW", "LYB",
    "AON", "ACGL", "EG", "IVZ", "WTW", "CB",
    "ALLE", "ETN", "JCI", "PNR", "TT",
    "ACN", "STX", "TEL", "NXPI", "FLEX",
    "BG",
    "MDT", "STE",
    "CCL", "APTV", "NCLH", "GRMN", "RCL",
)

SECRET_NAME_MARKERS: Final = ("KEY", "SECRET", "TOKEN", "AGENT")
MIN_SECRET_LENGTH: Final = 8
#: Alpaca's credential headers are ``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY``.
KEY_HEADER_PREFIX: Final = "apca"


def environment_secrets(env: Mapping[str, str]) -> dict[str, str]:
    """Variables named like a secret, long enough to search for, by name."""
    return {
        name: value
        for name, value in env.items()
        if any(marker in name.upper() for marker in SECRET_NAME_MARKERS)
        and len(value) >= MIN_SECRET_LENGTH
    }


def _reject_floats(value: Any, where: str = "$") -> None:
    if isinstance(value, float):
        raise SystemExit(f"ABORTED: a float at {where}; nothing was written")
    if isinstance(value, Mapping):
        for key, inner in value.items():
            _reject_floats(inner, f"{where}.{key}")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            _reject_floats(inner, f"{where}[{index}]")


def trim(rows: Sequence[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """The rows for :data:`ISIN_TICKERS`, sorted by symbol, and the tickers absent."""
    wanted = set(ISIN_TICKERS)
    kept: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("symbol") in wanted:
            symbol = row["symbol"]
            if symbol in kept:
                raise SystemExit(f"ABORTED: {symbol} is listed twice; nothing was written")
            kept[symbol] = row
    absent = sorted(wanted - set(kept))
    return [kept[s] for s in sorted(kept)], absent


def scrub_check(text: str, secrets: Mapping[str, str]) -> None:
    """Abort if ``text`` carries the key header's prefix or any secret's value."""
    lowered = text.lower()
    if KEY_HEADER_PREFIX in lowered:
        raise SystemExit("ABORTED: the fixture names the Alpaca key header. Nothing was written.")
    for name, secret in secrets.items():
        if secret.lower() in lowered:
            raise SystemExit(
                f"ABORTED: the fixture contains the value of {name}. Nothing was written."
            )


async def record(env: Mapping[str, str]) -> None:
    if OUTPUT.exists():
        raise SystemExit(f"ABORTED: refusing to overwrite {OUTPUT.name}. No request was made.")
    secrets = environment_secrets(env)
    print(
        f"scrubbing against {len(secrets)} environment value(s): "
        + ", ".join(sorted(secrets))
    )

    seen: list[tuple[int, str, str]] = []

    async def capture(response: httpx.Response) -> None:
        await response.aread()
        seen.append((response.status_code, response.url.path, response.text))

    client = httpx.AsyncClient(timeout=30.0, event_hooks={"response": [capture]})
    provider = AlpacaProvider.from_env(env, client=client)
    try:
        directory: AssetDirectory = await provider.active_equities()
    finally:
        await provider.aclose()
        await client.aclose()

    if len(seen) != 1:
        raise SystemExit(f"ABORTED: {len(seen)} responses observed, not 1; nothing was written")
    status, path, text = seen[0]
    print(f"  GET {path} -> {status}")
    if status != 200 or path != "/v2/assets":
        raise SystemExit("ABORTED: not a 200 from /v2/assets; nothing was written")
    rows = json.loads(text, parse_float=Decimal)
    if not isinstance(rows, list):
        raise SystemExit("ABORTED: the asset list is not a list; nothing was written")

    kept, absent = trim(rows)
    _reject_floats(kept)
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    envelope: dict[str, Any] = {
        "recorded_at": recorded_at,
        "recorder": RECORDER,
        "request": REQUEST,
        "status_code": status,
        "trimmed_from": len(rows),
        "directory_size": len(directory),
        "directory_skipped": directory.skipped,
        "directory_missing_attributes": directory.missing_attributes,
        "tickers": list(ISIN_TICKERS),
        "absent": absent,
        "note": (
            "TRIMMED: the unfiltered active us_equity list, one live request through "
            "AlpacaProvider.active_equities, reduced to the rows for the 29 tickers "
            f"tests/fixtures/openfigi/ names. The full response held {len(rows)} rows; "
            f"the provider's directory kept {len(directory)}. 'absent' lists any of the "
            "29 the active list did not carry."
        ),
        "body": scrub(kept),
    }
    out = dumps_exact(envelope) + "\n"
    scrub_check(out, secrets)
    OUTPUT.write_text(out, encoding="utf-8", newline="\n")
    print(f"  wrote {OUTPUT.name} ({len(out)} bytes): {len(kept)} rows, absent {absent}")
    for symbol in ISIN_TICKERS:
        asset = directory.get(symbol)
        if asset is None:
            print(f"  {symbol}: not in the directory")
        else:
            print(
                f"  {symbol}: tradable={asset.tradable} has_options={asset.has_options} "
                f"exchange={asset.exchange}"
            )


def main() -> None:
    asyncio.run(record(os.environ))


if __name__ == "__main__":
    main()
