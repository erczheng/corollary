"""``record_openfigi.py``: keyless, three requests, no request of its own, no secret written.

No live call is made here. Every run goes through an ``httpx.MockTransport``
handed to a keyless provider, so what is asserted on is the request the
provider actually builds when the recorder drives it.
"""

import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from record_openfigi import (  # noqa: E402
    EXPECTED_BATCH_SIZES,
    EXPECTED_ISIN_ONLY,
    IsinOnlyHolding,
    build_provider,
    check_against_q17,
    environment_secrets,
    isin_only_holdings,
    record,
)

from corollary.data.providers.openfigi import (  # noqa: E402
    KEYLESS_JOBS_PER_REQUEST,
    OPENFIGI_API_KEY_ENV,
    OPENFIGI_KEY_HEADER,
    OpenFigiCredentials,
    OpenFigiProvider,
)
from corollary.ratelimit import HostRateLimiter  # noqa: E402
from tests.test_hard_rules import WRITE_VERBS  # noqa: E402

RECORDER_PATH = Path(__file__).resolve().parent / "record_openfigi.py"

#: Rule 6: an obviously fake value, so a leak is greppable and harmless.
PLANTED = "PLANTED-SECRET-" + "0123456789"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def holdings() -> list[IsinOnlyHolding]:
    return [IsinOnlyHolding(fund=f, isin=i, issuer=f"ISSUER {i}") for f, i in EXPECTED_ISIN_ONLY]


def warning_reply(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=[{"warning": "No identifier found."} for _ in json.loads(request.content)])


def mock_provider(handler: Any, seen: list[httpx.Request]) -> OpenFigiProvider:
    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response: httpx.Response = handler(request)
        return response

    clock = FakeClock()
    return OpenFigiProvider(
        credentials=OpenFigiCredentials(api_key=None),
        transport=httpx.MockTransport(recording),
        limiter=HostRateLimiter(clock=clock, sleep=clock.sleep),
    )


NOW = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------- the 29 ISINs


def test_the_filings_yield_exactly_q17s_29_isin_only_lines() -> None:
    found = isin_only_holdings()
    assert [(h.fund, h.isin) for h in found] == list(EXPECTED_ISIN_ONLY)
    assert len(found) == 29 == len({h.isin for h in found})
    assert next(h.issuer for h in found if h.isin == "IE000S9YS762") == "Linde PLC"
    check_against_q17(found)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda hs: hs[:-1],
        lambda hs: hs + [IsinOnlyHolding("XLB", "US0000000002", "EXTRA")],
        lambda hs: [IsinOnlyHolding("XLY" if h.fund == "XLB" else h.fund, h.isin, h.issuer) for h in hs],
        lambda hs: list(reversed(hs)),
    ],
    ids=["missing", "extra", "wrong-fund", "reordered"],
)
def test_anything_but_q17s_set_is_refused_before_any_request(mutate: Any) -> None:
    with pytest.raises(SystemExit, match="spec Q17"):
        check_against_q17(mutate(holdings()))


