"""Record real Finnhub responses into ``tests/fixtures/finnhub/``.

Run by hand, never by the test suite, with the key in the process
environment only -- supplied by the launcher::

    uv run --env-file <path-to>/.env python tests/fixtures/record_finnhub.py        # market cap
    uv run --env-file <path-to>/.env python tests/fixtures/record_finnhub.py news   # step 4 news

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

**Nothing here loads ``.env``, in any mode** (rule 6). Every mode reads the
process environment and nothing else, exactly as ``corollary/`` does; the
launcher's ``--env-file`` is what puts the key there. The default path used to
call ``load_dotenv`` itself -- the news mode did not -- and
``tests/data/news/test_recorders_read_no_env_file.py`` now asserts, over the
syntax tree, that this file neither imports ``dotenv`` nor calls
``load_dotenv``.
"""

import json
import sys
from datetime import date, datetime, timezone
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


#: Phase 3 step 4's news recordings, as ``fixture name -> (path, params)``.
#: Filled in by :func:`news_requests` because the dates are relative to the
#: day of recording. Every name carries the ``p4_`` prefix, and
#: :func:`save_news` refuses to overwrite a file that already exists.
NEWS_PREFIX: Final = "p4_"

#: Rows kept per recorded news body. The full count is recorded beside it in
#: ``row_count``, with the ``related`` tally computed *before* truncation, so
#: the fixture states the measurement while staying small enough to review.
#: A body at or under this size is kept whole.
NEWS_KEEP_ROWS: Final = 12


def news_requests(today: date) -> dict[str, tuple[str, dict[str, str]]]:
    """Step 4's recordings: NVDA over two days, ARM (a UK issuer) over the same
    two days and over thirty, and one ``category=general`` page."""
    from datetime import timedelta

    yesterday = (today - timedelta(days=1)).isoformat()
    month = (today - timedelta(days=30)).isoformat()
    return {
        "p4_company_news_nvda": (
            "/company-news",
            {"symbol": "NVDA", "from": yesterday, "to": today.isoformat()},
        ),
        "p4_company_news_arm": (
            "/company-news",
            {"symbol": "ARM", "from": yesterday, "to": today.isoformat()},
        ),
        "p4_company_news_arm_30d": (
            "/company-news",
            {"symbol": "ARM", "from": month, "to": today.isoformat()},
        ),
        "p4_market_news_general": ("/news", {"category": "general"}),
    }


def _reject_floats(text: str) -> float:
    """``parse_float`` hook: a news body has no fractional numbers, so one is a
    shape change that re-serialising would silently round. Abort instead."""
    raise SystemExit(f"ABORTED: a news body carries a fractional number ({text})")


def save_news(
    name: str,
    request: str,
    status_code: int,
    body_text: str,
    secrets: list[str],
) -> dict[str, object]:
    """Record one news response, truncated, with its measurements beside it.

    Same proof as :func:`save`: the secret scan runs on the text as written,
    and a hit aborts with nothing on disk. Refuses a non-``p4_`` name and an
    existing file -- a committed fixture is evidence, never overwritten here.
    """
    if not name.startswith(NEWS_PREFIX):
        raise SystemExit(f"refusing fixture name {name!r}: must start with {NEWS_PREFIX}")
    out = OUTPUT_DIR / f"{name}.json"
    if out.exists():
        raise SystemExit(f"refusing to overwrite existing fixture {out}")
    body = json.loads(body_text, parse_float=_reject_floats)
    stats: dict[str, object] = {}
    if isinstance(body, list):
        stats["row_count"] = len(body)
        stats["rows_with_related"] = sum(
            1 for row in body if isinstance(row, dict) and str(row.get("related") or "").strip()
        )
        if body and all(isinstance(row, dict) and "id" in row for row in body):
            stats["max_id"] = max(int(row["id"]) for row in body)
        truncated = {".": len(body)} if len(body) > NEWS_KEEP_ROWS else {}
        kept = body[:NEWS_KEEP_ROWS]
    else:
        truncated = {}
        kept = body
    envelope = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request": request,
        "status_code": status_code,
        "stats": stats,
        "truncated": truncated,
        "body": kept,
    }
    text = json.dumps(envelope, indent=2, ensure_ascii=False) + "\n"
    for secret in secrets:
        if secret and secret.lower() in text.lower():
            raise SystemExit(f"ABORTED: {name} contains a credential. Nothing was written.")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"  wrote {name}.json ({status_code}, {len(text)} bytes) {stats}")
    return stats


def record_news() -> None:
    """``record_finnhub.py news``: step 4's company- and market-news bodies.

    Loads nothing from ``.env`` -- run it as
    ``uv run --env-file <path-to>/.env python tests/fixtures/record_finnhub.py news``
    so the key arrives in the process environment only (rule 6).
    """
    from zoneinfo import ZoneInfo

    credentials = FinnhubCredentials.from_env()
    secrets = [credentials.token]
    today = datetime.now(ZoneInfo("America/New_York")).date()
    with httpx.Client(timeout=30.0) as client:
        for name, (path, params) in news_requests(today).items():
            response = client.get(
                f"{FINNHUB_BASE_URL}{path}",
                params=params,
                headers={FINNHUB_TOKEN_HEADER: credentials.token},
            )
            print(f"{name}: HTTP {response.status_code}")
            try:
                json.loads(response.text)
            except json.JSONDecodeError:
                raise SystemExit(
                    f"ABORTED: {name} answered with non-JSON "
                    f"({len(response.content)} bytes; body not echoed)"
                )
            # The URL carries no token -- it travels in the header -- and is
            # scanned with the body regardless.
            save_news(name, str(response.request.url), response.status_code, response.text, secrets)


def main() -> None:
    if sys.argv[1:] == ["news"]:
        record_news()
        return
    if sys.argv[1:]:
        raise SystemExit(f"unknown mode {sys.argv[1:]!r}; expected nothing or 'news'")
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
                    f"ABORTED: {symbol} answered with non-JSON "
                    f"({len(response.content)} bytes; body not echoed)"
                )
            save(name, response.status_code, response.text, secrets)


if __name__ == "__main__":
    main()
