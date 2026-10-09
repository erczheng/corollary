"""Record SEC N-PORT holdings for the SPDR sector seed into ``tests/fixtures/sec/``.

Run by hand, never by the test suite, with every credential in the process
environment only -- supplied by the launcher::

    uv run --env-file <path-to>/.env python tests/fixtures/record_sec.py --dry-run
    uv run --env-file <path-to>/.env python tests/fixtures/record_sec.py

Owner decision (Phase 3 step 4): the eleven SPDR sector funds' holdings come
from the trust's quarterly **NPORT-P** filings on EDGAR, with each holding's
CUSIP resolved to a ticker through Alpaca's ``GET /v2/assets/{cusip}``. That
replaces the hand-downloaded SSGA spreadsheets. This script is the probe and
the recorder for that source; the provider that consumes it is a later unit.

What it records, all from the latest quarter the trust has filed:

* ``CIK0001064641.trimmed.json`` -- the trust's submissions list, cut down to
  that quarter's NPORT-P rows (columnar, exactly the vendor's shape).
* ``company_tickers_mf.trimmed.json`` -- SEC's own ticker/series/class map,
  cut down to this trust. **This is how a fund is identified**: by the
  ``seriesId`` SEC publishes against its ticker, never by list position and
  never by pattern-matching a series name.
* ``nport_<TICKER>_primary_doc.xml`` plus a ``.meta.json`` sidecar for each of
  the eleven sector funds, and one Premium Income filing's header (holdings
  removed) to prove the series filter excludes the other eleven.
* ``series_ids.json`` -- ticker -> seriesId / accession / report date.
* ``alpaca_asset_<CUSIP>.json`` -- a sample of CUSIP lookups, plus every CUSIP
  that failed to resolve, and ``cusip_survey.json`` -- every equity CUSIP in
  the eleven funds with the symbol Alpaca resolved it to (``--no-survey``
  limits the lookups to the sample).

**The declared User-Agent is the owner's name and email.** SEC's fair-access
policy requires one; this script reads it from ``SEC_USER_AGENT`` and refuses
to run without it -- there is no default, because a made-up identity is the
thing that policy forbids. That value, and both Alpaca key halves, are
secrets here, enforced rather than intended:

* **Nothing records a request header.** Fixtures carry the URL and the status
  and nothing that was sent.
* **Every body is redacted, then every file is proven clean.**
  :func:`redact` replaces each secret (and its URL- and XML-escaped forms) with
  ``[redacted]``; :func:`write_fixtures` then scans the text *as it will be
  written* and refuses the whole batch if any form survives. Nothing is on
  disk until every file has passed, and then the recording is written into a
  staging directory beside ``sec/`` and renamed into place: a disk error
  part-way leaves ``sec/`` as it was, and ``--overwrite`` replaces the whole
  directory rather than merging two recordings.
* **Alpaca answers 200 or 404, or the run stops.** A 401/403/429/5xx is not
  "no ticker for this CUSIP" and is never recorded as one.
* **Everything printed goes through** :func:`Recorder.say`, which redacts.
  Output is counts, status codes and file names.
* **A refusal from SEC stops the run.** A 403, or the "undeclared automated
  tool" page, exits non-zero with nothing written. It is not retried and it
  is not worked around.

GET only -- ``tests/test_hard_rules.py`` globs ``record_*.py`` into the
vendor surface, so this file is in scope of its write-verb guards by
construction. Nothing here loads ``.env`` (rule 6).

SEC is paced at one request per ``SEC_MIN_INTERVAL`` seconds, well under the
ten per second the policy allows; Alpaca's trading host at one per
``ALPACA_MIN_INTERVAL``, under its 200-per-minute bucket. A dev tool's sleep,
not the engine's limiter.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence
from urllib.parse import quote, quote_plus

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from corollary.data.providers.alpaca import (  # noqa: E402
    ALPACA_LIVE_KEY_ENV,
    ALPACA_LIVE_SECRET_ENV,
    ALPACA_PAPER_KEY_ENV,
    ALPACA_PAPER_SECRET_ENV,
    AlpacaCredentials,
)

OUTPUT_DIR: Final = Path(__file__).resolve().parent / "sec"

UA_ENV: Final = "SEC_USER_AGENT"

#: "SELECT SECTOR SPDR TRUST". CIK 1100949 is a dead namesake.
TRUST_CIK: Final = "0001064641"
SUBMISSIONS_URL: Final = f"https://data.sec.gov/submissions/CIK{TRUST_CIK}.json"
TICKERS_MF_URL: Final = "https://www.sec.gov/files/company_tickers_mf.json"
ARCHIVE_URL: Final = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/primary_doc.xml"

NPORT_FORM: Final = "NPORT-P"

#: The eleven sector funds. Resolved to seriesIds through SEC's ticker map.
SECTOR_TICKERS: Final = (
    "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
)

#: Funds whose primary_doc.xml is recorded byte-for-byte. The rest keep every
#: ``assetCat=EC`` holding and lose only the non-equity lines, which the
#: sidecar counts. XLK is the reference fund; XLU is a small one.
COMPLETE_TICKERS: Final = frozenset({"XLK", "XLU"})

EQUITY_COMMON: Final = "EC"

SEC_MIN_INTERVAL: Final = 0.25
ALPACA_MIN_INTERVAL: Final = 0.4

#: Lowercased marker of SEC's block page for a missing or bad User-Agent.
SEC_BLOCK_MARKER: Final = "undeclared automated tool"

#: How many of each sample fund's heaviest equity lines to look up in Alpaca.
SAMPLE_PER_FUND: Final = 5
SAMPLE_FUNDS: Final = ("XLK", "XLF")

#: Square brackets, not angle: "<redacted>" inside an XML body would be read
#: as an unclosed element and leave the fixture unparseable.
REDACTED: Final = "[redacted]"

#: Shorter than this and a "secret" would redact ordinary words.
MIN_SECRET_LENGTH: Final = 5

HEADER_NOTE: Final = (
    "request headers are not recorded: the User-Agent is the owner's declared "
    "identity and the Alpaca headers are credentials"
)


# ---------------------------------------------------------------------------
# Exit codes and errors
# ---------------------------------------------------------------------------

EXIT_MISSING_USER_AGENT: Final = 2
EXIT_SEC_REFUSED: Final = 3
EXIT_FAILED: Final = 4
EXIT_SECRET_FOUND: Final = 5
EXIT_WOULD_OVERWRITE: Final = 6


class RecorderError(Exception):
    """A stop. ``code`` is the process exit status; the message is redacted
    before it is printed."""

    code: int = EXIT_FAILED


class MissingUserAgent(RecorderError):
    code = EXIT_MISSING_USER_AGENT


class SecRefused(RecorderError):
    code = EXIT_SEC_REFUSED


class SecretFound(RecorderError):
    code = EXIT_SECRET_FOUND


class WouldOverwrite(RecorderError):
    code = EXIT_WOULD_OVERWRITE


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def secret_values(env: Mapping[str, str]) -> tuple[str, ...]:
    """Every value that must never be written or printed.

    The whole User-Agent, and separately: the name (everything before the
    email), the email, its local part and its domain -- so a body quoting only
    one piece of the identity is still caught. Both Alpaca key halves, paper
    and live -- live is never sent, and is scanned anyway.

    A piece shorter than :data:`MIN_SECRET_LENGTH` is dropped (it would redact
    ordinary words); the whole UA and the whole email still cover it.
    """
    found: list[str] = []
    user_agent = env.get(UA_ENV, "").strip()
    if user_agent:
        found.append(user_agent)
        for raw in user_agent.split():
            token = raw.strip("<>()[]{},;:\"'")
            if "@" in token:
                local, domain = token.rsplit("@", 1)
                found += [token, local, domain]
                name = user_agent[: user_agent.find(raw)].strip().rstrip("<([{,;:\"'").strip()
                if name:
                    found.append(name)
    for name in (
        ALPACA_PAPER_KEY_ENV,
        ALPACA_PAPER_SECRET_ENV,
        ALPACA_LIVE_KEY_ENV,
        ALPACA_LIVE_SECRET_ENV,
    ):
        value = env.get(name, "").strip()
        if value:
            found.append(value)
    unique = dict.fromkeys(v for v in found if len(v) >= MIN_SECRET_LENGTH)
    # Longest first, so the whole UA is replaced before its email token is.
    return tuple(sorted(unique, key=len, reverse=True))


def secret_forms(secret: str) -> tuple[str, ...]:
    """The literal and the escaped spellings a body or a URL could carry:
    percent-encoded (both spaces), and XML-escaped with and without quotes,
    with the apostrophe spelled each way XML allows."""
    xml_min = html.escape(secret, quote=False)
    return tuple(
        dict.fromkeys(
            (
                secret,
                quote(secret, safe=""),
                quote_plus(secret),
                html.escape(secret),
                xml_min,
                xml_min.replace("'", "&apos;").replace('"', "&quot;"),
                xml_min.replace("'", "&#39;").replace('"', "&#34;"),
            )
        )
    )


def redact(text: str, secrets: Sequence[str]) -> str:
    """Replace every form of every secret with ``[redacted]``, case-insensitively."""
    for secret in secrets:
        for form in secret_forms(secret):
            text = re.sub(re.escape(form), REDACTED, text, flags=re.IGNORECASE)
    return text


def redacted_headers(headers: Mapping[str, str], secrets: Sequence[str]) -> dict[str, str]:
    """Header names kept, every value that is or contains a secret replaced.

    Used for diagnostics only -- fixtures record no request header at all.
    """
    return {name: redact(value, secrets) for name, value in headers.items()}


def contains_secret(text: str, secrets: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(
        form.lower() in lowered for secret in secrets for form in secret_forms(secret)
    )


_SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _remove_tree(path: Path) -> None:
    """Remove a directory this module created and filled with plain files."""
    if not path.exists():
        return
    for child in path.iterdir():
        child.unlink()
    path.rmdir()


def write_fixtures(
    output_dir: Path, files: Mapping[str, str], secrets: Sequence[str], *, overwrite: bool
) -> list[Path]:
    """Write one recording as a whole directory, all or nothing.

    Every file is checked before any is written: an existing, non-empty
    ``output_dir`` without ``overwrite`` refuses the batch, and so does any
    form of any secret in the text *as it will be written*. Callers redact
    first; this is the proof that they did, not a second redaction.

    The files are written into a fresh temporary directory beside
    ``output_dir`` and renamed into place, so a disk error part-way leaves
    ``output_dir`` exactly as it was. With ``overwrite`` the previous
    recording is replaced as a whole directory -- two recordings are never
    merged, so no file from the last run survives into this one.
    """
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise WouldOverwrite(
            f"refusing to overwrite the existing recording in {output_dir.name}/ "
            "(pass --overwrite to replace it as a whole)"
        )
    for name, text in files.items():
        if not _SAFE_FILE_NAME.fullmatch(name):
            raise RecorderError(f"refusing an unsafe fixture file name {name!r}")
        if contains_secret(text, secrets):
            raise SecretFound(f"ABORTED: {name} contains a secret. Nothing was written.")
    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    stamp = f"{os.getpid()}-{time.time_ns()}"
    staging = parent / f".{output_dir.name}.staging-{stamp}"
    previous = parent / f".{output_dir.name}.previous-{stamp}"
    staging.mkdir()
    try:
        for name, text in files.items():
            # Bytes, so Windows newline translation cannot alter a recorded body.
            (staging / name).write_bytes(text.encode("utf-8"))
        if output_dir.exists():
            os.replace(output_dir, previous)
            try:
                os.replace(staging, output_dir)
            except BaseException:
                os.replace(previous, output_dir)
                raise
        else:
            os.replace(staging, output_dir)
    except BaseException:
        _remove_tree(staging)
        raise
    try:
        _remove_tree(previous)
    except OSError as exc:
        raise RecorderError(
            f"the new recording is in place, but the previous copy could not be removed "
            f"({type(exc).__name__}); delete {previous.name} by hand"
        ) from None
    return [output_dir / name for name in files]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class PacedClient:
    """GET with a minimum interval between requests to one host."""

    def __init__(
        self,
        client: httpx.Client,
        interval: float,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> None:
        self._client = client
        self._interval = interval
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None
        self.count = 0

    def get(self, url: str, headers: Mapping[str, str]) -> httpx.Response:
        if self._last is not None:
            wait = self._last + self._interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        try:
            return self._client.get(url, headers=dict(headers))
        except httpx.HTTPError as exc:
            raise RecorderError(f"GET {url} failed: {type(exc).__name__}: {exc}") from None
        finally:
            self._last = self._clock()
            self.count += 1


def check_sec_response(response: httpx.Response, label: str) -> None:
    """Stop on any SEC refusal, before anything about the body is kept."""
    if response.status_code == 403 or SEC_BLOCK_MARKER in response.text.lower():
        raise SecRefused(
            f"SEC refused {label}: HTTP {response.status_code} "
            f"({len(response.content)} bytes; body not echoed). Nothing was written. "
            f"Check {UA_ENV}; do not work around this."
        )
    if response.status_code != 200:
        raise RecorderError(
            f"SEC answered {label} with HTTP {response.status_code} "
            f"({len(response.content)} bytes; body not echoed). Nothing was written."
        )


def _reject_floats(text: str) -> Any:
    """``parse_float`` hook: re-serialising a float could round it. Abort instead."""
    raise RecorderError(f"a JSON body carries a fractional number ({text}); refusing to re-serialise")


def load_json(text: str) -> Any:
    try:
        return json.loads(text, parse_float=_reject_floats)
    except json.JSONDecodeError as exc:
        raise RecorderError(f"expected JSON, got a decode error at char {exc.pos}") from None


def dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Submissions and the ticker map
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Filing:
    accession: str
    filing_date: str
    report_date: str
    primary_document: str

    @property
    def url(self) -> str:
        return ARCHIVE_URL.format(cik=int(TRUST_CIK), accession=self.accession.replace("-", ""))


def latest_quarter(submissions: Mapping[str, Any]) -> tuple[str, list[int], list[Filing]]:
    """The latest report date with an NPORT-P, the row indices, and the filings.

    Amendments (``NPORT-P/A``) are a different form string and are excluded.
    """
    recent = submissions["filings"]["recent"]
    forms: list[str] = recent["form"]
    indices = [i for i, form in enumerate(forms) if form == NPORT_FORM]
    if not indices:
        raise RecorderError("the submissions list carries no NPORT-P filing")
    report_date = max(recent["reportDate"][i] for i in indices)
    kept = [i for i in indices if recent["reportDate"][i] == report_date]
    filings = [
        Filing(
            accession=recent["accessionNumber"][i],
            filing_date=recent["filingDate"][i],
            report_date=recent["reportDate"][i],
            primary_document=recent["primaryDocument"][i],
        )
        for i in kept
    ]
    return report_date, kept, filings


def trim_submissions(submissions: Mapping[str, Any], kept: Sequence[int]) -> dict[str, Any]:
    """Every top-level field, with ``filings.recent`` cut to ``kept`` rows.

    Columnar, like the source: each column keeps its name and loses the rows
    that are not in ``kept``.
    """
    recent = submissions["filings"]["recent"]
    total = len(recent["form"])
    trimmed_recent = {
        column: [values[i] for i in kept] if isinstance(values, list) else values
        for column, values in recent.items()
    }
    out: dict[str, Any] = {
        "_trimmed": (
            f"filings.recent cut from {total} rows to the {len(kept)} {NPORT_FORM} "
            "rows of the latest report date; every other field is as served"
        )
    }
    for key, value in submissions.items():
        if key == "filings":
            out[key] = {**value, "recent": trimmed_recent}
        else:
            out[key] = value
    return out


def trust_ticker_rows(tickers_mf: Mapping[str, Any]) -> tuple[list[str], list[list[Any]]]:
    fields: list[str] = tickers_mf["fields"]
    cik_at = fields.index("cik")
    rows = [row for row in tickers_mf["data"] if int(row[cik_at]) == int(TRUST_CIK)]
    return fields, rows


def sector_series(fields: Sequence[str], rows: Sequence[Sequence[Any]]) -> dict[str, tuple[str, str]]:
    """``ticker -> (seriesId, classId)`` for the eleven, from SEC's own map."""
    series_at, class_at, symbol_at = (
        fields.index("seriesId"),
        fields.index("classId"),
        fields.index("symbol"),
    )
    found: dict[str, tuple[str, str]] = {}
    for ticker in SECTOR_TICKERS:
        matches = [row for row in rows if str(row[symbol_at]).upper() == ticker]
        if len(matches) != 1:
            raise RecorderError(
                f"SEC's ticker map has {len(matches)} rows for {ticker} under this trust; expected 1"
            )
        series_id = str(matches[0][series_at])
        if not _SERIES_ID.fullmatch(series_id):
            raise RecorderError(f"SEC's ticker map gives {ticker} an unusable seriesId {series_id!r}")
        found[ticker] = (series_id, str(matches[0][class_at]))
    return found


