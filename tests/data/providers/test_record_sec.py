"""The SEC N-PORT recorder's secret handling and refusals, offline.

``tests/fixtures/record_sec.py`` sends the owner's declared identity as its
User-Agent and holds the Alpaca paper keys for the CUSIP lookups. These tests
prove, with no network, that neither can reach a fixture or stdout, that a
refusal from SEC or an error from Alpaca writes nothing, that a committed
recording is never silently replaced or merged, and -- through one complete
``MockTransport`` recording -- that funds are picked by seriesId and the
pacing is what the constants say. Every value below is invented.

The secrets carry ``/``, ``+``, ``=``, ``&`` and an apostrophe, so each
escaped spelling differs from the literal one; a test of an escaped form that
would still pass with escaping removed proves nothing.
"""

import dataclasses
import html
import importlib.util
import json
import random
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable
from urllib.parse import quote, quote_plus

import httpx
import pytest

from tests.data.news.test_recorders_read_no_env_file import dotenv_uses

RECORDER_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "record_sec.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("record_sec_under_test", RECORDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rs = _load()

# ASCII, because it travels as an HTTP header in the run tests.
UA_NAME = "Zoe O'Brien-Test"
UA_LOCAL = "zoe+sec"
UA_DOMAIN = "invented-mail.example"
UA_EMAIL = f"{UA_LOCAL}@{UA_DOMAIN}"
UA = f"{UA_NAME} {UA_EMAIL}"
KEY_ID = "kid/Invented+Key=01&x9"
SECRET_KEY = "sk/invented+secret=value&for/tests"
ENV = {
    "SEC_USER_AGENT": UA,
    "ALPACA_PAPER_API_KEY": KEY_ID,
    "ALPACA_PAPER_SECRET_KEY": SECRET_KEY,
}
SECRETS = rs.secret_values(ENV)

# Pure-function tests only (never a header): an internationalised domain, so
# no escaped spelling of this UA contains its domain -- or any other piece of
# it -- as-is.
UA_IDN = "Zoë O'Brien-Test zoe+sec@bücher-invented.example"
SECRETS_IDN = rs.secret_values({**ENV, "SEC_USER_AGENT": UA_IDN})

ESCAPERS: dict[str, Callable[[str], str]] = {
    "quote": lambda s: quote(s, safe=""),
    "quote_plus": quote_plus,
    "xml": html.escape,
    "xml_apos": lambda s: html.escape(s, quote=False).replace("'", "&apos;"),
}
ESCAPED_CASES = [
    pytest.param(secret, name, id=f"{name}-{i}")
    for i, secret in enumerate(SECRETS_IDN)
    for name, escape in ESCAPERS.items()
    if escape(secret) != secret
]


def _run(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    env: dict[str, str] | None = None,
    argv: list[str] | None = None,
    sleep: Callable[[float], None] = lambda _seconds: None,
    clock: Callable[[], float] = lambda: 0.0,
) -> tuple[int, list[str]]:
    printed: list[str] = []
    code = rs.main(
        argv=argv or [],
        env=ENV if env is None else env,
        transport=httpx.MockTransport(handler),
        output_dir=tmp_path / "sec",
        sleep=sleep,
        clock=clock,
        out=printed.append,
    )
    return code, printed


# --- redaction -------------------------------------------------------------


def test_the_secrets_cover_every_piece_of_the_user_agent_and_both_key_halves() -> None:
    for piece in (UA, UA_NAME, UA_EMAIL, UA_LOCAL, UA_DOMAIN, KEY_ID, SECRET_KEY):
        assert piece in SECRETS, piece


def test_redaction_replaces_every_secret_in_a_body() -> None:
    body = f"<x>{UA}</x> key={KEY_ID} secret={SECRET_KEY.upper()}"
    cleaned = rs.redact(body, SECRETS)
    assert not rs.contains_secret(cleaned, SECRETS)
    assert cleaned.count(rs.REDACTED) == 3


def test_the_name_and_the_local_part_are_redacted_on_their_own() -> None:
    cleaned = rs.redact(f"signed by {UA_NAME}; reply to {UA_LOCAL} at the usual place", SECRETS)
    assert "Brien" not in cleaned and UA_LOCAL not in cleaned
    assert cleaned.count(rs.REDACTED) == 2


