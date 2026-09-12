"""The broker's HTTP behaviour: hosts, budgets, headers, and every failure.

CLAUDE.md's execution coverage table asks for fills, partial fills,
rejections, disconnects and reconnects. In this phase there is no execution,
so the rows that exist are the ones a *read* surface can have:

* **Rejections** are HTTP statuses, and each maps to a type whose remedy is
  different. A 401 means the keys are wrong -- which is not hypothetical: the
  Alpaca MCP server is configured with non-paper keys and answers ``401
  {'code': 40110000, 'message': 'request is not authorized'}`` on every
  trading endpoint. Someone will meet that again, and the error should say so
  rather than reading as a bug in this code.
* **Disconnects** are ``httpx.HTTPError``, which must surface as a
  ``BrokerError`` rather than an httpx type -- the watchdog in rule 9 halts on
  a broker failure and should not have to know what transport is underneath.
* **Reconnects** are the absence of latched state: a failed call must not
  poison the next one.

**Two token buckets, not one.** ``data.alpaca.markets`` and
``paper-api.alpaca.markets`` each carry their own 200/min. The broker bills
the trading host and must never touch the market-data budget, because a
shared bucket throttles the process to half its real allowance and does it
invisibly -- nothing errors, the poll just runs at half cadence.
"""

import httpx
import pytest

from corollary.data.providers.alpaca import AlpacaCredentials, CredentialsError
from corollary.engine.execution.alpaca import ERROR_BODY_MAX, AlpacaBroker
from corollary.engine.execution.interface import (
    BrokerAuthError,
    BrokerError,
    BrokerRateLimitedError,
)
from corollary.ratelimit import (
    ALPACA_DATA_HOST,
    ALPACA_LIVE_TRADING_HOST,
    ALPACA_PAPER_TRADING_HOST,
    HostRateLimiter,
)

from .conftest import single


@pytest.mark.asyncio
async def test_requests_go_to_the_paper_trading_host(make_broker) -> None:
    broker, transport = make_broker(single("account"))
    await broker.account()
    assert transport.hosts == [ALPACA_PAPER_TRADING_HOST]
    assert ALPACA_LIVE_TRADING_HOST not in transport.hosts


@pytest.mark.asyncio
async def test_the_credentials_are_sent_as_headers(make_broker) -> None:
    broker, transport = make_broker(single("account"))
    await broker.account()
    headers = transport.requests[0].headers
    assert headers["APCA-API-KEY-ID"] == "PKTESTTESTTESTTEST"
    assert headers["APCA-API-SECRET-KEY"] == "not-a-real-secret"


@pytest.mark.asyncio
async def test_only_the_trading_budget_is_spent(make_broker, limiter) -> None:
    """One bucket per host, and the broker touches exactly one of them."""
    broker, _ = make_broker(single("account"))
    data_before = limiter.bucket_for(ALPACA_DATA_HOST).available

    await broker.account()

    trading = limiter.bucket_for(ALPACA_PAPER_TRADING_HOST)
    assert trading.available == trading.capacity - 1
    assert limiter.bucket_for(ALPACA_DATA_HOST).available == data_before


@pytest.mark.asyncio
async def test_a_paginated_call_spends_one_token_per_request(
    make_broker, limiter
) -> None:
    from .conftest import sequence

    broker, _ = make_broker(
        sequence("activities_fill_page1", "activities_fill_page2", "activities_end")
    )
    await broker.activities(page_size=2)
    trading = limiter.bucket_for(ALPACA_PAPER_TRADING_HOST)
    assert trading.available == trading.capacity - 3


@pytest.mark.asyncio
async def test_a_401_is_an_auth_error_naming_the_keys(make_broker) -> None:
    """The MCP server's failure mode, met on purpose here instead of live."""
    body = '{"code": 40110000, "message": "request is not authorized"}'
    broker, _ = make_broker(lambda _request: (401, body))

    with pytest.raises(BrokerAuthError) as raised:
        await broker.account()

    message = str(raised.value)
    assert "401" in message
    assert "key" in message.lower()
    assert "not authorized" in message


