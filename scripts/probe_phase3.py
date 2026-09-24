"""Phase 3 step 0 -- keyed probes and the label-volume measurement.

Run by hand, never by the test suite, and **only through the launcher** so no
agent ever reads ``.env`` (rule 6)::

    uv run --env-file <path-to>/.env python scripts/probe_phase3.py probes
    uv run --env-file <path-to>/.env python scripts/probe_phase3.py probes --overwrite
    uv run --env-file <path-to>/.env python scripts/probe_phase3.py labels
    uv run python scripts/probe_phase3.py stocktwits-poll --duration 3600

An existing ``p3_*.json`` is committed evidence and is refused by default; a
deliberate re-probe (``probes``, ``stocktwits-poll``) passes ``--overwrite``
to replace it. ``labels`` and ``scan`` write no fixture and take no such flag.

Nothing here loads ``.env`` itself. Keys come from ``os.environ`` only.

Safety properties, enforced below rather than intended:

* **GET only.** Every request goes through :func:`Http.get`, which calls
  ``httpx.Client.get`` and nothing else. No POST/PUT/DELETE, no MCP (rule 3),
  no ``alpaca`` SDK import (the vendor-import rule), no ``corollary`` provider
  class (none of them is needed and some open sockets).
* **No credential is printed, logged or written.** Keys travel in headers
  wherever the vendor allows it (Finnhub ``X-Finnhub-Token``, Alpaca
  ``APCA-*``, Massive ``Authorization: Bearer``). FRED has no header form, so
  its key is in the query string -- which is why *every* URL and *every*
  exception message passes through :func:`redact` before it is printed, and
  every fixture is scrubbed and then scanned (:func:`save_fixture`): the scan
  runs on the text as written, and a surviving key aborts the write.
* **Redaction sees into prose**, the lesson of ``tests/fixtures/record_alpaca.py``:
  a key embedded in an English sentence or a ``next_url`` is caught by the
  substring pass, not only by a field rule.
* **Budgets are honoured** by a per-host minimum interval, and a 429 is met
  with a back-off (``Retry-After`` if given), never a retry storm.

Outputs: redacted fixtures under ``tests/fixtures/{finnhub,alpaca,massive,
stocktwits,fred}/p3_*.json`` (the ``p3_`` prefix is enforced, so a pre-Phase-3
fixture is never overwritten, and an existing ``p3_`` one is replaced only
under ``--overwrite``) and machine-readable results under
``.claude/scratch/phase3_probe/`` (gitignored).
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Sequence

import httpx

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from corollary.calendars import nyse_session_close  # noqa: E402

FIXTURES: Final = REPO_ROOT / "tests" / "fixtures"
SCRATCH: Final = REPO_ROOT / ".claude" / "scratch" / "phase3_probe"

#: Environment variables whose *values* must never leave this process.
#: ``DISCORD_WEBHOOK_URL`` is deliberately not read at all.
SECRET_ENV: Final = (
    "FINNHUB_API_KEY",
    "MASSIVE_API_KEY",
    "FRED_API_KEY",
    "ALPACA_PAPER_API_KEY",
    "ALPACA_PAPER_SECRET_KEY",
)

FINNHUB: Final = "https://finnhub.io/api/v1"
ALPACA_DATA: Final = "https://data.alpaca.markets"
MASSIVE: Final = "https://api.massive.com"
STOCKTWITS: Final = "https://api.stocktwits.com/api/2"
FRED: Final = "https://api.stlouisfed.org/fred"

#: Minimum seconds between two requests to one host. Massive's 5/min is
#: verified to 429 on the fifth call in a minute, so 13s keeps four in any
#: sixty-second window. The rest sit well inside their published budgets.
HOST_INTERVAL: Final[Mapping[str, float]] = {
    "finnhub.io": 1.1,  # 60/min
    "data.alpaca.markets": 0.35,  # 200/min
    "api.massive.com": 13.0,  # 5/min
    "api.stlouisfed.org": 0.6,  # 120/min
    "api.stocktwits.com": 20.0,  # 180/hr of a ~200/hr budget
}

# --------------------------------------------------------------------------
# The watch universe (decision 12)
# --------------------------------------------------------------------------

#: The Markets universe, copied from ``UNIVERSE`` in
#: ``corollary/api/routes/markets.py`` (26 names) rather than imported, so the
#: probe does not pull the API package in.
MARKETS_UNIVERSE: Final = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "LLY",
    "WMT", "JPM", "UNH", "XOM", "COST", "HD", "SPY", "QQQ", "IWM", "XLE",
    "ARKK", "ARM", "ALAB", "RDDT", "RBRK", "CRWV", "CRCL",
)  # fmt: skip

#: **APPROXIMATION -- not the seed.** Decision 6's committed seed of the top
#: five holdings of each of the eleven SPDR sector ETFs does not exist until
#: step 4. This is a hand-written approximation of it, from memory of those
#: funds' largest holdings, good enough to size a label-volume estimate and
#: for nothing else. Step 4 replaces it.
SECTOR_LEADERS_APPROX: Final[Mapping[str, tuple[str, ...]]] = {
    "XLK": ("NVDA", "MSFT", "AAPL", "AVGO", "ORCL"),
    "XLF": ("BRK.B", "JPM", "V", "MA", "BAC"),
    "XLV": ("LLY", "JNJ", "ABBV", "UNH", "MRK"),
    "XLY": ("AMZN", "TSLA", "HD", "MCD", "BKNG"),
    "XLC": ("META", "GOOGL", "GOOG", "NFLX", "DIS"),
    "XLI": ("GE", "CAT", "RTX", "UBER", "GEV"),
    "XLP": ("WMT", "COST", "PG", "KO", "PM"),
    "XLE": ("XOM", "CVX", "COP", "WMB", "EOG"),
    "XLU": ("NEE", "SO", "CEG", "DUK", "VST"),
    "XLRE": ("WELL", "PLD", "AMT", "EQIX", "SPG"),
    "XLB": ("LIN", "SHW", "NEM", "ECL", "APD"),
}

#: Open-position underlyings are **omitted**: the probe does not read the
#: account, and the paper book's positions would change the set by a handful.
WATCH_TICKERS: Final[frozenset[str]] = frozenset(MARKETS_UNIVERSE) | frozenset(
    t for leaders in SECTOR_LEADERS_APPROX.values() for t in leaders
)

# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

REDACTED: Final = "<redacted>"

#: Query parameters and JSON fields that carry a credential in some vendor's
#: spelling. Matched case-insensitively; the value is replaced, the name kept.
_CRED_PARAM = re.compile(
    r"(?i)((?:api[_-]?key|apikey|access[_-]?token|token|secret(?:[_-]?key)?|"
    r"key[_-]?id|password)[\"']?\s*[=:]\s*[\"']?)([^&\s\"'<>,}]{6,})"
)
_SK_TOKEN = re.compile(r"sk-[A-Za-z0-9_-]{20,}")
_WEBHOOK = re.compile(r"(?i)https?://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+")


def secrets() -> list[str]:
    return [v for name in SECRET_ENV if (v := os.environ.get(name, "").strip())]


def redact(text: str) -> str:
    """Scrub every known secret and anything credential-shaped out of ``text``.

    The substring pass runs first and is case-insensitive, so a key quoted in
    prose, a ``next_url`` or an exception message is caught wherever it sits.
    The pattern passes then catch credential-shaped values whose literal this
    process does not hold.
    """
    for secret in secrets():
        text = re.sub(re.escape(secret), REDACTED, text, flags=re.IGNORECASE)
    text = _CRED_PARAM.sub(
        lambda m: m.group(1) + (m.group(2) if m.group(2) == REDACTED else REDACTED),
        text,
    )
    text = _SK_TOKEN.sub(REDACTED, text)
    return _WEBHOOK.sub(REDACTED, text)


_CRED_QUERY = re.compile(
    r"(?i)([?&])(?:api[_-]?key|apikey|access[_-]?token|token)=[^&#\s\"']*&?"
)


def safe_url(url: str) -> str:
    """A URL fit to record: redacted, then with credential parameters removed outright.

    Removed rather than left as ``api_key=<redacted>``, so the recorded URL
    carries nothing a credential scanner could mistake for a key.
    """
    stripped = _CRED_QUERY.sub(lambda m: m.group(1), redact(url))
    return stripped.rstrip("?&")


def leaked(text: str) -> list[str]:
    """Names (never values) of the env vars whose value appears in ``text``."""
    low = text.lower()
    return [
        name
        for name in SECRET_ENV
        if (v := os.environ.get(name, "").strip()) and v.lower() in low
    ]


def say(message: str) -> None:
    """The only print in this file. Everything shown passes :func:`redact`."""
    print(redact(message), flush=True)


def save_fixture(
    vendor: str, name: str, envelope: Mapping[str, Any], *, overwrite: bool = False
) -> Path:
    """Scrub, then scan the text as written, then write. The order is the guarantee.

    An existing ``p3_*.json`` is reviewed, committed evidence, so it is
    refused unless ``overwrite`` is passed -- set only by ``--overwrite`` on
    the command line. The check sits after the scan and before the write:
    the scrub and the scan always run, and a refusal writes nothing.
    """
    if not name.startswith("p3_"):
        raise SystemExit(f"refusing fixture name {name!r}: must start with p3_")
    text = redact(json.dumps(envelope, indent=2, ensure_ascii=False, default=str)) + "\n"
    hits = leaked(text)
    if hits or _SK_TOKEN.search(text) or _WEBHOOK.search(text):
        raise SystemExit(f"ABORTED: {vendor}/{name} still holds {hits}; nothing written")
    out = FIXTURES / vendor / f"{name}.json"
    if out.exists() and not overwrite:
        raise SystemExit(
            f"refusing to overwrite existing fixture {out}: pass --overwrite to replace it"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    say(f"  wrote tests/fixtures/{vendor}/{name}.json ({len(text):,} bytes)")
    return out


def save_scratch(name: str, payload: Any) -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    text = redact(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    if leaked(text):
        raise SystemExit(f"ABORTED: scratch {name} holds a credential")
    (SCRATCH / name).write_text(text + "\n", encoding="utf-8")


def truncate(body: Any, keep: int = 5) -> tuple[Any, dict[str, int]]:
    """Cut every list longer than ``keep`` (recursively), reporting original sizes."""
    cut: dict[str, int] = {}

    def walk(value: Any, path: str) -> Any:
        if isinstance(value, list):
            if len(value) > keep:
                cut[path or "$"] = len(value)
            return [walk(v, f"{path}[]") for v in value[:keep]]
        if isinstance(value, dict):
            items = list(value.items())
            # dict-of-contracts (option snapshots) is a list in disguise
            if len(items) > keep * 4 and all(isinstance(v, dict) for _, v in items):
                cut[path or "$"] = len(items)
                items = items[:keep]
            return {k: walk(v, f"{path}.{k}") for k, v in items}
        return value

    return walk(body, ""), cut


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


@dataclass
class Reply:
    status: int
    content_type: str
    url: str  # already redacted
    headers: dict[str, str]
    text: str
    body: Any  # parsed JSON, or None

    def envelope(self, body: Any | None = None, **extra: Any) -> dict[str, Any]:
        env: dict[str, Any] = {
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "request": self.url,
            "status_code": self.status,
            "content_type": self.content_type,
        }
        env.update(extra)
        if body is not None:
            env["body"] = body
        elif self.body is not None:
            env["body"] = self.body
        else:
            env["body_text_head"] = self.text[:500]
        return env


class Http:
    """GET with per-host pacing and 429 back-off. Nothing but ``get`` is called."""

    def __init__(self, user_agent: str | None = None) -> None:
        headers = {"User-Agent": user_agent} if user_agent else {}
        self._client = httpx.Client(timeout=30.0, headers=headers, follow_redirects=False)
        self._last: dict[str, float] = {}
        self.requests: Counter[str] = Counter()
        self.throttled: Counter[str] = Counter()

    def close(self) -> None:
        self._client.close()

    def _pace(self, host: str) -> None:
        gap = HOST_INTERVAL.get(host, 1.0)
        wait = self._last.get(host, 0.0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        *,
        retries: int = 3,
    ) -> Reply | None:
        host = httpx.URL(url).host
        for attempt in range(retries + 1):
            self._pace(host)
            self.requests[host] += 1
            try:
                r = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                say(f"  {host}: {type(exc).__name__}: {exc}")
                return None
            if r.status_code == 429 and attempt < retries:
                self.throttled[host] += 1
                retry_after = r.headers.get("retry-after", "")
                delay = float(retry_after) if retry_after.isdigit() else 30.0 * (attempt + 1)
                say(f"  {host}: 429, backing off {delay:.0f}s")
                time.sleep(delay)
                self._last[host] = time.monotonic()
                continue
            ctype = r.headers.get("content-type", "")
            try:
                body = r.json() if "json" in ctype or r.text[:1] in "[{" else None
            except ValueError:
                body = None
            return Reply(
                status=r.status_code,
                content_type=ctype,
                url=safe_url(str(r.request.url)),
                headers={k.lower(): v for k, v in r.headers.items()},
                text=r.text,
                body=body,
            )
        return None


def need(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not in the environment -- run through --env-file")
    return value


def finnhub_headers() -> dict[str, str]:
    return {"X-Finnhub-Token": need("FINNHUB_API_KEY")}


def alpaca_headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": need("ALPACA_PAPER_API_KEY"),
        "APCA-API-SECRET-KEY": need("ALPACA_PAPER_SECRET_KEY"),
    }


def massive_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {need('MASSIVE_API_KEY')}"}


def options_feed() -> str:
    """Feed name from config, never a literal (``ALPACA_OPTIONS_FEED``)."""
    return need("ALPACA_OPTIONS_FEED")


def one_line(tag: str, finding: str, results: dict[str, Any]) -> None:
    results.setdefault("findings", {})[tag] = finding
    say(f"FINDING {tag}: {finding}")


# --------------------------------------------------------------------------
# Sessions (from the market calendar, never hardcoded hours)
# --------------------------------------------------------------------------


def completed_sessions(n: int, now: datetime) -> list[tuple[date, datetime]]:
    """The last ``n`` sessions whose close is before ``now``, oldest first."""
    found: list[tuple[date, datetime]] = []
    day = now.date()
    while len(found) < n + 1:
        close = nyse_session_close(day)
        if close is not None and close <= now:
            found.append((day, close))
        day -= timedelta(days=1)
    return list(reversed(found))  # n+1: the first is the window's lower bound


class SessionBinner:
    """Attribute an instant to the session whose close is the first at or after it.

    Overnight and weekend news lands on the next session -- the first one in
    which anyone could act on it.
    """

    def __init__(self, sessions: list[tuple[date, datetime]]) -> None:
        self.start = sessions[0][1]
        self.days = [d for d, _ in sessions[1:]]
        self.closes = [c for _, c in sessions[1:]]

    def session(self, at: datetime) -> date | None:
        if at <= self.start or at > self.closes[-1]:
            return None
        return self.days[bisect.bisect_left(self.closes, at)]


def parse_instant(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


# --------------------------------------------------------------------------
# DRAFT rules patterns -- an estimate only; step 5 builds and re-measures the real ones
# --------------------------------------------------------------------------

#: **DRAFT.** PRD §9's eight patterns as plain case-insensitive regexes over a
#: headline, each with a direction. Written for step 0's volume *estimate*;
#: step 5 owns the real patterns and must re-measure. Any headline matching
#: both directions gets no label (ambiguous -> no label).
DRAFT_PATTERNS: Final[tuple[tuple[str, str, re.Pattern[str]], ...]] = tuple(
    (name, direction, re.compile(rx, re.IGNORECASE))
    for name, direction, rx in (
        # "tops" as a verb only: "Top Analyst Forecasts" is a roundup, not a beat
        ("beat", "bullish",
         r"\b(beats?|tops|topped|surpass(es|ed)?|exceeds?|exceeded)\b.{0,60}\b(estimates?|expectations|consensus|views?|forecasts?)\b"),
        ("miss", "bearish",
         r"\b(miss(es|ed)?|falls? short of|trails?|below)\b.{0,60}\b(estimates?|expectations|consensus|views?|forecasts?)\b"),
        ("guidance_raised", "bullish",
         r"\b(raises?|raised|boosts?|lifts?|ups|hikes?|increases?)\b.{0,40}\b(guidance|outlook|forecast)\b"),
        ("guidance_cut", "bearish",
         r"\b(cuts?|lowers?|lowered|slashes?|reduces?|trims?|withdraws?|pulls?)\b.{0,40}\b(guidance|outlook|forecast)\b"),
        # an analyst rating change, not a product "upgrade cycle"
        ("upgrade", "bullish",
         r"\bupgrades?\b.{0,60}\bto (a )?(strong buy|buy|overweight|outperform|accumulate|add|positive|neutral|hold|equal[- ]weight|market perform|sector perform|peer perform|in-line)\b|\b(stock |shares )?upgraded\b"),
        ("downgrade", "bearish",
         r"\bdowngrades?\b.{0,60}\bto (a )?(strong sell|sell|underweight|underperform|reduce|negative|neutral|hold|equal[- ]weight|market perform|sector perform|peer perform|in-line)\b|\b(stock |shares )?downgraded\b"),
        ("mna_target", "bullish",
         r"\b(to be acquired|agrees? to be (acquired|bought)|to be taken private|receives? .{0,30}(takeover|buyout|acquisition) (offer|bid|proposal))\b"),
        ("secondary_offering", "bearish",
         r"\b(secondary|follow-on)( public| stock| share)? offering\b|\b(common )?(stock|share) offering\b"),
        ("buyback", "bullish",
         r"\b(share|stock) (buyback|repurchase)\b|\brepurchase (program|plan|authori[sz]ation)\b"),
        ("exec_departure", "bearish",
         r"\b(ceo|cfo|coo|chief executive|chief financial|chief operating|chairman)\b.{0,40}\b(resigns?|steps? down|departs?|to leave|leaving|exits?|ousted|fired|to retire|retires?)\b"),
        ("fda_approval", "bullish",
         r"\bfda (approves?|approved|clears?|cleared|grants?)\b|\b(wins?|receives?|gets?|secures?|granted) (full |accelerated )?fda (approval|clearance)\b"),
        ("fda_rejection", "bearish",
         r"\bcomplete response letter\b|\bCRL\b|\bfda (rejects?|rejected|declines? to approve|refuses?)\b"),
    )
)  # fmt: skip


def draft_label(headline: str) -> tuple[str | None, list[str]]:
    hits = [(name, d) for name, d, rx in DRAFT_PATTERNS if rx.search(headline)]
    directions = {d for _, d in hits}
    if len(directions) != 1:
        return None, [n for n, _ in hits]
    return directions.pop(), [n for n, _ in hits]


def normalise_headline(headline: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", headline.lower()).split())


#: **DRAFT attribution, for the estimate only.** A label counts for a ticker
#: only when the headline names it: ``(TICKER)``, ``$TICKER``,
#: ``NYSE:``/``Nasdaq:TICKER``, a bare uppercase ticker of 3+ letters that is
#: not an English word, or one of these hand-written company names (matched
#: case-sensitively). Vendor ticker tags alone are too loose -- Finnhub's
#: ``/company-news`` returned Oracle and Grab headlines for ALAB -- and a
#: roundup headline tagged with ten tickers is not a label on any of them.
COMPANY_NAMES: Final[Mapping[str, tuple[str, ...]]] = {
    "AAPL": ("Apple",), "MSFT": ("Microsoft",), "NVDA": ("Nvidia", "NVIDIA"),
    "AMZN": ("Amazon",), "GOOGL": ("Alphabet", "Google"), "GOOG": ("Alphabet", "Google"),
    "META": ("Meta Platforms", "Meta"), "AVGO": ("Broadcom",), "TSLA": ("Tesla",),
    "LLY": ("Eli Lilly", "Lilly"), "WMT": ("Walmart",), "JPM": ("JPMorgan Chase",),
    "UNH": ("UnitedHealth",), "XOM": ("Exxon", "ExxonMobil"), "COST": ("Costco",),
    "HD": ("Home Depot",), "ARM": ("Arm Holdings",), "ALAB": ("Astera Labs",),
    "RDDT": ("Reddit",), "RBRK": ("Rubrik",), "CRWV": ("CoreWeave",),
    "CRCL": ("Circle Internet",), "ARKK": ("ARK Innovation",),
    "ORCL": ("Oracle",), "BRK.B": ("Berkshire",), "V": ("Visa",), "MA": ("Mastercard",),
    "BAC": ("Bank of America", "BofA"), "JNJ": ("Johnson & Johnson", "J&J"),
    "ABBV": ("AbbVie",), "MRK": ("Merck",), "MCD": ("McDonald's", "McDonald’s"),
    "BKNG": ("Booking Holdings",), "NFLX": ("Netflix",), "DIS": ("Disney",),
    "GE": ("GE Aerospace", "General Electric"), "CAT": ("Caterpillar",),
    "RTX": ("Raytheon", "Pratt & Whitney"), "UBER": ("Uber",), "GEV": ("GE Vernova",),
    "PG": ("Procter & Gamble", "P&G"), "KO": ("Coca-Cola",), "PM": ("Philip Morris",),
    "CVX": ("Chevron",), "COP": ("ConocoPhillips",), "WMB": ("Williams Companies",),
    "NEE": ("NextEra",), "SO": ("Southern Company", "Southern Co"),
    "CEG": ("Constellation Energy",), "DUK": ("Duke Energy",), "VST": ("Vistra",),
    "WELL": ("Welltower",), "PLD": ("Prologis",), "AMT": ("American Tower",),
    "EQIX": ("Equinix",), "SPG": ("Simon Property",), "LIN": ("Linde",),
    "SHW": ("Sherwin-Williams",), "NEM": ("Newmont",), "ECL": ("Ecolab",),
    "APD": ("Air Products",),
}  # fmt: skip

#: Uppercase tickers that are also words, so a bare match proves nothing.
_WORD_TICKERS: Final = frozenset({"ARM", "CAT", "WELL", "LIN", "DIS", "COST", "UBER", "META"})

#: A name immediately followed by a rating verb is the *broker*, not the
#: subject: "JP Morgan Upgrades Meta" is not a label on JPM.
_BROKER_VERB = r"(\s+(Securities|Research|Capital))?\s+(upgrades|downgrades|initiates|reiterates|maintains|(raises|lowers|cuts) (its )?price target)\b"


def _mention_pattern(ticker: str) -> re.Pattern[str]:
    t = re.escape(ticker)
    forms = [rf"\({t}\)", rf"\${t}\b", rf"\b(NYSE|NASDAQ|Nasdaq):\s?{t}\b"]
    if len(ticker) >= 3 and ticker not in _WORD_TICKERS:
        forms.append(rf"(?<![A-Za-z.]){t}(?![A-Za-z])")
    forms += [rf"\b{re.escape(n)}(?!(?i:{_BROKER_VERB}))" for n in COMPANY_NAMES.get(ticker, ())]
    return re.compile("|".join(forms))


_MENTION: Final[Mapping[str, re.Pattern[str]]] = {t: _mention_pattern(t) for t in WATCH_TICKERS}


def named_in(headline: str, tickers: Iterable[str]) -> set[str]:
    """The watch-universe tickers that ``headline`` itself names (draft attribution)."""
    return {t for t in tickers if t in _MENTION and _MENTION[t].search(headline)}


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------


def probe_finnhub(http: Http, today: date, results: dict[str, Any], *, overwrite: bool) -> None:
    h = finnhub_headers()
    premium: dict[str, Any] = {}
    for path, params in (
        ("/calendar/economic", {"from": today.isoformat(), "to": (today + timedelta(days=7)).isoformat()}),
        ("/etf/holdings", {"symbol": "XLK"}),
        ("/stock/dividend", {"symbol": "AAPL", "from": "2025-01-01", "to": today.isoformat()}),
        ("/stock/dividend2", {"symbol": "AAPL"}),
    ):
        r = http.get(FINNHUB + path, params, h)
        if r is None:
            premium[path] = "transport error"
            continue
        premium[path] = {"status": r.status, "content_type": r.content_type, "head": r.text[:120]}
        body, cut = truncate(r.body) if r.body is not None else (None, {})
        name = "p3_premium_" + path.strip("/").replace("/", "_")
        save_fixture("finnhub", name, r.envelope(body, truncated=cut), overwrite=overwrite)
    results["finnhub_premium"] = premium
    one_line("1 finnhub premium", "; ".join(
        f"{p} -> {v['status']} {v['content_type'].split(';')[0]}" if isinstance(v, dict) else f"{p} -> {v}"
        for p, v in premium.items()), results)

    # 2. earnings calendar, next 3 weeks
    r = http.get(FINNHUB + "/calendar/earnings",
                 {"from": today.isoformat(), "to": (today + timedelta(days=21)).isoformat()}, h)
    if r is not None and isinstance(r.body, dict):
        rows = r.body.get("earningsCalendar") or []
        dates = sorted({row.get("date") for row in rows if row.get("date")})
        upcoming = [row for row in rows if str(row.get("date", "")) >= today.isoformat()]
        hours = Counter(repr(row.get("hour")) for row in rows)
        watch_rows = [row for row in rows if row.get("symbol") in WATCH_TICKERS]
        results["finnhub_earnings"] = {
            "rows": len(rows), "upcoming_rows": len(upcoming),
            "first_date": dates[0] if dates else None, "last_date": dates[-1] if dates else None,
            "distinct_dates": len(dates), "hour_values": dict(hours),
            "watch_universe_rows": [{k: row.get(k) for k in ("symbol", "date", "hour")} for row in watch_rows],
        }
        body, cut = truncate(r.body, keep=8)
        save_fixture("finnhub", "p3_calendar_earnings", r.envelope(body, truncated=cut), overwrite=overwrite)
        one_line("2 finnhub earnings", f"HTTP {r.status}; {len(rows)} rows, {len(upcoming)} dated >= today, "
                 f"dates {dates[:1]}..{dates[-1:]}; hour values {dict(hours)}", results)
    else:
        one_line("2 finnhub earnings", f"no JSON: {r.status if r else 'transport error'}", results)

    # 3. recommendation trends
    r = http.get(FINNHUB + "/stock/recommendation", {"symbol": "AAPL"}, h)
    if r is not None:
        body, cut = truncate(r.body, keep=4) if r.body is not None else (None, {})
        save_fixture("finnhub", "p3_stock_recommendation_aapl", r.envelope(body, truncated=cut), overwrite=overwrite)
        keys = sorted(r.body[0].keys()) if isinstance(r.body, list) and r.body else []
        n = len(r.body) if isinstance(r.body, list) else 0
        one_line("3 finnhub recommendation", f"HTTP {r.status}; {n} periods; keys {keys}", results)

    # 4. quote for an index
    quotes: dict[str, Any] = {}
    for sym in ("^VIX", "VIX", "^GSPC"):
        r = http.get(FINNHUB + "/quote", {"symbol": sym}, h)
        if r is None:
            continue
        quotes[sym] = {"status": r.status, "body": r.body if r.body is not None else r.text[:120]}
        safe = sym.replace("^", "caret_").lower()
        save_fixture("finnhub", f"p3_quote_{safe}", r.envelope(), overwrite=overwrite)
    results["finnhub_quote_index"] = quotes
    one_line("4 finnhub index quote", "; ".join(f"{s} -> {v['status']} {v['body']}" for s, v in quotes.items()), results)


def probe_alpaca(http: Http, today: date, now: datetime, results: dict[str, Any], *, overwrite: bool) -> None:
    h = alpaca_headers()
    # 5. news shape
    r = http.get(ALPACA_DATA + "/v1beta1/news", {"limit": 5, "symbols": "AAPL"}, h)
    if r is not None:
        save_fixture("alpaca", "p3_news", r.envelope(), overwrite=overwrite)
        news = (r.body or {}).get("news") or [] if isinstance(r.body, dict) else []
        keys = sorted(news[0].keys()) if news else []
        one_line("5 alpaca news", f"HTTP {r.status}; top-level {sorted(r.body.keys()) if isinstance(r.body, dict) else None}; "
                 f"article keys {keys}", results)

    # 6. corporate actions: announced dividends ahead of ex_date?
    rows: list[dict[str, Any]] = []
    token: str | None = None
    first: Reply | None = None
    for _ in range(20):
        params: dict[str, Any] = {"types": "cash_dividend", "start": today.isoformat(),
                                  "end": (today + timedelta(days=60)).isoformat(), "limit": 1000}
        if token:
            params["page_token"] = token
        r = http.get(ALPACA_DATA + "/v1/corporate-actions", params, h)
        if r is None or not isinstance(r.body, dict):
            break
        first = first or r
        rows.extend((r.body.get("corporate_actions") or {}).get("cash_dividends") or [])
        token = r.body.get("next_page_token")
        if not token:
            break
    if first is not None:
        body, cut = truncate(first.body, keep=6)
        save_fixture("alpaca", "p3_corporate_actions_cash_dividend", first.envelope(body, truncated=cut), overwrite=overwrite)
        ahead = sorted((date.fromisoformat(row["ex_date"]) - today).days for row in rows if row.get("ex_date"))
        future = [d for d in ahead if d > 0]
        watch = sorted({str(row.get("symbol")) for row in rows if row.get("symbol") in WATCH_TICKERS})
        results["alpaca_dividends"] = {
            "rows": len(rows), "future_ex_date_rows": len(future),
            "days_ahead_max": max(future) if future else None,
            "days_ahead_median": statistics.median(future) if future else None,
            "days_ahead_histogram_weeks": dict(Counter(d // 7 for d in future)),
            "watch_universe_symbols": watch, "keys": sorted(rows[0].keys()) if rows else [],
            "status": first.status,
        }
        one_line("6 alpaca dividends", f"HTTP {first.status}; {len(rows)} rows ex_date in [today, +60d], "
                 f"{len(future)} strictly future; days ahead max {max(future) if future else None}, "
                 f"median {statistics.median(future) if future else None}; watch-universe payers {watch}", results)

    # 7. indicative option snapshot dailyBar.v
    feed = options_feed()
    r = http.get(ALPACA_DATA + "/v1beta1/options/snapshots/SPY",
                 {"feed": feed, "limit": 1000,
                  "expiration_date_lte": (today + timedelta(days=10)).isoformat()}, h)
    if r is not None and isinstance(r.body, dict):
        snaps: dict[str, Any] = r.body.get("snapshots") or {}
        with_bar = {k: v for k, v in snaps.items() if isinstance(v.get("dailyBar"), dict)}
        vols = [int(v["dailyBar"].get("v") or 0) for v in with_bar.values()]
        prev = {k: v["prevDailyBar"] for k, v in snaps.items() if isinstance(v.get("prevDailyBar"), dict)}
        bar_days = Counter(str(v["dailyBar"].get("t", ""))[:10] for v in with_bar.values())
        top = sorted(prev.items(), key=lambda kv: -int(kv[1].get("v") or 0))[:5]
        compare: list[dict[str, Any]] = []
        if top:
            day_s = str(top[0][1].get("t", ""))[:10]
            rb = http.get(ALPACA_DATA + "/v1beta1/options/bars",
                          {"symbols": ",".join(k for k, _ in top), "timeframe": "1Day",
                           "start": day_s, "end": day_s, "limit": 100}, h)
            bars = (rb.body or {}).get("bars") or {} if rb is not None and isinstance(rb.body, dict) else {}
            if rb is not None:
                save_fixture("alpaca", "p3_option_bars_spy_compare", rb.envelope(), overwrite=overwrite)
            for sym, pb in top:
                hist = bars.get(sym) or []
                compare.append({"symbol": sym, "prevDailyBar_t": pb.get("t"), "prevDailyBar_v": pb.get("v"),
                                "bars_v": hist[0].get("v") if hist else None,
                                "bars_t": hist[0].get("t") if hist else None})
        body, cut = truncate(r.body, keep=6)
        save_fixture("alpaca", "p3_option_snapshots_spy_indicative", r.envelope(body, truncated=cut), overwrite=overwrite)
        results["alpaca_indicative_volume"] = {
            "status": r.status, "contracts": len(snaps), "with_dailyBar": len(with_bar),
            "dailyBar_v_nonzero": sum(1 for v in vols if v > 0),
            "dailyBar_v_total": sum(vols), "dailyBar_v_max": max(vols) if vols else None,
            "dailyBar_dates": dict(bar_days), "with_prevDailyBar": len(prev),
            "prev_vs_bars": compare, "next_page_token_present": bool(r.body.get("next_page_token")),
        }
        matches = sum(1 for c in compare if c["bars_v"] is not None and c["bars_v"] == c["prevDailyBar_v"])
        one_line("7 alpaca indicative dailyBar.v", f"HTTP {r.status}; {len(snaps)} contracts (<=10 DTE), "
                 f"{len(with_bar)} with dailyBar, {sum(1 for v in vols if v > 0)} with v>0, total v {sum(vols)}, "
                 f"dailyBar dates {dict(bar_days)}; prevDailyBar.v equals /options/bars daily v on "
                 f"{matches}/{len(compare)} top contracts", results)
    else:
        one_line("7 alpaca indicative dailyBar.v", f"no JSON: {r.status if r else 'transport error'} {r.text[:200] if r else ''}", results)


def probe_stock_snapshot_cap(http: Http, symbols: Sequence[str], results: dict[str, Any]) -> None:
    """11. Does /v2/stocks/snapshots cap the symbols list? (Phase 2 carry-over)."""
    h = alpaca_headers()
    feed = need("ALPACA_STOCK_FEED_REALTIME")
    out: dict[str, Any] = {}
    for n in (100, 500, 1000):
        batch = list(symbols[:n])
        if len(batch) < n:
            break
        r = http.get(ALPACA_DATA + "/v2/stocks/snapshots", {"symbols": ",".join(batch), "feed": feed}, h)
        if r is None:
            out[str(n)] = "transport error"
            continue
        returned = len(r.body) if isinstance(r.body, dict) else None
        out[str(n)] = {"status": r.status, "returned": returned,
                       "error": None if returned is not None and r.status == 200 else r.text[:200]}
    results["alpaca_stock_snapshot_cap"] = out
    one_line("11 stocks/snapshots cap", json.dumps(out), results)


def probe_fred(http: Http, today: date, results: dict[str, Any], *, overwrite: bool) -> None:
    key = need("FRED_API_KEY")
    out: dict[str, Any] = {}
    for series in ("VIXCLS", "BAMLH0A0HYM2", "DGS3MO"):
        r = http.get(FRED + "/series/observations",
                     {"series_id": series, "api_key": key, "file_type": "json",
                      "sort_order": "desc", "limit": 10})
        if r is None:
            continue
        obs = (r.body or {}).get("observations") or [] if isinstance(r.body, dict) else []
        out[series] = {"status": r.status, "latest": [(o.get("date"), o.get("value")) for o in obs[:3]]}
        save_fixture("fred", f"p3_observations_{series.lower()}", r.envelope(), overwrite=overwrite)
    r = http.get(FRED + "/releases/dates",
                 {"api_key": key, "file_type": "json", "include_release_dates_with_no_data": "true",
                  "realtime_start": today.isoformat(), "realtime_end": (today + timedelta(days=30)).isoformat(),
                  "limit": 1000, "sort_order": "asc"})
    if r is not None and isinstance(r.body, dict):
        rows = r.body.get("release_dates") or []
        date_only = all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(x.get("date", ""))) for x in rows)
        keys = sorted({k for x in rows for k in x})
        out["releases_dates"] = {"status": r.status, "rows": len(rows), "all_dates_yyyy_mm_dd": date_only,
                                 "row_keys": keys,
                                 "date_span": [rows[0].get("date"), rows[-1].get("date")] if rows else None}
        body, cut = truncate(r.body, keep=10)
        save_fixture("fred", "p3_releases_dates", r.envelope(body, truncated=cut), overwrite=overwrite)
    results["fred"] = out
    one_line("10 fred", json.dumps(out), results)


def probe_massive_single(http: Http, results: dict[str, Any], *, overwrite: bool) -> list[str]:
    r = http.get(MASSIVE + "/v2/reference/news", {"limit": 1000}, massive_headers())
    if r is None:
        one_line("8 massive", "transport error", results)
        return []
    res = (r.body or {}).get("results") or [] if isinstance(r.body, dict) else []
    sentiments = Counter(i.get("sentiment") for a in res for i in (a.get("insights") or []))
    tickers = sorted({t for a in res for t in (a.get("tickers") or [])})
    span = [res[-1].get("published_utc"), res[0].get("published_utc")] if res else None
    body, cut = truncate(r.body, keep=5)
    if isinstance(body, dict) and "next_url" in body:
        body["next_url"] = safe_url(str(body["next_url"]))
    save_fixture("massive", "p3_reference_news_untickered", r.envelope(body, truncated=cut), overwrite=overwrite)
    results["massive_single"] = {"status": r.status, "articles": len(res), "sentiment_values": dict(sentiments),
                                 "published_span": span, "distinct_tickers": len(tickers),
                                 "article_keys": sorted(res[0].keys()) if res else [],
                                 "insight_keys": sorted({k for a in res for i in (a.get("insights") or []) for k in i})}
    one_line("8 massive", f"HTTP {r.status}; {len(res)} articles spanning {span}; sentiment values {dict(sentiments)}", results)
    return tickers


def run_probes(only: str | None = None, *, overwrite: bool) -> None:
    now = datetime.now(timezone.utc)
    today = now.date()
    results: dict[str, Any] = {"ran_at": now.isoformat(timespec="seconds")}
    http = Http()
    if only == "fred":  # re-record FRED alone; touches no other host's budget
        try:
            probe_fred(http, today, results, overwrite=overwrite)
        finally:
            save_scratch("probes_fred.json", results)
            http.close()
        return
    try:
        probe_finnhub(http, today, results, overwrite=overwrite)
        probe_fred(http, today, results, overwrite=overwrite)
        probe_alpaca(http, today, now, results, overwrite=overwrite)
        tickers = probe_massive_single(http, results, overwrite=overwrite)
        pool = sorted(set(tickers) | WATCH_TICKERS)
        probe_stock_snapshot_cap(http, [t for t in pool if re.fullmatch(r"[A-Z]{1,5}", t)], results)
    finally:
        results["requests_by_host"] = dict(http.requests)
        results["throttled_by_host"] = dict(http.throttled)
        save_scratch("probes.json", results)
        http.close()


# --------------------------------------------------------------------------
# Label-volume measurement
# --------------------------------------------------------------------------


@dataclass
class Article:
    vendor: str
    published: datetime
    headline: str
    tickers: frozenset[str]
    insights: list[dict[str, Any]] = field(default_factory=list)


def fetch_massive(http: Http, start: datetime, end: datetime, max_pages: int) -> tuple[list[Article], dict[str, Any]]:
    h = massive_headers()
    arts: list[Article] = []
    url: str | None = MASSIVE + "/v2/reference/news"
    params: dict[str, Any] | None = {
        "published_utc.gte": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_utc.lte": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 1000, "order": "asc", "sort": "published_utc",
    }
    pages = 0
    while url and pages < max_pages:
        r = http.get(url, params, h)
        pages += 1
        if r is None or not isinstance(r.body, dict) or r.status != 200:
            say(f"  massive page {pages}: {r.status if r else 'transport error'} {r.text[:200] if r else ''}")
            break
        for a in r.body.get("results") or []:
            at = parse_instant(a.get("published_utc"))
            if at is None:
                continue
            arts.append(Article("massive", at, a.get("title") or "", frozenset(a.get("tickers") or []),
                                list(a.get("insights") or [])))
        say(f"  massive page {pages}: {len(r.body.get('results') or [])} articles (total {len(arts)})")
        nxt = r.body.get("next_url")
        url, params = (str(nxt), None) if nxt else (None, None)
    meta = {"pages": pages, "complete": url is None, "articles": len(arts)}
    return arts, meta


def fetch_alpaca_news(http: Http, start: datetime, end: datetime) -> tuple[list[Article], dict[str, Any]]:
    h = alpaca_headers()
    arts: list[Article] = []
    token: str | None = None
    pages = 0
    symbols = ",".join(sorted(WATCH_TICKERS))
    while pages < 400:
        params: dict[str, Any] = {"symbols": symbols, "start": start.isoformat().replace("+00:00", "Z"),
                                  "end": end.isoformat().replace("+00:00", "Z"), "limit": 50, "sort": "asc"}
        if token:
            params["page_token"] = token
        r = http.get(ALPACA_DATA + "/v1beta1/news", params, h)
        pages += 1
        if r is None or not isinstance(r.body, dict) or r.status != 200:
            say(f"  alpaca news page {pages}: {r.status if r else 'transport error'} {r.text[:200] if r else ''}")
            break
        for a in r.body.get("news") or []:
            at = parse_instant(a.get("created_at"))
            if at is not None:
                arts.append(Article("alpaca", at, a.get("headline") or "", frozenset(a.get("symbols") or [])))
        token = r.body.get("next_page_token")
        if not token:
            break
    return arts, {"pages": pages, "complete": not token, "articles": len(arts)}


def fetch_finnhub_news(http: Http, start: datetime, end: datetime) -> tuple[list[Article], dict[str, Any]]:
    h = finnhub_headers()
    by_id: dict[Any, Article] = {}
    counts: dict[str, int] = {}
    capped: list[str] = []

    def window(sym: str, lo: date, hi: date, depth: int = 0) -> list[Any]:
        """One symbol's news over [lo, hi]; a response at the 250 cap is split in two."""
        r = http.get(FINNHUB + "/company-news", {"symbol": sym, "from": lo.isoformat(), "to": hi.isoformat()}, h)
        if r is None or not isinstance(r.body, list):
            say(f"  finnhub {sym} {lo}..{hi}: {r.status if r else 'transport error'}")
            return []
        if len(r.body) >= FINNHUB_NEWS_CAP and hi > lo and depth < 4:
            mid = lo + (hi - lo) // 2
            return window(sym, lo, mid, depth + 1) + window(sym, mid + timedelta(days=1), hi, depth + 1)
        if len(r.body) >= FINNHUB_NEWS_CAP:
            capped.append(f"{sym} {lo}..{hi}")
        return list(r.body)

    for sym in sorted(WATCH_TICKERS):
        rows = window(sym, start.date(), end.date())
        counts[sym] = len(rows)
        for a in rows:
            at = parse_instant(a.get("datetime"))
            if at is None:
                continue
            key = a.get("id") or (a.get("headline"), a.get("datetime"))
            art = by_id.get(key)
            if art is None:
                by_id[key] = Article("finnhub", at, a.get("headline") or "", frozenset({sym}))
            else:  # the same article returned for a second symbol
                art.tickers = art.tickers | {sym}
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
    return list(by_id.values()), {"symbols": len(counts), "articles": len(by_id), "largest_symbols": top,
                                  "windows_still_at_cap": capped,
                                  "cap_note": f"/company-news returns at most {FINNHUB_NEWS_CAP} rows; capped windows were split by date"}


