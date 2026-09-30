"""``corollary.data.providers.sec`` -- N-PORT holdings for the SPDR sector seed.

No network: every response is served by an ``httpx.MockTransport``.

SYNTHETIC: every input here is synthetic -- the files under
``tests/fixtures/sec_synthetic/`` (see its README) and the bodies built in
the tests. The live recording (``tests/fixtures/record_sec.py``) has not run
yet, so these prove the provider against the recorder's *assumptions* about
SEC's shapes, not against SEC.
"""

import json
import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import httpx
import pytest

from corollary.data.providers.interface import RateLimitedError
from corollary.data.providers.sec import (
    SEC_USER_AGENT_ENV,
    SPDR_SECTOR_TICKERS,
    NportDocument,
    NportFiling,
    NportFilings,
    NportHolding,
    SecAccessRefused,
    SecError,
    SecProvider,
    parse_nport_document,
    parse_nport_filings,
    parse_sector_series,
    select_sector_documents,
)
from corollary.ratelimit import SEC_DATA_HOST, SEC_WWW_HOST, HostRateLimiter

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "sec_synthetic"

#: SYNTHETIC identity. Spaces, an apostrophe and ``+`` on purpose: each has
#: an escaped spelling (``%27``/``&apos;``/``&#39;``, ``%2B``, ``%20``/``+``)
#: that a literal-only redactor would miss.
USER_AGENT = "O'Brien Test+Agent o'brien+sec@example.com"
IDENTITY_PIECES = (
    USER_AGENT,
    "O'Brien Test+Agent",
    "o'brien+sec@example.com",
    "o'brien+sec",
    "example.com",
)

QUARTER = date(2026, 6, 30)
XLK_SERIES = "S000006415"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


SERIES_MAP = fixture_json("accession_series.synthetic.json")


def all_forms(secret: str) -> list[str]:
    xml_min = escape(secret, quote=False)
    return [
        secret,
        quote(secret, safe=""),
        quote_plus(secret),
        escape(secret),
        xml_min.replace("'", "&apos;"),
        xml_min.replace("'", "&#39;"),
    ]


def assert_no_identity(text: str) -> None:
    lowered = text.lower()
    for piece in IDENTITY_PIECES:
        for form in all_forms(piece):
            assert form.lower() not in lowered, f"{form!r} leaked into {text!r}"


async def _never_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError(f"a test waited {seconds}s on the limiter")


def generous_limiter() -> HostRateLimiter:
    return HostRateLimiter(
        requests_per_minute=10_000, per_host={}, clock=lambda: 0.0, sleep=_never_sleep
    )


def document_for(accession: str) -> str:
    """The synthetic primary_doc.xml served for one synthetic accession."""
    series_id = SERIES_MAP["accessions"][accession]
    if series_id == XLK_SERIES and accession != "0001752724-26-000900":
        return fixture_text("nport_XLK.synthetic.xml")
    premium = {v: k for k, v in SERIES_MAP["premium"].items()}
    name = (
        f"{premium[series_id]} Premium Income Fund (SYNTHETIC)"
        if series_id in premium
        else f"{series_id} Select Sector SPDR Fund (SYNTHETIC)"
    )
    return (
        fixture_text("nport_template.synthetic.xml")
        .replace("@@SERIES_ID@@", series_id)
        .replace("@@SERIES_NAME@@", name)
        .replace("@@REP_PD_DATE@@", QUARTER.isoformat())
    )


def sec_route(request: httpx.Request) -> httpx.Response:
    """SEC as the recorder assumes it: three document kinds on two hosts."""
    url = request.url
    if url.host == SEC_WWW_HOST and url.path == "/files/company_tickers_mf.json":
        return httpx.Response(200, text=fixture_text("company_tickers_mf.synthetic.json"))
    if url.host == SEC_DATA_HOST and url.path == "/submissions/CIK0001064641.json":
        return httpx.Response(200, text=fixture_text("submissions_CIK0001064641.synthetic.json"))
    prefix = "/Archives/edgar/data/1064641/"
    if url.host == SEC_WWW_HOST and url.path.startswith(prefix) and url.path.endswith("/primary_doc.xml"):
        digits = url.path[len(prefix):].split("/")[0]
        accession = f"{digits[:10]}-{digits[10:12]}-{digits[12:]}"
        return httpx.Response(200, content=document_for(accession).encode("utf-8"))
    raise AssertionError(f"unrouted {request.method} {url}")


