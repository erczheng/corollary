"""Replay recorded Alpaca responses. No test in this package makes a live call.

Fixtures under ``tests/fixtures/alpaca/`` were captured by
``tests/fixtures/record_alpaca.py`` against the real paper account. They are
replayed through an ``httpx.MockTransport``, which means the request the
provider actually builds — path, query parameters, headers — is exercised, not
bypassed. A test double that returned parsed objects would pass while the URL
was wrong.

The transport also **records every request it serves**, so a test can assert
on which host was hit. That is how the two-rate-limit-bucket behaviour is
checked end to end rather than only at the unit level.

No float, anywhere on the replay path
-------------------------------------

The replay used to re-serialise through ``httpx.Response(json=...)``, so the
bytes the provider parsed were Python's ``repr`` of a double rather than the
text in the file. The loud half of the guarantee still held -- a ``.json()``
regression puts a float into ``_as_decimal``, which raises -- but a *quiet*
precision regression on a value that does not round-trip through a double
would have been invisible, because both sides of every assertion were reading
the same double back out.

So :class:`RecordingTransport` now serves the fixture file's own bytes, and
:func:`load_fixture` parses them with ``parse_float=Decimal`` exactly as the
provider's ``_decode`` does. The file is the source of truth character for
character.

One honest limitation: fixtures captured before ``record_alpaca.dumps_exact``
existed went through a ``json.loads``/``json.dumps`` round trip at *record*
time, so their last significant digit is a double's repr rather than Alpaca's.
Re-recording fixes that; nothing in this repository can.
"""

import json
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from corollary.data.providers.alpaca import AlpacaCredentials, AlpacaProvider, FeedConfig
from corollary.ratelimit import HostRateLimiter

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "alpaca"

#: The instant the fixtures were recorded, to the second. Feed selection and
#: time-to-expiry both depend on "now", so it is frozen rather than read from
#: the clock — otherwise the greek assertions rot overnight and the
#: near-expiry contract becomes an expired one next week. Same reasoning as
#: ``MARKET_TODAY`` on the frontend.
RECORDED_AT = datetime(2026, 9, 10, 19, 10, 0, tzinfo=timezone.utc)

#: The Basic plan, per CLAUDE.md.
BASIC_FEEDS = FeedConfig(
    options="indicative", stock_historical="sip", stock_realtime="iex"
)

#: Obviously fake, and short enough that the masking test can reason about it.
#: Rule 6: no key material in tests or fixtures.
TEST_CREDENTIALS = AlpacaCredentials(
    key_id="PKTESTTESTTESTTEST",
    secret_key="not-a-real-secret",
    trading_base_url="https://paper-api.alpaca.markets",
    is_paper=True,
)


