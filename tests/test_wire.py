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

from decimal import Decimal

import msgpack
import pytest

from corollary.wire import (
    ERROR_BODY_MAX,
    REDACTED,
    WireFormatError,
    as_decimal,
    as_int,
    decode_json,
    decode_msgpack,
    operator_text,
    url_secrets,
    vendor_detail,
)

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


# --------------------------------------------------------------------------
# msgpack, which is the option stream's only format
# --------------------------------------------------------------------------


def test_msgpack_floats_never_reach_the_decimal_boundary_as_floats() -> None:
    """A price off the option stream is a ``Decimal``, not a ``float``.

    The option stream is msgpack-only, and msgpack carries a price as an IEEE
    binary float64 -- there is no text to parse the way ``decode_json`` parses
    ``4.15``. So the decoder converts at the boundary, and the rest of the
    codebase never sees the float.
    """
    frame = msgpack.packb([{"T": "q", "bp": 1.24, "ap": 1.34, "bs": 4}])
    assert frame is not None
    decoded = decode_msgpack(frame)
    quote = decoded[0]
    assert isinstance(quote["bp"], Decimal)
    assert quote["bp"] == Decimal("1.24")
    assert quote["ap"] == Decimal("1.34")
    # A count stays a count: msgpack sends it as an integer and it is not money.
    assert quote["bs"] == 4
    assert isinstance(quote["bs"], int)


def test_msgpack_decimals_come_from_the_shortest_repr_not_the_binary_expansion() -> None:
    """``Decimal(1.24)`` is ``1.2399999...``; ``Decimal(str(1.24))`` is ``1.24``.

    The float64 nearest ``1.24`` has exactly one shortest decimal repr that
    round-trips to it, and that repr is the literal the vendor serialised. The
    binary expansion is the same number and the wrong *answer*: it makes every
    price comparison and every log line unreadable.
    """
    frame = msgpack.packb({"bp": 1.24})
    assert frame is not None
    assert decode_msgpack(frame)["bp"] == Decimal("1.24")
    assert Decimal(1.24) != Decimal("1.24")


def test_msgpack_conversion_reaches_nested_lists_and_maps() -> None:
    frame = msgpack.packb({"a": [{"b": [2.5]}]})
    assert frame is not None
    assert decode_msgpack(frame) == {"a": [{"b": [Decimal("2.5")]}]}


def test_msgpack_that_is_not_msgpack_raises_a_wire_error() -> None:
    with pytest.raises(WireFormatError):
        decode_msgpack(b"\xc1not msgpack at all")


# --------------------------------------------------------------------------
# Non-finite numbers, which only the binary path can manufacture
# --------------------------------------------------------------------------
#
# `Decimal('Infinity')` and `Decimal('NaN')` are *successful* conversions of a
# value that is not a number, and what they do downstream diverges by member
# of the same exception family: `int(Decimal('NaN'))` raises `ValueError` and
# `int(Decimal('Infinity'))` raises `OverflowError`. The first was contained
# by every catch on the stream path and the second by none of them, so one
# `{"bs": inf}` frame escaped `_publish`, `_handle`, `run_session` and `run`
# -- all of which catch only `SocketClosed` -- and killed the socket with rule
# 9's close condition never recorded. Refused here instead, at the one place
# a non-finite `Decimal` can be manufactured from a wire value.

#: Every non-finite a float64 can carry. `nan` is in the list because it is
#: the case that *looked* covered, and covering one member of the family is
#: how the other got through.
NON_FINITE = (float("inf"), float("-inf"), float("nan"))


@pytest.mark.parametrize("value", NON_FINITE)
@pytest.mark.parametrize("field", ["bp", "ap", "bs", "as"])
def test_a_non_finite_is_refused_wherever_a_vendor_can_put_one(
    field: str, value: float
) -> None:
    """Prices *and* sizes, which is the half that was missing.

    ``_price_or_none`` refused a non-finite bid; ``bs`` and ``as`` went
    through ``as_int``, where an infinity is an ``OverflowError`` nothing on
    the stream path caught.
    """
    frame = msgpack.packb([{"T": "q", "S": "AAPL241220C00150000", field: value}])
    assert frame is not None
    with pytest.raises(WireFormatError, match="not a finite number"):
        decode_msgpack(frame)


@pytest.mark.parametrize("value", NON_FINITE)
def test_a_non_finite_nested_in_a_frame_is_refused_too(value: float) -> None:
    """The walk is recursive, so the refusal has to be."""
    frame = msgpack.packb({"a": [{"b": [value]}]})
    assert frame is not None
    with pytest.raises(WireFormatError, match="not a finite number"):
        decode_msgpack(frame)