class Recorder:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make(
    handler: Callable[[httpx.Request], httpx.Response] = sec_route,
    *,
    limiter: HostRateLimiter | None = None,
    user_agent: str = USER_AGENT,
) -> tuple[SecProvider, Recorder]:
    recorder = Recorder(handler)
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    provider = SecProvider(
        user_agent=user_agent, client=client, limiter=limiter or generous_limiter()
    )
    return provider, recorder


# ------------------------------------------------------------ ticker map


async def test_the_sector_series_come_from_the_trusts_rows_by_ticker() -> None:
    provider, recorder = make()
    series = await provider.sector_fund_series()

    assert set(series) == set(SPDR_SECTOR_TICKERS)
    assert series == SERIES_MAP["sector"]
    # Another registrant's XLK row is listed first in the map; it is not ours.
    assert series["XLK"] == XLK_SERIES
    assert not set(series.values()) & set(SERIES_MAP["premium"].values())
    (request,) = recorder.requests
    assert (request.method, request.url.host) == ("GET", SEC_WWW_HOST)
    assert request.headers["User-Agent"] == USER_AGENT


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda rows: [r for r in rows if r[3] != "XLU"], "0 rows for XLU"),
        (lambda rows: rows + [[1064641, "S000006499", "C1", "XLU"]], "2 rows for XLU"),
        (lambda rows: [[*r[:1], "not-a-series", *r[2:]] if r[3] == "XLB" else r for r in rows], "XLB"),
    ],
)
async def test_the_ticker_map_is_refused_rather_than_read_partially(mutate, match) -> None:
    payload = fixture_json("company_tickers_mf.synthetic.json")
    payload["data"] = mutate(payload["data"])
    with pytest.raises(SecError, match=match):
        parse_sector_series(payload, cik="0001064641", tickers=SPDR_SECTOR_TICKERS)


async def test_a_ticker_map_without_the_named_fields_is_refused() -> None:
    with pytest.raises(SecError, match="fields"):
        parse_sector_series(
            {"fields": ["cik", "symbol"], "data": []}, cik=1064641, tickers=("XLK",)
        )


# ------------------------------------------------------------ submissions


async def test_the_latest_quarters_nport_rows_with_the_amendment_flagged(caplog) -> None:
    provider, recorder = make()
    with caplog.at_level(logging.WARNING, logger="corollary.data.providers.sec"):
        filings = await provider.latest_nport_filings()

    (request,) = recorder.requests
    assert request.url.host == SEC_DATA_HOST
    assert request.url.path == "/submissions/CIK0001064641.json"
    assert filings.report_date == QUARTER
    assert len(filings.originals) == 22
    assert [f.accession for f in filings.amendments] == ["0001752724-26-000900"]
    assert filings.amendments[0].is_amendment
    assert all(f.report_date == QUARTER for f in filings.filings)
    assert [f.accession for f in filings.filings] == sorted(f.accession for f in filings.filings)
    # The older quarter, the other forms and the unreadable row are all out.
    assert "0001752724-26-000500" not in {f.accession for f in filings.filings}
    assert filings.skipped == 1
    assert [r for r in caplog.records if getattr(r, "event", "") == "sec_submissions_row_skipped"]


async def test_submissions_whose_columns_disagree_are_refused() -> None:
    payload = fixture_json("submissions_CIK0001064641.synthetic.json")
    payload["filings"]["recent"]["reportDate"].pop()
    with pytest.raises(SecError, match="length"):
        parse_nport_filings(payload, cik=1064641)