@pytest.mark.parametrize(("secret", "escaper"), ESCAPED_CASES)
def test_each_escaped_form_is_caught_by_its_own_secret_alone(secret: str, escaper: str) -> None:
    """One secret at a time, so no other secret's literal can mask a missing
    escape -- which is exactly how the email's domain used to."""
    escaped = ESCAPERS[escaper](secret)
    assert secret not in escaped  # the literal alone would not have caught it
    assert rs.contains_secret(f"x={escaped};", (secret,))
    assert rs.redact(f"x={escaped};", (secret,)) == f"x={rs.REDACTED};"


def test_no_escaped_spelling_of_the_idn_user_agent_contains_a_literal_piece() -> None:
    literal_only = [s for s in SECRETS_IDN]
    for escape in (ESCAPERS["quote"], ESCAPERS["quote_plus"]):
        escaped = escape(UA_IDN)
        assert not any(piece.lower() in escaped.lower() for piece in literal_only)


def test_redacted_headers_keep_names_and_hide_values() -> None:
    headers = {
        "User-Agent": UA,
        "APCA-API-KEY-ID": KEY_ID,
        "APCA-API-SECRET-KEY": SECRET_KEY,
        "Accept": "application/json",
    }
    cleaned = rs.redacted_headers(headers, SECRETS)
    assert set(cleaned) == set(headers)
    assert cleaned["Accept"] == "application/json"
    assert not rs.contains_secret(repr(cleaned), SECRETS)


def test_printed_errors_are_redacted(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom while sending {UA} and {KEY_ID}", request=request)

    code, printed = _run(tmp_path, handler)
    assert code == rs.EXIT_FAILED
    assert printed and not rs.contains_secret("\n".join(printed), SECRETS)
    assert list(tmp_path.iterdir()) == []


# --- the writer ------------------------------------------------------------


def test_the_writer_refuses_a_batch_when_a_secret_survives(tmp_path: Path) -> None:
    out = tmp_path / "sec"
    with pytest.raises(rs.SecretFound):
        rs.write_fixtures(
            out, {"clean.json": "{}\n", "dirty.json": f'{{"ua": "{UA}"}}\n'}, SECRETS, overwrite=False
        )
    # All or nothing: the clean file ahead of it was not written either.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("escaper", ["quote", "quote_plus"])
@pytest.mark.parametrize("secret", [UA_IDN, KEY_ID, SECRET_KEY])
def test_the_writer_refuses_an_escaped_secret_too(tmp_path: Path, secret: str, escaper: str) -> None:
    escaped = ESCAPERS[escaper](secret)
    assert not any(piece in escaped for piece in SECRETS_IDN), "would pass with escaping removed"
    with pytest.raises(rs.SecretFound):
        rs.write_fixtures(tmp_path / "sec", {"f.json": f"q={escaped}"}, SECRETS_IDN, overwrite=False)
    assert list(tmp_path.iterdir()) == []


def test_the_writer_refuses_to_overwrite(tmp_path: Path) -> None:
    out = tmp_path / "sec"
    out.mkdir()
    (out / "series_ids.json").write_text("original\n", encoding="utf-8")
    with pytest.raises(rs.WouldOverwrite):
        rs.write_fixtures(out, {"other.json": "new\n"}, SECRETS, overwrite=False)
    assert sorted(p.name for p in out.iterdir()) == ["series_ids.json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sec"]


def test_overwrite_replaces_the_whole_recording_and_never_merges(tmp_path: Path) -> None:
    out = tmp_path / "sec"
    out.mkdir()
    (out / "series_ids.json").write_text("original\n", encoding="utf-8")
    (out / "alpaca_asset_STALE0001.json").write_text("{}\n", encoding="utf-8")
    rs.write_fixtures(out, {"series_ids.json": "replacement\n"}, SECRETS, overwrite=True)
    assert sorted(p.name for p in out.iterdir()) == ["series_ids.json"]
    assert (out / "series_ids.json").read_text(encoding="utf-8") == "replacement\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sec"], "no staging copy left"


@pytest.mark.parametrize("existing", [False, True])
def test_a_disk_error_part_way_leaves_the_target_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    out = tmp_path / "sec"
    if existing:
        out.mkdir()
        (out / "series_ids.json").write_text("original\n", encoding="utf-8")
    real_write = Path.write_bytes
    calls: list[str] = []

    def failing_write(self: Path, data: bytes) -> int:
        calls.append(self.name)
        if len(calls) == 2:
            raise OSError("disk full (invented)")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing_write)
    with pytest.raises(OSError):
        rs.write_fixtures(out, {"a.json": "1\n", "b.json": "2\n", "c.json": "3\n"}, SECRETS, overwrite=True)
    monkeypatch.undo()
    if existing:
        assert sorted(p.name for p in out.iterdir()) == ["series_ids.json"]
        assert (out / "series_ids.json").read_text(encoding="utf-8") == "original\n"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["sec"]
    else:
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["", "../escape.json", "a/b.json", ".hidden", "nport__header.xml/"])
def test_the_writer_refuses_an_unsafe_file_name(tmp_path: Path, name: str) -> None:
    with pytest.raises(rs.RecorderError):
        rs.write_fixtures(tmp_path / "sec", {name: "{}\n"}, SECRETS, overwrite=False)
    assert list(tmp_path.iterdir()) == []


