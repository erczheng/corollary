"""Record Phase 3 step 4's Alpaca fixtures, and measure the untickered feed.

Run by hand, never by the test suite, and **only through the launcher** --
nothing here loads ``.env`` (rule 6; pinned by
``tests/data/news/test_recorders_read_no_env_file.py``)::

    uv run --env-file <path-to>/.env python tests/fixtures/record_alpaca_news.py record
    uv run --env-file <path-to>/.env python tests/fixtures/record_alpaca_news.py measure
    uv run --env-file <path-to>/.env python tests/fixtures/record_alpaca_news.py hunt

* ``record`` writes ``tests/fixtures/alpaca/p4_*.json``: two pages of the
  untickered news feed over a fixed past window, a trimmed sample of the
  active-equity asset list, the standard-root contract checks, and one
  multi-symbol daily-bars page on the historical feed.
* ``roots`` writes only the standard-root contract checks (``record`` runs
  them too, and refuses once any ``p4_`` file it writes exists). Their query
  is the provider's own ``standard_root_params``; the committed
  ``p4_contracts_root_*`` files were captured with this script's older
  params, as ``record_roots`` explains.
* ``measure`` writes nothing. It paginates the last complete UTC day of the
  untickered feed and prints counts only: articles, distinct tags, how many
  are crypto-shaped, how many articles carry no symbol, and which timestamp
  the ``start``/``end`` window filters on.
* ``hunt`` writes nothing unless it finds what it looks for: a ``has_options``
  underlying whose only live contracts are adjusted. It scans the optionable
  list in batches on the trading host, confirms any hit with the exact
  one-request root check, and records that check as
  ``p4_contracts_root_adjusted_only.json``.

Safety properties, enforced below rather than intended:

* **GET only**, through :func:`Recorder.get`, which calls ``httpx`` ``get`` and
  nothing else.
* **No credential is written.** Every body is scrubbed by
  ``record_alpaca.scrub`` (field rules, then the substring pass that sees into
  prose) and the written text is scanned for both halves of the key pair; a
  hit aborts with nothing written.
* **No float.** Bodies are parsed at ``parse_float=Decimal``, and a float
  anywhere in the payload aborts the write rather than being serialised.
* **Never overwrites.** The ``p4_`` prefix is enforced and an existing file is
  refused: a recorded fixture is evidence, and replacing it is a decision to
  make by hand.
* **Budgets.** A minimum interval between requests per host keeps both
  Alpaca buckets far under 200/min.
"""

import asyncio
import json
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Sequence

import httpx

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from corollary.data.news.tradeability import adv_window_start  # noqa: E402
from corollary.calendars import NYSE_TZ  # noqa: E402
from corollary.wire import vendor_detail  # noqa: E402
from corollary.data.providers.alpaca import (  # noqa: E402
    DATA_BASE_URL,
    AlpacaCredentials,
    FeedConfig,
    FeedConfigError,
    standard_root_params,
)
from record_alpaca import dumps_exact, scrub  # noqa: E402

OUTPUT_DIR: Final = Path(__file__).resolve().parent / "alpaca"
PREFIX: Final = "p4_"

#: The fixed past window the news pages are recorded over. A weekday
#: mid-morning in New York, so the feed is busy enough to page.
NEWS_WINDOW_START: Final = "2026-09-23T13:30:00Z"
NEWS_WINDOW_END: Final = "2026-09-23T14:30:00Z"
NEWS_FIXTURE_PAGE: Final = 10
#: The vendor's page ceiling (``limit`` maximum 50, per the reference).
NEWS_PAGE_MAX: Final = 50

#: Assets kept in the trimmed asset-list fixture: standard optionable names,
#: an adjusted-root parent, a class share, an ETF, and a name without options
#: (filled in at run time from the response).
ASSET_SAMPLE: Final = ("AAPL", "NVDA", "SPY", "GME", "XRX", "BRK.B", "AMC")

#: ``[A-Z]{2,}USD`` style pairs -- the crypto tags the news feed carries.
CRYPTO_TAG: Final = re.compile(r"^[A-Z0-9]{2,10}(USD|USDT|USDC|BTC)$")

_MIN_INTERVAL_S: Final = 0.5