def fixture_text(name: str) -> str:
    return (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")


def load_fixture(name: str) -> dict[str, Any]:
    """One recorded response: ``{"status_code": int, "body": ...}``.

    ``parse_float=Decimal``, matching the provider. A test comparing a parsed
    price against ``load_fixture(...)`` is then comparing two exact decimals
    rather than two views of the same double.
    """
    result: dict[str, Any] = json.loads(fixture_text(name), parse_float=Decimal)
    return result


def fixture_body_bytes(name: str) -> bytes:
    """The exact bytes of a fixture's ``body``, sliced out of the file.

    ``raw_decode`` returns where the value ended, which is what makes an exact
    slice possible without re-serialising anything. The alternative --
    ``json.dumps(load_fixture(name)["body"])`` -- would put every number
    through Python's formatter on the way to the wire, which is the round trip
    this exists to remove.
    """
    text = fixture_text(name)
    marker = '"body":'
    start = text.index(marker) + len(marker)
    while text[start].isspace():
        start += 1
    _, end = json.JSONDecoder().raw_decode(text, start)
    return text[start:end].encode("utf-8")


def recorded_chain_instant(name: str) -> datetime:
    """When a chain fixture was actually captured, from its own quote stamps.

    More precise than :data:`RECORDED_AT` and, more importantly, it cannot
    drift out of step with the file. Time to expiry is the input the greeks
    are most sensitive to, so a comparison against the vendor's numbers has to
    use the vendor's own clock rather than a constant somebody remembers to
    update.
    """
    stamps = [
        entry["latestQuote"]["t"]
        for entry in load_fixture(name)["body"]["snapshots"].values()
        if entry.get("latestQuote")
    ]
    latest = max(stamps)
    # fromisoformat takes at most 6 fractional digits; Alpaca sends 9.
    return datetime.fromisoformat(latest[:26].rstrip("Z") + "+00:00")


#: What a route may hand back: a fixture name, or a literal
#: ``(status, body)`` for a response no recording could ever contain.
#:
#: The tuple form exists for the error-body tests. A body that echoes the
#: request's ``APCA-API-SECRET-KEY`` header back is not something Alpaca has
#: ever sent and not something the recorder could capture even if it had --
#: rule 6 forbids a fixture holding key material. It has to be synthesised in
#: the test, which is why this mirrors the broker conftest's ``Served``.
Served = str | tuple[int, str | bytes] | None
Route = Callable[[httpx.Request], Served]


class RecordingTransport(httpx.MockTransport):
    """A mock transport that logs what it served.

    ``route`` maps a request to a fixture name, or to a literal
    ``(status, body)`` pair. Returning ``None`` from it is a test failure
    rather than a 404, because an unrouted request means the provider built a
    URL nobody predicted — exactly the thing these tests exist to catch.
    """

    def __init__(self, route: Route) -> None:
        self.requests: list[httpx.Request] = []
        self._route = route
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        served = self._route(request)
        if served is None:
            raise AssertionError(
                f"no fixture routed for {request.method} {request.url}"
            )
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

    def params_for(self, path_fragment: str) -> dict[str, str]:
        """Query parameters of the first request whose path contains it."""
        for request in self.requests:
            if path_fragment in request.url.path:
                return dict(request.url.params)
        raise AssertionError(f"no request touched {path_fragment!r}")


@pytest.fixture
def limiter() -> HostRateLimiter:
    """A generous limiter with a clock that never advances.

    Capacity high enough that no test ever waits — these tests are about
    mapping, and the budget behaviour has its own file.
    """
    return HostRateLimiter(
        requests_per_minute=10_000, clock=lambda: 0.0, sleep=_never_sleep
    )


async def _never_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError(
        f"a replay test waited {seconds}s on the rate limiter; the fixture "
        "budget should be large enough that this never happens"
    )


@pytest.fixture
def make_provider(
    limiter: HostRateLimiter,
) -> Iterator[Callable[..., tuple[AlpacaProvider, RecordingTransport]]]:
    """Build a provider wired to a routing function over the fixtures."""
    created: list[httpx.AsyncClient] = []

    def build(
        route: Route,
        *,
        credentials: AlpacaCredentials = TEST_CREDENTIALS,
        feeds: FeedConfig = BASIC_FEEDS,
        now: datetime = RECORDED_AT,
        **kwargs: Any,
    ) -> tuple[AlpacaProvider, RecordingTransport]:
        transport = RecordingTransport(route)
        client = httpx.AsyncClient(transport=transport)
        created.append(client)
        provider = AlpacaProvider(
            credentials=credentials,
            feeds=feeds,
            client=client,
            limiter=limiter,
            now=lambda: now,
            **kwargs,
        )
        return provider, transport

    yield build


def single(name: str) -> Route:
    """Route every request to one fixture."""
    return lambda _request: name


def sequence(*names: Served) -> Route:
    """Serve the named fixtures in order, then repeat the last one.

    Used for the pagination loop. The final fixture must be one whose
    ``next_page_token`` is null, or the provider keeps asking -- which is
    exactly the behaviour under test, so it is not papered over.
    """
    calls = {"n": 0}

    def choose(_request: httpx.Request) -> Served:
        index = min(calls["n"], len(names) - 1)
        calls["n"] += 1
        return names[index]

    return choose


def by_path(mapping: dict[str, Route | str]):
    """Dispatch on a path fragment to a fixture name or a nested router."""

    def choose(request: httpx.Request) -> Served:
        for fragment, target in mapping.items():
            if fragment in str(request.url):
                return target(request) if callable(target) else target
        return None

    return choose


def chain_page(name: str) -> Route:
    """One recorded chain page, then a terminal page so the loop stops.

    The recorded pages all carry a ``next_page_token`` -- they are real pages
    from the middle of a real chain -- so a test that wants exactly one page
    still has to answer the follow-up request. ``option_chain_end`` is the
    shape Alpaca returns at the end: an empty map and a null token.
    """
    return sequence(name, "option_chain_end")


def contracts_page(name: str) -> Route:
    """One recorded contracts page, then a terminal page."""
    return sequence(name, "option_contracts_end")
