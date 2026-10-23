"""Phase 3 step 7, unit 7.1 -- the two keyed pre-flight probes.

Run by hand, never by the test suite, and **only through the launcher** so no
agent ever reads ``.env`` (rule 6)::

    uv run --env-file <path-to>/.env python scripts/probe_phase3_step7.py
    uv run --env-file <path-to>/.env python scripts/probe_phase3_step7.py --overwrite

Two questions, both carried by the spec into step 7:

1. **Q15** -- does Finnhub ``/calendar/ipo`` answer on this project's key? Its
   swagger ``premium``/``freeTier`` flags are null. A 403 is recorded and goes
   back to the owner; it is not worked around.
2. **Decision 7's dividend window** -- step 0 probed Alpaca corporate actions
   with ``end`` = today+60d, which censored every lead-time figure. This
   re-probes with ``end`` = today+365d (the reference states no maximum range)
   and measures how far ahead of its ``ex_date`` an announced cash dividend
   appears. Both ``data_quality`` values are probed, since the reference says
   ``complete`` (the default) can hold back actions ``all`` would return.

Everything reusable -- redaction, the leak scan, the GET-only paced client --
is imported from ``probe_phase3.py`` rather than copied, so the safety
properties documented there hold here unchanged. Fixtures are
``tests/fixtures/{finnhub,alpaca}/p7_*.json``; an existing one is committed
evidence and is refused unless ``--overwrite`` is passed (the 2c353ec guard).

**Exact decimals.** ``Http`` parses through ``json.loads``, so a parsed body
has been through a float. Each fixture therefore also carries ``body_raw``:
the response text verbatim (a deliberately small page where the full one is
large), so a ``parse_float=Decimal`` test has real bytes to read.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Final, Mapping, Sequence

from probe_phase3 import (
    ALPACA_DATA,
    FINNHUB,
    FIXTURES,
    WATCH_TICKERS,
    Http,
    Reply,
    _SK_TOKEN,
    _WEBHOOK,
    alpaca_headers,
    finnhub_headers,
    leaked,
    redact,
    save_scratch,
    say,
    truncate,
)

IPO_FORWARD_DAYS: Final = 30
IPO_BACKWARD_DAYS: Final = 30
DIVIDEND_FORWARD_DAYS: Final = 365
MAX_PAGES: Final = 40



def save_fixture(
    vendor: str, name: str, envelope: Mapping[str, Any], *, overwrite: bool
) -> None:
    """Scrub, scan the text as written, refuse an existing file, then write.

    Same order and same guarantee as ``probe_phase3.save_fixture``; only the
    enforced prefix differs (``p7_``), so this can never replace a step 0
    fixture.
    """
    if not name.startswith("p7_"):
        raise SystemExit(f"refusing fixture name {name!r}: must start with p7_")
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


def summarise(values: Sequence[int]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)

    def pct(p: float) -> int:
        return ordered[min(len(ordered) - 1, int(p * (len(ordered) - 1) + 0.5))]

    return {
        "n": len(ordered),
        "min": ordered[0],
        "p25": pct(0.25),
        "median": statistics.median(ordered),
        "p75": pct(0.75),
        "p90": pct(0.90),
        "p99": pct(0.99),
        "max": ordered[-1],
        "histogram_weeks": {str(k): v for k, v in sorted(Counter(d // 7 for d in ordered).items())},
    }


# --------------------------------------------------------------------------
# 1. Finnhub /calendar/ipo
# --------------------------------------------------------------------------


def probe_ipo(http: Http, today: date, results: dict[str, Any], *, overwrite: bool) -> None:
    h = finnhub_headers()
    windows = (
        ("p7_calendar_ipo", today, today + timedelta(days=IPO_FORWARD_DAYS)),
        ("p7_calendar_ipo_past", today - timedelta(days=IPO_BACKWARD_DAYS), today),
    )
    out: dict[str, Any] = {}
    for name, start, end in windows:
        r = http.get(FINNHUB + "/calendar/ipo", {"from": start.isoformat(), "to": end.isoformat()}, h)
        if r is None:
            out[name] = "transport error"
            continue
        rows = (r.body or {}).get("ipoCalendar") if isinstance(r.body, dict) else None
        rows = rows if isinstance(rows, list) else []
        raw_value_forms: dict[str, Counter[str]] = {}
        if r.status == 200:
            # How each field is spelled in the raw text: number, string or null.
            exact = json.loads(r.text, parse_float=Decimal)
            for row in (exact.get("ipoCalendar") or []) if isinstance(exact, dict) else []:
                for k, v in row.items():
                    raw_value_forms.setdefault(k, Counter())[type(v).__name__] += 1
        summary = {
            "status": r.status,
            "content_type": r.content_type,
            "window": [start.isoformat(), end.isoformat()],
            "top_level_keys": sorted(r.body.keys()) if isinstance(r.body, dict) else None,
            "rows": len(rows),
            "row_keys": sorted({k for row in rows for k in row}),
            "value_types_in_raw_text": {k: dict(c) for k, c in sorted(raw_value_forms.items())},
            "status_values": dict(Counter(repr(row.get("status")) for row in rows)),
            "exchange_values": dict(Counter(repr(row.get("exchange")) for row in rows)),
            "price_values_sample": [row.get("price") for row in rows[:10]],
            "dates": sorted({str(row.get("date")) for row in rows}),
            "rows_with_empty_symbol": sum(1 for row in rows if not row.get("symbol")),
            "head": r.text[:200] if r.status != 200 else None,
        }
        out[name] = summary
        body, cut = truncate(r.body, keep=50) if r.body is not None else (None, {})
        env = r.envelope(body, truncated=cut, summary=summary)
        if len(r.text) <= 64_000:
            env["body_raw"] = r.text
        save_fixture("finnhub", name, env, overwrite=overwrite)
        say(f"FINDING ipo {name}: HTTP {r.status}; {len(rows)} rows; keys {summary['row_keys']}; "
            f"status {summary['status_values']}")
        if r.status in (401, 403):
            say("  premium/forbidden: recorded, stopping the IPO side here (Q15: back to the owner)")
            break
    results["finnhub_calendar_ipo"] = out


# --------------------------------------------------------------------------
# 2. Alpaca corporate actions, long window
# --------------------------------------------------------------------------


def _days(a: str | None, b: date) -> int | None:
    return (date.fromisoformat(a) - b).days if a else None


def fetch_dividends(
    http: Http, h: Mapping[str, str], params: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[str], Reply | None, bool]:
    """Every page. Returns parsed rows (via ``parse_float=Decimal``), raw page texts,
    the first reply, and whether pagination finished inside ``MAX_PAGES``."""
    rows: list[dict[str, Any]] = []
    raws: list[str] = []
    first: Reply | None = None
    token: str | None = None
    for _ in range(MAX_PAGES):
        p = dict(params)
        if token:
            p["page_token"] = token
        r = http.get(ALPACA_DATA + "/v1/corporate-actions", p, h)
        if r is None:
            return rows, raws, first, False
        first = first or r
        if r.status != 200:
            return rows, raws, first, False
        raws.append(r.text)
        exact = json.loads(r.text, parse_float=Decimal)
        rows.extend((exact.get("corporate_actions") or {}).get("cash_dividends") or [])
        token = exact.get("next_page_token")
        if not token:
            return rows, raws, first, True
    return rows, raws, first, False


def analyse_dividends(
    rows: list[dict[str, Any]], raws: list[str], today: date, start: date, end: date
) -> dict[str, Any]:
    lead = [d for row in rows if (d := _days(row.get("ex_date"), today)) is not None]
    future = [d for d in lead if d > 0]
    ex_to_process = [
        (date.fromisoformat(row["process_date"]) - date.fromisoformat(row["ex_date"])).days
        for row in rows if row.get("process_date") and row.get("ex_date")
    ]
    ex_to_pay = [
        (date.fromisoformat(row["payable_date"]) - date.fromisoformat(row["ex_date"])).days
        for row in rows if row.get("payable_date") and row.get("ex_date")
    ]
    # Which date does the start/end window actually filter on?
    in_window: dict[str, int] = {}
    for f in ("ex_date", "record_date", "payable_date", "process_date"):
        in_window[f] = sum(
            1 for row in rows
            if row.get(f) and start <= date.fromisoformat(row[f]) <= end
        )
    # The rate's spelling in the raw text, row-aligned: re-parse each page with
    # hooks that keep the number's literal text and say which hook fired. A
    # rate spelled ``1`` goes through ``parse_int``, not ``parse_float`` -- so
    # ``parse_float=Decimal`` alone would hand it back as an ``int``.
    tokens: list[tuple[str, str]] = []  # (spelling, literal text)
    for raw in raws:
        tagged = json.loads(raw, parse_float=lambda t: ("float", t), parse_int=lambda t: ("int", t))
        for row in (tagged.get("corporate_actions") or {}).get("cash_dividends") or []:
            v = row.get("rate")
            if isinstance(v, tuple):
                tokens.append((v[0], v[1]))
            else:
                tokens.append((type(v).__name__, repr(v)))
    forms = Counter(
        "exponent" if kind == "float" and re.search(r"[eE]", text) else kind
        for kind, text in tokens
    )
    numeric = [text for kind, text in tokens if kind in ("float", "int")]
    parsed_with_float_hook_only = Counter(type(row.get("rate")).__name__ for row in rows)
    sig = [len(t.lstrip("-").replace(".", "").lstrip("0")) for t in numeric if "e" not in t.lower()]
    via_decimal_of_float = [t for t in numeric if Decimal(float(t)) != Decimal(t)]
    via_repr_of_float = [t for t in numeric if Decimal(repr(float(t))) != Decimal(t)]
    trailing_zero = [t for t in numeric if "." in t and t.endswith("0")]
    ex_next_14 = Counter(
        str(row["ex_date"]) for row in rows
        if (d := _days(row.get("ex_date"), today)) is not None and 0 <= d <= 14
    )
    future_rows = [row for row in rows if (d := _days(row.get("ex_date"), today)) is not None and d > 0]
    watch = sorted(
        {(str(row["symbol"]), str(row["ex_date"])) for row in future_rows if row.get("symbol") in WATCH_TICKERS}
    )
    furthest = sorted(future_rows, key=lambda row: str(row.get("ex_date")))[-5:]
    return {
        "rows": len(rows),
        "distinct_ids": len({row.get("id") for row in rows}),
        "row_keys": sorted({k for row in rows for k in row}),
        "key_presence": dict(Counter(k for row in rows for k in row)),
        "ex_date_minus_today_all": summarise(lead),
        "ex_date_minus_today_future_only": summarise(future),
        "ex_date_after_today": len(future),
        "ex_date_on_or_before_today": len(lead) - len(future),
        "process_minus_ex_days": summarise(ex_to_process),
        "payable_minus_ex_days": summarise(ex_to_pay),
        "rows_with_date_in_window": in_window,
        "special_true": sum(1 for row in rows if row.get("special") is True),
        "sub_type_values": dict(Counter(repr(row.get("sub_type")) for row in rows)),
        "rate_text_forms": dict(forms),
        "rate_text_examples": [t for _, t in tokens[:8]],
        "rate_int_spelled_examples": [t for k, t in tokens if k == "int"][:10],
        "rate_types_under_parse_float_Decimal_only": dict(parsed_with_float_hook_only),
        "rate_max_significant_digits": max(sig) if sig else None,
        "rate_texts_changed_by_Decimal_of_float": len(via_decimal_of_float),
        "rate_texts_changed_by_Decimal_of_repr_float": len(via_repr_of_float),
        "rate_texts_with_trailing_zero": len(trailing_zero),
        "rate_trailing_zero_examples": trailing_zero[:5],
        "ex_date_counts_next_14_days": dict(sorted(ex_next_14.items())),
        "watch_universe_future_ex_dates": [f"{s} {d}" for s, d in watch],
        "furthest_future_rows": [
            {k: str(row.get(k)) for k in ("symbol", "ex_date", "record_date", "payable_date", "process_date", "rate")}
            for row in furthest
        ],
    }


def probe_dividends(http: Http, today: date, results: dict[str, Any], *, overwrite: bool) -> None:
    h = alpaca_headers()
    start, end = today, today + timedelta(days=DIVIDEND_FORWARD_DAYS)
    base = {"types": "cash_dividend", "start": start.isoformat(), "end": end.isoformat(), "limit": 1000}
    out: dict[str, Any] = {}
    for quality, name in (("complete", "p7_corporate_actions_cash_dividend_long"),
                          ("all", "p7_corporate_actions_cash_dividend_long_all")):
        rows, raws, first, finished = fetch_dividends(http, h, {**base, "data_quality": quality})
        if first is None:
            out[quality] = "transport error"
            continue
        summary: dict[str, Any] = {"status": first.status, "pages": len(raws), "pagination_finished": finished,
                                   "data_quality": quality, "window": [start.isoformat(), end.isoformat()]}
        if first.status == 200:
            summary.update(analyse_dividends(rows, raws, today, start, end))
            save_scratch(f"dividends_long_{quality}_rows.json", rows)  # gitignored, for re-analysis
        else:
            summary["head"] = first.text[:300]
        out[quality] = summary
        body, cut = truncate(first.body, keep=6) if first.body is not None else (None, {})
        env = first.envelope(body, truncated=cut, summary=summary)
        if first.status == 200:
            # A small page verbatim, for byte-exact parse_float=Decimal tests.
            small = http.get(ALPACA_DATA + "/v1/corporate-actions", {**base, "data_quality": quality, "limit": 6}, h)
            if small is not None and small.status == 200:
                env["body_raw_request"] = small.url
                env["body_raw"] = small.text
        save_fixture("alpaca", name, env, overwrite=overwrite)
        fut = summary.get("ex_date_minus_today_future_only", {})
        say(f"FINDING dividends[{quality}]: HTTP {first.status}; {summary.get('rows')} rows over "
            f"{len(raws)} pages (finished={finished}); future ex_date {fut}; "
            f"rate forms {summary.get('rate_text_forms')}; window-match {summary.get('rows_with_date_in_window')}")
    if isinstance(out.get("complete"), dict) and isinstance(out.get("all"), dict):
        out["all_minus_complete_rows"] = (out["all"].get("rows") or 0) - (out["complete"].get("rows") or 0)
    results["alpaca_dividends_long"] = out


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--only", choices=["ipo", "dividends"], default=None)
    parser.add_argument("--overwrite", action="store_true",
                        help="replace existing tests/fixtures/*/p7_*.json (refused by default)")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    today = now.date()
    results: dict[str, Any] = {"ran_at": now.isoformat(timespec="seconds")}
    http = Http()
    try:
        if args.only in (None, "ipo"):
            probe_ipo(http, today, results, overwrite=args.overwrite)
        if args.only in (None, "dividends"):
            probe_dividends(http, today, results, overwrite=args.overwrite)
    finally:
        results["requests_by_host"] = dict(http.requests)
        results["throttled_by_host"] = dict(http.throttled)
        save_scratch("probes_step7.json", results)
        http.close()


if __name__ == "__main__":
    main()