class Recorder:
    def __init__(self, client: httpx.AsyncClient, credentials: AlpacaCredentials) -> None:
        self._client = client
        self._credentials = credentials
        self.secrets = (credentials.key_id, credentials.secret_key)
        self._last: dict[str, float] = {}

    async def get(self, base: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        host = httpx.URL(base).host
        wait = self._last.get(host, 0.0) + _MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last[host] = time.monotonic()
        response = await self._client.get(
            base + path, params=params, headers=self._credentials.headers()
        )
        print(f"  GET {host}{path} -> {response.status_code}")
        body = json.loads(response.text, parse_float=Decimal) if response.text else None
        if response.status_code >= 400:
            # Redacted before it is bounded -- truncating first could cut a
            # secret at the boundary so the prefix no longer matches.
            text = vendor_detail(
                response.text, secrets=[s for s in self.secrets if s], limit=300
            )
            print(f"    error body: {text}")
        return {"status_code": response.status_code, "body": body}


def _reject_floats(value: Any, where: str = "$") -> None:
    if isinstance(value, float):
        raise SystemExit(f"ABORTED: a float at {where}; nothing was written.")
    if isinstance(value, dict):
        for key, inner in value.items():
            _reject_floats(inner, f"{where}.{key}")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            _reject_floats(inner, f"{where}[{index}]")


def save(name: str, payload: Any, secrets: Sequence[str]) -> None:
    """Prefix, refuse-to-overwrite, no floats, scrub, scan, write -- in that order."""
    if not name.startswith(PREFIX):
        raise SystemExit(f"ABORTED: {name} lacks the {PREFIX} prefix.")
    target = OUTPUT_DIR / f"{name}.json"
    if target.exists():
        raise SystemExit(
            f"ABORTED: {target.name} exists and is committed evidence. "
            "Nothing was written; replace it by hand if that is the intent."
        )
    _reject_floats(payload)
    text = dumps_exact(scrub(payload)) + "\n"
    for secret in secrets:
        if secret and secret in text:
            raise SystemExit(f"ABORTED: a credential appeared in {name}. Nothing was written.")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # LF explicitly: the repo is LF (.gitattributes), and Windows would write CRLF.
    target.write_bytes(text.encode("utf-8"))
    print(f"  wrote {target.name} ({len(text):,} bytes)")


def _envelope(request: str, recorded: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "request": request,
        **extra,
        **recorded,
    }


def _qs(params: dict[str, Any]) -> str:
    return "&".join(f"{k}={v}" for k, v in params.items())


def _last_session_before(day: date) -> date:
    from corollary.data.news.tradeability import nyse_is_session

    probe = day - timedelta(days=1)
    while not nyse_is_session(probe):
        probe -= timedelta(days=1)
    return probe


def _new_york_today() -> date:
    """The date the provider's root check uses: today in New York, not UTC."""
    return datetime.now(NYSE_TZ).date()


# ----------------------------------------------------------------- record


async def record(rec: Recorder, feeds: FeedConfig, trading: str) -> None:
    print("\nuntickered news, two pages over a fixed past window:")
    params: dict[str, Any] = {
        "start": NEWS_WINDOW_START,
        "end": NEWS_WINDOW_END,
        "limit": NEWS_FIXTURE_PAGE,
        "sort": "asc",
    }
    page1 = await rec.get(DATA_BASE_URL, "/v1beta1/news", params)
    save("p4_news_untickered_page1", _envelope(f"/v1beta1/news?{_qs(params)}", page1), rec.secrets)
    token = (page1["body"] or {}).get("next_page_token")
    params2 = {**params, "page_token": token}
    page2 = await rec.get(DATA_BASE_URL, "/v1beta1/news", params2)
    save(
        "p4_news_untickered_page2",
        _envelope(f"/v1beta1/news?{_qs({**params, 'page_token': '<page1 token>'})}", page2),
        rec.secrets,
    )

    print("\nactive equities, unfiltered vs attributes=has_options:")
    plain_params = {"status": "active", "asset_class": "us_equity"}
    plain = await rec.get(trading, "/v2/assets", plain_params)
    opt_params = {**plain_params, "attributes": "has_options"}
    optionable = await rec.get(trading, "/v2/assets", opt_params)
    plain_rows = plain["body"] or []
    opt_rows = optionable["body"] or []
    with_attr = [a for a in plain_rows if "has_options" in (a.get("attributes") or [])]
    print(
        f"  unfiltered: {len(plain_rows)} assets, {len(with_attr)} carry has_options; "
        f"attributes=has_options: {len(opt_rows)} assets; "
        f"rows with an `attributes` key: {sum(1 for a in plain_rows if 'attributes' in a)}; "
        f"optionable symbols equal: "
        f"{sorted(a['symbol'] for a in with_attr) == sorted(a['symbol'] for a in opt_rows)}"
    )
    keep = [a for a in plain_rows if a.get("symbol") in ASSET_SAMPLE]
    no_options = next(
        (
            a
            for a in sorted(plain_rows, key=lambda r: str(r.get("symbol")))
            if a.get("tradable") and "has_options" not in (a.get("attributes") or [])
        ),
        None,
    )
    not_tradable = next(
        (a for a in sorted(plain_rows, key=lambda r: str(r.get("symbol"))) if not a.get("tradable")),
        None,
    )
    for extra in (no_options, not_tradable):
        if extra is not None and extra not in keep:
            keep.append(extra)
    save(
        "p4_assets_active_sample",
        _envelope(
            f"/v2/assets?{_qs(plain_params)}",
            {"status_code": plain["status_code"], "body": keep},
            trimmed_from=len(plain_rows),
            note=(
                "TRIMMED: the unfiltered active us_equity list, reduced to a sample. "
                f"The full response held {len(plain_rows)} assets, {len(with_attr)} of "
                "them carrying has_options in `attributes`."
            ),
        ),
        rec.secrets,
    )

    await record_roots(rec, trading)
    await record_bars(rec, feeds)


async def record_roots(rec: Recorder, trading: str) -> None:
    # With an explicit expiry horizon. The ``p4_contracts_root_default_window_*``
    # fixtures are the same four requests *without* one, recorded first by
    # mistake and kept as evidence: ``expiration_date_lte`` defaults to the
    # next weekend, so XRX (no weeklies) and GME1 answered empty there.
    #
    # The query comes from the provider's own ``standard_root_params``, so a
    # fixture recorded from now on is exactly the provider's request. The
    # committed ``p4_contracts_root_{aapl,gme,gme1,xrx,adjusted_only}``
    # fixtures predate that: they were captured with this script's older
    # params -- ``expiration_date_gte=<today>``, no ``status``, and a horizon
    # of 31 Dec of year+4 -- where the provider sends ``status=active``, no
    # ``gte`` and today + 1,464 days. Not re-recorded, because the
    # conclusions they carry do not depend on the difference: each answer is
    # one contract (``limit=1``) whose root and underlying are what
    # ``has_standard_contract`` decides on, ``gte=today`` and
    # ``status=active`` both select live contracts, and both horizons reach
    # past every listed LEAPS. Each envelope's ``request`` records what was
    # actually sent.
    print("\nstandard-root checks (underlying_symbols + root_symbol, limit=1, horizon):")
    for name, underlying, root in (
        ("p4_contracts_root_aapl", "AAPL", "AAPL"),
        ("p4_contracts_root_gme", "GME", "GME"),
        ("p4_contracts_root_gme1", "GME", "GME1"),
        ("p4_contracts_root_xrx", "XRX", "XRX"),
    ):
        root_params = standard_root_params(underlying, root, today=_new_york_today())
        got = await rec.get(trading, "/v2/options/contracts", root_params)
        rows = (got["body"] or {}).get("option_contracts") or []
        print(
            f"    {underlying}/{root}: {[(c['symbol'], c['root_symbol'], str(c['size'])) for c in rows]}"
        )
        save(name, _envelope(f"/v2/options/contracts?{_qs(root_params)}", got), rec.secrets)


async def record_bars(rec: Recorder, feeds: FeedConfig) -> None:
    print("\ndaily bars for ADV, historical feed:")
    today = datetime.now(timezone.utc).date()
    last = _last_session_before(today)
    start = adv_window_start(today)
    bar_params = {
        "symbols": "AAPL,SPY,XRX",
        "timeframe": "1Day",
        "start": start.isoformat(),
        "end": last.isoformat(),
        "feed": feeds.stock_historical,
        "adjustment": "split",
        "limit": 10000,
        "sort": "asc",
    }
    got = await rec.get(DATA_BASE_URL, "/v2/stocks/bars", bar_params)
    save("p4_stock_bars_adv", _envelope(f"/v2/stocks/bars?{_qs(bar_params)}", got), rec.secrets)


# ----------------------------------------------------------------- measure


async def measure(rec: Recorder) -> None:
    day = datetime.now(timezone.utc).date() - timedelta(days=1)
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    print(f"\nuntickered news for the complete UTC day {day}:")
    params: dict[str, Any] = {
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": NEWS_PAGE_MAX,
        "sort": "asc",
    }
    rows: list[dict[str, Any]] = []
    token: str | None = None
    pages = 0
    while True:
        got = await rec.get(DATA_BASE_URL, "/v1beta1/news", {**params, "page_token": token} if token else params)
        pages += 1
        body = got["body"] or {}
        rows.extend(body.get("news") or [])
        token = body.get("next_page_token")
        if not token or pages >= 60:
            break
    ids = [r.get("id") for r in rows]
    tags = Counter(t for r in rows for t in (r.get("symbols") or []))
    crypto = sorted(t for t in tags if CRYPTO_TAG.match(t))
    zero = sum(1 for r in rows if not r.get("symbols"))

    def ts(value: Any) -> datetime:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    created_out = sum(1 for r in rows if not (start <= ts(r["created_at"]) <= end + timedelta(seconds=1)))
    updated_out = sum(1 for r in rows if not (start <= ts(r["updated_at"]) <= end + timedelta(seconds=1)))
    edited = sum(1 for r in rows if r["created_at"] != r["updated_at"])
    updated_sorted = [ts(r["updated_at"]) for r in rows] == sorted(ts(r["updated_at"]) for r in rows)
    created_sorted = [ts(r["created_at"]) for r in rows] == sorted(ts(r["created_at"]) for r in rows)
    sources = Counter(str(r.get("source")) for r in rows)
    null_url = sum(1 for r in rows if not r.get("url"))
    blank_headline = sum(1 for r in rows if not str(r.get("headline") or "").strip())
    print(f"  null_or_blank_url={null_url} blank_headline={blank_headline}")
    print(
        f"  pages={pages} complete={not token} articles={len(rows)} distinct_ids={len(set(ids))}\n"
        f"  distinct_tags={len(tags)} crypto_shaped={len(crypto)} zero_symbol_articles={zero}\n"
        f"  created_at outside window={created_out} updated_at outside window={updated_out} "
        f"edited(created!=updated)={edited}\n"
        f"  ascending by updated_at={updated_sorted} ascending by created_at={created_sorted}\n"
        f"  sources={dict(sources)}\n"
        f"  crypto tag sample={crypto[:12]}\n"
        f"  most-tagged={tags.most_common(5)}"
    )


# ----------------------------------------------------------------- hunt


async def hunt(rec: Recorder, trading: str) -> None:
    print("\nhunting a has_options underlying with only adjusted contracts:")
    optionable = await rec.get(
        trading, "/v2/assets", {"status": "active", "asset_class": "us_equity", "attributes": "has_options"}
    )
    symbols = sorted(str(a["symbol"]) for a in optionable["body"] or [] if a.get("tradable"))
    print(f"  {len(symbols)} tradable optionable names")
    today = datetime.now(timezone.utc).date()
    horizon = (today + timedelta(days=45)).isoformat()
    only_adjusted: list[str] = []
    batch_size = 100
    requests = 0
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        standard: set[str] = set()
        adjusted: set[str] = set()
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "underlying_symbols": ",".join(batch),
                "expiration_date_lte": horizon,
                "type": "call",
                "limit": 10000,
            }
            if token:
                params["page_token"] = token
            got = await rec.get(trading, "/v2/options/contracts", params)
            requests += 1
            body = got["body"] or {}
            for c in body.get("option_contracts") or []:
                (standard if c.get("root_symbol") == c.get("underlying_symbol") else adjusted).add(
                    str(c["underlying_symbol"])
                )
            token = body.get("next_page_token")
            if not token:
                break
        hits = sorted(adjusted - standard)
        if hits:
            print(f"    batch {i // batch_size}: adjusted-only within {horizon}: {hits}")
            only_adjusted.extend(hits)
        if requests >= 180:
            print("  stopped at 180 requests")
            break
    print(f"  candidates: {only_adjusted} ({requests} requests)")
    for underlying in only_adjusted:
        root_params = standard_root_params(underlying, underlying, today=_new_york_today())
        got = await rec.get(trading, "/v2/options/contracts", root_params)
        rows = (got["body"] or {}).get("option_contracts") or []
        print(f"    confirm {underlying}: {len(rows)} standard contract(s) at any expiry")
        if not rows:
            any_params = {
                "underlying_symbols": underlying,
                "expiration_date_lte": root_params["expiration_date_lte"],
                "limit": 3,
            }
            sample = await rec.get(trading, "/v2/options/contracts", any_params)
            print(
                "      its contracts: "
                f"{[(c['symbol'], c['root_symbol']) for c in (sample['body'] or {}).get('option_contracts') or []]}"
            )
            save(
                "p4_contracts_root_adjusted_only",
                _envelope(
                    f"/v2/options/contracts?{_qs(root_params)}",
                    got,
                    underlying_contracts_sample=sample["body"],
                ),
                rec.secrets,
            )
            return
    print("  no has_options underlying with only adjusted contracts was found")


MODES: Final = ("record", "roots", "measure", "hunt")


async def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in MODES:
        raise SystemExit(f"usage: record_alpaca_news.py {{{'|'.join(MODES)}}}")
    try:
        feeds = FeedConfig.from_env()
    except FeedConfigError as exc:
        raise SystemExit(f"ABORTED before any request: {exc}") from exc
    credentials = AlpacaCredentials.paper_from_env()
    print(f"credentials: {credentials}; feeds: {feeds}")
    async with httpx.AsyncClient(timeout=60.0) as client:
        rec = Recorder(client, credentials)
        if mode == "record":
            await record(rec, feeds, credentials.trading_base_url)
        elif mode == "roots":
            await record_roots(rec, credentials.trading_base_url)
        elif mode == "measure":
            await measure(rec)
        else:
            await hunt(rec, credentials.trading_base_url)


if __name__ == "__main__":
    asyncio.run(main())