# --- refusals at the source ------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_missing_user_agent_exits_non_zero_before_any_request(
    tmp_path: Path, value: str | None
) -> None:
    env = {k: v for k, v in ENV.items() if k != "SEC_USER_AGENT"}
    if value is not None:
        env["SEC_USER_AGENT"] = value
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    code, printed = _run(tmp_path, handler, env=env)
    assert code == rs.EXIT_MISSING_USER_AGENT
    assert calls == []
    assert any("SEC_USER_AGENT is missing" in line for line in printed)
    assert list(tmp_path.iterdir()) == []


def test_a_403_writes_nothing_and_exits_non_zero(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return httpx.Response(403, text=f"MARKER-403-BODY Forbidden for {UA}")

    code, printed = _run(tmp_path, handler)
    assert code == rs.EXIT_SEC_REFUSED
    assert seen == [UA], "the declared User-Agent is what SEC receives; a refusal is not retried"
    assert list(tmp_path.iterdir()) == []
    output = "\n".join(printed)
    assert not rs.contains_secret(output, SECRETS)
    assert "MARKER-403-BODY" not in output, "the refusal body is never echoed, not even redacted"


def test_the_undeclared_tool_page_is_a_refusal_even_on_200(tmp_path: Path) -> None:
    page = "<html><h1>Your Request Originates from an Undeclared Automated Tool</h1></html>"
    code, _ = _run(tmp_path, lambda request: httpx.Response(200, text=page))
    assert code == rs.EXIT_SEC_REFUSED
    assert list(tmp_path.iterdir()) == []


# --- the N-PORT trim -------------------------------------------------------


def _holding(name: str, cusip: str, pct: str, cat: str, extra: str = "") -> str:
    return (
        "      <invstOrSec>\n"
        f"        <name>{name}</name>\n"
        f"        <cusip>{cusip}</cusip>\n"
        f"        <pctVal>{pct}</pctVal>\n"
        f"        <assetCat>{cat}</assetCat>\n"
        f"{extra}"
        "      </invstOrSec>\n"
    )


def _nport(series_id: str, series_name: str, holdings: str, header_extra: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<edgarSubmission xmlns="http://www.sec.gov/edgar/nport">\n'
        "  <formData>\n"
        f"{header_extra}"
        "    <genInfo>\n"
        f"      <seriesName>{series_name}</seriesName>\n"
        f"      <seriesId>{series_id}</seriesId>\n"
        "      <repPdEnd>2026-09-30</repPdEnd>\n"
        "      <repPdDate>2026-06-30</repPdDate>\n"
        "    </genInfo>\n"
        "    <invstOrSecs>\n"
        f"{holdings}"
        "    </invstOrSecs>\n"
        "  </formData>\n"
        "</edgarSubmission>\n"
    )


NPORT = _nport(
    "S000000001",
    "Invented Sector Fund",
    _holding("Alpha Corp", "000000A01", "60.5", "EC")
    + _holding("Cash Sweep Fund", "000000B02", "0.4", "STIV")
    + _holding("Beta Inc", "N/A", "39.1", "EC"),
)


def test_the_trim_keeps_every_equity_line_and_drops_the_rest() -> None:
    doc = rs.parse_nport(NPORT)
    assert doc.series_id == "S000000001"
    assert [h.name for h in doc.equity] == ["Alpha Corp", "Beta Inc"]
    trimmed, removed = rs.trim_non_equity(NPORT, doc)
    assert removed == 1
    kept = rs.parse_nport(trimmed)
    assert [h.name for h in kept.holdings] == ["Alpha Corp", "Beta Inc"]
    assert "Cash Sweep" not in trimmed


def test_an_absent_cusip_is_none_not_a_string() -> None:
    doc = rs.parse_nport(NPORT)
    assert [h.cusip for h in doc.equity] == ["000000A01", None]


def test_the_trim_refuses_when_its_block_count_disagrees_with_the_parser() -> None:
    doc = rs.parse_nport(NPORT)
    padded = dataclasses.replace(doc, holdings=doc.holdings + [doc.holdings[0]])
    with pytest.raises(rs.RecorderError):
        rs.trim_non_equity(NPORT, padded)


def test_the_trim_refuses_when_a_kept_line_is_not_equity() -> None:
    """A bond whose nested instrument says EC matches the text test but not
    the parser, which reads the holding's own assetCat. Kept, it would be a
    non-EC line in a file that claims to hold only EC lines."""
    nested = "        <derivativeInfo><assetCat>EC</assetCat></derivativeInfo>\n"
    text = _nport(
        "S000000001",
        "Invented Sector Fund",
        _holding("Alpha Corp", "000000A01", "60.5", "EC")
        + _holding("Gamma Note", "000000C03", "1.0", "DBT", extra=nested),
    )
    doc = rs.parse_nport(text)
    assert len(doc.equity) == 1
    with pytest.raises(rs.RecorderError, match="disagree"):
        rs.trim_non_equity(text, doc)


def test_stripping_holdings_leaves_a_parseable_header() -> None:
    header = rs.strip_holdings(NPORT)
    doc = rs.parse_nport(header)
    assert doc.series_id == "S000000001" and doc.holdings == []


# --- the Premium Income sample ---------------------------------------------


def _excluded(series_id: str, name: str) -> tuple[Any, str, Any]:
    filing = rs.Filing("0000000000-26-000099", "2026-08-28", "2026-06-30", "primary_doc.xml")
    return filing, "", rs.NportDoc(series_id, name, "", "")


def test_the_premium_sample_is_chosen_by_name_not_position() -> None:
    chosen = rs.premium_sample(
        [_excluded("S000000009", "Invented Equal Weight Fund"),
         _excluded("S200000001", "Invented Technology Premium Income Fund")]
    )
    assert chosen[2].series_id == "S200000001"


def test_no_premium_income_filing_is_a_refusal() -> None:
    with pytest.raises(rs.RecorderError, match="Premium Income"):
        rs.premium_sample([_excluded("S000000009", "Invented Equal Weight Fund")])


@pytest.mark.parametrize("series_id", ["", "S0/../x", "s 1"])
def test_an_unusable_series_id_never_names_a_file(series_id: str) -> None:
    with pytest.raises(rs.RecorderError, match="seriesId"):
        rs.premium_sample([_excluded(series_id, "Invented Premium Income Fund")])


# --- one complete recording, offline ---------------------------------------

TRUST = int(rs.TRUST_CIK)
SERIES = {t: f"S1000000{i:02d}" for i, t in enumerate(rs.SECTOR_TICKERS, start=1)}
PREMIUM_SERIES = "S200000001"
DECOY_SERIES = "S000000009"  # sorts first among the excluded; not Premium Income
BERKSHIRE_CUSIP = "084670702"
SHARED_CUSIP = "000000M01"
UNRESOLVED_CUSIP = "999000Z01"


def _own_cusip(ticker: str) -> str:
    return f"1{rs.SECTOR_TICKERS.index(ticker):02d}000A01"


def _fund_xml(ticker: str) -> str:
    holdings = _holding(f"{ticker} Alpha Corp", _own_cusip(ticker), "50.1", "EC")
    holdings += _holding("Shared Mega Corp", SHARED_CUSIP, "30.0", "EC")
    holdings += _holding("Cash Sweep Fund", "000000C01", "0.5", "STIV")
    header = ""
    if ticker == "XLC":
        # The owner's name, XML-escaped, in a holding name: parsed into the
        # survey and the unresolved-CUSIP envelope, and printed.
        holdings += _holding(html.escape(f"{UA_NAME} Holdings {UA_EMAIL}"), UNRESOLVED_CUSIP, "0.1", "EC")
    if ticker == "XLF":
        holdings += _holding("Berkshire Hathaway Inc Class B", BERKSHIRE_CUSIP, "12.0", "EC")
        header = f"    <signature>{html.escape(SECRET_KEY)}</signature>\n"
    return _nport(SERIES[ticker], f"The {ticker} Select Sector SPDR Fund", holdings, header)


def _recording() -> dict[str, Any]:
    """Submissions, ticker map and documents, with list positions shuffled so
    nothing can be matched to a fund by where it sits."""
    rng = random.Random(20260926)
    docs = {s: _fund_xml(t) for t, s in SERIES.items()}
    docs[PREMIUM_SERIES] = _nport(
        PREMIUM_SERIES, "Invented Technology Premium Income Fund",
        _holding("Alpha Corp", "000000A01", "90.0", "EC"),
    )
    docs[DECOY_SERIES] = _nport(
        DECOY_SERIES, "Invented Equal Weight Fund", _holding("Alpha Corp", "000000A01", "90.0", "EC")
    )
    order = list(docs)
    rng.shuffle(order)
    accession = {s: f"0000000000-26-{n:06d}" for n, s in enumerate(order, start=1)}
    rows = [(accession[s], "NPORT-P", "2026-06-30") for s in order]
    rows += [("0000000000-26-000501", "NPORT-P", "2026-03-31"),
             ("0000000000-26-000502", "NPORT-P/A", "2026-06-30"),
             ("0000000000-26-000503", "N-CSR", "2026-06-30")]
    rng.shuffle(rows)
    submissions = {
        "cik": str(TRUST),
        "name": "INVENTED SECTOR TRUST",
        "filings": {
            "recent": {
                "accessionNumber": [r[0] for r in rows],
                "filingDate": ["2026-08-28" for _ in rows],
                "reportDate": [r[2] for r in rows],
                "form": [r[1] for r in rows],
                "primaryDocument": ["primary_doc.xml" for _ in rows],
            },
            "files": [],
        },
    }
    data = [[TRUST, s, "C" + s[1:], t] for t, s in SERIES.items()]
    data += [[TRUST, PREMIUM_SERIES, "C200000001", "XPIN"], [TRUST, DECOY_SERIES, "C000000009", "XEQW"],
             [99, "S900000001", "C900000001", "XLK"]]  # another trust's XLK: filtered by cik
    rng.shuffle(data)
    tickers_mf = {"fields": ["cik", "seriesId", "classId", "symbol"], "data": data}
    return {
        "submissions": submissions,
        "tickers_mf": tickers_mf,
        "xml_by_path": {
            f"/Archives/edgar/data/{TRUST}/{accession[s].replace('-', '')}/primary_doc.xml": text
            for s, text in docs.items()
        },
        "accession": accession,
        "docs": docs,
    }


def _asset(cusip: str) -> httpx.Response:
    if cusip == UNRESOLVED_CUSIP:
        return httpx.Response(404, json={"code": 40410000, "message": f"asset not found for {cusip}"})
    symbol = {SHARED_CUSIP: "MEGA", BERKSHIRE_CUSIP: "BRK.B"}.get(cusip, f"T{cusip[:3]}")
    # The key in a body is invented and absurd; it proves the asset files are redacted.
    name = f"Mega Corp {KEY_ID}" if cusip == SHARED_CUSIP else f"{symbol} Inc"
    return httpx.Response(
        200,
        json={"id": f"id-{cusip}", "class": "us_equity", "exchange": "NYSE", "symbol": symbol,
              "name": name, "status": "active", "tradable": True},
    )


class _Served:
    def __init__(self, alpaca_override: Callable[[str], httpx.Response] | None = None) -> None:
        self.recording = _recording()
        self.alpaca_override = alpaca_override
        self.sec: list[httpx.Request] = []
        self.alpaca: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v2/assets/"):
            assert request.url.host not in {"www.sec.gov", "data.sec.gov"}
            assert request.headers["APCA-API-KEY-ID"] == KEY_ID
            self.alpaca.append(request)
            cusip = path.rsplit("/", 1)[1]
            return self.alpaca_override(cusip) if self.alpaca_override else _asset(cusip)
        assert request.headers["User-Agent"] == UA
        assert "APCA-API-KEY-ID" not in request.headers, "keys never go to SEC"
        self.sec.append(request)
        if request.url.host == "data.sec.gov":
            return httpx.Response(200, json=self.recording["submissions"])
        if path == "/files/company_tickers_mf.json":
            return httpx.Response(200, json=self.recording["tickers_mf"])
        text = self.recording["xml_by_path"].get(path)
        return httpx.Response(200, text=text) if text else httpx.Response(404, text="not here")


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _files(out: Path) -> dict[str, str]:
    return {p.name: p.read_bytes().decode("utf-8") for p in sorted(out.iterdir())}


def test_a_complete_recording_is_clean_and_maps_each_fund_by_series_id(tmp_path: Path) -> None:
    served = _Served()
    fake = _FakeClock()
    code, printed = _run(tmp_path, served, sleep=fake.sleep, clock=fake.clock)
    assert code == 0, printed
    out = tmp_path / "sec"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sec"], "no staging copy left"
    files = _files(out)

    # Rule 6: no secret, in any spelling, and no request header, in any file or line printed.
    for name, text in files.items():
        assert not rs.contains_secret(text, SECRETS), name
        assert "APCA" not in text, name
        assert "Brien" not in text and UA_LOCAL not in text, name
    assert not rs.contains_secret("\n".join(printed), SECRETS)

    # Each fund is its seriesId's filing, never a list position's.
    series_ids = json.loads(files["series_ids.json"])
    accession = served.recording["accession"]
    for ticker, series_id in SERIES.items():
        entry = series_ids["funds"][ticker]
        assert entry["series_id"] == series_id
        assert entry["accession"] == accession[series_id]
        meta = json.loads(files[f"nport_{ticker}_primary_doc.meta.json"])
        assert meta["series_id"] == series_id and meta["accession"] == accession[series_id]
        doc = rs.parse_nport(files[f"nport_{ticker}_primary_doc.xml"])
        assert doc.series_id == series_id and doc.series_name == f"The {ticker} Select Sector SPDR Fund"
        if ticker in rs.COMPLETE_TICKERS:
            assert files[f"nport_{ticker}_primary_doc.xml"] == served.recording["docs"][series_id]
        else:
            assert all(h.asset_cat == rs.EQUITY_COMMON for h in doc.holdings)
            assert meta["non_equity_removed"] == 1

    # The Premium Income filing, chosen by name over the decoy that sorts first.
    header = files[f"nport_excluded_{PREMIUM_SERIES}_header.xml"]
    assert "invstOrSecs" not in header and rs.parse_nport(header).series_id == PREMIUM_SERIES
    assert {e["series_id"] for e in series_ids["excluded_same_quarter"]} == {PREMIUM_SERIES, DECOY_SERIES}
    assert not any(name.startswith(f"nport_excluded_{DECOY_SERIES}") for name in files)

    # The survey: a 404 is recorded as unresolved, a 200 as its symbol.
    rows = {r["cusip"]: r for r in json.loads(files["cusip_survey.json"])["rows"]}
    assert rows[UNRESOLVED_CUSIP]["status_code"] == 404 and rows[UNRESOLVED_CUSIP]["symbol"] is None
    assert rows[BERKSHIRE_CUSIP]["symbol"] == "BRK.B" and rows[SHARED_CUSIP]["funds"] == list(rs.SECTOR_TICKERS)
    assert f"alpaca_asset_{UNRESOLVED_CUSIP}.json" in files
    assert f"alpaca_asset_{SHARED_CUSIP}.json" in files

    # Pacing: SEC at its interval, then Alpaca at its own, each from its first request.
    expected = [rs.SEC_MIN_INTERVAL] * (len(served.sec) - 1)
    expected += [rs.ALPACA_MIN_INTERVAL] * (len(served.alpaca) - 1)
    assert fake.sleeps == pytest.approx(expected)
    assert len(served.sec) == 2 + len(served.recording["docs"])
    assert len(served.alpaca) == len(rows)


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    code, printed = _run(tmp_path, _Served(), argv=["--dry-run"])
    assert code == 0, printed
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_an_alpaca_error_is_not_an_unresolved_cusip(tmp_path: Path, status: int) -> None:
    """Only 200 and 404 answer "what ticker is this CUSIP". Anything else stops
    the run, and nothing -- not the SEC files already fetched -- is written."""
    served = _Served(alpaca_override=lambda cusip: httpx.Response(status, text="MARKER-ALPACA-BODY"))
    code, printed = _run(tmp_path, served)
    assert code == rs.EXIT_FAILED
    assert len(served.alpaca) == 1, "the first error stops the lookups"
    assert list(tmp_path.iterdir()) == []
    output = "\n".join(printed)
    assert f"HTTP {status}" in output and "MARKER-ALPACA-BODY" not in output


def test_an_alpaca_200_without_a_json_object_stops_the_run(tmp_path: Path) -> None:
    served = _Served(alpaca_override=lambda cusip: httpx.Response(200, text="<html>maintenance</html>"))
    code, _ = _run(tmp_path, served)
    assert code == rs.EXIT_FAILED
    assert list(tmp_path.iterdir()) == []


# --- rule 6 ----------------------------------------------------------------


def test_the_recorder_never_loads_the_env_file() -> None:
    assert dotenv_uses(RECORDER_PATH) == []
