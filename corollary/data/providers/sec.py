"""SEC EDGAR over HTTPS -- N-PORT holdings for the SPDR sector seed.

Owner decision (Phase 3 step 4): the eleven Select Sector SPDR funds'
holdings come from the trust's quarterly **NPORT-P** filings, and each
holding's CUSIP is resolved to a ticker by
:meth:`corollary.data.providers.alpaca.AlpacaProvider.asset_by_cusip`. That
replaces the hand-downloaded SSGA spreadsheets of decision 6. This file is
the entire SEC surface area, the way ``fred.py`` is FRED's; storage, the
quarterly job and the loader switch are the next unit's.

Three documents, three methods:

* ``www.sec.gov/files/company_tickers_mf.json`` -- SEC's own mutual-fund map
  (``cik``, ``seriesId``, ``classId``, ``symbol``).
  :meth:`SecProvider.sector_fund_series` reads ticker -> seriesId for the
  eleven under the trust's CIK. **A fund is identified by seriesId, never by
  list position and never by matching a series name**: the trust files 22
  NPORT-P a quarter (11 sector funds, 11 "Premium Income"), and another
  registrant can list a symbol that collides.
* ``data.sec.gov/submissions/CIK##########.json`` --
  :meth:`SecProvider.latest_nport_filings`: the latest quarter's NPORT-P rows,
  and any NPORT-P/A for that quarter, flagged.
* ``www.sec.gov/Archives/edgar/data/{cik}/{accession}/primary_doc.xml`` --
  :meth:`SecProvider.nport_holdings`: ``genInfo`` and every ``<invstOrSec>``.
  N-PORT carries **no tickers**; a holding is a name, a CUSIP, an ISIN and a
  ``pctVal``. The submissions list does not say which series a filing is
  for, so a quarter's 22 documents are all fetched and
  :func:`select_sector_documents` matches them to funds by ``genInfo/seriesId``.

What this file is deliberate about
----------------------------------

**The User-Agent is the owner's name and email, and it is treated as a
secret.** SEC's fair-access policy requires a declared ``User-Agent``;
:meth:`SecProvider.from_env` reads it from ``SEC_USER_AGENT`` and there is no
default -- an invented one would be a false declaration. Unset or blank, the
provider is ``None`` and one log line names the variable, never a value. The
UA travels in a header only (never a URL, so httpx's request log line cannot
carry it) and nothing here logs a header. Every piece of text this module
did not compose -- httpx's exception strings included -- is scrubbed of the
whole UA, the name part, the email and its local part and domain, each in
its literal, percent-, plus- and XML-escaped spellings, case-insensitively.
A transport error's :class:`SecError` is built from that scrubbed text and
raised **outside** the ``except`` block, so neither ``__cause__`` nor
``__context__`` holds the httpx exception, whose ``.request.headers`` carry
the UA. (``from None`` only hides a context from printing; it keeps it.)

**A refusal is final.** A 403, or SEC's "undeclared automated tool" page at
any status, raises :class:`SecAccessRefused`. Nothing in this module retries
anything; that one is called out because retrying it is how a requester's IP
gets blocked for longer.

**Decimal from text.** ``pctVal`` goes from the XML text to ``Decimal``
directly; the JSON documents are decoded with every number exact. A holding
row that cannot be read is skipped and counted, never failing the document;
a document without its ``genInfo`` series or report date fails as a whole.

**Metered against the shared limiter.** ``data.sec.gov`` and ``www.sec.gov``
draw on one bucket in :mod:`corollary.ratelimit` (4 per second), because
SEC's 10/s ceiling is per requester across both.
"""

import logging
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any, Final
from urllib.parse import quote, quote_plus
from xml.parsers import expat

import httpx

from corollary.data.providers.interface import ProviderError, RateLimitedError
from corollary.ratelimit import SEC_DATA_HOST, SEC_WWW_HOST, HostRateLimiter, default_limiter
from corollary.wire import WireFormatError, decode_json, vendor_detail

