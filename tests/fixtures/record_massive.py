"""Record real Massive news pages into ``tests/fixtures/massive/``.

Run by hand, never by the test suite, with the key in the process
environment only -- nothing here loads ``.env`` (rule 6)::

    uv run --env-file <path-to>/.env python tests/fixtures/record_massive.py

Records the exact query :class:`~corollary.data.providers.massive
.MassiveProvider` sends -- ascending by ``published_utc`` from a
``published_utc.gt`` cursor -- at ``limit=3`` rather than 1,000, so the
second file is a real ``next_url`` follow and both stay small enough to
review. Step 0's ``p3_reference_news_untickered`` already holds the
``limit=1000`` shape.

``unauthorized`` mode records the vendor's refusal of a key. It is run with a
**deliberately invalid** key set on the command line, never ``.env``::

    MASSIVE_API_KEY=invalid-for-fixture uv run python tests/fixtures/record_massive.py unauthorized

and aborts unless the answer is a 401 or 403 -- a 200 would mean a working
key was supplied, which is not what this mode is for.

Safety, enforced below:

* **No credential is ever written.** The key travels as ``Authorization:
  Bearer``, never in a URL, and the text of every file is scanned for it
  (case-insensitively) before writing. A hit aborts with nothing on disk.
* **No committed fixture is overwritten**, and every name carries ``p4_``.
* **No float is re-serialised.** Bodies are parsed with
  :func:`_reject_floats`: a news body has no fractional numbers, so one is a
  shape change that ``json.dumps`` would silently round. Abort instead.
* **GET only.** ``tests/test_hard_rules.py`` globs ``record_*.py``, so this
  file is inside the vendor-surface write-verb guard by construction.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from corollary.data.providers.massive import (  # noqa: E402
    MASSIVE_BASE_URL,
    MASSIVE_NEWS_PATH,
    MassiveCredentials,
)

OUTPUT_DIR: Final = Path(__file__).resolve().parent / "massive"
PAGE_SIZE: Final = 3

#: What ``unauthorized`` mode states about its own fixture, written beside the
#: body so a reader of the file need not find this script to learn it.
UNAUTHORIZED_NOTE: Final = (
    "Recorded with a deliberately invalid key supplied in the process "
    "environment (not .env). This is Massive's answer to a key it does not "
    "recognise. It is NOT necessarily the answer to a valid key calling an "
    "endpoint above its plan: that case was not recorded, so a plan "
    "refusal's status and body are unverified."
)


def _reject_floats(text: str) -> float:
    """``parse_float`` hook: abort on a fractional number rather than round it."""
    raise SystemExit(f"ABORTED: a Massive body carries a fractional number ({text})")


def _decode(response: httpx.Response) -> Any:
    try:
        return json.loads(response.text, parse_float=_reject_floats)
    except json.JSONDecodeError:
        raise SystemExit(
            f"ABORTED: HTTP {response.status_code} answered with non-JSON "
            f"({len(response.text)} bytes); nothing was written"
        )


def save(
    name: str,
    request: str,
    status_code: int,
    body: Any,
    secret: str,
    *,
    note: str | None = None,
) -> None:
    if not name.startswith("p4_"):
        raise SystemExit(f"refusing fixture name {name!r}: must start with p4_")
    out = OUTPUT_DIR / f"{name}.json"
    if out.exists():
        raise SystemExit(f"refusing to overwrite existing fixture {out}")
    envelope: dict[str, Any] = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request": request,
        "status_code": status_code,
    }
    if note is not None:
        envelope["note"] = note
    envelope["body"] = body
    text = json.dumps(envelope, indent=2, ensure_ascii=False) + "\n"
    if secret.lower() in text.lower():
        raise SystemExit(f"ABORTED: {name} contains the key. Nothing was written.")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"  wrote {name}.json ({status_code}, {len(text)} bytes)")


def _first_page_params() -> dict[str, str]:
    since = (datetime.now(timezone.utc) - timedelta(hours=6)).replace(microsecond=0)
    return {
        "limit": str(PAGE_SIZE),
        "order": "asc",
        "sort": "published_utc",
        "published_utc.gt": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def record_pages() -> None:
    credentials = MassiveCredentials.from_env()
    headers = credentials.headers()
    with httpx.Client(timeout=30.0) as client:
        first = client.get(
            f"{MASSIVE_BASE_URL}{MASSIVE_NEWS_PATH}",
            params=_first_page_params(),
            headers=headers,
        )
        print(f"page 1: HTTP {first.status_code}")
        body = _decode(first)
        save("p4_reference_news_asc_page1", str(first.request.url), first.status_code, body, credentials.token)
        next_url = body.get("next_url") if isinstance(body, dict) else None
        if not next_url:
            raise SystemExit("page 1 carried no next_url; nothing to follow")
        target, base = httpx.URL(next_url), httpx.URL(MASSIVE_BASE_URL)
        if (target.scheme, target.host, target.port) != (base.scheme, base.host, base.port):
            raise SystemExit("next_url points at another origin; not sending the key there")
        if target.userinfo:
            raise SystemExit("next_url carries userinfo; not following it")
        second = client.get(target, headers=headers)
        print(f"page 2: HTTP {second.status_code}")
        save("p4_reference_news_asc_page2", str(second.request.url), second.status_code, _decode(second), credentials.token)


def record_unauthorized() -> None:
    credentials = MassiveCredentials.from_env()
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{MASSIVE_BASE_URL}{MASSIVE_NEWS_PATH}",
            params=_first_page_params(),
            headers=credentials.headers(),
        )
    print(f"unauthorized: HTTP {response.status_code}")
    if response.status_code not in (401, 403):
        raise SystemExit(
            f"ABORTED: expected a refusal, got HTTP {response.status_code}. Run this "
            "mode with a deliberately invalid MASSIVE_API_KEY. Nothing was written."
        )
    save(
        "p4_unauthorized",
        str(response.request.url),
        response.status_code,
        _decode(response),
        credentials.token,
        note=UNAUTHORIZED_NOTE,
    )


def main() -> None:
    if sys.argv[1:] == ["unauthorized"]:
        record_unauthorized()
        return
    if sys.argv[1:]:
        raise SystemExit(f"unknown mode {sys.argv[1:]!r}; expected nothing or 'unauthorized'")
    record_pages()


if __name__ == "__main__":
    main()
