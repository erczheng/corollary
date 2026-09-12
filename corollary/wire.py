"""The HTTP response boundary -- exact-``Decimal`` decoding, and what a failed
response may write to a log. Vendor-neutral.

Vendor-neutral on purpose, the same way :mod:`corollary.instruments` and
:mod:`corollary.calendars` are: RFC-3339 timestamps, money-as-a-string and
money-as-a-JSON-number are facts about HTTP APIs rather than about Alpaca, so
this sits outside ``data/providers/`` and the next provider gets it for free.

**Why this module exists at all.** Two files in this codebase talk to a vendor
over HTTP -- ``data/providers/alpaca.py`` for market data and
``engine/execution/alpaca.py`` for the trading API -- and both have to convert
the same three kinds of value:

* a price into an exact :class:`~decimal.Decimal`,
* an RFC-3339 stamp with **nanosecond** precision into an aware UTC
  ``datetime``, which is thirty lines of fiddly truncation that must not exist
  twice,
* a count into an ``int`` where absent stays absent rather than becoming 0.

The alternative was one of them importing the other's privates, which would
have made the market-data provider a dependency of the broker for no reason
either of them chose.

The decisive rule, restated here because this is where it is enforced
--------------------------------------------------------------------

CLAUDE.md: *"Money as ``Decimal``, never ``float``."* :func:`decode_json` is
the only sanctioned entry point for a response body, and ``json.loads(text,
parse_float=Decimal)`` is the whole reason: ``4.15`` becomes
``Decimal('4.15')`` having never existed as an IEEE double. ``httpx``'s
``response.json()`` offers no hook for this, so calling it anywhere on a money
path loses precision before any of our code sees the number.

That matters on **both** hosts, not only the market-data one. The trading API
returns money as strings (``"avg_entry_price": "8.21"``), which is the easy
case -- but ``GET /v2/account/portfolio/history`` returns bare JSON numbers
(``"base_value": 8413.04``, ``"equity": [8425.21, ...]``), so a trading-side
parser that skipped ``parse_float=Decimal`` on the grounds that "the trading
API sends strings" would put the entire equity curve through doubles.

:func:`as_decimal` **raises on a ``float``** rather than converting one. By the
time a ``Decimal`` could be reconstructed from ``4.15`` the precision is
already gone, so a bypassed decoder has to be loud. That raise is the tripwire
for the whole arrangement.

Error types
-----------

Malformed *data* raises :class:`WireFormatError`; a wrong Python *type* raises
``TypeError``, because that means the decoder was bypassed rather than that
the vendor sent something odd. Each caller re-labels the former as its own
vendor-boundary error through :func:`translating`, so ``ProviderError`` keeps
meaning what it means inside the data provider and ``BrokerError`` keeps
meaning what it means inside the broker.

The other half of the boundary: the body of a response that *failed*
---------------------------------------------------------------------

:func:`vendor_detail` is the same boundary seen from the error path. A 4xx
body is still a response body -- it simply does not decode into anything, so
it gets quoted into an exception message instead, and an exception message is
log output. Rule 6 says *"No keys in code, in tests, in fixtures, or in log
output"*, and both vendor files send ``APCA-API-KEY-ID`` and
``APCA-API-SECRET-KEY`` on every request, so anything in front of the vendor
that reflects request headers into an error page writes a credential into a
body one of them then interpolates verbatim.

It lives here rather than in either vendor file for the reason the coercions
do: the market-data provider and the broker need identical treatment, and the
alternative was one of them importing the other's privates. It was in
``engine/execution/alpaca.py`` first, where the provider could not reach it --
and the provider's ``_get`` correspondingly had no redaction and no bound at
all.

**One honest wrinkle in the "vendor-neutral" claim.** ``_ACCOUNT_NUMBER``
matches the shape of an *Alpaca paper* account number. Truncating and stripping
a caller-supplied secret are facts about logging; ``PA`` plus ten characters is
a fact about one vendor. It sits here anyway because both vendor files need
exactly it, and a third module holding one regex is worse than one stated
exception. A second provider widens ``_ACCOUNT_NUMBER`` -- today a single
``re.compile`` at the foot of this module, not a tuple -- into an alternation,
rather than forking the function.
"""

import functools
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Final, TypeVar

__all__ = [
    "ERROR_BODY_MAX",
    "REDACTED",
    "WireFormatError",
    "as_date",
    "as_datetime",
    "as_decimal",
    "as_int",
    "clean_params",
    "decode_json",
    "require_aware",
    "rfc3339",
    "translating",
    "vendor_detail",
]