# ---------------------------------------------------------------------------
# N-PORT documents
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return None


def _first(root: ET.Element, name: str) -> ET.Element | None:
    for element in root.iter():
        if _local(element.tag) == name:
            return element
    return None


@dataclass(frozen=True)
class Holding:
    name: str
    cusip: str | None
    asset_cat: str | None
    pct_val: Decimal | None


@dataclass
class NportDoc:
    series_id: str
    series_name: str
    rep_pd_end: str
    rep_pd_date: str
    holdings: list[Holding] = field(default_factory=list)

    @property
    def equity(self) -> list[Holding]:
        return [h for h in self.holdings if h.asset_cat == EQUITY_COMMON]


def _usable_cusip(value: str | None) -> str | None:
    """A nine-character CUSIP, or None. N-PORT spells an absent one ``N/A``
    or zeros."""
    if value is None:
        return None
    value = value.strip().upper()
    if len(value) != 9 or not value.isalnum() or set(value) == {"0"}:
        return None
    return value


def parse_nport(xml_text: str) -> NportDoc:
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError as exc:
        raise RecorderError(f"primary_doc.xml does not parse: {exc}") from None
    gen = _first(root, "genInfo")
    if gen is None:
        raise RecorderError("primary_doc.xml has no genInfo")
    doc = NportDoc(
        series_id=_child_text(gen, "seriesId") or "",
        series_name=_child_text(gen, "seriesName") or "",
        rep_pd_end=_child_text(gen, "repPdEnd") or "",
        rep_pd_date=_child_text(gen, "repPdDate") or "",
    )
    for element in root.iter():
        if _local(element.tag) != "invstOrSec":
            continue
        pct_text = _child_text(element, "pctVal")
        try:
            pct = Decimal(pct_text) if pct_text else None
        except InvalidOperation:
            pct = None
        doc.holdings.append(
            Holding(
                name=_child_text(element, "name") or "",
                cusip=_usable_cusip(_child_text(element, "cusip")),
                asset_cat=_child_text(element, "assetCat"),
                pct_val=pct,
            )
        )
    return doc