#: Observed 2026-09-24: ``/company-news`` answered exactly 250 rows (or 248-249
#: after the vendor's own dedup) for the busiest names over a 14-day window.
FINNHUB_NEWS_CAP: Final = 248


def _dump_articles(vendors: Mapping[str, list[Article]]) -> None:
    save_scratch("articles.json", {
        v: [{"published": a.published.isoformat(), "headline": a.headline, "tickers": sorted(a.tickers),
             "insights": a.insights} for a in arts]
        for v, arts in vendors.items()
    })


def _load_articles() -> dict[str, list[Article]]:
    raw = json.loads((SCRATCH / "articles.json").read_text(encoding="utf-8"))
    return {
        v: [Article(v, datetime.fromisoformat(a["published"]), a["headline"], frozenset(a["tickers"]),
                    list(a["insights"])) for a in arts]
        for v, arts in raw.items()
    }


def rules_tally(vendors: Mapping[str, list[Article]], binner: SessionBinner, days: list[str],
                named: bool) -> dict[str, Any]:
    """Directional DRAFT-pattern labels per session, per vendor and deduplicated.

    ``named=False`` credits every watch-universe ticker the vendor tagged;
    ``named=True`` credits only the tagged tickers the headline itself names
    (:func:`named_in`) -- the stricter, more honest attribution.
    """
    rules: dict[str, Any] = {}
    dedup_pairs: dict[str, set[tuple[str, str]]] = {d: set() for d in days}
    dedup_articles: dict[str, set[str]] = {d: set() for d in days}
    pattern_hits: Counter[str] = Counter()
    examples: list[dict[str, str]] = []
    for vendor, arts in vendors.items():
        pairs = {d: 0 for d in days}
        distinct = {d: 0 for d in days}
        for a in arts:
            s = binner.session(a.published)
            watch = a.tickers & WATCH_TICKERS
            if named:
                watch = frozenset(named_in(a.headline, watch))
            if s is None or not watch:
                continue
            label, names = draft_label(a.headline)
            if label is None:
                continue
            key = s.isoformat()
            norm = normalise_headline(a.headline)
            pattern_hits.update(names)
            pairs[key] += len(watch)
            distinct[key] += 1
            dedup_articles[key].add(norm)
            dedup_pairs[key].update((norm, t) for t in watch)
            examples.append({"vendor": vendor, "session": key, "label": label, "patterns": ",".join(names),
                             "tickers": ",".join(sorted(watch)), "headline": a.headline[:160]})
        rules[vendor] = {"pairs_per_session": pairs, "articles_per_session": distinct,
                         "pairs": summarise(pairs.values()), "articles": summarise(distinct.values())}
    rules["deduplicated"] = {
        "pairs_per_session": {d: len(v) for d, v in dedup_pairs.items()},
        "articles_per_session": {d: len(v) for d, v in dedup_articles.items()},
        "pairs": summarise(len(v) for v in dedup_pairs.values()),
        "articles": summarise(len(v) for v in dedup_articles.values()),
    }
    rules["pattern_hits"] = dict(pattern_hits)
    rules["examples"] = examples
    return rules