def test_the_refusal_names_where_the_non_finite_was() -> None:
    """Rule 8's standard applied to a decode: the rule, and the inputs."""
    frame = msgpack.packb([{"S": "AAPL241220C00150000", "bs": float("inf")}])
    assert frame is not None
    with pytest.raises(WireFormatError) as caught:
        decode_msgpack(frame)
    assert "bs" in str(caught.value)


def test_no_decoded_frame_can_reach_int_of_infinity() -> None:
    """The escape itself, pinned. ``OverflowError`` is an ``ArithmeticError``.

    The first two lines are the hazard -- an infinity survives the conversion
    to ``Decimal`` and then raises out of the family the stream path did not
    catch. The last is that no frame can hand one to ``as_int`` any more.
    """
    with pytest.raises(OverflowError):
        int(Decimal("Infinity"))
    assert isinstance(Decimal("Infinity"), Decimal)

    frame = msgpack.packb({"S": "AAPL241220C00150000", "bs": float("inf")})
    assert frame is not None
    with pytest.raises(WireFormatError):
        decode_msgpack(frame)


def test_a_finite_frame_still_decodes() -> None:
    """The permit beside the refusal: a guard that refuses everything passes
    every test above and stops the feed working."""
    frame = msgpack.packb([{"T": "q", "bp": 1.24, "bs": 4, "ap": 1.34, "as": 5}])
    assert frame is not None
    quote = decode_msgpack(frame)[0]
    assert quote["bp"] == Decimal("1.24")
    assert as_int(quote["bs"]) == 4


@pytest.mark.parametrize("text", ["Infinity", "-Infinity", "NaN", "nan", "inf"])
def test_a_non_finite_money_string_is_refused_as_well(text: str) -> None:
    """The other door, and it is on the REST path rather than the socket.

    Alpaca returns money as strings, and ``Decimal('Infinity')`` parses from
    one as happily as from a float64. Refusing only at
    :func:`decode_msgpack` would leave ``"avg_entry_price": "NaN"`` able to
    manufacture the same value.
    """
    with pytest.raises(WireFormatError, match="not a finite number"):
        as_decimal(text)


def test_a_non_finite_decimal_is_not_a_count() -> None:
    """``as_int`` answers the family rather than letting it out.

    Reached only by a caller holding a ``Decimal`` from somewhere other than a
    decode; the refusal costs nothing and the ``OverflowError`` cost a socket.
    """
    with pytest.raises(WireFormatError, match="not a finite number"):
        as_int(Decimal("Infinity"))
    with pytest.raises(WireFormatError, match="not a finite number"):
        as_int(Decimal("NaN"))


def test_json_non_finite_arrives_as_a_float_and_is_refused_by_type() -> None:
    """Why fix A is msgpack-specific, stated as a test rather than as prose.

    ``json.loads`` routes ``NaN`` and ``Infinity`` through ``parse_constant``,
    not ``parse_float``, so the JSON path hands back a **float** -- and a
    float at the money boundary is the deliberately-untranslated ``TypeError``
    that every stream catch already contains. The binary path is the only one
    that can manufacture a non-finite ``Decimal``.
    """
    decoded = decode_json('{"qty": NaN, "price": Infinity}')
    assert isinstance(decoded["qty"], float)
    assert isinstance(decoded["price"], float)
    with pytest.raises(TypeError):
        as_decimal(decoded["price"])
    with pytest.raises(TypeError):
        as_int(decoded["qty"])


# --------------------------------------------------------------------------
# operator_text -- the boundary for owner-typed free text (rule 6)
# --------------------------------------------------------------------------
#
# Every value below is an obviously fake dummy. The key-shaped ones are
# assembled from halves so the repository's credential sweep
# (tests/fixtures/test_record_alpaca.py) does not have to excuse them.

#: Deliberately **shorter than 32 characters**, so the long-run shape cannot
#: catch it: redacting it proves the configured-URL derivation, not the shape.
_FAKE_HOOK_TOKEN = "fake-hook-token-xyz0"
_FAKE_HOOK_ID = "123456789012345678"
_FAKE_HOOK = f"https://discord.com/api/webhooks/{_FAKE_HOOK_ID}/{_FAKE_HOOK_TOKEN}"


def test_operator_text_leaves_plain_prose_untouched() -> None:
    reason = "checking the AAPL241220C00150000 fill by hand; back after lunch"
    assert operator_text(reason) == reason


