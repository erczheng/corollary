"""The ``no_vendor_network`` guard every API test runs under (``conftest.py``).

A test under ``tests/api`` that resolves a non-loopback host is refused at the
lookup and then **fails**, naming the host. Refusing alone would hide it: the
app catches a vendor's connection error and keeps serving, which is exactly
what made the shipped-scheduler boot test pass for weeks while it talked to
the real Alpaca hosts with dummy keys -- and, on 2026-11-16, hang the gate.

No test here opens a socket to anything. Every lookup a vendor name reaches
is the guard's, and each test checks the guard is the installed
``socket.getaddrinfo`` before it asks for one. The names are under
``.invalid`` (RFC 6761) besides, so even a guard that had been bypassed could
reach no host.
"""

import socket

import httpx
import pytest

from tests.api.conftest import VendorLookupGuard

#: Never resolves anywhere (RFC 6761), so nothing here names a real vendor.
VENDOR = "vendor.invalid"


def test_every_api_test_runs_with_the_guard_installed(
    no_vendor_network: VendorLookupGuard,
) -> None:
    assert socket.getaddrinfo is no_vendor_network


def test_a_vendor_lookup_is_refused_and_recorded(
    no_vendor_network: VendorLookupGuard,
) -> None:
    assert socket.getaddrinfo is no_vendor_network
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo(VENDOR, 443)
    # anyio hands the host over as bytes.
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo(VENDOR.encode("ascii"), 443)
    assert no_vendor_network.refused == [VENDOR, VENDOR]
    no_vendor_network.expect_refused()


@pytest.mark.asyncio
async def test_an_httpx_request_to_a_vendor_fails_at_the_lookup(
    no_vendor_network: VendorLookupGuard,
) -> None:
    """The layer the guard sits at is the one the vendor clients really use.

    ``httpx.AsyncClient`` with its default transport, as every provider in
    ``corollary/data/providers`` builds it: httpcore, anyio, the event loop's
    ``getaddrinfo``, and so the guard. A connect error, at once, with the
    host recorded -- not a socket.
    """
    assert socket.getaddrinfo is no_vendor_network
    async with httpx.AsyncClient(timeout=5.0) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"https://{VENDOR}/v2/account")
    assert no_vendor_network.refused == [VENDOR]
    no_vendor_network.expect_refused()


@pytest.mark.asyncio
async def test_a_configured_proxy_cannot_carry_a_vendor_request_past_the_guard(
    no_vendor_network: VendorLookupGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a proxy configured on loopback, the vendor's name is still looked up.

    Through a proxy, the only lookup would be the proxy's own, which is
    loopback and allowed, and the vendor's name would travel inside the
    CONNECT, unrecorded. The guard's ``NO_PROXY=*`` makes httpx connect
    directly, so the vendor's name meets the guard. Port 9 (discard) on
    loopback: even if proxying were on, nothing would answer there.
    """
    assert socket.getaddrinfo is no_vendor_network
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    async with httpx.AsyncClient(timeout=5.0, trust_env=True) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"https://{VENDOR}/v2/account")
    assert no_vendor_network.refused == [VENDOR]
    no_vendor_network.expect_refused()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", b"127.0.0.1", None])
def test_a_loopback_lookup_passes_through(
    no_vendor_network: VendorLookupGuard, host: str | bytes | None
) -> None:
    assert socket.getaddrinfo is no_vendor_network
    try:
        socket.getaddrinfo(host, 8000, type=socket.SOCK_STREAM)
    except socket.gaierror:
        # A machine without IPv6 has no ``::1``; that is the real resolver's
        # answer, not a refusal -- which is what this test checks.
        pass
    assert no_vendor_network.refused == []


def test_an_unexpected_lookup_fails_the_test_naming_the_host() -> None:
    """The teardown check, on a guard of its own that is never installed."""
    guard = VendorLookupGuard(real=socket.getaddrinfo)
    with pytest.raises(socket.gaierror):
        guard("data.vendor.invalid", 443)
    with pytest.raises(socket.gaierror):
        guard("paper.vendor.invalid", 443)
    failure = guard.failure()
    assert failure is not None
    assert "data.vendor.invalid" in failure
    assert "paper.vendor.invalid" in failure
    assert "expect_refused" in failure


def test_a_test_that_expects_its_refusals_does_not_fail() -> None:
    guard = VendorLookupGuard(real=socket.getaddrinfo)
    with pytest.raises(socket.gaierror):
        guard(VENDOR, 443)
    guard.expect_refused()
    assert guard.failure() is None


def test_a_test_that_looked_nothing_up_does_not_fail() -> None:
    assert VendorLookupGuard(real=socket.getaddrinfo).failure() is None