def summarise(values: Iterable[int]) -> dict[str, Any]:
    vals = list(values)
    if not vals:
        return {"mean": None, "min": None, "max": None}
    return {"mean": round(statistics.mean(vals), 2), "min": min(vals), "max": max(vals)}


def run_labels(n_sessions: int, massive_pages: int, from_cache: bool = False) -> None:
    now = datetime.now(timezone.utc)
    sessions = completed_sessions(n_sessions, now)
    binner = SessionBinner(sessions)
    start, end = sessions[0][1], sessions[-1][1]
    days = [d.isoformat() for d in binner.days]
    say(f"window: {start.isoformat()} -> {end.isoformat()} ({n_sessions} sessions {days[0]}..{days[-1]})")
    http = Http()
    out: dict[str, Any] = {
        "ran_at": now.isoformat(timespec="seconds"), "sessions": days,
        "window_utc": [start.isoformat(), end.isoformat()],
        "attribution": "an article belongs to the first session whose close is at or after its publication",
        "watch_universe": {"size": len(WATCH_TICKERS), "markets_universe": len(MARKETS_UNIVERSE),
                           "sector_leaders": "HAND-WRITTEN APPROXIMATION of decision 6's seed (step 4 builds the real one)",
                           "open_position_underlyings": "omitted", "MARKET": "no vendor tags MARKET; contributes 0"},
    }
    if from_cache:
        cached = _load_articles()
        massive, alpaca, finnhub = cached["massive"], cached["alpaca"], cached["finnhub"]
        out["fetch"] = json.loads((SCRATCH / "fetch_meta.json").read_text(encoding="utf-8"))
        massive_meta = out["fetch"]["massive"]
    else:
        try:
            massive, massive_meta = fetch_massive(http, start, end, massive_pages)
            alpaca, alpaca_meta = fetch_alpaca_news(http, start, end)
            finnhub, finnhub_meta = fetch_finnhub_news(http, start, end)
        finally:
            http.close()
        out["fetch"] = {"massive": massive_meta, "alpaca": alpaca_meta, "finnhub": finnhub_meta,
                        "requests_by_host": dict(http.requests), "throttled_by_host": dict(http.throttled)}
        _dump_articles({"massive": massive, "alpaca": alpaca, "finnhub": finnhub})
        save_scratch("fetch_meta.json", out["fetch"])

    # --- Massive vendor tier
    all_sentiments: Counter[str] = Counter()
    per: dict[str, Counter[str]] = {d: Counter() for d in days}
    for a in massive:
        s = binner.session(a.published)
        for ins in a.insights:
            all_sentiments[str(ins.get("sentiment"))] += 1
            if s is not None and ins.get("ticker") in WATCH_TICKERS:
                per[s.isoformat()][str(ins.get("sentiment"))] += 1
    directional = {d: c["positive"] + c["negative"] for d, c in per.items()}
    neutral = {d: c["neutral"] for d, c in per.items()}
    total_dir, total_neu = sum(directional.values()), sum(neutral.values())
    out["massive"] = {
        "sentiment_values_all_tickers": dict(all_sentiments),
        "per_session": {d: dict(c) for d, c in per.items()},
        "directional_per_session": summarise(directional.values()),
        "neutral_per_session": summarise(neutral.values()),
        "neutral_share": round(total_neu / (total_dir + total_neu), 3) if total_dir + total_neu else None,
        "complete": massive_meta["complete"],
    }

    # --- DRAFT rules tier (estimate only)
    vendors = {"massive": massive, "alpaca": alpaca, "finnhub": finnhub}
    label = "DRAFT-PATTERN ESTIMATE -- step 5 builds the real patterns and must re-measure"
    for mode, named in (("tagged", False), ("named", True)):
        rules = rules_tally(vendors, binner, days, named)
        rules["label"] = label
        rules["attribution"] = ("every vendor-tagged watch ticker" if not named
                                else "only tagged watch tickers the headline itself names (draft attribution)")
        rules["threshold_1_5_per_session"] = {
            "dedup_pairs_mean_below": (rules["deduplicated"]["pairs"]["mean"] or 0) < 1.5,
            "dedup_articles_mean_below": (rules["deduplicated"]["articles"]["mean"] or 0) < 1.5,
        }
        out[f"rules_draft_{mode}"] = rules
        say(f"RULES[{mode}] dedup " + json.dumps({k: v for k, v in rules["deduplicated"].items()}))
        for vendor in vendors:
            say(f"RULES[{mode}] {vendor} pairs {rules[vendor]['pairs']} articles {rules[vendor]['articles']}")
        say(f"PATTERNS[{mode}] " + json.dumps(rules["pattern_hits"]))
    save_scratch("labels.json", out)
    say("MASSIVE " + json.dumps({k: v for k, v in out["massive"].items() if k != "per_session"}))


