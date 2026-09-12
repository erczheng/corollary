"""``corollary.wire.vendor_detail`` -- what a vendor error body may write to a log.

The coercions in this module are exercised end to end by the two vendor
surfaces that use them (``tests/data/providers/`` and
``tests/engine/execution/``). The redaction is not: it exists precisely for a
response neither vendor has ever actually sent, so no recorded fixture can
reach it and no replay test can reach it by accident either. It gets direct
coverage here, at the only place that owns it.

Rule 6 is the whole subject: *"No keys in code, in tests, in fixtures, or in
log output."* An exception message **is** log output -- it reaches the
handler, every traceback holding the object that raised, and rule 9's watchdog
path.
"""

from corollary.wire import ERROR_BODY_MAX, REDACTED, vendor_detail

#: Forty characters, the width Alpaca issues, and obviously not one of them.
#: The width is the point: a real secret fits inside :data:`ERROR_BODY_MAX`
#: five times over, so bounding an error body is no protection at all against
#: a credential arriving inside one. Registered by value in
#: ``tests/fixtures/test_record_alpaca.py``'s ``PLACEHOLDER_IDENTIFIERS``.
FAKE_SECRET_KEY = "WIREnotarealsecretWIREnotarealsecret0000"

#: A paper account number's shape. Deliberately a different invented value
#: from the ones the broker and provider tests use, so a test that passed
#: because some *other* file's constant happened to be redacted would still
#: fail here.
FAKE_ACCOUNT_NUMBER = "PA7WIREFAKE0"


def test_a_secret_in_a_body_is_replaced_rather_than_quoted() -> None:
    """The case the helper exists for, at the level that owns it.

    Alpaca echoes no credential in an error body. Nothing about that is a
    guarantee: anything in front of it that reflects request headers into an
    error page -- a WAF, a corporate proxy, a future error shape -- puts the
    value that actually authenticates into a body the caller then interpolates
    into an exception message.
    """
    body = f'{{"message": "rejected; APCA-API-SECRET-KEY: {FAKE_SECRET_KEY}"}}'
    assert len(body) <= ERROR_BODY_MAX, (
        "the whole body sits inside the bound, so truncation cannot be what "
        "removes anything below"
    )

    detail = vendor_detail(body, secrets=(FAKE_SECRET_KEY,))

    assert FAKE_SECRET_KEY not in detail
    # Replaced, not dropped: an error nobody can read is its own failure.
    assert detail.count(REDACTED) == 1
    assert "rejected" in detail


def test_both_halves_of_a_key_pair_are_replaced() -> None:
    """Two secrets in, two substitutions out -- the id is not the only one sent."""
    key_id = "PKWIREWIREWIREWIRE"
    body = (
        f'{{"message": "headers were APCA-API-KEY-ID: {key_id}, '
        f'APCA-API-SECRET-KEY: {FAKE_SECRET_KEY}"}}'
    )
    detail = vendor_detail(body, secrets=(key_id, FAKE_SECRET_KEY))
    assert key_id not in detail
    assert FAKE_SECRET_KEY not in detail
    assert detail.count(REDACTED) == 2


def test_an_empty_secret_does_not_blank_the_whole_body() -> None:
    """``"".replace`` splices between every character. The guard is load-bearing.

    A caller holding half a credential pair -- a provider built from a
    partially populated environment, say -- would otherwise turn a readable
    error into one ``<redacted>`` per character.
    """
    detail = vendor_detail('{"message": "nope"}', secrets=("", FAKE_SECRET_KEY))
    assert detail == '{"message": "nope"}'


def test_an_account_number_is_replaced_without_being_named() -> None:
    """The shape rule, for an identifier no caller can pass in as a literal.

    An account number is not a credential the caller holds, so it cannot go in
    ``secrets``. It is matched by form instead -- and the form is real: a
    ``FEE`` activity's ``description`` on this host reads *"CAT fee for
    proceed of N trades on <date> by PA..."*, where a rule written about field
    names cannot see it.
    """
    detail = vendor_detail(f'{{"message": "for account {FAKE_ACCOUNT_NUMBER}"}}')
    assert FAKE_ACCOUNT_NUMBER not in detail
    assert REDACTED in detail
    assert "for account" in detail


def test_an_occ_symbol_survives() -> None:
    """The redaction is not allowed to eat the error.

    ``PANW251219C00150000`` opens with the same two letters as a paper account
    number and is the single most useful token in a 404 about a contract. The
    pattern is anchored on the exact twelve-character width for this reason:
    the shortest possible OCC symbol is sixteen characters, so no contract can
    be mistaken for an account number.
    """
    detail = vendor_detail('{"message": "contract PANW251219C00150000 not found"}')
    assert "PANW251219C00150000" in detail
    assert REDACTED not in detail


def test_a_long_body_is_truncated_and_says_so() -> None:
    """An HTML error page from a proxy is not a reason to write 40kB to a log.

    Truncation that does not announce itself is indistinguishable from a
    vendor that sent exactly that much.
    """
    detail = vendor_detail("x" * 40_000)
    assert len(detail) < ERROR_BODY_MAX + 100
    assert "truncated" in detail
    assert "39700" in detail


def test_a_short_body_is_quoted_whole() -> None:
    """The bound is for surprises. A real Alpaca 4xx is tens of characters."""
    body = '{"code": 40110000, "message": "request is not authorized"}'
    assert vendor_detail(body) == body


def test_whitespace_collapses_to_one_line() -> None:
    """One response, one log record -- a stray HTML page cannot fake records."""
    detail = vendor_detail('{\n  "code": 40110000,\n\t"message": "nope"\n}')
    assert "\n" not in detail
    assert "\t" not in detail
    assert "nope" in detail


def test_redaction_runs_before_truncation() -> None:
    """The other order cuts an identifier in half and keeps the half.

    The body is built so the account number **straddles** the cap: it starts
    six characters short of it, so truncating first would drop the tail and
    leave ``PA7WIR`` in the log -- worth no less to whoever is reading it than
    the whole thing.
    """
    prefix = "rejected for account "
    filler = "x" * (ERROR_BODY_MAX - 6 - len(prefix) - 1) + " "
    body = f"{prefix}{filler}{FAKE_ACCOUNT_NUMBER} while placing nothing"
    assert body.index(FAKE_ACCOUNT_NUMBER) == ERROR_BODY_MAX - 6, "straddles the cap"

    detail = vendor_detail(body)
    assert FAKE_ACCOUNT_NUMBER[:6] not in detail


def test_a_straddling_secret_is_redacted_before_the_cap_too() -> None:
    """Same ordering claim, for the half that actually authenticates."""
    prefix = "gateway rejected; APCA-API-SECRET-KEY: "
    filler = "x" * (ERROR_BODY_MAX - 10 - len(prefix) - 1) + " "
    body = f"{prefix}{filler}{FAKE_SECRET_KEY} end"
    assert body.index(FAKE_SECRET_KEY) == ERROR_BODY_MAX - 10, "straddles the cap"

    detail = vendor_detail(body, secrets=(FAKE_SECRET_KEY,))
    assert FAKE_SECRET_KEY[:10] not in detail
    assert REDACTED in detail