@pytest.mark.asyncio
async def test_a_403_is_an_auth_error_too(make_broker) -> None:
    broker, _ = make_broker(lambda _request: (403, '{"message": "forbidden"}'))
    with pytest.raises(BrokerAuthError):
        await broker.positions()


@pytest.mark.asyncio
async def test_a_429_is_its_own_type(make_broker) -> None:
    """The local bucket and the server window disagreed. Not a retry-blind."""
    broker, _ = make_broker(lambda _request: (429, '{"message": "too many"}'))
    with pytest.raises(BrokerRateLimitedError) as raised:
        await broker.account()
    assert "429" in str(raised.value)


@pytest.mark.asyncio
async def test_a_500_is_a_plain_broker_error(make_broker) -> None:
    broker, _ = make_broker(lambda _request: (500, '{"message": "boom"}'))
    with pytest.raises(BrokerError) as raised:
        await broker.account()
    assert "500" in str(raised.value)


@pytest.mark.asyncio
async def test_a_rate_limit_error_is_a_broker_error(make_broker) -> None:
    """So a caller that only knows ``BrokerError`` still catches everything."""
    assert issubclass(BrokerRateLimitedError, BrokerError)
    assert issubclass(BrokerAuthError, BrokerError)


@pytest.mark.asyncio
async def test_a_dropped_connection_surfaces_as_a_broker_error(
    make_broker,
) -> None:
    """The watchdog halts on this; it must not have to catch an httpx type."""
    broker, _ = make_broker(lambda request: httpx.ConnectError("no route", request=request))
    with pytest.raises(BrokerError) as raised:
        await broker.account()
    assert "no route" in str(raised.value)


@pytest.mark.asyncio
async def test_a_timeout_surfaces_as_a_broker_error(make_broker) -> None:
    broker, _ = make_broker(
        lambda request: httpx.ReadTimeout("timed out", request=request)
    )
    with pytest.raises(BrokerError):
        await broker.positions()