# --------------------------------------------------------------------------
# StockTwits one-hour poll
# --------------------------------------------------------------------------

_INTERESTING_HEADER = re.compile(r"(?i)^(cf-|x-|retry-after|ratelimit|server$|content-type$)")


def run_stocktwits(duration: float, interval: float, user_agent: str | None, *, overwrite: bool) -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    log_path = SCRATCH / "stocktwits_poll.jsonl"
    summary_path = SCRATCH / "stocktwits_poll_summary.json"
    client = httpx.Client(timeout=30.0, follow_redirects=False,
                          headers={"User-Agent": user_agent} if user_agent else {})
    cursors: dict[str, int] = {}
    started = time.monotonic()
    started_at = datetime.now(timezone.utc)
    status: Counter[str] = Counter()
    header_names: Counter[str] = Counter()
    cookie_names: Counter[str] = Counter()
    events: list[dict[str, Any]] = []
    messages = labelled = 0
    more_true = 0
    sample_saved = False
    i = 0
    backoff = 0.0

    def write_summary(final: bool) -> dict[str, Any]:
        summary = {
            "final": final, "started_at": started_at.isoformat(timespec="seconds"),
            "elapsed_s": round(time.monotonic() - started, 1), "requests": sum(status.values()),
            "planned_interval_s": interval, "planned_duration_s": duration,
            "universe": list(MARKETS_UNIVERSE), "status_counts": dict(status),
            "throttle_or_challenge_events": events, "header_names_seen": dict(header_names),
            "cookie_names_seen": dict(cookie_names), "messages": messages, "labelled": labelled,
            "labelled_share": round(labelled / messages, 3) if messages else None,
            "responses_with_cursor_more_true": more_true,
            "user_agent": user_agent or f"python-httpx/{httpx.__version__}",
            "note": "polled overnight ET if started_at is outside the session; cadence test, not an intraday volume sample",
        }
        summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        return summary

    with log_path.open("a", encoding="utf-8") as log:
        while time.monotonic() - started < duration:
            tick = time.monotonic()
            sym = MARKETS_UNIVERSE[i % len(MARKETS_UNIVERSE)]
            i += 1
            params = {"since": cursors[sym]} if sym in cursors else None
            entry: dict[str, Any] = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sym": sym}
            try:
                r = client.get(f"{STOCKTWITS}/streams/symbol/{sym}.json", params=params)
            except httpx.HTTPError as exc:
                entry["error"] = type(exc).__name__
                status["transport_error"] += 1
            else:
                ctype = r.headers.get("content-type", "")
                entry.update(status=r.status_code, ctype=ctype.split(";")[0])
                status[str(r.status_code)] += 1
                for k in r.headers.keys():
                    if _INTERESTING_HEADER.match(k):
                        header_names[k.lower()] += 1
                for c in r.cookies.jar:
                    cookie_names[c.name] += 1
                rl = {k.lower(): v for k, v in r.headers.items()
                      if re.match(r"(?i)(x-)?rate-?limit|retry-after", k)}
                if rl:
                    entry["ratelimit_headers"] = rl
                challenge = r.status_code in (403, 503) or "html" in ctype or "cf-mitigated" in r.headers
                if r.status_code == 429 or challenge:
                    events.append({**entry, "head": redact(r.text[:200]),
                                   "cf_headers": {k: v for k, v in r.headers.items() if k.lower().startswith("cf-")}})
                    backoff = min(max(backoff * 2, 300.0), 1800.0)
                    ra = r.headers.get("retry-after", "")
                    if ra.isdigit():
                        backoff = max(backoff, float(ra))
                elif r.status_code == 200:
                    backoff = 0.0
                    body = r.json()
                    msgs = body.get("messages") or []
                    n_lab = sum(1 for m in msgs if _st_label(m))
                    messages += len(msgs)
                    labelled += n_lab
                    cur = body.get("cursor") or {}
                    more_true += 1 if cur.get("more") else 0
                    newest = max([int(m["id"]) for m in msgs if "id" in m] + [cursors.get(sym, 0)])
                    if newest:
                        cursors[sym] = newest
                    entry.update(n=len(msgs), labelled=n_lab, more=cur.get("more"))
                    if not sample_saved and msgs:
                        sample, cut = truncate(_strip_users(body), keep=5)
                        save_fixture("stocktwits", "p3_streams_symbol_sample",
                                     {"recorded_at": entry["t"], "request": f"{STOCKTWITS}/streams/symbol/{sym}.json",
                                      "status_code": r.status_code, "content_type": ctype,
                                      "truncated": cut, "body": sample}, overwrite=overwrite)
                        sample_saved = True
            log.write(json.dumps(entry) + "\n")
            log.flush()
            write_summary(final=False)
            wait = interval + backoff - (time.monotonic() - tick)
            if wait > 0:
                time.sleep(min(wait, max(0.0, duration - (time.monotonic() - started))))
    client.close()
    summary = write_summary(final=True)
    save_fixture("stocktwits", "p3_poll_summary", summary, overwrite=overwrite)
    say(json.dumps(summary, indent=1))