async def test_submissions_without_any_nport_are_refused() -> None:
    payload = {"filings": {"recent": {c: [] for c in (
        "accessionNumber", "filingDate", "reportDate", "form", "primaryDocument")}}}
    with pytest.raises(SecError, match="no readable NPORT-P"):
        parse_nport_filings(payload, cik=1064641)


# ------------------------------------------------------------ primary_doc.xml


async def test_the_document_is_parsed_with_exact_decimals_and_every_category(caplog) -> None:
    provider, recorder = make()
    accession = next(a for a, s in SERIES_MAP["accessions"].items()
                     if s == XLK_SERIES and a != "0001752724-26-000900")
    with caplog.at_level(logging.WARNING, logger="corollary.data.providers.sec"):
        doc = await provider.nport_holdings(accession)

    (request,) = recorder.requests
    assert request.url.host == SEC_WWW_HOST
    assert request.url.path == (
        f"/Archives/edgar/data/1064641/{accession.replace('-', '')}/primary_doc.xml"
    )
    assert (doc.series_id, doc.report_date, doc.period_end) == (
        XLK_SERIES, QUARTER, date(2026, 9, 30)
    )
    nvda = doc.holdings[0]
    assert nvda == NportHolding(
        name="NVIDIA Corp",
        cusip="67066G104",
        isin="US67066G1040",
        pct_val=Decimal("15.01234567890123456789"),
        asset_cat="EC",
    )
    # More digits than a double carries: the text went to Decimal directly.
    assert str(nvda.pct_val) == "15.01234567890123456789"
    assert all(type(h.pct_val) is Decimal for h in doc.holdings)
    assert doc.holdings[2].name == "AT&T Inc"
    assert {h.asset_cat for h in doc.holdings} == {"EC", "STIV", "OTHER"}
    assert len(doc.holdings) == 7


async def test_only_common_equity_is_in_the_equity_subset() -> None:
    doc = parse_nport_document(fixture_text("nport_XLK.synthetic.xml"))
    names = [h.name for h in doc.equity]
    assert names == [
        "NVIDIA Corp",
        "Apple Inc",
        "AT&T Inc",
        "Synthetic Holding With No CUSIP",
        "Synthetic Holding With Zero CUSIP",
    ]
    assert "Synthetic Government Money Market Fund" not in names
    assert "Synthetic Cash Collateral" not in names


async def test_absent_identifiers_are_none_not_guessed() -> None:
    doc = parse_nport_document(fixture_text("nport_XLK.synthetic.xml"))
    by_name = {h.name: h for h in doc.holdings}
    no_cusip = by_name["Synthetic Holding With No CUSIP"]
    assert (no_cusip.cusip, no_cusip.isin) == (None, "US9999999992")
    zero = by_name["Synthetic Holding With Zero CUSIP"]
    assert (zero.cusip, zero.isin) == (None, None)


async def test_malformed_rows_are_skipped_and_counted_never_failing_the_document(caplog) -> None:
    doc = parse_nport_document(fixture_text("nport_XLK.synthetic.xml"))
    assert doc.skipped == 4
    reasons = " | ".join(doc.skipped_reasons)
    for fragment in ("is not a number", "no pctVal", "is not finite", "no assetCat"):
        assert fragment in reasons

    provider, _ = make()
    accession = next(a for a, s in SERIES_MAP["accessions"].items()
                     if s == XLK_SERIES and a != "0001752724-26-000900")
    with caplog.at_level(logging.WARNING, logger="corollary.data.providers.sec"):
        await provider.nport_holdings(accession)
    skipped = [r for r in caplog.records if getattr(r, "event", "") == "sec_nport_rows_skipped"]
    assert len(skipped) == 1 and skipped[0].count == 4


@pytest.mark.parametrize(
    ("replace", "match"),
    [
        (("<seriesId>S000006415</seriesId>", ""), "seriesId"),
        (("<repPdDate>2026-06-30</repPdDate>", "<repPdDate>June</repPdDate>"), "repPdDate"),
        (("<genInfo>", "<genInfoX>"), "genInfo"),
    ],
)
async def test_a_document_without_its_header_fails_as_a_whole(replace, match) -> None:
    text = fixture_text("nport_XLK.synthetic.xml")
    if replace[0] == "<genInfo>":
        text = text.replace("<genInfo>", "<genInfoX>").replace("</genInfo>", "</genInfoX>")
    else:
        text = text.replace(*replace)
    with pytest.raises(SecError, match=match):
        parse_nport_document(text)


