"""Record real Alpaca responses into ``tests/fixtures/alpaca/``.

Run by hand, never by the test suite::

    uv run python tests/fixtures/record_alpaca.py

**The suite itself makes no live calls.** It replays what this script
captured, through an ``httpx.MockTransport``. That split is the point: real
shapes, deterministic tests, no rate budget spent and no dependence on market
hours.

Three safety properties, enforced below rather than merely intended:

* **No credential is ever written.** Every recorded body is scanned for the
  key id and the secret before it is saved; a hit aborts the run without
  writing.
* **No account identifier is written.** Everything here is market data or
  public contract reference data, which carries none -- but the scrubber runs
  anyway, because "this endpoint has no account fields" is a claim that ages
  badly.
* **Nothing is placed, cancelled or modified.** GET only.

``python-dotenv`` loads ``.env`` at runtime. This script reads it; nothing
under ``corollary/`` does -- the process environment is the engine's input.
"""

import asyncio
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from corollary.data.providers.alpaca import (  # noqa: E402
    DATA_BASE_URL,
    AlpacaCredentials,
    FeedConfig,
    FeedConfigError,
)

OUTPUT_DIR = Path(__file__).resolve().parent / "alpaca"

#: An expiry five weeks out, where Alpaca populates IV and greeks on every
#: strike. The near-dated chain only carries them near the money.
GREEKS_EXPIRY = "2026-10-16"

#: The window the adjusted-contract hunt runs over.
ADJUSTED_FROM = "2026-09-01"
ADJUSTED_TO = "2028-12-31"

#: Adjusted contracts had to be hunted for -- none of the liquid names carry
#: one. These two batches are where GME1 and XRX1 turned up.
ADJUSTED_CANDIDATES = (
    ["VMW", "ATVI", "SIRI", "LUMN", "AMC", "GME"],
    ["VTRS", "ZBH", "XRX", "HPE", "CTVA", "FTV"],
)

#: Keys redacted wherever they appear, at any depth. ``id`` and
#: ``*_asset_id`` are Alpaca's own catalogue UUIDs rather than anything
#: private, but nothing in this codebase reads them and fixed placeholders
#: keep the fixture diff-stable across re-recordings.
_SCRUB_KEYS = frozenset(
    {
        "account_id",
        "account_number",
        "account_blocked",
        "client_order_id",
        "asset_id",
        "underlying_asset_id",
        "id",
    }
)
_REDACTED = "00000000-0000-0000-0000-000000000000"


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                _REDACTED
                if key in _SCRUB_KEYS and isinstance(inner, str)
                else scrub(inner)
            )
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


#: Wraps a ``Decimal`` on the way through ``json.dumps``, which has no hook
#: for emitting one as a bare JSON number: ``default=`` may only return
#: something *serializable*, so a Decimal comes back quoted. The sentinel is
#: ``\x01``, which cannot occur in a JSON string, so unwrapping it afterwards
#: cannot touch real data.
_NUMBER_SENTINEL = "\x01"
#: Built from the sentinel's *escaped* form: ``json.dumps`` writes a
#: control character as a ``\\u0001`` escape rather than as itself, so a
#: pattern containing the raw byte would never match. Derived from
#: ``json.dumps`` rather than typed out, so the two cannot drift.
_ESCAPED_SENTINEL = re.escape(json.dumps(_NUMBER_SENTINEL)[1:-1])
_SENTINEL_PATTERN = re.compile(
    f'"{_ESCAPED_SENTINEL}(-?[0-9][0-9.eE+-]*){_ESCAPED_SENTINEL}"'
)


def _tag_decimal(value: Any) -> str:
    if isinstance(value, Decimal):
        return f"{_NUMBER_SENTINEL}{value}{_NUMBER_SENTINEL}"
    raise TypeError(f"cannot serialise {type(value).__name__}: {value!r}")


