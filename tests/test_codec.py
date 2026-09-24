"""``Codec.decode``: a frame is read by its socket's codec, not by its opcode.

A websocket frame's text/binary opcode is transport framing, not a format
declaration. Alpaca's paper trading host sends its JSON ``authorization`` and
``listening`` replies in **binary** frames; the decoder used to dispatch on the
opcode alone, handed those bytes to msgpack (which reads ``{`` as fixint 123
and then fails on "extra data"), dropped them as undecodable, never saw
``listening``, and rule 9's handshake condition halted the engine ~90s after
every start and every resume. Live, paper keys, market open.
"""

from decimal import Decimal

import msgpack
import pytest

from corollary.sockets import JSON_CODEC, MSGPACK_CODEC
from corollary.wire import WireFormatError

AUTHORIZATION_BYTES = (
    b'{"stream":"authorization",'
    b'"data":{"status":"authorized","action":"authenticate"}}'
)
AUTHORIZATION = {
    "stream": "authorization",
    "data": {"status": "authorized", "action": "authenticate"},
}


# --------------------------------------------------------------------------
# JSON codec: binary frames are UTF-8 JSON
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        AUTHORIZATION_BYTES,
        bytearray(AUTHORIZATION_BYTES),
        memoryview(AUTHORIZATION_BYTES),
    ],
    ids=["bytes", "bytearray", "memoryview"],
)
def test_a_binary_frame_on_a_json_socket_is_json(frame: bytes) -> None:
    assert JSON_CODEC.decode(frame) == AUTHORIZATION


def test_a_text_frame_on_a_json_socket_is_still_json() -> None:
    assert JSON_CODEC.decode(AUTHORIZATION_BYTES.decode("utf-8")) == AUTHORIZATION


def test_money_in_a_binary_json_frame_is_an_exact_decimal() -> None:
    decoded = JSON_CODEC.decode(b'{"price":4.15,"qty":"1","count":3}')
    assert decoded["price"] == Decimal("4.15")
    assert type(decoded["price"]) is Decimal
    assert str(decoded["price"]) == "4.15"
    assert type(decoded["count"]) is int


def test_invalid_utf8_on_a_json_socket_raises() -> None:
    with pytest.raises(WireFormatError, match="UTF-8"):
        JSON_CODEC.decode(b'{"stream":"\xff\xfe"}')


def test_non_json_bytes_on_a_json_socket_raise() -> None:
    with pytest.raises(WireFormatError, match="not JSON"):
        JSON_CODEC.decode(b"not json at all")


def test_msgpack_bytes_on_a_json_socket_are_not_guessed_at() -> None:
    """A JSON socket reads JSON. It does not fall back to msgpack either."""
    packed = msgpack.packb({"stream": "listening"}, use_bin_type=True)
    with pytest.raises(WireFormatError):
        JSON_CODEC.decode(packed)


# --------------------------------------------------------------------------
# msgpack codec: unchanged
# --------------------------------------------------------------------------


def test_binary_on_a_msgpack_socket_is_msgpack() -> None:
    packed = msgpack.packb([{"T": "q", "bp": 4.15}], use_bin_type=True)
    decoded = MSGPACK_CODEC.decode(packed)
    assert decoded == [{"T": "q", "bp": Decimal("4.15")}]


def test_text_on_a_msgpack_socket_is_json() -> None:
    """The pre-negotiation error the vendor sends as text, before msgpack."""
    decoded = MSGPACK_CODEC.decode('[{"T":"error","code":400,"msg":"invalid syntax"}]')
    assert decoded == [{"T": "error", "code": 400, "msg": "invalid syntax"}]


def test_json_shaped_bytes_on_a_msgpack_socket_are_not_reinterpreted() -> None:
    """A msgpack socket never guesses JSON from bytes that look like it.

    ``{`` is 0x7b, a valid msgpack positive fixint, so these bytes are a
    msgpack integer followed by trailing data -- an error, and it stays one.
    Sniffing would make a msgpack stream's decoding depend on its content.
    """
    with pytest.raises(WireFormatError, match="not msgpack"):
        MSGPACK_CODEC.decode(AUTHORIZATION_BYTES)


def test_a_lone_brace_byte_on_a_msgpack_socket_is_the_integer_123() -> None:
    assert MSGPACK_CODEC.decode(b"{") == 123