__all__ = [
    "ARCHIVE_URL_TEMPLATE",
    "EQUITY_COMMON",
    "NPORT_AMENDMENT_FORM",
    "NPORT_FORM",
    "SEC_BLOCK_MARKER",
    "SEC_USER_AGENT_ENV",
    "SPDR_SECTOR_TICKERS",
    "SPDR_SECTOR_TRUST_CIK",
    "SUBMISSIONS_URL_TEMPLATE",
    "TICKERS_MF_URL",
    "NportDocument",
    "NportFiling",
    "NportFilings",
    "NportHolding",
    "SecAccessRefused",
    "SecError",
    "SecProvider",
    "SectorFund",
    "SectorSelection",
    "parse_nport_document",
    "parse_nport_filings",
    "parse_sector_series",
    "select_sector_documents",
]

logger = logging.getLogger(__name__)

#: The declared identity, ``"Name email"``. Named in ``.env.example``.
SEC_USER_AGENT_ENV: Final = "SEC_USER_AGENT"

#: "SELECT SECTOR SPDR TRUST". CIK 1100949 is a dead namesake.
SPDR_SECTOR_TRUST_CIK: Final = "0001064641"

#: The eleven sector funds, resolved to seriesIds through SEC's ticker map.
SPDR_SECTOR_TICKERS: Final = (
    "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
)

NPORT_FORM: Final = "NPORT-P"
NPORT_AMENDMENT_FORM: Final = "NPORT-P/A"

#: ``assetCat`` for common equity -- the only holdings the seed keeps.
EQUITY_COMMON: Final = "EC"

SUBMISSIONS_URL_TEMPLATE: Final = f"https://{SEC_DATA_HOST}/submissions/CIK{{cik10}}.json"
TICKERS_MF_URL: Final = f"https://{SEC_WWW_HOST}/files/company_tickers_mf.json"
ARCHIVE_URL_TEMPLATE: Final = (
    f"https://{SEC_WWW_HOST}/Archives/edgar/data/{{cik}}/{{accession}}/primary_doc.xml"
)

#: Lower-cased marker of SEC's block page for a missing or undeclared UA.
SEC_BLOCK_MARKER: Final = "undeclared automated tool"

#: Shorter than this and a piece of the identity would redact ordinary words;
#: the whole UA and the whole email still cover it.
_MIN_SECRET_LENGTH: Final = 5

# ASCII digits spelled out: ``\d`` on a ``str`` also matches every Unicode
# decimal digit, which ``int()`` then accepts.
_CIK_RE: Final = re.compile(r"[0-9]{1,10}")
_ACCESSION_RE: Final = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
_SERIES_ID_RE: Final = re.compile(r"S[0-9]{9}")
#: Eight letters or digits, then the check character, which is always a digit.
_CUSIP_RE: Final = re.compile(r"[A-Z0-9]{8}[0-9]")
_ISIN_RE: Final = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")

#: How many skipped-row reasons a log line quotes.
_REASON_SAMPLE: Final = 5


class SecError(ProviderError):
    """SEC could not answer, or answered with something that is not the document."""


class SecAccessRefused(SecError):
    """SEC refused the request: a 403, or its undeclared-automated-tool page.

    Never retried. The remedy is the declared identity in
    ``SEC_USER_AGENT`` (or waiting out a rate block), not another request.
    """


# ------------------------------------------------------------------ values


@dataclass(frozen=True, slots=True)
class NportFiling:
    """One NPORT-P or NPORT-P/A row of the submissions list."""

    accession: str
    form: str
    filing_date: date
    report_date: date
    primary_document: str

    @property
    def is_amendment(self) -> bool:
        return self.form == NPORT_AMENDMENT_FORM


@dataclass(frozen=True, slots=True)
class NportFilings:
    """The latest quarter's N-PORT rows for one registrant.

    ``report_date`` is the latest ``reportDate`` of any original NPORT-P;
    ``filings`` are every NPORT-P and NPORT-P/A row carrying it, sorted by
    accession. ``skipped`` counts N-PORT rows (any quarter) that could not be
    read, each logged.
    """

    cik: str
    report_date: date
    filings: tuple[NportFiling, ...]
    skipped: int = 0

    @property
    def originals(self) -> tuple[NportFiling, ...]:
        return tuple(f for f in self.filings if not f.is_amendment)

    @property
    def amendments(self) -> tuple[NportFiling, ...]:
        return tuple(f for f in self.filings if f.is_amendment)