@pytest.mark.asyncio
async def test_a_wrong_set_makes_no_request(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    with pytest.raises(SystemExit):
        await record(mock_provider(warning_reply, seen), holdings()[:-1], tmp_path, secrets=())
    assert seen == []
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------ keyless


@pytest.mark.risk
def test_the_recorder_builds_its_provider_keyless_even_with_a_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 6: a key in the environment cannot reach a request or a fixture."""
    monkeypatch.setenv(OPENFIGI_API_KEY_ENV, PLANTED)
    provider = build_provider()
    assert provider.jobs_per_request == KEYLESS_JOBS_PER_REQUEST == 10
    assert OPENFIGI_KEY_HEADER not in provider._headers()
    assert PLANTED not in repr(provider._credentials)
    source = RECORDER_PATH.read_text(encoding="utf-8")
    assert "from_env" not in source
    assert OPENFIGI_API_KEY_ENV not in source


@pytest.mark.asyncio
async def test_a_run_is_three_requests_of_10_10_9(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    written = await record(
        mock_provider(warning_reply, seen), holdings(), tmp_path, secrets=(), now=NOW
    )
    assert EXPECTED_BATCH_SIZES == (10, 10, 9)
    assert [len(json.loads(r.content)) for r in seen] == [10, 10, 9]
    assert all(OPENFIGI_KEY_HEADER.lower() not in r.headers for r in seen)
    assert [p.name for p in written] == [f"mapping_batch_{n}.json" for n in (1, 2, 3)]
    sent = [job["idValue"] for r in seen for job in json.loads(r.content)]
    assert sent == [i for _, i in EXPECTED_ISIN_ONLY]

    for n, path in enumerate(written, start=1):
        envelope = json.loads(path.read_text(encoding="utf-8"))
        assert envelope["recorded_at"] == "2026-10-01T14:00:00+00:00"
        assert envelope["recorder"] == "tests/fixtures/record_openfigi.py"
        assert "record_openfigi.py" in envelope["note"]
        assert (envelope["batch"], envelope["batches"]) == (n, 3)
        assert envelope["request"]["keyed"] is False
        assert envelope["request"]["jobs"] == json.loads(seen[n - 1].content)
        assert [c["isin"] for c in envelope["context"]] == [
            j["idValue"] for j in envelope["request"]["jobs"]
        ]
        assert envelope["response"] == [{"warning": "No identifier found."}] * len(
            envelope["request"]["jobs"]
        )
    # The issuer name is carried in the fixture and never sent.
    assert all(b"ISSUER" not in r.content for r in seen)


@pytest.mark.asyncio
async def test_an_existing_fixture_is_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / "mapping_batch_1.json").write_text("{}", encoding="utf-8")
    seen: list[httpx.Request] = []
    with pytest.raises(SystemExit, match="overwrite"):
        await record(mock_provider(warning_reply, seen), holdings(), tmp_path, secrets=())
    assert seen == []
    assert (tmp_path / "mapping_batch_1.json").read_text(encoding="utf-8") == "{}"


# -------------------------------------------------------------------- rule 6


def _echo(text: str) -> Any:
    def reply(request: httpx.Request) -> httpx.Response:
        jobs = json.loads(request.content)
        return httpx.Response(200, json=[{"warning": text} for _ in jobs])

    return reply


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply_text", "secrets"),
    [
        (f"echo {PLANTED}", (PLANTED,)),
        (f"echo {PLANTED.lower()}", (PLANTED,)),
        (f"{OPENFIGI_KEY_HEADER}: x", ()),
        ("apikey=x", ()),
    ],
    ids=["env-secret", "env-secret-any-case", "key-header-name", "key-like-word"],
)
async def test_a_fixture_carrying_a_secret_is_refused_and_nothing_written(
    tmp_path: Path, reply_text: str, secrets: tuple[str, ...]
) -> None:
    """Rule 6: the abort names no value, and no file reaches disk -- not even batch 1."""
    seen: list[httpx.Request] = []
    out = tmp_path / "openfigi"
    with pytest.raises(SystemExit) as raised:
        await record(mock_provider(_echo(reply_text), seen), holdings(), out, secrets=secrets)
    assert PLANTED not in str(raised.value) and PLANTED.lower() not in str(raised.value)
    assert "Nothing was written" in str(raised.value)
    assert not out.exists() or list(out.iterdir()) == []


@pytest.mark.asyncio
async def test_a_numeric_value_in_a_reply_is_refused_rather_than_rounded(tmp_path: Path) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        jobs = json.loads(request.content)
        return httpx.Response(200, content=json.dumps([{"warning": "x", "n": 1.5} for _ in jobs]))

    seen: list[httpx.Request] = []
    with pytest.raises(SystemExit, match="Decimal"):
        await record(mock_provider(reply, seen), holdings(), tmp_path, secrets=())
    assert list(tmp_path.iterdir()) == []


def test_environment_secrets_selects_by_name_and_length() -> None:
    env = {
        "OPENFIGI_API_KEY": "k" * 8,
        "ALPACA_PAPER_SECRET_KEY": "s" * 40,
        "FINNHUB_TOKEN": "t" * 20,
        "SEC_USER_AGENT": "Corollary research agent",
        "SHORT_KEY": "abc",
        "PATH": "C:/Windows/system32/long/enough",
    }
    assert set(environment_secrets(env)) == {
        "k" * 8,
        "s" * 40,
        "t" * 20,
        "Corollary research agent",
    }


# -------------------------------------------------------------------- rule 1


@pytest.mark.risk
def test_the_recorder_makes_no_request_of_its_own() -> None:
    """Rule 1: every byte on the wire goes through the provider's one exempt site.

    The recorder imports no HTTP client, nothing that reaches the broker, and
    calls exactly two things on the provider: ``map_isin_batches`` and
    ``aclose``. It touches none of the provider's private members either --
    ``_client``, ``_post_jobs``, ``_headers`` -- so the exempt call site's
    pinned shape is the whole of what it can send.
    """
    tree = ast.parse(RECORDER_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import in the recorder"
            imported.add(node.module or "")
    forbidden = ("httpx", "urllib", "http", "socket", "ssl", "requests", "aiohttp", "alpaca")
    for module in imported:
        root = module.split(".")[0]
        assert root not in forbidden, f"the recorder imports {module}"
        assert "alpaca" not in module.lower(), f"the recorder imports {module}"
        assert not module.startswith("corollary.engine"), f"the recorder imports {module}"
    assert imported >= {"corollary.data.providers.openfigi", "corollary.data.providers.sec"}

    provider_calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in WRITE_VERBS, f"line {node.lineno}: .{node.attr}"
            assert not node.attr.startswith("_") or node.attr.startswith("__"), (
                f"line {node.lineno}: the recorder reaches a private member .{node.attr}"
            )
            if isinstance(node.value, ast.Name) and node.value.id == "provider":
                provider_calls.add(node.attr)
    assert provider_calls == {"jobs_per_request", "map_isin_batches", "aclose"}
