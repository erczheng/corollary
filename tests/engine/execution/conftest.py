"""Replay recorded trading-host responses. No test here makes a live call.

Fixtures under ``tests/fixtures/alpaca/`` were captured by
``tests/fixtures/record_alpaca.py`` against the real paper account -- account,
positions, ``nested=true`` orders, both activity branches, the activity
pagination loop and the portfolio history. They are replayed through an
``httpx.MockTransport``, so the request the broker actually builds -- path,
query parameters, headers, host -- is exercised rather than bypassed. A test
double returning parsed objects would pass while the URL was wrong, and
``nested=true`` is exactly the parameter whose absence would be invisible.

Bytes, not re-serialised objects
--------------------------------

The transport serves the fixture file's own bytes and the broker parses them
with ``corollary.wire.decode_json``. That matters more on this host than on
the market-data one: ``GET /v2/account/portfolio/history`` returns money as
**bare JSON numbers**, so a replay that round-tripped through
``httpx.Response(json=...)`` would put the whole equity curve through a double
on the way *in* and both sides of every assertion would then be reading the
same double back out. The file is the source of truth character for character.

No live host, ever
------------------

:data:`TEST_CREDENTIALS` is obviously fake and points at the paper host. Rule
5: paper is the default everywhere, and nothing in this phase constructs live
credentials.
"""

import json
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from corollary.engine.execution.alpaca import AlpacaBroker
from corollary.data.providers.alpaca import AlpacaCredentials
from corollary.ratelimit import ALPACA_PAPER_TRADING_HOST, HostRateLimiter

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "alpaca"

PAPER_BASE_URL = f"https://{ALPACA_PAPER_TRADING_HOST}"

#: Obviously fake. Rule 6: no key material in tests or fixtures.
TEST_CREDENTIALS = AlpacaCredentials(
    key_id="PKTESTTESTTESTTEST",
    secret_key="not-a-real-secret",
    trading_base_url=PAPER_BASE_URL,
    is_paper=True,
)


def fixture_text(name: str) -> str:
    return (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")


def load_fixture(name: str) -> dict[str, Any]:
    """One recorded response: ``{"status_code": int, "body": ...}``.

    ``parse_float=Decimal``, matching the broker. A test comparing a parsed
    price against ``load_fixture(...)`` is then comparing two exact decimals
    rather than two views of one double.
    """
    result: dict[str, Any] = json.loads(fixture_text(name), parse_float=Decimal)
    return result


def fixture_body_bytes(name: str) -> bytes:
    """The exact bytes of a fixture's ``body``, sliced out of the file.

    ``raw_decode`` reports where the value ended, which is what makes an exact
    slice possible without re-serialising anything.
    """
    text = fixture_text(name)
    marker = '"body":'
    start = text.index(marker) + len(marker)
    while text[start].isspace():
        start += 1
    _, end = json.JSONDecoder().raw_decode(text, start)
    return text[start:end].encode("utf-8")


class RecordingTransport(httpx.MockTransport):
    """A mock transport that logs what it served.

    ``route`` maps a request to a fixture name, to a ``(status, body)`` pair,
    or to an exception to raise. Returning ``None`` is a test failure rather
    than a 404: an unrouted request means the broker built a URL nobody
    predicted, which is the thing these tests exist to catch.
    """

    def __init__(self, route: "Route") -> None:
        self.requests: list[httpx.Request] = []
        self._route = route
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        served = self._route(request)
        if served is None:
            raise AssertionError(f"no fixture routed for {request.method} {request.url}")
        if isinstance(served, BaseException):
            raise served
        if isinstance(served, tuple):
            status, body = served
            return httpx.Response(
                status_code=status,
                content=body.encode("utf-8") if isinstance(body, str) else body,
                headers={"content-type": "application/json"},
                request=request,
            )
        return httpx.Response(
            status_code=int(load_fixture(served)["status_code"]),
            content=fixture_body_bytes(served),
            headers={"content-type": "application/json"},
            request=request,
        )

    @property
    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def params_for(self, path_fragment: str) -> dict[str, str]:
        """Query parameters of the first request whose path contains it."""
        for request in self.requests:
            if path_fragment in request.url.path:
                return dict(request.url.params)
        raise AssertionError(f"no request touched {path_fragment!r}")

    def all_params_for(self, path_fragment: str) -> list[dict[str, str]]:
        return [
            dict(request.url.params)
            for request in self.requests
            if path_fragment in request.url.path
        ]


Served = str | tuple[int, str | bytes] | BaseException | None
Route = Callable[[httpx.Request], Served]


async def _never_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError(
        f"a replay test waited {seconds}s on the rate limiter; the fixture "
        "budget should be large enough that this never happens"
    )


@pytest.fixture
def limiter() -> HostRateLimiter:
    """A generous limiter with a clock that never advances.

    These tests are about shapes and requests; the budget behaviour has its
    own file in ``tests/test_ratelimit.py``.
    """
    return HostRateLimiter(
        requests_per_minute=10_000, clock=lambda: 0.0, sleep=_never_sleep
    )


@pytest.fixture
def make_broker(
    limiter: HostRateLimiter,
) -> Iterator[Callable[..., tuple[AlpacaBroker, RecordingTransport]]]:
    """Build a broker wired to a routing function over the fixtures."""
    created: list[httpx.AsyncClient] = []

    def build(
        route: Route,
        *,
        credentials: AlpacaCredentials = TEST_CREDENTIALS,
        **kwargs: Any,
    ) -> tuple[AlpacaBroker, RecordingTransport]:
        transport = RecordingTransport(route)
        client = httpx.AsyncClient(transport=transport)
        created.append(client)
        broker = AlpacaBroker(
            credentials=credentials,
            client=client,
            limiter=limiter,
            **kwargs,
        )
        return broker, transport

    yield build


def single(name: str) -> Route:
    """Route every request to one fixture."""
    return lambda _request: name


def sequence(*served: Served) -> Route:
    """Serve the given responses in order, then repeat the last one.

    Used for the pagination loop. The final entry must be a page the loop
    stops on -- for activities that is an empty array, because this endpoint
    has no ``next_page_token`` to say so.
    """
    calls = {"n": 0}

    def choose(_request: httpx.Request) -> Served:
        index = min(calls["n"], len(served) - 1)
        calls["n"] += 1
        return served[index]

    return choose