@dataclass(frozen=True, slots=True)
class NportHolding:
    """One ``<invstOrSec>``. ``pct_val`` is percent of the fund's net assets.

    ``cusip`` and ``isin`` are ``None`` where N-PORT spells an absent one
    (``N/A``, zeros) or the value is not the identifier's shape.
    """

    name: str
    cusip: str | None
    isin: str | None
    pct_val: Decimal
    asset_cat: str


@dataclass(frozen=True, slots=True)
class NportDocument:
    """One parsed ``primary_doc.xml``.

    ``report_date`` is ``genInfo/repPdDate``, the as-of date of the holdings;
    ``period_end`` is ``repPdEnd``, the fiscal period's end. ``holdings`` are
    every readable row, any asset category; :attr:`equity` is the ``EC``
    subset the seed keeps. ``skipped`` rows could not be read.
    """

    series_id: str
    series_name: str
    report_date: date
    period_end: date | None
    holdings: tuple[NportHolding, ...]
    skipped: int = 0
    skipped_reasons: tuple[str, ...] = ()

    @property
    def equity(self) -> tuple[NportHolding, ...]:
        return tuple(h for h in self.holdings if h.asset_cat == EQUITY_COMMON)


@dataclass(frozen=True, slots=True)
class SectorFund:
    ticker: str
    series_id: str
    filing: NportFiling
    document: NportDocument


@dataclass(frozen=True)
class SectorSelection:
    """The sector funds matched to their documents by seriesId.

    ``funds`` always holds each fund's **original** NPORT-P; an NPORT-P/A is
    never selected. ``excluded_series`` are the series among the documents
    that are not a sector fund (the Premium Income funds), sorted.

    **``amended`` is derived from the filing index, not from what was
    fetched.** It names every sector ticker for which an NPORT-P/A exists
    this quarter *or cannot be ruled out*, sorted:

    * an amendment whose document was among the documents is attributed by
      its ``genInfo/seriesId`` -- to that ticker, or to no ticker at all if
      it is another series' (a Premium Income fund's);
    * an amendment the index lists but whose document was **not** fetched
      cannot be attributed -- the submissions list does not say which series
      a filing is for -- so it lands in ``unattributed_amendments`` and
      **every** sector ticker is in ``amended``.

    So ``amended == ()`` means the index lists no NPORT-P/A for the quarter
    that could belong to a sector fund, never merely that the caller did not
    fetch one. ``amendments`` maps each ticker to the amendments attributed
    to it. What an amendment *means* for the seed -- block, or keep the
    original -- is the caller's decision; this module does not guess.
    """

    funds: Mapping[str, SectorFund]
    excluded_series: tuple[str, ...] = ()
    amended: tuple[str, ...] = ()
    amendments: Mapping[str, tuple[NportFiling, ...]] = field(default_factory=dict)
    unattributed_amendments: tuple[NportFiling, ...] = ()


# ------------------------------------------------------------------ parsing


def _cik10(cik: str | int) -> str:
    text = str(cik).strip()
    if not _CIK_RE.fullmatch(text):
        raise ValueError(f"{cik!r} is not a CIK of up to ten digits")
    return text.zfill(10)