T = TypeVar("T")


class WireFormatError(ValueError):
    """A wire value was present but is not what it claims to be.

    A ``ValueError`` rather than a ``RuntimeError`` because it describes the
    *value*, not the call. Callers wrap it in their own vendor-boundary error
    via :func:`translating`.
    """


def translating(
    error: type[Exception], coerce: Callable[[Any], T]
) -> Callable[[Any], T]:
    """Wrap a coercion so a :class:`WireFormatError` surfaces as ``error``.

    One line per coercion at each vendor boundary, instead of a ``try`` at
    every call site or a shared error type that would force the two vendor
    surfaces to agree on an exception hierarchy neither of them chose.

    ``TypeError`` is deliberately **not** translated: it means the decoder was
    bypassed and a float reached the money path, which is a programming error
    in this codebase rather than a vendor response worth reporting to a
    caller.
    """

    @functools.wraps(coerce)
    def wrapped(value: Any) -> T:
        try:
            return coerce(value)
        except WireFormatError as exc:
            raise error(str(exc)) from exc

    return wrapped


# --------------------------------------------------------------------------
# What a failed response may write into a log
# --------------------------------------------------------------------------

#: How much of a vendor error body reaches an exception message -- and through
#: it, the logs.
#:
#: Alpaca's GET error bodies are ``{"code": ..., "message": ...}`` and run to
#: tens of characters, so nobody debugging a real 4xx meets this cap: the whole
#: body is quoted, and a test pins that. What it bounds is a *surprising*
#: body -- a proxy's HTML error page, a stack trace, a redirect blob -- which
#: would otherwise be copied verbatim and unbounded into a log line.
#:
#: It is deliberately **not** the protection against a credential. A 40-
#: character secret fits inside this five times over; :func:`vendor_detail`'s
#: substitution pass is what removes one.
ERROR_BODY_MAX: Final = 300

#: What replaces an identifier found in vendor free text.
REDACTED: Final = "<redacted>"

#: The shape of an Alpaca **paper account number**, blanked out of any vendor
#: string either vendor file quotes.
#:
#: No leak has been demonstrated through an error body; Alpaca's carry a code
#: and a message and no credential. This bounds a channel rather than patching
#: a known hole, and the channel is worth bounding because an account number
#: *was* found in this vendor's free text elsewhere on this host: a ``FEE``
#: activity's ``description`` reads *"CAT fee for proceed of N trades on
#: <date> by PA..."*, where a rule written about field names cannot see it. An
#: error ``message`` is free text from the same vendor.
#:
#: Anchored on the exact width -- ``PA`` and ten more characters -- rather than
#: left open-ended, because an OCC symbol can begin ``PA`` too and naming the
#: contract is the entire value of a 404 about one. ``PANW251219C00150000`` is
#: nineteen characters and the shortest possible OCC symbol is sixteen, so no
#: contract can be mistaken for a twelve-character account number.
#:
#: A **live** account number is bare digits and is deliberately not covered: no
#: digit-run rule can tell one from a quantity, a price or an epoch, and a
#: redaction that eats those leaves an error nobody can read, which is its own
#: failure. Rule 5 keeps both vendor surfaces on paper in any case.
_ACCOUNT_NUMBER: Final = re.compile(r"\bPA[0-9A-Z]{10}\b")


def vendor_detail(text: str, *, secrets: Sequence[str] = ()) -> str:
    """A vendor error body, bounded and de-identified, for an exception message.

    Callers pass **both halves of the key pair** in ``secrets``, because both
    are sent: Alpaca's auth is ``APCA-API-KEY-ID`` plus ``APCA-API-SECRET-KEY``
    on every request, and of the two it is the secret that authenticates.

    Whitespace collapses first, so one response stays one log record and a cap
    counted in characters counts content rather than indentation. Redaction
    runs **before** truncation: the other order can cut an identifier in half
    and keep the half, which is worth no less to whoever reads the log.

    An empty string in ``secrets`` is skipped rather than substituted --
    ``"".replace`` splices between every character, so a caller holding half a
    credential pair would otherwise turn a readable error into one
    ``<redacted>`` per character.
    """
    detail = " ".join(text.split())
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, REDACTED)
    detail = _ACCOUNT_NUMBER.sub(REDACTED, detail)
    if len(detail) <= ERROR_BODY_MAX:
        return detail
    return (
        f"{detail[:ERROR_BODY_MAX]}… "
        f"({len(detail) - ERROR_BODY_MAX} characters truncated)"
    )


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------