def test_operator_text_keeps_an_occ_symbol_that_starts_like_a_key_id() -> None:
    # AKAM is an optionable root and ``AK`` is a live key-id prefix. Naming the
    # contract is the point of such a reason, so the key-id shape excludes OCC.
    reason = "halting: AKAM251219C00150000 printed at zero"
    assert operator_text(reason) == reason


def test_operator_text_redacts_a_configured_secret() -> None:
    out = operator_text(
        "pasted dummy-secret-xyz by mistake", secrets=("dummy-secret-xyz",)
    )
    assert out == f"pasted {REDACTED} by mistake"


@pytest.mark.parametrize(
    "fragment",
    [
        _FAKE_HOOK,
        f"/api/webhooks/{_FAKE_HOOK_ID}/{_FAKE_HOOK_TOKEN}",
        f"{_FAKE_HOOK_ID}/{_FAKE_HOOK_TOKEN}",
        _FAKE_HOOK_TOKEN,
    ],
)
def test_operator_text_redacts_every_part_of_a_configured_webhook(
    fragment: str,
) -> None:
    out = operator_text(f"see {fragment} now", secrets=(_FAKE_HOOK,))
    assert _FAKE_HOOK_TOKEN not in out
    assert REDACTED in out


@pytest.mark.parametrize(
    "credential",
    [
        # Unconfigured webhook URLs, on every host Discord serves them from.
        "https://discord.com/api/webhooks/1/short",
        "https://canary.discord.com/api/webhooks/1/short",
        "https://ptb.discordapp.com/api/v10/webhooks/1/short",
        # Anthropic-shaped key.
        "sk-ant-api03-filler",
        # Alpaca key-id shape, paper and live.
        "PK" + "PLANTEDPLANTED00",
        "AK" + "PLANTEDPLANTED00",
        # Long unbroken base64/hex-ish runs.
        "0123456789abcdef" * 2,
        "ZmFrZS1ub3Qt" + "YS1zZWNyZXQ+" + "anVzdC1maWxs/ZXI=",
    ],
)
def test_operator_text_redacts_unconfigured_credential_shapes(credential: str) -> None:
    out = operator_text(f"oops {credential} pasted")
    assert out == f"oops {REDACTED} pasted"


def test_operator_text_fits_its_limit_even_when_redaction_lengthens_it() -> None:
    # A three-character secret becomes ten characters, so a 255-character
    # reason grows past the column. The bound holds *after* redaction.
    reason = " ".join(["abc"] * 64)
    assert len(reason) == 255
    out = operator_text(reason, secrets=("abc",), limit=256)
    assert len(out) <= 256
    assert "abc" not in out


def test_url_secrets_splits_a_webhook_into_its_authenticating_parts() -> None:
    parts = url_secrets(_FAKE_HOOK)
    assert parts[0] == _FAKE_HOOK
    assert _FAKE_HOOK_TOKEN in parts
    assert _FAKE_HOOK_ID in parts
    assert url_secrets("") == ()


#: An unbalanced IPv6 bracket: ``urllib.parse.urlsplit`` raises ``ValueError``
#: on it. A dummy value, like every URL in this file.
_MALFORMED_HOOK_TOKEN = "dummy-token-not-real"
_MALFORMED_HOOK = f"https://[::1/api/webhooks/1/{_MALFORMED_HOOK_TOKEN}"


@pytest.mark.risk
def test_url_secrets_never_raises_on_a_malformed_url() -> None:
    """The Discord sink's constructor calls this; a raise aborted the lifespan."""
    parts = url_secrets(_MALFORMED_HOOK)
    assert parts[0] == _MALFORMED_HOOK
    assert _MALFORMED_HOOK_TOKEN in parts
    # Longest first, so the whole value is replaced before any part of it.
    assert list(parts) == sorted(parts, key=len, reverse=True)


@pytest.mark.risk
def test_url_secrets_on_a_malformed_url_splits_query_and_fragment_too() -> None:
    parts = url_secrets(f"{_MALFORMED_HOOK}?wait=true")
    assert _MALFORMED_HOOK_TOKEN in parts


@pytest.mark.risk
def test_operator_text_redacts_a_malformed_webhooks_token_on_its_own() -> None:
    # The token is 20 characters: under the 32-character shape, and the host
    # is not Discord's, so only expansion of the configured value catches it.
    out = operator_text(
        f"halting, see {_MALFORMED_HOOK_TOKEN}", secrets=(_MALFORMED_HOOK,)
    )
    assert _MALFORMED_HOOK_TOKEN not in out
    assert "halting" in out