_HOLDING_BLOCK = re.compile(r"[ \t]*<invstOrSec>.*?</invstOrSec>[ \t]*(?:\r?\n)?", re.S)
_HOLDINGS_SECTION = re.compile(r"[ \t]*<invstOrSecs>.*?</invstOrSecs>[ \t]*(?:\r?\n)?", re.S)


def trim_non_equity(xml_text: str, doc: NportDoc) -> tuple[str, int]:
    """Remove every ``<invstOrSec>`` whose ``assetCat`` is not EC.

    Text surgery rather than parse-and-reserialise, so every kept byte is the
    vendor's own. The text match and the parser can disagree -- a namespace
    prefix or an attribute on the tag, or an ``<assetCat>EC</assetCat>``
    nested deeper than the holding's own -- and a trim that silently kept or
    dropped the wrong lines is worse than none. So it refuses unless the
    block count matches the parser before the trim, and after it the parser
    finds exactly ``doc.equity``'s count of holdings, every one of them EC.
    """
    expected_blocks = len(doc.holdings)
    blocks = _HOLDING_BLOCK.findall(xml_text)
    if len(blocks) != expected_blocks:
        raise RecorderError(
            f"trim found {len(blocks)} holding blocks, the parser {expected_blocks}; not trimming"
        )
    removed = 0

    def keep(match: re.Match[str]) -> str:
        nonlocal removed
        if f"<assetCat>{EQUITY_COMMON}</assetCat>" in match.group(0):
            return match.group(0)
        removed += 1
        return ""

    trimmed = _HOLDING_BLOCK.sub(keep, xml_text)
    after = parse_nport(trimmed)
    wanted = len(doc.equity)
    not_equity = sum(1 for h in after.holdings if h.asset_cat != EQUITY_COMMON)
    if len(after.holdings) != wanted or not_equity or removed != expected_blocks - wanted:
        raise RecorderError(
            f"the trim and the parser disagree: {len(after.holdings)} holdings kept "
            f"({not_equity} not EC), {removed} removed; the parser has {wanted} EC of "
            f"{expected_blocks}. Not trimming"
        )
    return trimmed, removed