#: SYNTHETIC: an entity-substitution document. N-PORT declares no DTD. Were the
#: entity expanded, the header would read as a complete, valid document.
ENTITY_DOCUMENT = (
    '<!DOCTYPE x [<!ENTITY a "PWNED">]>'
    "<edgarSubmission><genInfo><seriesName>&a;</seriesName><seriesId>S000006415</seriesId>"
    "<repPdDate>2026-06-30</repPdDate></genInfo></edgarSubmission>"
)


@pytest.mark.parametrize(
    "encoded",
    [
        ('<?xml version="1.0"?>' + ENTITY_DOCUMENT).encode("utf-8"),
        # UTF-16 (BOM + two bytes a character): no raw-bytes scan finds "<!doctype" here.
        ('<?xml version="1.0" encoding="UTF-16"?>' + ENTITY_DOCUMENT).encode("utf-16"),
        ('<?xml version="1.0" encoding="UTF-16LE"?>' + ENTITY_DOCUMENT).encode("utf-16-le"),
        ('<?xml version="1.0"?>' + ENTITY_DOCUMENT).encode("utf-8-sig"),
        # A DTD with no internal subset and no entity is refused too.
        b'<?xml version="1.0"?><!DOCTYPE edgarSubmission SYSTEM "http://example.invalid/x.dtd">'
        b"<edgarSubmission/>",
    ],
    ids=["utf-8", "utf-16", "utf-16-le", "utf-8-bom", "external-dtd"],
)
async def test_a_dtd_or_entity_is_refused_by_the_parser_in_any_encoding(encoded) -> None:
    with pytest.raises(SecError, match="DTD") as caught:
        parse_nport_document(encoded)
    assert "PWNED" not in str(caught.value)


async def test_a_utf16_document_without_a_dtd_still_parses() -> None:
    # The refusal is not a refusal of UTF-16: the XLK fixture re-encoded reads the same.
    text = fixture_text("nport_XLK.synthetic.xml").replace(
        'encoding="UTF-8"', 'encoding="UTF-16"', 1
    )
    assert 'encoding="UTF-16"' in text
    assert parse_nport_document(text.encode("utf-16")) == parse_nport_document(
        fixture_text("nport_XLK.synthetic.xml")
    )


async def test_an_unparseable_document_raises() -> None:
    with pytest.raises(SecError, match="does not parse") as caught:
        parse_nport_document("<edgarSubmission><genInfo>")
    assert caught.value.__context__ is None and caught.value.__cause__ is None


def one_holding(cusip: str) -> str:
    """SYNTHETIC one-holding document for the identifier-shape cases."""
    return (
        "<edgarSubmission><genInfo><seriesId>S000006415</seriesId>"
        "<repPdDate>2026-06-30</repPdDate></genInfo><invstOrSec><name>X Corp</name>"
        f"<cusip>{cusip}</cusip><pctVal>1.5</pctVal><assetCat>EC</assetCat>"
        "</invstOrSec></edgarSubmission>"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("67066G104", "67066G104"),
        ("037833100", "037833100"),
        ("67066g104", "67066G104"),
        ("67066G10X", None),  # the check character is always a digit
        ("ABCDEFGHI", None),
        ("67066G10\u0664", None),  # an Arabic-Indic digit is not a check digit
        ("000000000", None),
        ("N/A", None),
    ],
)
async def test_a_cusip_must_end_in_a_check_digit(raw, expected) -> None:
    (holding,) = parse_nport_document(one_holding(raw)).holdings
    assert holding.cusip == expected


@pytest.mark.parametrize("accession", ["", "0001752724-26-00001", "0001752724/26/000001", "../x"])
async def test_a_malformed_accession_is_refused_before_any_request(accession) -> None:
    provider, recorder = make()
    with pytest.raises(ValueError):
        await provider.nport_holdings(accession)
    assert recorder.requests == []