def dumps_exact(payload: Any) -> str:
    """Serialise without any number ever having been a ``float``.

    The recorder used to do ``json.dumps(json.loads(response.text))``, so every
    price in every fixture was Python's shortest round-trip repr of an IEEE
    double rather than Alpaca's own digits. The *values* survive that for
    anything under about 17 significant figures, which is most of them -- but
    IV and greeks arrive with sixteen or seventeen, and the whole reason this
    module's provider owns its JSON decoder is that a double is not where a
    price should live even for one statement.

    So: parsed with ``parse_float=Decimal``, emitted from the Decimal's own
    string. A fixture now carries the digits Alpaca sent.
    """
    return _SENTINEL_PATTERN.sub(
        r"\1", json.dumps(payload, indent=2, sort_keys=True, default=_tag_decimal)
    )


def save(name: str, payload: Any, secrets: tuple[str, ...]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    text = dumps_exact(scrub(payload)) + "\n"
    for secret in secrets:
        if secret and secret in text:
            raise SystemExit(
                f"ABORTED: a credential appeared in {name}. Nothing was written."
            )
    (OUTPUT_DIR / f"{name}.json").write_text(text, encoding="utf-8")
    print(f"  wrote {name}.json ({len(text):,} bytes)")


async def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")

    # Feeds come from the environment, through the same code path the engine
    # uses, and a missing one stops the run.
    #
    # This used to build a `FeedConfig` by hand with `or "indicative"` /
    # `or "sip"` / `or "iex"` fallbacks, on the reasoning that a hand-run
    # recorder is not the engine. True, and still the wrong shape: this was
    # the only place in the repository outside the provider where a feed
    # literal sat next to the name of the variable it was meant to make
    # unnecessary, and three lines are exactly what somebody copies when
    # `from_env` raises on them somewhere it matters. A recorder that cannot
    # find its configuration should say so, not invent it.
    try:
        feeds = FeedConfig.from_env()
    except FeedConfigError as exc:
        raise SystemExit(
            f"ABORTED before any request: {exc}\n\n"
            "Set the three feed variables in .env (the names are in "
            ".env.example) and run again."
        ) from exc
    print(f"recording with: {feeds}")

    credentials = AlpacaCredentials.paper_from_env()
    secrets = (credentials.key_id, credentials.secret_key)
    print(f"credentials: {credentials}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        headers = credentials.headers()

        async def get(base: str, path: str, params: dict[str, Any]) -> Any:
            response = await client.get(base + path, params=params, headers=headers)
            print(f"  GET {path} -> {response.status_code}")
            return {
                "status_code": response.status_code,
                # `parse_float=Decimal`, exactly as the provider's `_decode`
                # does. `response.json()` here would put every price through a
                # double before it ever reached the file.
                "body": (
                    json.loads(response.text, parse_float=Decimal)
                    if response.text
                    else None
                ),
            }

        print("\nrecording market data:")
        save(
            "stock_snapshots",
            await get(
                DATA_BASE_URL,
                "/v2/stocks/snapshots",
                {"symbols": "NVDA,SPY,AAPL", "feed": feeds.stock_realtime},
            ),
            secrets,
        )
        save(
            "stock_bars_daily",
            await get(
                DATA_BASE_URL,
                "/v2/stocks/bars",
                {
                    "symbols": "NVDA,SPY",
                    "timeframe": "1Day",
                    "start": "2026-08-03",
                    "end": "2026-08-14",
                    "feed": feeds.stock_historical,
                    "adjustment": "split",
                    "limit": 10000,
                    "sort": "asc",
                },
            ),
            secrets,
        )

        # Two pages of one chain, so the pagination loop is replayed rather
        # than assumed.
        page1 = await get(
            DATA_BASE_URL,
            "/v1beta1/options/snapshots/NVDA",
            {"feed": feeds.options, "limit": 100, "root_symbol": "NVDA"},
        )
        save("option_chain_nvda_page1", page1, secrets)
        save(
            "option_chain_nvda_page2",
            await get(
                DATA_BASE_URL,
                "/v1beta1/options/snapshots/NVDA",
                {
                    "feed": feeds.options,
                    "limit": 100,
                    "root_symbol": "NVDA",
                    "page_token": (page1["body"] or {}).get("next_page_token"),
                },
            ),
            secrets,
        )

        # The ground-truth set for derived greeks: at this tenor Alpaca
        # populates IV and greeks on every strike, so ours can be checked
        # against theirs rather than only against a textbook.
        save(
            "option_chain_nvda_dated",
            await get(
                DATA_BASE_URL,
                "/v1beta1/options/snapshots/NVDA",
                {
                    "feed": feeds.options,
                    "limit": 200,
                    "root_symbol": "NVDA",
                    "expiration_date": GREEKS_EXPIRY,
                    "strike_price_gte": 190,
                    "strike_price_lte": 250,
                },
            ),
            secrets,
        )
        save(
            "stock_snapshot_nvda",
            await get(
                DATA_BASE_URL,
                "/v2/stocks/snapshots",
                {"symbols": "NVDA", "feed": feeds.stock_realtime},
            ),
            secrets,
        )
        save(
            "option_chain_opra_denied",
            await get(
                DATA_BASE_URL,
                "/v1beta1/options/snapshots/NVDA",
                {"feed": "opra", "limit": 10},
            ),
            secrets,
        )
        save(
            "stock_quotes_latest",
            await get(
                DATA_BASE_URL,
                "/v2/stocks/quotes/latest",
                {"symbols": "NVDA,SPY", "feed": feeds.stock_realtime},
            ),
            secrets,
        )
        # Option *bars* are the only historical option data that exists:
        # there is no historical quotes endpoint at all, and trades reach
        # back 7 days. Anything older than a week is bars and nothing else.
        save(
            "option_bars_daily",
            await get(
                DATA_BASE_URL,
                "/v1beta1/options/bars",
                {
                    "symbols": "NVDA261016C00220000",
                    "timeframe": "1Day",
                    "start": "2026-08-03",
                    "end": "2026-09-05",
                    # No `feed` here: this endpoint rejects one with a 400.
                    "limit": 10000,
                    "sort": "asc",
                },
            ),
            secrets,
        )
        save(
            "option_quotes_latest",
            await get(
                DATA_BASE_URL,
                "/v1beta1/options/quotes/latest",
                {"symbols": "NVDA260911C00205000", "feed": feeds.options},
            ),
            secrets,
        )

        print("\nrecording contract reference data (trading host):")
        save(
            "option_contracts_nvda",
            await get(
                credentials.trading_base_url,
                "/v2/options/contracts",
                {"underlying_symbols": "NVDA", "status": "active", "limit": 100},
            ),
            secrets,
        )

        adjusted: list[Any] = []
        standard: list[Any] = []
        for batch in ADJUSTED_CANDIDATES:
            page = await get(
                credentials.trading_base_url,
                "/v2/options/contracts",
                {
                    "underlying_symbols": ",".join(batch),
                    "status": "active",
                    "expiration_date_gte": ADJUSTED_FROM,
                    "expiration_date_lte": ADJUSTED_TO,
                    "limit": 10000,
                },
            )
            for contract in (page["body"] or {}).get("option_contracts", []):
                if contract.get("root_symbol") != contract["underlying_symbol"]:
                    adjusted.append(contract)
                else:
                    standard.append(contract)

        seen: dict[str, int] = {}
        picked: list[Any] = []
        for contract in sorted(adjusted, key=lambda c: str(c["symbol"])):
            root = str(contract["root_symbol"])
            if seen.get(root, 0) < 3:
                seen[root] = seen.get(root, 0) + 1
                picked.append(contract)
        print(
            f"  adjusted found: {len(adjusted)} across roots {sorted(seen)}; "
            f"multipliers seen: {sorted({str(c['multiplier']) for c in adjusted})}"
        )
        save(
            "option_contracts_adjusted",
            {
                "status_code": 200,
                "body": {
                    "option_contracts": picked + standard[:6],
                    "next_page_token": None,
                },
            },
            secrets,
        )

        # The same adjusted contract with its deliverables -- the field that
        # tells the truth when `multiplier` does not.
        save(
            "option_contracts_deliverables",
            await get(
                credentials.trading_base_url,
                "/v2/options/contracts",
                {
                    "underlying_symbols": "GME",
                    "root_symbol": "GME1",
                    "status": "active",
                    "expiration_date_gte": ADJUSTED_FROM,
                    "expiration_date_lte": ADJUSTED_TO,
                    "show_deliverables": "true",
                    "limit": 5,
                },
            ),
            secrets,
        )

    print("\ndone. Fixtures are in tests/fixtures/alpaca/.")


if __name__ == "__main__":
    asyncio.run(main())
