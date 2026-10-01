"""OpenFIGI over REST -- ISIN to FIGI and ticker, for the N-PORT lines with no CUSIP.

Phase 3 spec Q17 chose OpenFIGI as the ``IsinResolver`` for the 29 holdings
the SPDR funds' N-PORT filings identify by ISIN alone. This module is the
entire OpenFIGI surface area: one public method,
:meth:`OpenFigiProvider.map_isins`. It *finds candidates*; it accepts none.
Q17's acceptance rule -- a US-listed equity, exactly one ticker, active in the
broker's asset list -- belongs to the resolver that calls this, on the
response side.

Five things this file is deliberate about
-----------------------------------------

**It holds the one write verb on the vendor surface, and it is exempt by
name (spec Q21).** OpenFIGI's mapping endpoint is POST-only, and rule 1's
structural guard (``tests/test_hard_rules.py``) forbids a non-GET call on the
vendor surface. The owner exempted exactly one call site: this file, a single
``self._client.post`` whose URL is the inline literal
``"https://api.openfigi.com/v3/mapping"`` -- never a name, because a module
global can be rebound at runtime -- and whose keywords are exactly ``json``,
``headers`` and ``follow_redirects=False``, so a redirect can never carry the
key to another host. The client is built here, from a timeout and an optional
test transport, and never injected: an outside client could bring a
``base_url``, default headers or event hooks the call site cannot see. A
second post here -- any URL, including that one -- fails the guard. The
guard's purpose is unchanged: nothing reaches the *broker* with a verb that
changes anything. To keep that true structurally, this file imports nothing
from ``corollary.engine.execution``, from the broker's SDK, or from the
broker's market-data provider, reads no broker variable, and names no broker
host -- and a guard test walks its imports transitively to hold it there.
A mapping job changes nothing at OpenFIGI either: it is a lookup that happens
to be spelled POST.

**The body is ISINs and nothing else.** A JSON list of
``{"idType": "ID_ISIN", "idValue": isin}``. No ``exchCode`` filter: the
acceptance rule filters to US listings on the response side, and asking
OpenFIGI to filter would hide the multi-listing evidence the rule uses to
refuse an ambiguous answer. **A holding's name is never sent** -- the method
takes identifiers only; the name exists for the resolver's log line.

**The key is optional and lives in one header.** ``OPENFIGI_API_KEY`` unset
means the keyless limits (10 jobs a request), not an error; set, it is sent
only as ``X-OPENFIGI-APIKEY`` and raises the batch to 100 jobs. It is never
logged -- httpx's own request line carries the method and URL, not headers --
and every piece of third-party text is scrubbed of it before it reaches an
exception message.

**Metered against the shared limiter, never retried.** ``api.openfigi.com``
has its own bucket in :mod:`corollary.ratelimit`, sized so that no rolling
window exceeds the keyless ceiling -- and so, a fortiori, the keyed one. A
429 is :class:`RateLimitedError` and ends the call; whether to try again is
the caller's decision, on its own schedule.

**Failures carry no httpx exception.** An ``httpx`` exception holds the
request, headers included. Every error here is raised *outside* the
``except`` block that caught the transport failure, so neither ``__cause__``
nor ``__context__`` carries one into a traceback or a log.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx

from corollary.data.providers.interface import ProviderError, RateLimitedError
from corollary.ratelimit import (
    OPENFIGI_BUCKET_REQUESTS_PER_MINUTE,
    OPENFIGI_HOST,
    HostRateLimiter,
    default_limiter,
)
from corollary.wire import WireFormatError, decode_json, vendor_detail

__all__ = [
    "ISIN_PATTERN",
    "KEYED_JOBS_PER_REQUEST",
    "KEYLESS_JOBS_PER_REQUEST",
    "OPENFIGI_API_KEY_ENV",
    "OPENFIGI_KEY_HEADER",
    "FigiRecord",
    "MappingResult",
    "OpenFigiCredentials",
    "OpenFigiError",
    "OpenFigiProvider",
    "mapping_jobs",
    "parse_mapping_response",
    "validate_isin",
]

logger = logging.getLogger(__name__)

#: The environment variable holding the optional key. Named in ``.env.example``.
OPENFIGI_API_KEY_ENV: Final = "OPENFIGI_API_KEY"

#: The header the key travels in, and the only place it travels.
OPENFIGI_KEY_HEADER: Final = "X-OPENFIGI-APIKEY"

#: Documented jobs-per-request caps.
KEYLESS_JOBS_PER_REQUEST: Final = 10
KEYED_JOBS_PER_REQUEST: Final = 100

#: The one identifier type this module asks about.
ID_TYPE_ISIN: Final = "ID_ISIN"

#: An ISIN's shape: two-letter country, nine alphanumerics, one check digit.
#: ``re.ASCII`` so a full-width digit or letter is not a match. The check
#: digit is not verified -- shape only; OpenFIGI's ``error`` answers the rest.
ISIN_PATTERN: Final = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", re.ASCII)

#: The record fields kept from a ``data`` entry: OpenFIGI's name -> ours.
_RECORD_FIELDS: Final = (
    ("figi", "figi"),
    ("ticker", "ticker"),
    ("exchCode", "exch_code"),
    ("marketSector", "market_sector"),
    ("securityType", "security_type"),
    ("securityType2", "security_type2"),
    ("compositeFIGI", "composite_figi"),
    ("shareClassFIGI", "share_class_figi"),
    ("name", "name"),
    ("securityDescription", "security_description"),
)

#: The three mutually exclusive keys a per-job result may carry.
_RESULT_KEYS: Final = frozenset({"data", "warning", "error"})


class OpenFigiError(ProviderError):
    """OpenFIGI could not answer, or answered with something that is not a mapping."""


@dataclass(frozen=True, slots=True)
class OpenFigiCredentials:
    """The optional API key. ``None`` means keyless.

    ``__repr__`` is overridden because a dataclass repr would put the key into
    any traceback holding a provider. Rule 6.
    """

    api_key: str | None = None

    def __repr__(self) -> str:
        state = "<hidden>" if self.api_key else "None"
        return f"OpenFigiCredentials(api_key={state})"

    __str__ = __repr__

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "OpenFigiCredentials":
        """Read the optional key. Unset or blank is keyless, never an error."""
        import os

        source: Mapping[str, str] = os.environ if env is None else env
        api_key = (source.get(OPENFIGI_API_KEY_ENV) or "").strip()
        return cls(api_key=api_key or None)


@dataclass(frozen=True, slots=True)
class FigiRecord:
    """One candidate instrument from a ``data`` entry, as OpenFIGI described it."""

    figi: str | None
    ticker: str | None
    exch_code: str | None
    market_sector: str | None
    security_type: str | None
    security_type2: str | None
    composite_figi: str | None
    share_class_figi: str | None
    name: str | None
    security_description: str | None


@dataclass(frozen=True, slots=True)
class MappingResult:
    """What OpenFIGI said about one ISIN.

    Exactly one of three: ``records`` (the candidates, every one kept, in
    OpenFIGI's order), ``warning`` (no match -- ``"No identifier found."``),
    or ``error`` (the job itself was refused). Several candidates are not an
    answer; deciding is the acceptance rule's job, not this type's.
    """

    isin: str
    records: tuple[FigiRecord, ...] = ()
    warning: str | None = None
    error: str | None = None

    @property
    def matched(self) -> bool:
        return bool(self.records)


def validate_isin(value: object) -> str:
    """``value`` if it is shaped like an ISIN; ``ValueError`` otherwise.

    Exact: no stripping, no upper-casing. A malformed identifier is a bug
    upstream, and quietly repairing it is how a name or a CUSIP ends up sent
    as if it were an ISIN.
    """
    if not isinstance(value, str) or ISIN_PATTERN.fullmatch(value) is None:
        shown = repr(value)[:40]
        raise ValueError(f"{shown} is not an ISIN (2 letters, 9 alphanumerics, 1 digit)")
    return value


def mapping_jobs(isins: Sequence[str]) -> list[dict[str, str]]:
    """The request body for ``isins``: one ISIN job each, and nothing else."""
    return [{"idType": ID_TYPE_ISIN, "idValue": isin} for isin in isins]


def _text_field(value: object, what: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise OpenFigiError(f"{what} is {type(value).__name__}, not a string")


def _record(raw: object, isin: str) -> FigiRecord:
    if not isinstance(raw, Mapping):
        raise OpenFigiError(f"{isin}: a data entry is not an object")
    values = {
        ours: _text_field(raw.get(theirs), f"{isin}: {theirs}")
        for theirs, ours in _RECORD_FIELDS
    }
    return FigiRecord(**values)


def _result(raw: object, isin: str, secrets: Sequence[str]) -> MappingResult:
    if not isinstance(raw, Mapping):
        raise OpenFigiError(f"{isin}: the result is not an object")
    present = _RESULT_KEYS & set(raw)
    if len(present) != 1:
        raise OpenFigiError(
            f"{isin}: expected exactly one of data/warning/error, got {sorted(present)}"
        )
    if "data" in present:
        data = raw["data"]
        if not isinstance(data, list):
            raise OpenFigiError(f"{isin}: data is not a list")
        return MappingResult(isin=isin, records=tuple(_record(r, isin) for r in data))
    if "warning" in present:
        warning = _text_field(raw["warning"], f"{isin}: warning")
        return MappingResult(isin=isin, warning=vendor_detail(warning or "", secrets=secrets))
    error = _text_field(raw["error"], f"{isin}: error")
    return MappingResult(isin=isin, error=vendor_detail(error or "", secrets=secrets))


def parse_mapping_response(
    isins: Sequence[str], payload: Any, *, secrets: Sequence[str] = ()
) -> dict[str, MappingResult]:
    """One batch's reply, aligned to the ISINs that were sent.

    OpenFIGI answers one result per job, in job order. A reply of the wrong
    length, or with any malformed entry, refuses the **whole** batch: a
    half-read reply would be a partial answer presented as a whole one.

    ``warning`` and ``error`` are OpenFIGI's own text, so they are bounded and
    scrubbed of ``secrets`` (the key, when one is held) like any other.
    """
    if not isinstance(payload, list):
        raise OpenFigiError("the response body is not a list")
    if len(payload) != len(isins):
        raise OpenFigiError(
            f"sent {len(isins)} jobs and received {len(payload)} results"
        )
    return {isin: _result(raw, isin, secrets) for isin, raw in zip(isins, payload)}


class OpenFigiProvider:
    """OpenFIGI's mapping endpoint, metered by the shared limiter.

    Construct with :meth:`from_env` in production. Tests pass ``transport=``
    (an ``httpx.MockTransport``) and therefore make **no live calls at all**.

    **The client is always built here, never injected** (spec Q21). Only a
    *transport* comes in from outside, and a transport sees a request after
    it is built; a whole client could carry a ``base_url``, default headers,
    event hooks, auth or redirect-following that the exempt call site cannot
    see. ``tests/test_hard_rules.py`` holds this construction to
    ``httpx.AsyncClient(timeout=..., transport=...)``, bound once.
    """

    def __init__(
        self,
        *,
        credentials: OpenFigiCredentials,
        transport: httpx.AsyncBaseTransport | None = None,
        limiter: HostRateLimiter | None = None,
    ) -> None:
        self._credentials = credentials
        self._client = httpx.AsyncClient(timeout=15.0, transport=transport)
        self._limiter = limiter if limiter is not None else default_limiter()

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, **kwargs: Any
    ) -> "OpenFigiProvider":
        """Build from the process environment. Never raises for a missing key."""
        return cls(credentials=OpenFigiCredentials.from_env(env), **kwargs)

    @property
    def limiter(self) -> HostRateLimiter:
        return self._limiter

    @property
    def jobs_per_request(self) -> int:
        """10 keyless, 100 keyed -- the documented caps."""
        if self._credentials.api_key:
            return KEYED_JOBS_PER_REQUEST
        return KEYLESS_JOBS_PER_REQUEST

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "OpenFigiProvider":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- HTTP

    @property
    def _secrets(self) -> tuple[str, ...]:
        return (self._credentials.api_key,) if self._credentials.api_key else ()

    def _scrub(self, text: str) -> str:
        """Third-party text, bounded and with the key removed, for a message."""
        return vendor_detail(text, secrets=self._secrets)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._credentials.api_key:
            headers[OPENFIGI_KEY_HEADER] = self._credentials.api_key
        return headers

    async def _post_jobs(self, jobs: list[dict[str, str]]) -> Any:
        """One mapping request. **The only write verb in this module** (spec Q21).

        Each failure is raised after the ``try`` has closed, so no ``httpx``
        exception -- request and headers attached -- is chained onto it.
        """
        await self._limiter.acquire(OPENFIGI_HOST)
        response: httpx.Response | None = None
        failure: str | None = None
        try:
            # The URL is spelled inline, and the keywords are exactly these
            # three: rule 1's guard exempts this call by that shape and no
            # other (spec Q21). Never follow a redirect -- the key header
            # would travel with it.
            response = await self._client.post(
                "https://api.openfigi.com/v3/mapping",
                json=jobs,
                headers=self._headers(),
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            failure = f"POST /v3/mapping failed ({type(exc).__name__}): {self._scrub(str(exc))}"
        if failure is not None or response is None:
            raise OpenFigiError(failure or "POST /v3/mapping returned no response")

        logger.debug(
            "openfigi mapping batch",
            extra={"jobs": len(jobs), "status": response.status_code},
        )
        if response.status_code == 429:
            raise RateLimitedError(
                "POST /v3/mapping returned 429 despite the local budget of "
                f"{OPENFIGI_BUCKET_REQUESTS_PER_MINUTE}/min. The server window and "
                "the local bucket disagree -- another process may share this "
                "address or key. Not retried."
            )
        if response.status_code != 200:
            raise OpenFigiError(
                f"POST /v3/mapping returned {response.status_code}: "
                f"{self._scrub(response.text)}"
            )
        decoded: Any = None
        undecodable: str | None = None
        try:
            decoded = decode_json(response.text)
        except WireFormatError as exc:
            undecodable = f"POST /v3/mapping: {self._scrub(str(exc))}"
        if undecodable is not None:
            raise OpenFigiError(undecodable)
        return decoded

    # ------------------------------------------------------------ endpoint

    async def map_isins(self, isins: Sequence[str]) -> dict[str, MappingResult]:
        """What OpenFIGI says about each ISIN, keyed in first-seen order.

        Every ISIN is shape-checked before any request, and one bad value
        refuses the whole call. Duplicates are asked once. Batches go out
        sequentially at :attr:`jobs_per_request`, each drawing one token from
        the ``api.openfigi.com`` bucket; any failure ends the call with no
        partial result and no retry.
        """
        if isinstance(isins, str):
            raise TypeError("map_isins takes a sequence of ISINs, not one string")
        unique = list(dict.fromkeys(validate_isin(isin) for isin in isins))
        results: dict[str, MappingResult] = {}
        size = self.jobs_per_request
        for start in range(0, len(unique), size):
            batch = unique[start : start + size]
            payload = await self._post_jobs(mapping_jobs(batch))
            results.update(parse_mapping_response(batch, payload, secrets=self._secrets))
        return results