def _st_label(message: Mapping[str, Any]) -> str | None:
    """A message's user label: ``entities.sentiment.basic`` (or a bare string)."""
    sentiment = (message.get("entities") or {}).get("sentiment")
    if isinstance(sentiment, dict):
        value = sentiment.get("basic")
        return str(value) if value else None
    return str(sentiment) if sentiment else None


def _strip_users(body: Any) -> Any:
    """Pseudonymise third-party users in a recorded stream: shape kept, identity dropped."""
    if isinstance(body, dict):
        out: dict[str, Any] = {}
        for k, v in body.items():
            if k == "user" and isinstance(v, dict):
                out[k] = {kk: (REDACTED if isinstance(vv, str) else 0 if isinstance(vv, int) and not isinstance(vv, bool) else vv)
                          for kk, vv in v.items() if not isinstance(vv, (dict, list))}
            else:
                out[k] = _strip_users(v)
        return out
    if isinstance(body, list):
        return [_strip_users(v) for v in body]
    return body


def run_scan(paths: Sequence[str]) -> None:
    """Scan files for the *literal* values of the keys in the environment.

    Reports file and env-var **names** only. Pattern greps can only find
    credential-shaped text; this is the one check that knows the actual keys,
    so it is the proof that nothing recorded or printed holds one.
    """
    held = [n for n in SECRET_ENV if os.environ.get(n, "").strip()]
    files = sorted({p for arg in paths for p in (Path(arg).rglob("*") if Path(arg).is_dir() else [Path(arg)])
                    if p.is_file()})
    hits = {str(p): leaked(p.read_text(encoding="utf-8", errors="replace")) for p in files}
    hits = {p: names for p, names in hits.items() if names}
    say(f"scan: {len(files)} files against {len(held)} env values ({', '.join(held)}); "
        f"{'CLEAN' if not hits else 'HITS ' + json.dumps(hits)}")
    if hits:
        raise SystemExit(1)