async def test_a_malformed_cik_is_refused_before_any_request() -> None:
    provider, recorder = make()
    with pytest.raises(ValueError):
        await provider.latest_nport_filings("CIK1064641")
    assert recorder.requests == []


# ------------------------------------------------------------ series selection


async def _quarter(order: Callable[[list[NportFiling]], list[NportFiling]] = list):
    provider, _ = make()
    series = await provider.sector_fund_series()
    filings = await provider.latest_nport_filings()
    docs: list[tuple[NportFiling, NportDocument]] = []
    for filing in order(list(filings.filings)):
        docs.append((filing, await provider.nport_holdings(filing.accession)))
    return series, docs, filings


async def test_the_eleven_are_selected_among_twenty_two_by_series_id() -> None:
    series, docs, filings = await _quarter()
    assert len([d for d in docs if not d[0].is_amendment]) == 22
    # The decoy -- a Premium Income filing -- is the first filing and sorts first by seriesId.
    assert docs[0][1].series_id == "S000000001"
    assert "Premium Income" in docs[0][1].series_name

    selection = select_sector_documents(series, docs, filings=filings)

    assert set(selection.funds) == set(SPDR_SECTOR_TICKERS)
    for ticker, fund in selection.funds.items():
        assert fund.document.series_id == series[ticker] == fund.series_id
        assert not fund.filing.is_amendment
        assert SERIES_MAP["accessions"][fund.filing.accession] == series[ticker]
    assert selection.funds["XLK"].document.series_name == "The Technology Select Sector SPDR Fund"
    assert selection.excluded_series == tuple(sorted(SERIES_MAP["premium"].values()))
    assert selection.amended == ("XLE",)
    assert [f.accession for f in selection.amendments["XLE"]] == ["0001752724-26-000900"]


async def test_the_selection_does_not_depend_on_list_order() -> None:
    series, forward, filings = await _quarter()
    _, backward, _ = await _quarter(lambda f: list(reversed(f)))
    a = select_sector_documents(series, forward, filings=filings)
    b = select_sector_documents(series, backward, filings=filings)
    assert {t: f.filing.accession for t, f in a.funds.items()} == {
        t: f.filing.accession for t, f in b.funds.items()
    }
    assert (a.excluded_series, a.amended) == (b.excluded_series, b.amended)


async def test_a_missing_or_doubled_sector_series_is_refused() -> None:
    series, docs, filings = await _quarter()
    without_xlu = [d for d in docs if d[1].series_id != series["XLU"]]
    with pytest.raises(SecError, match="XLU .* 0 original"):
        select_sector_documents(series, without_xlu, filings=filings)
    xlu = next(d for d in docs if d[1].series_id == series["XLU"])
    with pytest.raises(SecError, match="XLU .* 2 original"):
        select_sector_documents(series, [*docs, xlu], filings=filings)


async def test_a_document_reporting_another_date_than_its_filing_is_refused() -> None:
    series, docs, filings = await _quarter()
    filing, doc = next(d for d in docs if d[1].series_id == XLK_SERIES)
    moved = NportFiling(
        filing.accession, filing.form, filing.filing_date, date(2026, 3, 31), filing.primary_document
    )
    swapped = [(moved, doc) if f.accession == filing.accession else (f, d) for f, d in docs]
    index = NportFilings(
        filings.cik,
        filings.report_date,
        tuple(moved if f.accession == filing.accession else f for f in filings.filings),
    )
    with pytest.raises(SecError, match="XLK"):
        select_sector_documents(series, swapped, filings=index)


async def test_a_document_whose_filing_is_not_in_the_index_is_refused() -> None:
    series, docs, filings = await _quarter()
    filing, doc = next(d for d in docs if d[1].series_id == XLK_SERIES)
    moved = NportFiling(
        filing.accession, filing.form, filing.filing_date, date(2026, 3, 31), filing.primary_document
    )
    swapped = [(moved, doc) if f.accession == filing.accession else (f, d) for f, d in docs]
    with pytest.raises(SecError, match="not in CIK0001064641's 2026-06-30 filing index"):
        select_sector_documents(series, swapped, filings=filings)