@pytest.mark.asyncio
async def test_a_failed_call_does_not_poison_the_next_one(make_broker) -> None:
    """Reconnect. There is no latched failure state on this object.

    Rule 9 is deliberate about what *does* latch: the engine's halt. A
    transport failure is not that, and a broker that stayed broken after one
    timeout would turn every blip into a manual resume.
    """
    calls = {"n": 0}

    def route(request: httpx.Request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.ConnectError("dropped", request=request)
        return "account"

    broker, _ = make_broker(route)

    with pytest.raises(BrokerError):
        await broker.account()
    account = await broker.account()
    assert account.status == "ACTIVE"


@pytest.mark.asyncio
async def test_a_non_json_body_is_a_broker_error_not_a_crash(make_broker) -> None:
    broker, _ = make_broker(lambda _request: (200, "<html>maintenance</html>"))
    with pytest.raises(BrokerError) as raised:
        await broker.account()
    assert "JSON" in str(raised.value)


@pytest.mark.asyncio
async def test_a_list_where_an_object_belongs_is_a_broker_error(
    make_broker,
) -> None:
    broker, _ = make_broker(lambda _request: (200, "[]"))
    with pytest.raises(BrokerError):
        await broker.account()


@pytest.mark.parametrize(
    "path, body, missing",
    [
        ("/v2/account", '{"cash": "1"}', "created_at"),
        (
            "/v2/orders",
            '[{"id": "x", "order_class": "simple", "side": "buy"}]',
            "created_at",
        ),
        (
            "/v2/account/activities",
            '[{"activity_type": "FILL", "id": "1::a", "order_id": "b",'
            ' "side": "buy", "qty": "1", "price": "1"}]',
            "transaction_time",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_missing_required_field_is_a_broker_error_not_a_key_error(
    make_broker, path, body, missing
) -> None:
    """The boundary must fail in its own currency.

    ``raw["created_at"]`` would raise ``KeyError``, which escapes past a
    watchdog catching ``BrokerError`` as a crash rather than a halt. So every
    required field is read through a reject that names it.
    """
    broker, _ = make_broker(lambda _request: (200, body))
    call = {
        "/v2/account": broker.account,
        "/v2/orders": broker.orders,
        "/v2/account/activities": broker.activities,
    }[path]

    with pytest.raises(BrokerError) as raised:
        await call()
    assert missing in str(raised.value)


def test_from_env_uses_paper_credentials(monkeypatch) -> None:
    """Rule 5: paper is the default, everywhere. Nothing here goes live."""
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "PKFAKEFAKEFAKEFAKE")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "fake-secret")
    monkeypatch.setenv("ALPACA_LIVE_API_KEY", "AKSHOULDNOTBEUSED")
    monkeypatch.setenv("ALPACA_LIVE_SECRET_KEY", "should-not-be-used")

    broker = AlpacaBroker.from_env()
    assert broker.is_paper is True
    # An equality, not a substring test: `api.alpaca.markets` is a substring
    # of `paper-api.alpaca.markets`, so "the live host is absent" cannot be
    # checked by containment and a test that tried would pass either way.
    assert broker.base_url == f"https://{ALPACA_PAPER_TRADING_HOST}"
    assert ALPACA_LIVE_TRADING_HOST in ALPACA_PAPER_TRADING_HOST, (
        "the substring overlap this test is written around"
    )


def test_from_env_raises_when_the_paper_keys_are_missing(monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_PAPER_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET_KEY", raising=False)
    with pytest.raises(CredentialsError):
        AlpacaBroker.from_env()


def test_the_broker_never_logs_or_prints_a_secret() -> None:
    """Rule 6. ``AlpacaCredentials`` masks itself; the broker must not undo it."""
    credentials = AlpacaCredentials(
        key_id="PKVISIBLEVISIBLE",
        secret_key="the-secret-value",
        trading_base_url=f"https://{ALPACA_PAPER_TRADING_HOST}",
        is_paper=True,
    )
    broker = AlpacaBroker(credentials=credentials)
    rendered = f"{broker!r} {broker!s}"
    assert "the-secret-value" not in rendered
    assert "PKVISIBLEVISIBLE" not in rendered
    assert "paper" in rendered


@pytest.mark.asyncio
async def test_the_broker_shares_the_process_budget_by_default() -> None:
    """A private limiter per component believes it holds 200/min for itself.

    Two such components in one process believe they hold 400/min between them
    against a server-side 200 -- invisible until the 429s start.
    """
    from corollary.ratelimit import default_limiter

    credentials = AlpacaCredentials(
        key_id="PKFAKE",
        secret_key="fake",
        trading_base_url=f"https://{ALPACA_PAPER_TRADING_HOST}",
        is_paper=True,
    )
    broker = AlpacaBroker(credentials=credentials)
    assert broker.limiter is default_limiter()

    own = HostRateLimiter(requests_per_minute=5)
    assert AlpacaBroker(credentials=credentials, limiter=own).limiter is own


@pytest.mark.asyncio
async def test_the_client_is_closed_only_when_the_broker_opened_it(
    make_broker,
) -> None:
    broker, _ = make_broker(single("account"))
    await broker.aclose()
    # The injected client is the test's to close, so a second call is still
    # usable -- this must not have shut the transport down.
    account = await broker.account()
    assert account.status == "ACTIVE"


# --------------------------------------------------------------------------
# What a vendor error body is allowed to write into a log
# --------------------------------------------------------------------------
#
# A 4xx body is interpolated into the exception message, and an exception
# message reaches the logs. Rule 6 is not only about keys in source: *"no keys
# in code, in tests, in fixtures, or in log output"*.
#
# No concrete leak has been constructed -- Alpaca's GET error bodies are
# `{"code": ..., "message": ...}` and carry no credential. This bounds the
# channel rather than patching a known hole, and the reason it is worth
# bounding is that this same change set found an account number in vendor
# **free text**: a `FEE` row's description read "CAT fee for proceed of 15
# trades on 2026-09-10 by PA0EXAMPLE00", where a field-name rule could not see
# it. An error `message` is free text from the same vendor. The number quoted
# there is the placeholder below, not the one that leaked -- rule 6 does not
# stop at the fixtures, and a comment is a worse place to keep an identifier
# than a fixture is, because nothing scans it on the way in.
#
# The error must stay *readable* -- an error nobody can debug is its own
# failure -- so the last test here is as important as the others.

#: An account number's shape, fabricated. Same shape as the real one that
#: leaked, deliberately not the real one: rule 6 covers tests too, and the
#: point of a shape rule is that it needs no real example to fire.
#:
#: The same invented value as ``tests/fixtures/test_record_alpaca.py``'s, on
#: purpose: that module's sweep now reads every ``.py`` and ``.md`` in the tree
#: and excuses a short list of values vouched for as fabricated, so one
#: placeholder repository-wide is one entry on that list rather than two.
FAKE_ACCOUNT_NUMBER = "PA0EXAMPLE00"

#: An OCC symbol that also starts ``PA``. A 404 naming a contract that does
#: not exist is precisely the error you need to be able to read.
PANW_CONTRACT = "PANW251219C00150000"


@pytest.mark.asyncio
async def test_a_short_error_body_is_still_quoted_in_full(make_broker) -> None:
    """The bound must not cost anything on the bodies Alpaca actually sends."""
    body = '{"code": 40010001, "message": "qty must be positive"}'
    broker, _ = make_broker(lambda _request: (422, body))
    with pytest.raises(BrokerError) as raised:
        await broker.account()
    assert "qty must be positive" in str(raised.value)
    assert "40010001" in str(raised.value)


@pytest.mark.asyncio
async def test_an_unbounded_error_body_is_capped_and_says_so(make_broker) -> None:
    """An HTML error page from a proxy is not a reason to write 40kB to a log."""
    body = '{"message": "' + "x" * 40_000 + '"}'
    broker, _ = make_broker(lambda _request: (500, body))
    with pytest.raises(BrokerError) as raised:
        await broker.account()

    message = str(raised.value)
    assert len(message) < 1_000
    # Truncation that does not announce itself is indistinguishable from a
    # vendor that sent exactly that much.
    assert "truncated" in message
    assert "500" in message


@pytest.mark.asyncio
async def test_a_multiline_error_body_becomes_one_line(make_broker) -> None:
    """One record per line, so a stray HTML page cannot fake log records."""
    body = '{\n  "code": 40110000,\n  "message": "nope"\n}'
    broker, _ = make_broker(lambda _request: (500, body))
    with pytest.raises(BrokerError) as raised:
        await broker.account()
    assert "\n" not in str(raised.value)
    assert "nope" in str(raised.value)


@pytest.mark.parametrize("status", [401, 403, 500])
@pytest.mark.asyncio
async def test_an_account_number_in_an_error_body_is_redacted(
    make_broker, status
) -> None:
    """Both call sites, not just the one that happened to be read first."""
    body = f'{{"message": "rejected for account {FAKE_ACCOUNT_NUMBER}"}}'
    broker, _ = make_broker(lambda _request: (status, body))
    with pytest.raises(BrokerError) as raised:
        await broker.account()

    message = str(raised.value)
    assert FAKE_ACCOUNT_NUMBER not in message
    # Redacted, not deleted: the sentence still says what happened.
    assert "rejected for account" in message


@pytest.mark.asyncio
async def test_a_truncated_body_cannot_leave_half_an_identifier(
    make_broker,
) -> None:
    """Redact first, cap second. The other order cuts an id in half, and keeps it.

    The body is built so the account number **straddles** the cap: it begins
    six characters short of it, so truncating first would drop the tail and
    leave ``PAFAKE`` in the log -- a partial identifier, which is worth no
    less to whoever is reading than a whole one.

    It sits after a space because that is how an identifier appears in prose
    ("... trades on 2026-09-10 by PA..."), and because the redaction wants a
    word boundary rather than matching a twelve-character tail inside some
    longer run of characters that is not an account number at all.
    """
    prefix = '{"message": "rejected for account '
    filler = "x" * (ERROR_BODY_MAX - 6 - len(prefix) - 1) + " "
    body = f'{prefix}{filler}{FAKE_ACCOUNT_NUMBER} while placing nothing"}}'
    assert body.index(FAKE_ACCOUNT_NUMBER) == ERROR_BODY_MAX - 6, "straddles the cap"

    broker, _ = make_broker(lambda _request: (400, body))
    with pytest.raises(BrokerError) as raised:
        await broker.account()

    assert FAKE_ACCOUNT_NUMBER[:6] not in str(raised.value)


#: Forty characters, the length Alpaca issues, and obviously not one of them.
#: The length is why it is stated rather than shortened: a real secret fits
#: inside :data:`ERROR_BODY_MAX` five times over, so the bound on an error
#: body is no protection at all against one arriving in it.
FAKE_SECRET_KEY = "NOTAREALSECRETnotarealsecret000000000000"

#: The pair the echo test authenticates with. ``conftest``'s stand-in secret is
#: sixteen characters; this one is the real width, because the claim under
#: test is about what fits inside the bound.
ECHOING_CREDENTIALS = AlpacaCredentials(
    key_id="PKTESTTESTTESTTEST",
    secret_key=FAKE_SECRET_KEY,
    trading_base_url=f"https://{ALPACA_PAPER_TRADING_HOST}",
    is_paper=True,
)


@pytest.mark.asyncio
async def test_the_key_pair_is_redacted_if_a_body_ever_echoes_it(
    make_broker,
) -> None:
    """Rule 6, **both halves**. Alpaca echoes neither; that is not a guarantee.

    The key id alone used to go into the redaction set, on the stated grounds
    that it was "the one piece of credential material this object holds". It
    was not: ``headers()`` sends ``APCA-API-SECRET-KEY`` on every request, so
    anything in front of Alpaca that echoes request headers into an error page
    -- a WAF, a corporate proxy, a future Alpaca error shape -- puts the
    credential that actually *authenticates* into a body this code then
    interpolates into an exception message, and an exception message is log
    output. Of the two, the secret is the one that must not survive.
    """
    body = (
        '{"message": "blocked at the gateway; request headers were '
        f'APCA-API-KEY-ID: {ECHOING_CREDENTIALS.key_id}, '
        f'APCA-API-SECRET-KEY: {FAKE_SECRET_KEY}"}}'
    )
    assert len(body) <= ERROR_BODY_MAX, (
        "the whole body sits inside the bound, so truncation cannot be what "
        "removes anything below"
    )

    broker, _ = make_broker(
        lambda _request: (401, body), credentials=ECHOING_CREDENTIALS
    )
    with pytest.raises(BrokerAuthError) as raised:
        await broker.account()

    message = str(raised.value)
    assert FAKE_SECRET_KEY not in message
    assert ECHOING_CREDENTIALS.key_id not in message
    # Two substitutions, not a dropped body: an error nobody can read is its
    # own failure. ``<redacted>`` is the module's ``_REDACTED``.
    assert message.count("<redacted>") == 2
    assert "blocked at the gateway" in message


@pytest.mark.asyncio
async def test_an_option_symbol_survives_the_redaction(make_broker) -> None:
    """The bound is not allowed to eat the error.

    ``PANW251219C00150000`` starts with the same two letters as a paper
    account number and is the single most useful token in a 404 about a
    contract. A redaction broad enough to take it is a redaction that turns
    every debuggable 4xx into "something was rejected".
    """
    body = f'{{"code": 40410000, "message": "contract {PANW_CONTRACT} not found"}}'
    broker, _ = make_broker(lambda _request: (404, body))
    with pytest.raises(BrokerError) as raised:
        await broker.account()
    assert PANW_CONTRACT in str(raised.value)