def strip_holdings(xml_text: str) -> str:
    """The document without its ``<invstOrSecs>`` section: header and fund info."""
    trimmed, count = _HOLDINGS_SECTION.subn("", xml_text, count=1)
    if count != 1:
        raise RecorderError("no <invstOrSecs> section to strip")
    parse_nport(trimmed)
    return trimmed


#: What a seriesId must look like before it goes into a file name: never
#: empty, never a path separator. SEC's are ``S`` and nine digits.
_SERIES_ID = re.compile(r"[A-Z0-9]+")

#: The excluded same-quarter filing recorded as proof of the series filter.
PREMIUM_MARKER: Final = "premium income"


def premium_sample(
    excluded: Sequence[tuple["Filing", str, "NportDoc"]],
) -> tuple["Filing", str, "NportDoc"]:
    """The first (by seriesId) excluded filing whose seriesName contains
    "Premium Income". Chosen by name, never by position; refuses if none, or
    if its seriesId is not fit for a file name."""
    matches = [item for item in excluded if PREMIUM_MARKER in item[2].series_name.lower()]
    if not matches:
        raise RecorderError(
            f"no excluded same-quarter filing has a seriesName containing 'Premium Income' "
            f"({len(excluded)} excluded); nothing to record as the filter's proof"
        )
    chosen = min(matches, key=lambda item: item[2].series_id)
    if not _SERIES_ID.fullmatch(chosen[2].series_id):
        raise RecorderError(
            f"the Premium Income filing {chosen[0].accession} carries an unusable seriesId "
            f"{chosen[2].series_id!r}; refusing to name a file after it"
        )
    return chosen


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class FundResult:
    ticker: str
    class_id: str
    filing: Filing
    doc: NportDoc
    xml_written: str
    complete: bool
    non_equity_removed: int
    bytes_original: int


