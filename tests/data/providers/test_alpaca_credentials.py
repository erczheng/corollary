"""Credential handling and fixture hygiene. Rule 6, enforced rather than trusted.

CLAUDE.md: *"No keys in code, in tests, in fixtures, or in log output."* These
are synchronous and live in their own module so the package-wide
``pytest.mark.asyncio`` in ``test_alpaca_provider.py`` does not apply to them.
"""

import re

import pytest

from corollary.data.providers.alpaca import AlpacaCredentials, CredentialsError

from .conftest import FIXTURE_DIR, TEST_CREDENTIALS


# --------------------------------------------------------------------------


def test_credentials_never_render_the_secret() -> None:
    credentials = AlpacaCredentials(
        key_id="PKABCDEFGHIJKL",
        secret_key="super-secret-value",
        trading_base_url="https://paper-api.alpaca.markets",
        is_paper=True,
    )
    for rendered in (repr(credentials), str(credentials), f"{credentials}"):
        assert "super-secret-value" not in rendered
        assert "PKABCDEFGHIJKL" not in rendered
        assert "hidden" in rendered


def test_credentials_go_into_headers_and_not_the_query_string(
    make_provider,
) -> None:
    headers = TEST_CREDENTIALS.headers()
    assert headers["APCA-API-KEY-ID"] == TEST_CREDENTIALS.key_id
    assert headers["APCA-API-SECRET-KEY"] == TEST_CREDENTIALS.secret_key


def test_missing_credentials_raise_and_name_both_variables() -> None:
    with pytest.raises(CredentialsError) as caught:
        AlpacaCredentials.paper_from_env({})
    message = str(caught.value)
    assert "ALPACA_PAPER_API_KEY" in message
    assert "ALPACA_PAPER_SECRET_KEY" in message


def test_paper_is_the_default_account() -> None:
    """Rule 5: every cold start comes up in Paper."""
    credentials = AlpacaCredentials.paper_from_env(
        {"ALPACA_PAPER_API_KEY": "PKX", "ALPACA_PAPER_SECRET_KEY": "s"}
    )
    assert credentials.is_paper is True
    assert "paper-api" in credentials.trading_base_url


def test_no_fixture_contains_anything_key_shaped() -> None:
    """Rule 6, enforced over the recorded files rather than trusted."""
    pattern = re.compile(r"\b(PK|AK)[A-Z0-9]{16,}\b")
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert not pattern.search(text), f"{path.name} looks like it holds a key"
        assert "APCA-API" not in text