async def test_an_amendment_in_the_index_that_was_not_fetched_still_flags_the_funds() -> None:
    # The caller fetched only the originals. The index lists an NPORT-P/A and
    # does not say which series it is for, so no sector fund can be cleared.
    series, docs, filings = await _quarter()
    originals_only = [d for d in docs if not d[0].is_amendment]
    assert len(originals_only) == len(docs) - 1

    selection = select_sector_documents(series, originals_only, filings=filings)

    assert selection.amended == tuple(sorted(SPDR_SECTOR_TICKERS))
    assert [f.accession for f in selection.unattributed_amendments] == ["0001752724-26-000900"]
    assert selection.amendments == {}
    assert set(selection.funds) == set(SPDR_SECTOR_TICKERS)
    assert all(not f.filing.is_amendment for f in selection.funds.values())


async def test_a_fetched_amendment_is_attributed_to_its_fund_alone() -> None:
    series, docs, filings = await _quarter()
    selection = select_sector_documents(series, docs, filings=filings)
    assert selection.amended == ("XLE",)
    assert selection.unattributed_amendments == ()


async def test_an_index_without_amendments_flags_nothing() -> None:
    series, docs, filings = await _quarter()
    originals = [d for d in docs if not d[0].is_amendment]
    index = NportFilings(filings.cik, filings.report_date, filings.originals)
    selection = select_sector_documents(series, originals, filings=index)
    assert selection.amended == () and selection.unattributed_amendments == ()


# ------------------------------------------------------------ the declared identity


async def test_no_user_agent_means_no_provider_and_one_line_naming_the_variable(caplog) -> None:
    for env in ({}, {SEC_USER_AGENT_ENV: ""}, {SEC_USER_AGENT_ENV: "   "}):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="corollary.data.providers.sec"):
            assert SecProvider.from_env(env) is None
        (record,) = caplog.records
        assert SEC_USER_AGENT_ENV in record.getMessage()
        assert record.event == "sec_user_agent_missing"


async def test_an_undeclarable_user_agent_is_refused_without_echoing_it(caplog) -> None:
    # SYNTHETIC: a non-ASCII identity an HTTP header cannot carry.
    value = "Zoë Tester zoe.tester@example.com"
    with caplog.at_level(logging.WARNING, logger="corollary.data.providers.sec"):
        assert SecProvider.from_env({SEC_USER_AGENT_ENV: value}) is None
    (record,) = caplog.records
    assert "zoe.tester" not in record.getMessage().lower()
    assert "Zoë" not in record.getMessage()
    with pytest.raises(ValueError) as caught:
        SecProvider(user_agent=value)
    assert "zoe.tester" not in str(caught.value)


async def test_there_is_no_default_user_agent() -> None:
    with pytest.raises(ValueError, match=SEC_USER_AGENT_ENV):
        SecProvider(user_agent="  ")


async def test_the_declared_user_agent_is_sent_verbatim_and_hidden_from_repr() -> None:
    provider = SecProvider.from_env(
        {SEC_USER_AGENT_ENV: f"  {USER_AGENT} "},
        client=httpx.AsyncClient(transport=httpx.MockTransport(sec_route)),
        limiter=generous_limiter(),
    )
    assert provider is not None
    assert_no_identity(repr(provider))
    captured: list[httpx.Request] = []

    def spy(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return sec_route(request)

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(spy))
    await provider.sector_fund_series()
    assert captured[0].headers["User-Agent"] == USER_AGENT