@dataclass
class CusipResult:
    cusip: str
    name: str
    funds: list[str]
    status_code: int
    body_text: str
    symbol: str | None
    asset_class: str | None
    exchange: str | None
    status: str | None
    tradable: bool | None


class Recorder:
    def __init__(
        self,
        env: Mapping[str, str],
        transport: httpx.BaseTransport | None,
        output_dir: Path,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
        out: Callable[[str], None],
    ) -> None:
        self.env = env
        self.transport = transport
        self.output_dir = output_dir
        self.sleep = sleep
        self.clock = clock
        self.out = out
        self.secrets = secret_values(env)

    def say(self, message: str) -> None:
        self.out(redact(message, self.secrets))

    def user_agent(self) -> str:
        value = self.env.get(UA_ENV, "").strip()
        if not value:
            raise MissingUserAgent(
                f"{UA_ENV} is missing from the process environment. SEC requires a "
                "declared User-Agent; there is no default. Run with --env-file. If it "
                "is in the file already: uv's --env-file parser drops an unquoted value "
                f'containing a space, so write it as {UA_ENV}="Name email" (quoted).'
            )
        return value

    def run(self, *, dry_run: bool, overwrite: bool, alpaca: bool, survey: bool) -> int:
        user_agent = self.user_agent()
        sec_headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        files: dict[str, str] = {}
        with httpx.Client(transport=self.transport, timeout=30.0) as client:
            sec = PacedClient(client, SEC_MIN_INTERVAL, self.sleep, self.clock)

            def sec_get(url: str, label: str) -> httpx.Response:
                response = sec.get(url, sec_headers)
                self.say(f"SEC {label}: HTTP {response.status_code}, {len(response.content)} bytes")
                if response.status_code == 403:
                    sent = redacted_headers(sec_headers, self.secrets)
                    self.say(f"  headers sent (values redacted): {sorted(sent)}")
                check_sec_response(response, label)
                return response

            submissions = load_json(sec_get(SUBMISSIONS_URL, "submissions").text)
            report_date, kept, filings = latest_quarter(submissions)
            self.say(f"latest {NPORT_FORM} report date {report_date}: {len(filings)} filings")
            files[f"CIK{TRUST_CIK}.trimmed.json"] = redact(
                dumps(trim_submissions(submissions, kept)), self.secrets
            )

            tickers_mf = load_json(sec_get(TICKERS_MF_URL, "company_tickers_mf").text)
            fields, rows = trust_ticker_rows(tickers_mf)
            series = sector_series(fields, rows)
            files["company_tickers_mf.trimmed.json"] = redact(
                dumps(
                    {
                        "_trimmed": (
                            f"data cut from {len(tickers_mf['data'])} rows to the "
                            f"{len(rows)} rows whose cik is {int(TRUST_CIK)}"
                        ),
                        "fields": fields,
                        "data": rows,
                    }
                ),
                self.secrets,
            )

            docs: list[tuple[Filing, str, NportDoc]] = []
            for filing in filings:
                response = sec_get(filing.url, f"primary_doc {filing.accession}")
                text = response.content.decode("utf-8")
                docs.append((filing, text, parse_nport(text)))

            funds, excluded = self._select(series, docs)
            for fund in funds:
                stem = f"nport_{fund.ticker}_primary_doc"
                files[f"{stem}.xml"] = redact(fund.xml_written, self.secrets)
                files[f"{stem}.meta.json"] = redact(
                    dumps(self._meta(fund)), self.secrets
                )
            premium_filing, premium_text, premium_doc = premium_sample(excluded)
            stem = f"nport_excluded_{premium_doc.series_id}_header"
            files[f"{stem}.xml"] = redact(
                strip_holdings(premium_text), self.secrets
            )
            files[f"{stem}.meta.json"] = redact(
                dumps(
                    {
                        "series_id": premium_doc.series_id,
                        "series_name": premium_doc.series_name,
                        "accession": premium_filing.accession,
                        "filing_date": premium_filing.filing_date,
                        "report_date": premium_filing.report_date,
                        "url": premium_filing.url,
                        "recorded_at": now_utc(),
                        "trimmed": (
                            "the <invstOrSecs> section is removed; header, genInfo and "
                            "fundInfo are as served. Recorded to prove a same-trust, "
                            "same-quarter filing outside the eleven is excluded by seriesId"
                        ),
                        "request_headers": HEADER_NOTE,
                    }
                ),
                self.secrets,
            )
            files["series_ids.json"] = redact(
                dumps(self._series_fixture(report_date, funds, excluded)), self.secrets
            )

            cusips: list[CusipResult] = []
            if alpaca:
                cusips = self._lookup_cusips(client, funds, survey)
                files.update(self._cusip_files(cusips, funds, survey))

        self._print_findings(funds, excluded, cusips)
        self.say(f"SEC requests: {sec.count}")
        if dry_run:
            self.say(f"dry run: {len(files)} files prepared, none written")
            return 0
        written = write_fixtures(self.output_dir, files, self.secrets, overwrite=overwrite)
        for path in written:
            self.say(f"  wrote {path.name} ({path.stat().st_size} bytes)")
        return 0

    def _select(
        self,
        series: Mapping[str, tuple[str, str]],
        docs: Sequence[tuple[Filing, str, NportDoc]],
    ) -> tuple[list[FundResult], list[tuple[Filing, str, NportDoc]]]:
        by_series: dict[str, list[tuple[Filing, str, NportDoc]]] = {}
        for item in docs:
            by_series.setdefault(item[2].series_id, []).append(item)
        wanted = {series_id for series_id, _ in series.values()}
        funds: list[FundResult] = []
        for ticker in SECTOR_TICKERS:
            series_id, class_id = series[ticker]
            matches = by_series.get(series_id, [])
            if len(matches) != 1:
                raise RecorderError(
                    f"{ticker} ({series_id}) has {len(matches)} {NPORT_FORM} filings "
                    "in the latest quarter; expected 1"
                )
            filing, text, doc = matches[0]
            if ticker in COMPLETE_TICKERS:
                written, removed, complete = text, 0, True
            else:
                written, removed = trim_non_equity(text, doc)
                complete = False
            funds.append(
                FundResult(
                    ticker=ticker,
                    class_id=class_id,
                    filing=filing,
                    doc=doc,
                    xml_written=written,
                    complete=complete,
                    non_equity_removed=removed,
                    bytes_original=len(text.encode("utf-8")),
                )
            )
        excluded = sorted(
            (item for item in docs if item[2].series_id not in wanted),
            key=lambda item: item[2].series_id,
        )
        if not excluded:
            raise RecorderError("no same-quarter filing outside the eleven; nothing proves the filter")
        return funds, excluded

    def _meta(self, fund: FundResult) -> dict[str, Any]:
        equity = fund.doc.equity
        return {
            "ticker": fund.ticker,
            "series_id": fund.doc.series_id,
            "series_name": fund.doc.series_name,
            "class_id": fund.class_id,
            "accession": fund.filing.accession,
            "filing_date": fund.filing.filing_date,
            "report_date": fund.filing.report_date,
            "rep_pd_end": fund.doc.rep_pd_end,
            "url": fund.filing.url,
            "recorded_at": now_utc(),
            "complete": fund.complete,
            "trimmed": (
                "none: byte-for-byte as served"
                if fund.complete
                else "every <invstOrSec> whose assetCat is not EC is removed; all else as served"
            ),
            "holdings_total": len(fund.doc.holdings),
            "equity_lines": len(equity),
            "non_equity_removed": fund.non_equity_removed,
            "equity_pct_val_sum": str(sum((h.pct_val or Decimal(0) for h in equity), Decimal(0))),
            "equity_lines_without_cusip": sum(1 for h in equity if h.cusip is None),
            "bytes_original": fund.bytes_original,
            "request_headers": HEADER_NOTE,
        }

    def _series_fixture(
        self,
        report_date: str,
        funds: Sequence[FundResult],
        excluded: Sequence[tuple[Filing, str, NportDoc]],
    ) -> dict[str, Any]:
        return {
            "trust_cik": TRUST_CIK,
            "report_date": report_date,
            "source": (
                "tickers -> seriesId from SEC company_tickers_mf.json; each confirmed "
                "against genInfo/seriesId in that fund's primary_doc.xml"
            ),
            "funds": {
                fund.ticker: {
                    "series_id": fund.doc.series_id,
                    "series_name": fund.doc.series_name,
                    "class_id": fund.class_id,
                    "accession": fund.filing.accession,
                    "filing_date": fund.filing.filing_date,
                    "report_date": fund.filing.report_date,
                }
                for fund in funds
            },
            "excluded_same_quarter": [
                {
                    "series_id": doc.series_id,
                    "series_name": doc.series_name,
                    "accession": filing.accession,
                }
                for filing, _, doc in excluded
            ],
        }

    def _lookup_cusips(
        self, client: httpx.Client, funds: Sequence[FundResult], survey: bool
    ) -> list[CusipResult]:
        try:
            credentials = AlpacaCredentials.paper_from_env(self.env)
        except Exception as exc:  # the message names env vars, never values
            raise RecorderError(f"Alpaca paper credentials unavailable: {type(exc).__name__}") from None
        names: dict[str, str] = {}
        holders: dict[str, list[str]] = {}
        for fund in funds:
            for holding in fund.doc.equity:
                if holding.cusip is None:
                    continue
                names.setdefault(holding.cusip, holding.name)
                holders.setdefault(holding.cusip, []).append(fund.ticker)
        targets = sorted(names) if survey else self._sample(funds)
        trading = PacedClient(client, ALPACA_MIN_INTERVAL, self.sleep, self.clock)
        results: list[CusipResult] = []
        for cusip in targets:
            response = trading.get(
                f"{credentials.trading_base_url}/v2/assets/{cusip}", credentials.headers()
            )
            asset = self._asset_answer(cusip, response)
            results.append(
                CusipResult(
                    cusip=cusip,
                    name=names.get(cusip, ""),
                    funds=holders.get(cusip, []),
                    status_code=response.status_code,
                    body_text=response.text,
                    symbol=asset.get("symbol"),
                    asset_class=asset.get("class"),
                    exchange=asset.get("exchange"),
                    status=asset.get("status"),
                    tradable=asset.get("tradable"),
                )
            )
        resolved = sum(1 for r in results if r.symbol)
        self.say(f"Alpaca CUSIP lookups: {len(results)} requested, {resolved} resolved")
        return results

    @staticmethod
    def _asset_answer(cusip: str, response: httpx.Response) -> dict[str, Any]:
        """The asset for a 200, ``{}`` for a 404, and a stop for anything else.

        Only those two are answers about the CUSIP. A 401/403 is a credentials
        problem, a 429 is the bucket, a 5xx is Alpaca -- recorded as "no ticker"
        they would land in ``cusip_survey.json`` as hundreds of false facts
        about unresolvable CUSIPs. Stopping here writes nothing, because the
        batch is written only after every request. The body is not echoed.
        """
        status = response.status_code
        if status == 404:
            return {}
        if status != 200:
            raise RecorderError(
                f"Alpaca answered GET /v2/assets/{cusip} with HTTP {status} "
                f"({len(response.content)} bytes; body not echoed). Only 200 and 404 are "
                "answers about a CUSIP; nothing was written."
            )
        try:
            body = json.loads(response.text)
        except json.JSONDecodeError:
            body = None
        if not isinstance(body, dict):
            raise RecorderError(
                f"Alpaca answered GET /v2/assets/{cusip} with HTTP 200 but no JSON object; "
                "nothing was written."
            )
        return body

    def _sample(self, funds: Sequence[FundResult]) -> list[str]:
        """The heaviest equity lines of the sample funds, plus any Berkshire
        class-share line -- the ticker whose punctuation is the question."""
        chosen: list[str] = []
        for fund in funds:
            if fund.ticker not in SAMPLE_FUNDS:
                continue
            ranked = sorted(
                (h for h in fund.doc.equity if h.cusip),
                key=lambda h: h.pct_val or Decimal(0),
                reverse=True,
            )
            chosen += [h.cusip for h in ranked[:SAMPLE_PER_FUND] if h.cusip]
            chosen += [h.cusip for h in fund.doc.equity if h.cusip and "BERKSHIRE" in h.name.upper()]
        return list(dict.fromkeys(chosen))

    def _cusip_files(
        self, results: Sequence[CusipResult], funds: Sequence[FundResult], survey: bool
    ) -> dict[str, str]:
        sample = set(self._sample(funds))
        files: dict[str, str] = {}
        for result in results:
            if result.cusip not in sample and result.symbol:
                continue
            try:
                json.loads(result.body_text)
                body = result.body_text.strip()
            except json.JSONDecodeError:
                body = json.dumps(result.body_text)
            envelope = (
                "{\n"
                f'  "request": {json.dumps("GET /v2/assets/" + result.cusip)},\n'
                '  "host": "paper trading host",\n'
                f'  "request_headers": {json.dumps(HEADER_NOTE)},\n'
                f'  "nport_name": {json.dumps(result.name)},\n'
                f'  "status_code": {result.status_code},\n'
                f'  "body": {body}\n'
                "}\n"
            )
            files[f"alpaca_asset_{result.cusip}.json"] = redact(
                envelope, self.secrets
            )
        if survey:
            files["cusip_survey.json"] = redact(
                dumps(
                    {
                        "recorded_at": now_utc(),
                        "note": (
                            "every assetCat=EC CUSIP across the eleven sector funds, "
                            "looked up one by one at GET /v2/assets/{cusip}"
                        ),
                        "rows": [
                            {
                                "cusip": r.cusip,
                                "nport_name": r.name,
                                "funds": r.funds,
                                "status_code": r.status_code,
                                "symbol": r.symbol,
                                "class": r.asset_class,
                                "exchange": r.exchange,
                                "status": r.status,
                                "tradable": r.tradable,
                            }
                            for r in results
                        ],
                    }
                ),
                self.secrets,
            )
        return files

    def _print_findings(
        self,
        funds: Sequence[FundResult],
        excluded: Sequence[tuple[Filing, str, NportDoc]],
        cusips: Sequence[CusipResult],
    ) -> None:
        self.say("ticker | seriesId | report | filed | EC lines | EC pctVal sum | EC w/o CUSIP | non-EC")
        for fund in funds:
            equity = fund.doc.equity
            total = sum((h.pct_val or Decimal(0) for h in equity), Decimal(0))
            missing = sum(1 for h in equity if h.cusip is None)
            self.say(
                f"{fund.ticker} | {fund.doc.series_id} | {fund.filing.report_date} | "
                f"{fund.filing.filing_date} | {len(equity)} | {total} | {missing} | "
                f"{len(fund.doc.holdings) - len(equity)}"
            )
        self.say(f"excluded same-quarter filings: {len(excluded)}")
        for _, _, doc in excluded:
            self.say(f"  {doc.series_id} {doc.series_name}")
        for result in cusips:
            if not result.symbol or "BERKSHIRE" in result.name.upper():
                self.say(
                    f"  CUSIP {result.cusip} ({result.name}): HTTP {result.status_code} "
                    f"-> {result.symbol}"
                )


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    output_dir: Path = OUTPUT_DIR,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(description="Record SEC N-PORT fixtures (GET only).")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report; write nothing")
    parser.add_argument("--overwrite", action="store_true", help="replace existing fixtures")
    parser.add_argument("--no-alpaca", action="store_true", help="skip the CUSIP lookups")
    parser.add_argument("--no-survey", action="store_true", help="look up the sample only")
    args = parser.parse_args(argv)
    recorder = Recorder(
        env=os.environ if env is None else env,
        transport=transport,
        output_dir=output_dir,
        sleep=sleep,
        clock=clock,
        out=out,
    )
    try:
        return recorder.run(
            dry_run=args.dry_run,
            overwrite=args.overwrite,
            alpaca=not args.no_alpaca,
            survey=not args.no_survey,
        )
    except RecorderError as exc:
        recorder.say(f"STOPPED: {exc}")
        return exc.code
    except Exception as exc:  # a shape surprise: still redacted, still non-zero
        recorder.say(f"STOPPED on an unexpected {type(exc).__name__}: {exc}. Nothing was written.")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