# Offered only on the subcommands that write fixtures (``probes`` and
# ``stocktwits-poll``); ``labels`` and ``scan`` write none, so they reject it.
OVERWRITE_HELP: Final = "replace existing tests/fixtures/*/p3_*.json (refused by default)"


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("probes")
    pr.add_argument("--only", choices=["fred"], default=None)
    pr.add_argument("--overwrite", action="store_true", help=OVERWRITE_HELP)
    lab = sub.add_parser("labels")
    lab.add_argument("--sessions", type=int, default=10)
    lab.add_argument("--massive-pages", type=int, default=80)
    lab.add_argument("--from-cache", action="store_true",
                     help="re-tally .claude/scratch/phase3_probe/articles.json without fetching")
    st = sub.add_parser("stocktwits-poll")
    st.add_argument("--duration", type=float, default=3600.0)
    st.add_argument("--interval", type=float, default=20.0)
    st.add_argument("--user-agent", default=None)
    st.add_argument("--overwrite", action="store_true", help=OVERWRITE_HELP)
    sc = sub.add_parser("scan")
    sc.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    if args.cmd == "scan":
        run_scan(args.paths)
    elif args.cmd == "probes":
        run_probes(args.only, overwrite=args.overwrite)
    elif args.cmd == "labels":
        run_labels(args.sessions, args.massive_pages, args.from_cache)
    else:
        run_stocktwits(args.duration, args.interval, args.user_agent, overwrite=args.overwrite)


if __name__ == "__main__":
    main()
