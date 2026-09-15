"""Record real Finnhub responses into ``tests/fixtures/finnhub/``.

Run by hand, never by the test suite::

    uv run python tests/fixtures/record_finnhub.py

**The suite itself makes no live calls.** It replays what this script
captured, through an ``httpx.MockTransport`` -- the same split
``record_alpaca.py`` sets up, and for the same reasons: real shapes,
deterministic tests, no rate budget spent, no dependence on market hours.

Three safety properties, enforced below rather than merely intended:

* **No credential is ever written.** The token travels in the
  ``X-Finnhub-Token`` header rather than in the query string precisely so the
  recorded URL cannot carry it, and every recorded body is still scanned for
  the key before it is saved. A hit aborts the run without writing.
* **The scan is a substring pass as well as a field pass.** Vendors embed
  identifiers in free prose -- ``record_alpaca.py`` learned this from a ``FEE``
  row whose ``description`` quoted the account number in the middle of an
  English sentence, where a rule written about field names cannot see it.
  Nothing in ``/stock/profile2`` is account-scoped, so there is no field to
  redact here; the substring scan is what makes that a finding rather than an
  assumption.
* **Nothing is placed, cancelled or modified.** GET only, and
  ``tests/test_hard_rules.py`` asserts it: its vendor-surface write-verb
  guard globs ``record_*.py``, so this file is in scope by construction
  rather than by being listed, and so is the next recorder anybody writes.
  That guard used to name ``record_alpaca.py`` alone, and for one revision
  the sentence above this one was simply false.

``python-dotenv`` loads ``.env`` at runtime. This script reads it; nothing
under ``corollary/`` does -- the process environment is the engine's input.
"""

import json
import sys
from pathlib import Path
from typing import Final

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from corollary.data.providers.finnhub import (  # noqa: E402
    FINNHUB_BASE_URL,
    FINNHUB_TOKEN_HEADER,
    FinnhubCredentials,
)

OUTPUT_DIR = Path(__file__).resolve().parent / "finnhub"

#: What to capture, as ``fixture name -> symbol``.
#:
#: Three shapes rather than one, because the three are the whole of the
#: decision this column rests on: a company with a market cap, a **fund**
#: which has none and must stay ``null``, and a symbol the vendor does not
#: know -- which answers the same way the fund does and must therefore be
#: read the same way.
SYMBOLS: Final[dict[str, str]] = {
    "profile2_aapl": "AAPL",
    "profile2_spy": "SPY",
    "profile2_unknown": "ZZZZ",
}


def save(name: str, status_code: int, body_text: str, secrets: list[str]) -> None:
    """Write one recorded response, after proving it holds no credential.

    The scan runs on the **text as it will be written**, so it is a proof
    rather than a precaution: anything in ``secrets`` that survives to this
    point aborts the run with nothing on disk.
    """
    for secret in secrets:
        if secret and secret.lower() in body_text.lower():
            raise SystemExit(
                f"ABORTED: {name} contains a credential. Nothing was written."
            )
    # Reparse and re-emit so the file is readable, but keep every number's
    # own text: `parse_float=str` would quote them, so the body is embedded
    # verbatim instead and only the envelope is formatted.
    payload = f'{{\n  "status_code": {status_code},\n  "body": {body_text.strip()}\n}}\n'
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / f"{name}.json").write_text(payload, encoding="utf-8")
    print(f"  wrote {name}.json ({status_code}, {len(body_text)} bytes)")


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    credentials = FinnhubCredentials.from_env()
    secrets = [credentials.token]

    with httpx.Client(timeout=15.0) as client:
        for name, symbol in SYMBOLS.items():
            response = client.get(
                f"{FINNHUB_BASE_URL}/stock/profile2",
                params={"symbol": symbol},
                headers={FINNHUB_TOKEN_HEADER: credentials.token},
            )
            print(f"{symbol}: HTTP {response.status_code}")
            try:
                json.loads(response.text)
            except json.JSONDecodeError:
                raise SystemExit(
                    f"ABORTED: {symbol} answered with non-JSON: "
                    f"{response.text[:200]!r}"
                )
            save(name, response.status_code, response.text, secrets)


if __name__ == "__main__":
    main()