def decode_json(text: str) -> Any:
    """Parse a response body with every JSON number as an exact ``Decimal``.

    The whole Decimal argument for both vendor files rests on these lines.
    ``json.loads`` builds a ``float`` for ``4.15`` by default and the precision
    is gone before any of our code sees it; ``parse_float=Decimal`` hands back
    ``Decimal('4.15')``, constructed from the literal text.

    Integers keep their ``int`` type -- ``parse_int`` is left alone
    deliberately, because volume, trade counts and epoch stamps are counts,
    not money.
    """
    try:
        return json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise WireFormatError(f"response body is not JSON: {exc}") from exc


def as_decimal(value: Any) -> Decimal | None:
    """A wire value as an exact ``Decimal``, or ``None`` if absent.

    Handles all three shapes a JSON API uses for money: a JSON number already
    decoded to ``Decimal`` by :func:`decode_json`, an integer, and a string.
    **A ``float`` raises** rather than being converted -- reaching this with
    one means the decoder was bypassed, which is the exact silent
    precision-loss this module is arranged to prevent.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; never money
        raise TypeError(f"expected a number, got a bool: {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return Decimal(stripped)
        except ArithmeticError as exc:
            raise WireFormatError(f"{value!r} is not a number") from exc
    if isinstance(value, float):
        raise TypeError(
            f"a float ({value!r}) reached the Decimal boundary. Responses must "
            "be parsed with decode_json(), which uses parse_float=Decimal -- "
            "see corollary.wire's module docstring."
        )
    raise TypeError(f"cannot read {type(value).__name__} ({value!r}) as a number")


def as_int(value: Any) -> int | None:
    """A count, or ``None``. Absent stays absent -- never coerced to zero.

    Open interest is the caller that matters: some contracts genuinely have
    none, and a zero there would be a claim about the *market* -- that nobody
    holds the contract -- where a null is a claim about the *data*.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"expected a count, got a bool: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        return int(stripped) if stripped else None
    raise TypeError(f"cannot read {type(value).__name__} ({value!r}) as a count")


def as_datetime(value: Any) -> datetime:
    """An RFC-3339 timestamp as an aware UTC ``datetime``.

    Alpaca stamps to nanoseconds; Python resolves to microseconds, so the tail
    is truncated rather than rounded. That is lossless for every use here --
    nothing sequences trades by sub-microsecond ties -- and stated so nobody
    later assumes the value round-trips exactly.
    """
    if not isinstance(value, str):
        raise WireFormatError(f"expected an RFC-3339 timestamp, got {value!r}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # fromisoformat accepts at most 6 fractional digits; Alpaca sends 9.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        rest = tail
        for index, char in enumerate(tail):
            if not char.isdigit():
                digits, rest = tail[:index], tail[index:]
                break
        else:
            digits, rest = tail, ""
        text = f"{head}.{digits[:6]:0<6}{rest}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise WireFormatError(f"{value!r} is not an RFC-3339 timestamp") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def as_date(value: Any) -> date | None:
    """A ``YYYY-MM-DD`` date, or ``None`` if absent.

    A **date**, not an instant. CLAUDE.md's frontend note applies to the
    backend too: an option expiry and an activity's settlement date are dates,
    and turning one into a local-midnight datetime puts it a day out on any
    afternoon in New York.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise WireFormatError(f"expected a YYYY-MM-DD date, got {value!r}")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise WireFormatError(f"{value!r} is not a YYYY-MM-DD date") from exc


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


def require_aware(moment: datetime | None, name: str) -> None:
    """Refuse a naive datetime, naming which argument it was.

    Same reason ``db.types.UtcDateTime`` refuses one at the database boundary:
    market data is Eastern, the server clock is whatever the machine says, and
    guessing between them is a silent multi-hour window error that returns
    plausible data.
    """
    if moment is None:
        return
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(
            f"{name} must be timezone-aware; got a naive datetime {moment!r}"
        )


def rfc3339(moment: datetime | None) -> str | None:
    """A UTC RFC-3339 string, or ``None`` to let the vendor default it."""
    if moment is None:
        return None
    require_aware(moment, "start/end")
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def clean_params(params: Mapping[str, Any] | None) -> dict[str, str]:
    """Drop ``None`` values and render the rest as strings.

    ``httpx`` would happily send ``feed=None``; Alpaca would answer 400.
    """
    if not params:
        return {}
    cleaned: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            cleaned[key] = "true" if value else "false"
        elif isinstance(value, (date, datetime)):
            cleaned[key] = value.isoformat()
        else:
            cleaned[key] = str(value)
    return cleaned