async def test_a_transport_error_quoting_the_identity_is_scrubbed_in_every_spelling() -> None:
    # SYNTHETIC: a transport failure whose text carries every spelling of the
    # identity -- literal, upper-cased, percent- and plus-encoded, XML-escaped.
    leaked = " ".join(
        form if i % 2 else form.upper()
        for piece in IDENTITY_PIECES
        for i, form in enumerate(all_forms(piece))
    )

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connect failed: {leaked}", request=request)

    provider, _ = make(boom)
    with pytest.raises(SecError) as caught:
        await provider.sector_fund_series()
    assert_no_identity(str(caught.value))
    # Neither link holds the httpx exception, whose request headers carry the UA.
    # (`from None` would pass a __cause__ check and still keep it on __context__.)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_nothing_logged_over_a_whole_quarter_carries_the_identity(caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        series, docs, filings = await _quarter()
        select_sector_documents(series, docs, filings=filings)
    for record in caplog.records:
        assert_no_identity(record.getMessage())
        assert_no_identity(repr(record.__dict__))


# ------------------------------------------------------------ refusals


def reply(status: int, body: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, text=body)


#: SYNTHETIC: the gist of SEC's block page, echoing the declared identity back
#: so a test can prove the body is never quoted.
BLOCK_PAGE = (
    "<html><body><h1>Your Request Originates from an Undeclared Automated Tool</h1>"
    f"<p>User-Agent: {USER_AGENT}</p></body></html>"
)


@pytest.mark.parametrize(("status", "body"), [(403, "<html>Forbidden</html>"), (403, BLOCK_PAGE), (200, BLOCK_PAGE)])
async def test_a_refusal_raises_its_own_type_once_and_is_never_retried(status, body) -> None:
    provider, recorder = make(reply(status, body))
    with pytest.raises(SecAccessRefused) as caught:
        await provider.latest_nport_filings()
    assert len(recorder.requests) == 1
    assert_no_identity(str(caught.value))
    assert "Undeclared" not in str(caught.value)


async def test_other_failures_are_errors_but_not_refusals() -> None:
    provider, _ = make(reply(503, f"down {USER_AGENT}"))
    with pytest.raises(SecError) as caught:
        await provider.latest_nport_filings()
    assert not isinstance(caught.value, SecAccessRefused)
    assert "503" in str(caught.value)
    assert_no_identity(str(caught.value))

    provider, _ = make(reply(429, "slow down"))
    with pytest.raises(RateLimitedError):
        await provider.latest_nport_filings()


async def test_a_200_that_is_not_json_raises() -> None:
    provider, _ = make(reply(200, "<html>maintenance</html>"))
    with pytest.raises(SecError) as caught:
        await provider.sector_fund_series()
    assert caught.value.__context__ is None and caught.value.__cause__ is None


# ------------------------------------------------------------ the shared bucket


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


async def test_requests_to_both_sec_hosts_are_paced_by_one_bucket() -> None:
    """Four per second across data. and www. together; the fifth waits."""
    clock = FakeClock()
    provider, recorder = make(limiter=HostRateLimiter(clock=clock, sleep=clock.sleep))
    await provider.sector_fund_series()          # www
    filings = await provider.latest_nport_filings()  # data
    await provider.nport_holdings(filings.filings[0].accession)  # www
    await provider.nport_holdings(filings.filings[1].accession)  # www
    assert clock.slept == []
    await provider.nport_holdings(filings.filings[2].accession)  # www, fifth
    assert clock.slept == [pytest.approx(0.25)]
    assert {r.url.host for r in recorder.requests} == {SEC_DATA_HOST, SEC_WWW_HOST}


# ------------------------------------------------------------ the real recording
#
# tests/fixtures/sec/ is SEC as served on 2026-09-28 (ce8696a): the trust's 22
# NPORT-P rows for 2026-06-30, the eleven sector funds' primary_doc.xml byte for
# byte, and one Premium Income header. The ten Premium Income documents nobody
# recorded are served that one header -- same trust, same quarter, another series.

REAL = Path(__file__).resolve().parents[2] / "fixtures" / "sec"
REAL_SERIES = {
    "XLB": "S000006414", "XLC": "S000062095", "XLE": "S000006410", "XLF": "S000006411",
    "XLI": "S000006413", "XLK": "S000006415", "XLP": "S000006409", "XLRE": "S000051152",
    "XLU": "S000006416", "XLV": "S000006412", "XLY": "S000006408",
}
REAL_ACCESSIONS = {
    json.loads((REAL / f"nport_{t}_primary_doc.meta.json").read_text("utf-8"))["accession"]: t
    for t in REAL_SERIES
}