def _date(value: object, what: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{what} is a {type(value).__name__}, not a date string")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"{what} {value[:20]!r} is not an ISO date") from None


def parse_sector_series(
    payload: Any, *, cik: str | int, tickers: Sequence[str]
) -> dict[str, str]:
    """``ticker -> seriesId`` for ``tickers``, from ``company_tickers_mf.json``.

    Only rows under ``cik`` count -- another registrant's row for the same
    symbol is not this trust's fund. Each ticker must appear exactly once
    there with a seriesId of SEC's ``S`` + nine digits shape, or the whole
    map is refused: a seed missing a fund is not a smaller seed.
    """
    if not isinstance(payload, Mapping):
        raise SecError("the ticker map is not an object")
    fields = payload.get("fields")
    rows = payload.get("data")
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise SecError("the ticker map carries no fields/data arrays")
    try:
        cik_at, series_at, symbol_at = (
            fields.index("cik"), fields.index("seriesId"), fields.index("symbol"),
        )
    except ValueError:
        raise SecError(f"the ticker map's fields {fields!r} lack cik/seriesId/symbol") from None
    wanted = int(_cik10(cik))
    width = max(cik_at, series_at, symbol_at) + 1
    trust_rows: list[list[Any]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < width:
            continue
        try:
            row_cik = int(str(row[cik_at]))
        except ValueError:
            continue
        if row_cik == wanted:
            trust_rows.append(row)
    found: dict[str, str] = {}
    for ticker in tickers:
        matches = [r for r in trust_rows if str(r[symbol_at]).strip().upper() == ticker]
        if len(matches) != 1:
            raise SecError(
                f"SEC's ticker map has {len(matches)} rows for {ticker} under CIK "
                f"{wanted}; expected exactly 1"
            )
        series_id = str(matches[0][series_at]).strip()
        if not _SERIES_ID_RE.fullmatch(series_id):
            raise SecError(f"SEC's ticker map gives {ticker} an unusable seriesId {series_id[:20]!r}")
        found[ticker] = series_id
    if len(set(found.values())) != len(found):
        raise SecError("SEC's ticker map gives two sector tickers one seriesId")
    return found


def parse_nport_filings(payload: Any, *, cik: str | int) -> NportFilings:
    """The latest quarter's NPORT-P rows, and its NPORT-P/A rows, from submissions.

    ``filings.recent`` is columnar: one list per field, one index per row.
    Columns of different lengths refuse the document. An N-PORT row whose
    accession or dates cannot be read is skipped, logged and counted.
    """
    cik10 = _cik10(cik)
    filings = payload.get("filings") if isinstance(payload, Mapping) else None
    recent = filings.get("recent") if isinstance(filings, Mapping) else None
    if not isinstance(recent, Mapping):
        raise SecError(f"CIK{cik10}: the submissions document has no filings.recent")
    columns = ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument")
    lists = [recent.get(name) for name in columns]
    if not all(isinstance(values, list) for values in lists):
        raise SecError(f"CIK{cik10}: filings.recent lacks one of {', '.join(columns)}")
    lengths = {len(values) for values in lists if isinstance(values, list)}
    if len(lengths) != 1:
        raise SecError(f"CIK{cik10}: filings.recent columns disagree in length {sorted(lengths)}")
    accessions, filed, reported, forms, documents = lists
    assert isinstance(forms, list) and isinstance(accessions, list)  # for mypy
    assert isinstance(filed, list) and isinstance(reported, list) and isinstance(documents, list)

    rows: list[NportFiling] = []
    skipped = 0
    for i, form in enumerate(forms):
        if form not in (NPORT_FORM, NPORT_AMENDMENT_FORM):
            continue
        try:
            accession = accessions[i]
            if not isinstance(accession, str) or not _ACCESSION_RE.fullmatch(accession):
                raise ValueError(f"accession {str(accession)[:30]!r} is not ##########-##-######")
            document = documents[i] if isinstance(documents[i], str) else ""
            rows.append(
                NportFiling(
                    accession=accession,
                    form=form,
                    filing_date=_date(filed[i], "filingDate"),
                    report_date=_date(reported[i], "reportDate"),
                    primary_document=document,
                )
            )
        except ValueError as exc:
            skipped += 1
            logger.warning(
                "sec submissions row %d skipped: %s",
                i,
                exc,
                extra={
                    "event": "sec_submissions_row_skipped",
                    "rule": "an N-PORT row that cannot be read is skipped and logged, never guessed",
                    "cik": cik10,
                    "row": i,
                    "cause": str(exc),
                },
            )
    originals = [r for r in rows if r.form == NPORT_FORM]
    if not originals:
        raise SecError(f"CIK{cik10}: the submissions list carries no readable {NPORT_FORM}")
    latest = max(r.report_date for r in originals)
    kept = sorted((r for r in rows if r.report_date == latest), key=lambda r: r.accession)
    return NportFilings(cik=cik10, report_date=latest, filings=tuple(kept), skipped=skipped)


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(element: ET.Element, name: str) -> ET.Element | None:
    """The first **direct** child named ``name``, namespace ignored.

    Direct only: a derivative's nested counterparty also has a ``<name>``.
    """
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _child_text(element: ET.Element, name: str) -> str | None:
    child = _child(element, name)
    return None if child is None else (child.text or "").strip()


def _first(root: ET.Element, name: str) -> ET.Element | None:
    for element in root.iter():
        if _local(element.tag) == name:
            return element
    return None


def _usable_cusip(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().upper()
    if not _CUSIP_RE.fullmatch(text) or set(text) == {"0"}:
        return None
    return text


def _usable_isin(holding: ET.Element) -> str | None:
    identifiers = _child(holding, "identifiers")
    raw: str | None = None
    if identifiers is not None:
        isin = _child(identifiers, "isin")
        if isin is not None:
            raw = isin.get("value") or (isin.text or "")
    if raw is None:
        raw = _child_text(holding, "isin")
    if raw is None:
        return None
    text = raw.strip().upper()
    return text if _ISIN_RE.fullmatch(text) else None


def _holding(element: ET.Element) -> NportHolding:
    name = _child_text(element, "name")
    if not name:
        raise ValueError("no name")
    asset_cat = _child_text(element, "assetCat")
    if not asset_cat:
        conditional = _child(element, "assetConditional")
        asset_cat = (conditional.get("assetCat") or "").strip() if conditional is not None else ""
    if not asset_cat:
        raise ValueError(f"{name[:60]}: no assetCat")
    pct_text = _child_text(element, "pctVal")
    if not pct_text:
        raise ValueError(f"{name[:60]}: no pctVal")
    try:
        pct_val = Decimal(pct_text)
    except InvalidOperation:
        raise ValueError(f"{name[:60]}: pctVal {pct_text[:30]!r} is not a number") from None
    if not pct_val.is_finite():
        raise ValueError(f"{name[:60]}: pctVal {pct_text[:30]!r} is not finite")
    return NportHolding(
        name=name,
        cusip=_usable_cusip(_child_text(element, "cusip")),
        isin=_usable_isin(element),
        pct_val=pct_val,
        asset_cat=asset_cat.upper(),
    )


_DECLARATION_REFUSED: Final = (
    "primary_doc.xml declares a DTD or an entity; refusing to parse it"
)


def _refuse_declaration(*_args: object) -> None:
    raise SecError(_DECLARATION_REFUSED)


def _refuse_external_entity(*_args: object) -> int:
    """expat's external-entity callback returns a status; this one never returns."""
    raise SecError(_DECLARATION_REFUSED)


def _expanded(name: str) -> str:
    """expat's ``uri}local`` spelling to ElementTree's ``{uri}local``."""
    return "{" + name if "}" in name else name


def _parse_xml(raw: bytes) -> ET.Element:
    """Parse with expat directly, refusing any DTD or entity **inside the parser**.

    The refusal is expat's own doctype and entity-declaration callbacks, so
    it sees the document after expat has decoded it -- UTF-8, UTF-16 or any
    declared encoding alike. (A scan of the raw bytes for ``<!DOCTYPE``
    could not: in UTF-16 every character is two bytes and the marker is not
    there to find.) N-PORT declares neither, and without a DTD there is no
    entity to expand. The tree is built by :class:`ET.TreeBuilder`, the same
    builder ``ET.fromstring`` uses; comments and processing instructions are
    dropped, which nothing here reads.
    """
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator="}")
    parser.buffer_text = True
    refuse: Callable[..., None] = _refuse_declaration
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.UnparsedEntityDeclHandler = refuse
    parser.NotationDeclHandler = refuse
    refuse_external: Callable[..., int] = _refuse_external_entity
    parser.ExternalEntityRefHandler = refuse_external

    def start(tag: str, attrs: dict[str, str]) -> None:
        builder.start(_expanded(tag), {_expanded(k): v for k, v in attrs.items()})

    def end(tag: str) -> None:
        builder.end(_expanded(tag))

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    failure: str | None = None
    try:
        parser.Parse(raw, True)
    except expat.ExpatError as exc:
        # Only the text is kept: the SecError is raised below, outside this
        # block, so it chains to nothing.
        failure = (
            f"primary_doc.xml does not parse: {expat.ErrorString(exc.code)} "
            f"(line {exc.lineno}, column {exc.offset})"
        )
    if failure is not None:
        raise SecError(failure)
    return builder.close()


def parse_nport_document(xml: bytes | str) -> NportDocument:
    """One ``primary_doc.xml``: ``genInfo`` and every readable holding.

    A DTD or entity declaration is refused by the parser itself
    (:func:`_parse_xml`), whatever the document's encoding -- N-PORT has
    neither, and entity expansion is the one thing an XML parser does with
    untrusted input that it should not. ``seriesId`` and ``repPdDate`` are
    required; a holding that cannot be read is skipped with its reason.
    """
    raw = xml.encode("utf-8") if isinstance(xml, str) else xml
    root = _parse_xml(raw)
    gen = _first(root, "genInfo")
    if gen is None:
        raise SecError("primary_doc.xml has no genInfo")
    series_id = _child_text(gen, "seriesId") or ""
    if not _SERIES_ID_RE.fullmatch(series_id):
        raise SecError(f"primary_doc.xml genInfo carries no usable seriesId ({series_id[:20]!r})")
    try:
        report_date = _date(_child_text(gen, "repPdDate"), "repPdDate")
    except ValueError as exc:
        raise SecError(f"{series_id}: {exc}") from None
    end_text = _child_text(gen, "repPdEnd")
    try:
        period_end = _date(end_text, "repPdEnd") if end_text else None
    except ValueError as exc:
        raise SecError(f"{series_id}: {exc}") from None

    holdings: list[NportHolding] = []
    reasons: list[str] = []
    for element in root.iter():
        if _local(element.tag) != "invstOrSec":
            continue
        try:
            holdings.append(_holding(element))
        except ValueError as exc:
            reasons.append(str(exc))
    return NportDocument(
        series_id=series_id,
        series_name=_child_text(gen, "seriesName") or "",
        report_date=report_date,
        period_end=period_end,
        holdings=tuple(holdings),
        skipped=len(reasons),
        skipped_reasons=tuple(reasons),
    )


def select_sector_documents(
    series_by_ticker: Mapping[str, str],
    documents: Iterable[tuple[NportFiling, NportDocument]],
    *,
    filings: NportFilings,
) -> SectorSelection:
    """Match each sector ticker to its quarter's original NPORT-P, by seriesId.

    ``filings`` is the quarter's index (:meth:`SecProvider.latest_nport_filings`)
    and is required: every document's filing must be one of its rows, or
    :class:`SecError` -- a document from another quarter or registrant is
    not this quarter's holdings. The index is also what
    :attr:`SectorSelection.amended` is derived from, so an NPORT-P/A the
    caller did not fetch is still reported (see :class:`SectorSelection`).

    Each ticker's seriesId must have **exactly one** original filing among
    ``documents``, whose document's ``repPdDate`` equals the filing's
    ``reportDate``; otherwise :class:`SecError`. Which position a filing
    held in the list is irrelevant by construction. Documents for any other
    series are excluded and named; NPORT-P/A documents are never selected.
    """
    indexed = set(filings.filings)
    by_series: dict[str, list[tuple[NportFiling, NportDocument]]] = {}
    amendments: dict[str, list[NportFiling]] = {}
    fetched_amendments: set[NportFiling] = set()
    for filing, document in documents:
        if filing not in indexed:
            raise SecError(
                f"{filing.form} {filing.accession} ({filing.report_date.isoformat()}) is not "
                f"in CIK{filings.cik}'s {filings.report_date.isoformat()} filing index"
            )
        if filing.is_amendment:
            amendments.setdefault(document.series_id, []).append(filing)
            fetched_amendments.add(filing)
        else:
            by_series.setdefault(document.series_id, []).append((filing, document))
    unattributed = tuple(f for f in filings.amendments if f not in fetched_amendments)
    funds: dict[str, SectorFund] = {}
    for ticker, series_id in series_by_ticker.items():
        candidates = by_series.get(series_id, [])
        if len(candidates) != 1:
            raise SecError(
                f"{ticker} ({series_id}) has {len(candidates)} original {NPORT_FORM} "
                "documents in this quarter; expected exactly 1"
            )
        filing, document = candidates[0]
        if document.report_date != filing.report_date:
            raise SecError(
                f"{ticker} ({series_id}): {filing.accession} is listed for "
                f"{filing.report_date.isoformat()} but its document reports "
                f"{document.report_date.isoformat()}"
            )
        funds[ticker] = SectorFund(ticker, series_id, filing, document)
    sector_series = set(series_by_ticker.values())
    attributed = sorted(t for t, s in series_by_ticker.items() if s in amendments)
    # An amendment nobody fetched could be any sector fund's: all are flagged.
    amended = sorted(series_by_ticker) if unattributed else attributed
    return SectorSelection(
        funds=funds,
        excluded_series=tuple(sorted(set(by_series) - sector_series)),
        amended=tuple(amended),
        amendments={t: tuple(amendments[series_by_ticker[t]]) for t in attributed},
        unattributed_amendments=unattributed,
    )


# ------------------------------------------------------------------ secrets


def _identity_pieces(user_agent: str) -> tuple[str, ...]:
    """The UA, collapsed, and its name / email / local part / domain, longest first."""
    found = [user_agent, " ".join(user_agent.split())]
    for raw in user_agent.split():
        token = raw.strip("<>()[]{},;:\"'")
        if "@" in token:
            local, domain = token.rsplit("@", 1)
            found += [token, local, domain]
            name = user_agent[: user_agent.find(raw)].strip().rstrip("<([{,;:\"'").strip()
            if name:
                found.append(name)
    unique = dict.fromkeys(p for p in found if len(p) >= _MIN_SECRET_LENGTH)
    return tuple(sorted(unique, key=len, reverse=True))


def _secret_forms(secret: str) -> tuple[str, ...]:
    """Literal, percent- and plus-encoded, and XML-escaped spellings."""
    xml_min = escape(secret, quote=False)
    return tuple(
        dict.fromkeys(
            (
                secret,
                quote(secret, safe=""),
                quote_plus(secret),
                escape(secret),
                xml_min,
                xml_min.replace("'", "&apos;").replace('"', "&quot;"),
                xml_min.replace("'", "&#39;").replace('"', "&#34;"),
            )
        )
    )


def _is_declarable(user_agent: str) -> bool:
    """Printable ASCII only: an HTTP header value httpx can encode.

    Anything else would fail inside httpx with an encoding error whose
    ``object`` is the UA itself -- refused here, before a request exists.
    """
    return all(" " <= ch <= "~" for ch in user_agent)


# ------------------------------------------------------------------ provider


class SecProvider:
    """SEC EDGAR N-PORT documents over HTTPS, metered by the shared limiter.

    Construct with :meth:`from_env` in production. The explicit constructor
    exists so tests can inject an ``httpx.MockTransport`` client and make no
    live call at all.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        client: httpx.AsyncClient | None = None,
        limiter: HostRateLimiter | None = None,
    ) -> None:
        agent = user_agent.strip()
        if not agent:
            raise ValueError(f"{SEC_USER_AGENT_ENV} is blank; SEC requires a declared identity")
        if not _is_declarable(agent):
            raise ValueError(
                f"{SEC_USER_AGENT_ENV} holds a character that is not printable ASCII; "
                "an HTTP header cannot carry it (the value is not shown)"
            )
        self._user_agent = agent
        self._patterns = tuple(
            re.compile(re.escape(form), re.IGNORECASE)
            for piece in _identity_pieces(agent)
            for form in _secret_forms(piece)
        )
        self._client = client if client is not None else httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None
        self._limiter = limiter if limiter is not None else default_limiter()

    def __repr__(self) -> str:
        return "SecProvider(user_agent=<hidden>)"

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, **kwargs: Any
    ) -> "SecProvider | None":
        """Build from the environment, or ``None`` -- logged -- if the UA is absent.

        The log line names the variable and never a value. A value that is
        present but unusable (non-ASCII) is also ``None``, logged the same way.
        """
        source: Mapping[str, str] = os.environ if env is None else env
        agent = (source.get(SEC_USER_AGENT_ENV) or "").strip()
        reason: str | None = None
        if not agent:
            reason = "is not set"
        elif not _is_declarable(agent):
            reason = "holds a character that is not printable ASCII"
        if reason is not None:
            logger.warning(
                "%s %s; the SEC N-PORT source is unavailable. SEC requires a declared "
                '"Name email" User-Agent and none is invented. The name is in .env.example.',
                SEC_USER_AGENT_ENV,
                reason,
                extra={
                    "event": "sec_user_agent_missing",
                    "rule": "SEC fair access requires a declared User-Agent; there is no default",
                    "variable": SEC_USER_AGENT_ENV,
                },
            )
            return None
        return cls(user_agent=agent, **kwargs)

    @property
    def limiter(self) -> HostRateLimiter:
        return self._limiter

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "SecProvider":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- HTTP

    def _scrub(self, text: str) -> str:
        """Third-party text, bounded and stripped of every spelling of the identity."""
        return vendor_detail(text, patterns=self._patterns)

    async def _get(self, url: str, label: str) -> httpx.Response:
        """One GET on either SEC host. Refusals raise; no status is retried.

        SEC bodies are never quoted into a message: an error page is HTML
        and says nothing a status code does not, and a page that reflected
        the request would reflect the identity.
        """
        await self._limiter.acquire(httpx.URL(url).host)
        response: httpx.Response | None = None
        failure = ""
        try:
            response = await self._client.get(
                url,
                headers={"User-Agent": self._user_agent, "Accept-Encoding": "gzip, deflate"},
            )
        except httpx.HTTPError as exc:
            # Keep only the scrubbed text. The SecError is raised *outside*
            # this block: raised in it, even ``from None``, it would still hold
            # the httpx exception on ``__context__`` -- and that exception's
            # ``.request.headers`` carries the User-Agent.
            failure = f"GET {label} failed ({type(exc).__name__}): {self._scrub(str(exc))}"
        if response is None:
            raise SecError(failure)
        body_size = len(response.content)
        refused_page = SEC_BLOCK_MARKER in response.text.lower()
        if response.status_code == 403 or refused_page:
            raise SecAccessRefused(
                f"SEC refused {label}: HTTP {response.status_code}"
                f"{' (its undeclared-automated-tool page)' if refused_page else ''}, "
                f"{body_size} bytes, body not quoted. Not retried: check "
                f"{SEC_USER_AGENT_ENV} is a real \"Name email\" identity."
            )
        if response.status_code == 429:
            raise RateLimitedError(
                f"GET {label} returned 429 despite the local SEC budget; another "
                "process may be sharing this address"
            )
        if response.status_code != 200:
            raise SecError(
                f"GET {label} returned HTTP {response.status_code} "
                f"({body_size} bytes, body not quoted)"
            )
        return response

    async def _get_json(self, url: str, label: str) -> Any:
        response = await self._get(url, label)
        failure = ""
        try:
            return decode_json(response.text)
        except WireFormatError as exc:
            # Raised outside the block, as in _get, so nothing is chained.
            failure = f"GET {label}: {self._scrub(str(exc))}"
        raise SecError(failure)

    # ------------------------------------------------------------ endpoints

    async def sector_fund_series(
        self,
        *,
        cik: str | int = SPDR_SECTOR_TRUST_CIK,
        tickers: Sequence[str] = SPDR_SECTOR_TICKERS,
    ) -> dict[str, str]:
        """``ticker -> seriesId`` for the sector funds, from SEC's ticker map."""
        payload = await self._get_json(TICKERS_MF_URL, "company_tickers_mf.json")
        return parse_sector_series(payload, cik=cik, tickers=tickers)

    async def latest_nport_filings(
        self, cik: str | int = SPDR_SECTOR_TRUST_CIK
    ) -> NportFilings:
        """The latest quarter's NPORT-P rows, with its NPORT-P/A rows flagged."""
        cik10 = _cik10(cik)
        payload = await self._get_json(
            SUBMISSIONS_URL_TEMPLATE.format(cik10=cik10), f"submissions CIK{cik10}"
        )
        return parse_nport_filings(payload, cik=cik10)

    async def nport_holdings(
        self, accession: str, cik: str | int = SPDR_SECTOR_TRUST_CIK
    ) -> NportDocument:
        """One filing's ``primary_doc.xml``, parsed. Skipped rows are logged once."""
        if not _ACCESSION_RE.fullmatch(accession):
            raise ValueError(f"{accession!r} is not an accession number ##########-##-######")
        cik_int = int(_cik10(cik))
        url = ARCHIVE_URL_TEMPLATE.format(cik=cik_int, accession=accession.replace("-", ""))
        response = await self._get(url, f"primary_doc.xml {accession}")
        document = parse_nport_document(response.content)
        if document.skipped:
            sample = [self._scrub(r) for r in document.skipped_reasons[:_REASON_SAMPLE]]
            logger.warning(
                "sec n-port %s (%s): %d holding row(s) skipped: %s",
                accession,
                document.series_id,
                document.skipped,
                "; ".join(sample),
                extra={
                    "event": "sec_nport_rows_skipped",
                    "rule": "a holding row that cannot be read is skipped and counted, never guessed",
                    "accession": accession,
                    "series_id": document.series_id,
                    "count": document.skipped,
                    "sample": sample,
                },
            )
        return document