def real_route(request: httpx.Request) -> httpx.Response:
    url = request.url
    if url.host == SEC_WWW_HOST and url.path == "/files/company_tickers_mf.json":
        return httpx.Response(200, text=(REAL / "company_tickers_mf.trimmed.json").read_text("utf-8"))
    if url.host == SEC_DATA_HOST and url.path == "/submissions/CIK0001064641.json":
        return httpx.Response(200, text=(REAL / "CIK0001064641.trimmed.json").read_text("utf-8"))
    prefix = "/Archives/edgar/data/1064641/"
    if url.host == SEC_WWW_HOST and url.path.startswith(prefix):
        digits = url.path[len(prefix):].split("/")[0]
        ticker = REAL_ACCESSIONS.get(f"{digits[:10]}-{digits[10:12]}-{digits[12:]}")
        name = (
            f"nport_{ticker}_primary_doc.xml" if ticker
            else "nport_excluded_S000093831_header.xml"
        )
        return httpx.Response(200, content=(REAL / name).read_bytes())
    raise AssertionError(f"unrouted {request.method} {url}")


async def test_real_the_eleven_are_selected_among_the_trusts_twenty_two() -> None:
    provider, recorder = make(real_route)
    series = await provider.sector_fund_series()
    filings = await provider.latest_nport_filings()
    assert series == REAL_SERIES
    assert filings.report_date == QUARTER
    assert len(filings.originals) == 22 and filings.amendments == ()
    docs = [(f, await provider.nport_holdings(f.accession)) for f in filings.filings]
    selection = select_sector_documents(series, docs, filings=filings)

    assert {t: f.series_id for t, f in selection.funds.items()} == REAL_SERIES
    assert {f.filing.accession: t for t, f in selection.funds.items()} == REAL_ACCESSIONS
    assert all(f.filing.filing_date == date(2026, 8, 28) for f in selection.funds.values())
    assert selection.excluded_series == ("S000093831",)
    assert selection.amended == () and selection.unattributed_amendments == ()
    archive = [r for r in recorder.requests if "/Archives/" in r.url.path]
    assert len(archive) == 22


@pytest.mark.parametrize(
    ("ticker", "holdings", "equity", "categories"),
    [("XLK", 79, 74, {"DE", "EC", "STIV"}), ("XLU", 36, 31, {"DE", "EC", "STIV"}), ("XLE", 21, 21, {"EC"})],
)
async def test_real_only_ec_lines_are_equity(
    ticker: str, holdings: int, equity: int, categories: set[str]
) -> None:
    document = parse_nport_document((REAL / f"nport_{ticker}_primary_doc.xml").read_bytes())
    assert document.series_id == REAL_SERIES[ticker]
    assert document.report_date == QUARTER and document.skipped == 0
    assert len(document.holdings) == holdings
    assert {h.asset_cat for h in document.holdings} == categories
    assert len(document.equity) == equity
    assert all(h.asset_cat == "EC" for h in document.equity)


async def test_real_ec_lines_without_a_cusip_are_isin_only_issuers_not_cash() -> None:
    """The 29 are foreign-domiciled members with ``000000000`` and a real ISIN."""
    no_cusip: dict[str, list[str]] = {}
    for ticker in REAL_SERIES:
        raw = (REAL / f"nport_{ticker}_primary_doc.xml").read_bytes()
        for h in parse_nport_document(raw).equity:
            if h.cusip is None:
                assert h.isin is not None and h.isin[:2] != "US", h
                assert h.pct_val > 0
                no_cusip.setdefault(ticker, []).append(h.name)
    assert {t: len(v) for t, v in no_cusip.items()} == {
        "XLB": 5, "XLF": 6, "XLI": 5, "XLK": 5, "XLP": 1, "XLV": 2, "XLY": 5
    }
    assert "Linde PLC" in no_cusip["XLB"] and "Chubb Ltd" in no_cusip["XLF"]
