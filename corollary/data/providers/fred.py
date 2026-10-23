"""FRED over REST -- the St. Louis Fed's economic series.

Phase 3 builds this for its macro context (``VIXCLS``, ``BAMLH0A0HYM2``,
release dates) and, per decision 19, uses it first for the risk-free rate:
``DGS3MO``, the 3-month bill, replaces ``rates.FALLBACK_RISK_FREE_RATE``
wherever an observation has been obtained.

**This is the entire FRED surface area**, the way ``data/providers/alpaca.py``
is Alpaca's and ``finnhub.py`` Finnhub's. Two methods:
:meth:`FredProvider.observations` (step 3's series) and
:meth:`FredProvider.release_dates` (Q3's economic calendar, unit 7.2b-R),
which reads ``/fred/releases/dates`` forward with
``include_release_dates_with_no_data=true`` -- dates only, never times.

Four things this file is deliberate about
-----------------------------------------

**The key travels in the query string, so every URL carries it.** FRED has no
header form for its key. Every request URL therefore contains the credential,
and ``httpx`` quotes the request URL in its transport errors. Every piece of
text this module did not compose -- exception messages and response bodies
alike -- goes through :meth:`FredProvider._scrub` before it reaches an
exception message, and this module never logs a request URL at all.

**Values are strings, and parse straight to ``Decimal``.** FRED serves every
observation's ``value`` as a JSON string (``"4.16"``). No float exists on the
path: the body is decoded with :func:`corollary.wire.decode_json` and the
string goes to ``Decimal`` directly.

**``"."`` is not a number -- it is FRED saying "no observation".** A holiday,
or a day the source did not publish, reads ``"value": "."``, and
``Decimal(".")`` raises ``InvalidOperation``. It is recognised *before* any
conversion and becomes ``value=None``; it is not caught after a failed parse,
because a catch there would equally swallow a genuinely malformed value, and
those must be refused (:class:`FredError`), not silently read as a gap.

**httpx's own request log line carries the URL, key included.** httpx logs
``HTTP Request: GET <full URL> ...`` at INFO on its ``httpx`` logger for every
request, so any handler at INFO -- uvicorn's included -- would write the key
into the log. ``tests/data/providers/test_fred_provider.py`` found this on its
first run. :class:`_RedactQueryKey` is installed on that logger for the life
of the provider and rewrites any record carrying the key, the same move
``engine/notify.py`` makes for the Discord webhook URL.

**Metered against the shared limiter.** ``api.stlouisfed.org`` is 120/min in
:mod:`corollary.ratelimit`; a private limiter here would be a second bucket
against one server-side ceiling.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx

from corollary.data.providers.interface import ProviderError, RateLimitedError
from corollary.ratelimit import FRED_HOST, FRED_REQUESTS_PER_MINUTE, HostRateLimiter, default_limiter
from corollary.wire import WireFormatError, as_date, clean_params, decode_json, vendor_detail

__all__ = [
    "FRED_API_KEY_ENV",
    "FRED_BASE_URL",
    "MISSING_VALUE",
    "FredCredentials",
    "FredCredentialsError",
    "FredError",
    "FredObservation",
    "FredProvider",
    "FredReleaseDate",
    "FredReleaseDatesPage",
    "RELEASE_DATES_MAX_PAGES",
    "RELEASE_DATES_PAGE_LIMIT",
    "parse_observations",
    "parse_release_dates",
    "release_dates_params",
]

logger = logging.getLogger(__name__)

#: The environment variable holding the key. Named in ``.env.example``.
FRED_API_KEY_ENV: Final = "FRED_API_KEY"

#: The REST root. ``api.stlouisfed.org`` is the host the rate limiter meters.
FRED_BASE_URL: Final = f"https://{FRED_HOST}/fred"

#: FRED's marker for "no observation on this date". Not a number.
MISSING_VALUE: Final = "."

#: The logger httpx writes every request line to, URL and query included.
_HTTPX_LOGGER: Final = "httpx"

#: ``api_key=<anything>`` in a query string, so a record is scrubbed even if it
#: carries a key other than the one this provider holds (a second process's,
#: or one set after a reload).
_API_KEY_PARAM: Final = re.compile(r"(?i)(api_key=)[^&\s\"'<>]+")

#: How many observations a default request asks for. Ten covers a fortnight of
#: sessions, so a missed day or two of refreshes is back-filled by the next.
DEFAULT_OBSERVATION_LIMIT: Final = 10

#: ``/fred/releases/dates``' documented ``limit`` ceiling: "integer between 1
#: and 1000". (The per-release ``/fred/release/dates`` allows 10000; this is
#: the all-releases endpoint, and its ceiling is the smaller one.)
#: https://fred.stlouisfed.org/docs/api/fred/releases_dates.html
RELEASE_DATES_PAGE_LIMIT: Final = 1000

#: Pages :meth:`FredProvider.release_dates` reads before refusing. A 30-day
#: forward window was 842 rows (one page) when probed on 2026-09-24; ten pages
#: is ten thousand rows, so reaching it means the ``count`` is wrong, not that
#: the window is long, and a loop that trusted it would spend the FRED budget.
RELEASE_DATES_MAX_PAGES: Final = 10

#: Exactly ``YYYY-MM-DD``. ``date.fromisoformat`` alone also accepts
#: ``20261014`` and ``2026-W42-3`` since Python 3.11, which FRED never sends.
_ISO_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")


class FredCredentialsError(RuntimeError):
    """No FRED key in the environment.

    Its own class, not Alpaca's ``CredentialsError``: the vendors fail
    independently, and a caller that degrades FRED to "unavailable" must not
    swallow a missing Alpaca pair by accident.
    """


class FredError(ProviderError):
    """FRED could not answer, or answered with something that is not data."""


@dataclass(frozen=True, slots=True)
class FredCredentials:
    """The API key.

    ``__repr__`` is overridden because a dataclass repr would put the key into
    any traceback holding a provider. Rule 6.
    """

    api_key: str

    def __repr__(self) -> str:
        return "FredCredentials(api_key=<hidden>)"

    __str__ = __repr__

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "FredCredentials":
        import os

        source: Mapping[str, str] = os.environ if env is None else env
        api_key = (source.get(FRED_API_KEY_ENV) or "").strip()
        if not api_key:
            raise FredCredentialsError(
                f"{FRED_API_KEY_ENV} is not set in the environment. FRED is the "
                "risk-free rate's source; without it derived greeks use the "
                "stated default. The name is in .env.example and the value "
                "never appears in code, fixtures or logs."
            )
        return cls(api_key=api_key)


@dataclass(frozen=True, slots=True)
class FredObservation:
    """One observation of one series.

    ``value`` is ``None`` when FRED reported ``"."`` -- no observation on that
    date -- and an exact ``Decimal`` otherwise, in the series' own units
    (``DGS3MO`` is percent).
    """

    series_id: str
    date: date
    value: Decimal | None


def _observation_value(series_id: str, day: date, raw: object) -> Decimal | None:
    """One ``value`` field. The marker is checked **before** any conversion."""
    if not isinstance(raw, str):
        raise FredError(
            f"{series_id} {day.isoformat()}: expected the value as a string, got "
            f"{type(raw).__name__}"
        )
    text = raw.strip()
    if text == MISSING_VALUE:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise FredError(
            f"{series_id} {day.isoformat()}: {text[:40]!r} is neither a number nor "
            f"FRED's missing-observation marker {MISSING_VALUE!r}"
        ) from exc
    if not value.is_finite():
        raise FredError(f"{series_id} {day.isoformat()}: {text[:40]!r} is not finite")
    return value


def parse_observations(series_id: str, payload: Any) -> list[FredObservation]:
    """A ``/fred/series/observations`` body as observations, **newest first**.

    Sorted here rather than trusted to ``sort_order=desc``: the order is ours
    to state. Any malformed row refuses the whole body -- a half-parsed
    response would be a partial answer presented as a whole one.
    """
    if not isinstance(payload, Mapping):
        raise FredError(f"{series_id}: the response body is not an object")
    rows = payload.get("observations")
    if not isinstance(rows, list):
        raise FredError(f"{series_id}: the response carries no observations list")
    observations: list[FredObservation] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise FredError(f"{series_id}: an observation is not an object")
        try:
            day = as_date(row.get("date"))
        except WireFormatError as exc:
            raise FredError(f"{series_id}: {exc}") from exc
        if day is None:
            raise FredError(f"{series_id}: an observation has no date")
        observations.append(
            FredObservation(
                series_id=series_id,
                date=day,
                value=_observation_value(series_id, day, row.get("value")),
            )
        )
    observations.sort(key=lambda o: o.date, reverse=True)
    return observations


@dataclass(frozen=True, slots=True)
class FredReleaseDate:
    """One scheduled (or past) date of one FRED release.

    A **date only**: FRED's release calendar carries no time of day (probed
    2026-09-24, spec *Constraints -> FRED*). The time comes from the committed
    table in ``data/seeds/econ_release_times.csv``, never from here.
    ``release_last_updated`` is deliberately not kept: it is the release's last
    revision stamp, not a scheduled time, and carrying it would invite reading
    it as one.
    """

    release_id: int
    release_name: str
    date: date


@dataclass(frozen=True, slots=True)
class FredReleaseDatesPage:
    """One page of ``/fred/releases/dates``: its rows and the total ``count``."""

    count: int
    rows: tuple[FredReleaseDate, ...]


def release_dates_params(
    start: date, end: date, *, offset: int, limit: int = RELEASE_DATES_PAGE_LIMIT
) -> dict[str, str | int]:
    """The query for one page of release dates in ``[start, end]``, key excluded.

    Public so ``tests/fixtures/record_fred.py`` records with exactly the
    request this module sends. Per
    https://fred.stlouisfed.org/docs/api/fred/releases_dates.html:

    * ``include_release_dates_with_no_data=true`` -- the default ``false``
      "excludes release dates that do not have data", which is every date that
      has not happened yet. A forward calendar is nothing *but* those.
    * ``realtime_start`` / ``realtime_end`` bound the release dates returned.
      Both are sent: the defaults are the first of the current year and
      ``9999-12-31``.
    * ``order_by=release_date``, ``sort_order=asc`` -- stated rather than
      defaulted (the documented ``sort_order`` default is ``desc``), so pages
      are cut from a stable order.
    """
    return {
        "file_type": "json",
        "include_release_dates_with_no_data": "true",
        "realtime_start": start.isoformat(),
        "realtime_end": end.isoformat(),
        "order_by": "release_date",
        "sort_order": "asc",
        "limit": limit,
        "offset": offset,
    }


def _release_date_row(row: object) -> FredReleaseDate:
    if not isinstance(row, Mapping):
        raise FredError("releases/dates: a release date is not an object")
    release_id = row.get("release_id")
    # bool is an int subclass; True is not release 1.
    if isinstance(release_id, bool) or not isinstance(release_id, int):
        raise FredError(
            f"releases/dates: release_id {str(release_id)[:40]!r} is not an integer"
        )
    name = row.get("release_name")
    if not isinstance(name, str) or not name.strip():
        raise FredError(f"releases/dates: release {release_id} has no release_name")
    raw_day = row.get("date")
    if not isinstance(raw_day, str) or not _ISO_DATE.fullmatch(raw_day):
        raise FredError(
            f"releases/dates: release {release_id} date {str(raw_day)[:40]!r} is not YYYY-MM-DD"
        )
    try:
        day = date.fromisoformat(raw_day)
    except ValueError as exc:
        raise FredError(
            f"releases/dates: release {release_id} date {raw_day!r} is not a calendar date"
        ) from exc
    return FredReleaseDate(release_id=release_id, release_name=name.strip(), date=day)


def parse_release_dates(payload: Any) -> FredReleaseDatesPage:
    """One ``/fred/releases/dates`` body. Any malformed row refuses the whole page."""
    if not isinstance(payload, Mapping):
        raise FredError("releases/dates: the response body is not an object")
    rows = payload.get("release_dates")
    if not isinstance(rows, list):
        raise FredError("releases/dates: the response carries no release_dates list")
    count = payload.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise FredError(
            f"releases/dates: count {str(count)[:40]!r} is not a non-negative integer"
        )
    return FredReleaseDatesPage(count=count, rows=tuple(_release_date_row(r) for r in rows))


def _redact_query_key(text: str, secrets: Sequence[str]) -> str:
    """``text`` with the key and any ``api_key=`` value replaced. Unbounded."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return _API_KEY_PARAM.sub(lambda m: m.group(1) + "<redacted>", text)


class _RedactQueryKey(logging.Filter):
    """Scrubs the key out of httpx's own request log line.

    A filter on the ``httpx`` logger runs before any handler, wherever the
    record propagates to, so the record itself is rewritten: the message is
    rendered, scrubbed, and its arguments dropped. Records carrying no key
    pass unchanged.
    """

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # pragma: no cover - a malformed record upstream
            return True
        scrubbed = _redact_query_key(rendered, self._secrets)
        if scrubbed != rendered:
            record.msg = scrubbed
            record.args = ()
        return True


class FredProvider:
    """FRED series over REST, with a per-host request budget.

    Construct with :meth:`from_env` in production. The explicit constructor
    exists so tests can inject an ``httpx.MockTransport`` client and therefore
    make **no live calls at all**.
    """

    def __init__(
        self,
        *,
        credentials: FredCredentials,
        client: httpx.AsyncClient | None = None,
        limiter: HostRateLimiter | None = None,
        base_url: str = FRED_BASE_URL,
    ) -> None:
        self._credentials = credentials
        self._client = client if client is not None else httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None
        self._limiter = limiter if limiter is not None else default_limiter()
        self._base_url = base_url.rstrip("/")
        # Installed before the first request can be made, removed on close.
        self._log_filter = _RedactQueryKey((credentials.api_key,))
        logging.getLogger(_HTTPX_LOGGER).addFilter(self._log_filter)

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, **kwargs: Any
    ) -> "FredProvider":
        """Build from the process environment. Raises :class:`FredCredentialsError`."""
        return cls(credentials=FredCredentials.from_env(env), **kwargs)

    @property
    def limiter(self) -> HostRateLimiter:
        return self._limiter

    @property
    def secrets(self) -> Sequence[str]:
        """What must never appear in text leaving this provider."""
        return (self._credentials.api_key,)

    async def aclose(self) -> None:
        """Close the transport if we opened it; the log filter goes last."""
        try:
            if self._owns_client:
                await self._client.aclose()
        finally:
            logging.getLogger(_HTTPX_LOGGER).removeFilter(self._log_filter)

    async def __aenter__(self) -> "FredProvider":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- HTTP

    def _scrub(self, text: str) -> str:
        """Third-party text, bounded and de-identified, for a message.

        The key is in every request URL, so this is not the exotic case it is
        for a header-authenticated vendor: an ``httpx`` transport error names
        the URL it failed on, key and all. **Every** quotation of text this
        module did not compose goes through here.
        """
        return vendor_detail(text, secrets=self.secrets, patterns=(_API_KEY_PARAM,))

    async def _get(self, path: str, params: Mapping[str, Any]) -> Any:
        await self._limiter.acquire(FRED_HOST)
        query = clean_params({**params, "api_key": self._credentials.api_key})
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                params=query,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise FredError(
                f"GET {path} failed ({type(exc).__name__}): {self._scrub(str(exc))}"
            ) from exc

        if response.status_code == 429:
            raise RateLimitedError(
                f"GET {path} returned 429 despite the local budget of "
                f"{FRED_REQUESTS_PER_MINUTE}/min. The server window and the local "
                "bucket disagree -- another process may be sharing this key."
            )
        if response.status_code in (400, 401, 403):
            # FRED answers a bad key with a 400, not a 401.
            raise FredError(
                f"GET {path} returned {response.status_code}: "
                f"{self._scrub(response.text)}. If the key was refused, check "
                f"{FRED_API_KEY_ENV}, not the code."
            )
        if response.status_code >= 400:
            raise FredError(
                f"GET {path} returned {response.status_code}: "
                f"{self._scrub(response.text)}"
            )
        try:
            return decode_json(response.text)
        except WireFormatError as exc:
            raise FredError(f"GET {path}: {self._scrub(str(exc))}") from exc

    # ------------------------------------------------------------ endpoints

    async def observations(
        self, series_id: str, *, limit: int = DEFAULT_OBSERVATION_LIMIT
    ) -> list[FredObservation]:
        """The ``limit`` most recent observations of ``series_id``, newest first.

        Missing observations (FRED's ``"."``) are included with
        ``value=None``: whether to store a gap is the caller's decision.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        payload = await self._get(
            "/series/observations",
            {
                "series_id": series_id,
                "file_type": "json",
                "sort_order": "desc",
                "limit": limit,
            },
        )
        return parse_observations(series_id, payload)

    async def release_dates(
        self, start: date, end: date, *, page_limit: int = RELEASE_DATES_PAGE_LIMIT
    ) -> list[FredReleaseDate]:
        """Every release date of every FRED release in ``[start, end]``, scheduled ones included.

        One request per page of ``page_limit`` rows (each metered against the
        FRED bucket), read by ``offset`` until the body's ``count`` is reached.
        Sorted ``(date, release_id)`` here rather than trusted to the wire.

        Refused (:class:`FredError`), never returned short: a page that comes
        back empty before ``count`` is reached, and a ``count`` that would take
        more than :data:`RELEASE_DATES_MAX_PAGES` pages. A partial calendar
        presented as a whole one would hide a release -- and since unit
        7.2c-1 a complete fetch is what lets the store *withdraw* rows the
        fetch no longer lists, so a short read would delete real events.

        ``count`` is checked, not trusted (unit 7.2c-1). Offset paging over a
        list that changes between requests repeats or skips rows while the
        total still reaches ``count``. So the read is refused when a later
        page states a different ``count`` from the first, and unless **both**
        the rows read and the distinct ``(release_id, date)`` pairs among them
        number exactly ``count`` -- which refuses a repeated row, within a page
        or across two, and more rows than stated, including a repeat that a
        surplus row would otherwise pad back up to the count.

        **What this cannot catch.** A removal and an insertion between two
        page requests leave ``count`` unchanged, and can shift a row across
        the page boundary so that it is never read while every row that *is*
        read is distinct. Nothing in the body distinguishes that from a clean
        read. Repeats are caught; this kind of skip is not. In practice one
        page covers today's window -- 842 rows against a page of 1,000 when
        probed -- so there is no boundary for a row to slip across.
        """
        if end < start:
            raise ValueError(f"end {end.isoformat()} is before start {start.isoformat()}")
        if not 1 <= page_limit <= RELEASE_DATES_PAGE_LIMIT:
            raise ValueError(
                f"page_limit must be 1..{RELEASE_DATES_PAGE_LIMIT}, got {page_limit}"
            )
        rows: list[FredReleaseDate] = []
        stated: int | None = None
        for page_number in range(RELEASE_DATES_MAX_PAGES):
            payload = await self._get(
                "/releases/dates",
                release_dates_params(start, end, offset=len(rows), limit=page_limit),
            )
            page = parse_release_dates(payload)
            if stated is None:
                stated = page.count
            elif page.count != stated:
                raise FredError(
                    f"releases/dates: page {page_number + 1} states a count of "
                    f"{page.count}, page 1 stated {stated}; the calendar changed while "
                    "it was being paged, so offsets no longer line up"
                )
            if len(rows) + len(page.rows) >= page.count:
                rows.extend(page.rows)
                distinct = len({(r.release_id, r.date) for r in rows})
                if len(rows) != page.count or distinct != page.count:
                    raise FredError(
                        f"releases/dates: read {len(rows)} rows, {distinct} distinct "
                        f"(release_id, date) pairs, against a stated count of "
                        f"{page.count}; refused rather than presented as the whole calendar"
                    )
                rows.sort(key=lambda r: (r.date, r.release_id))
                return rows
            if not page.rows:
                raise FredError(
                    f"releases/dates: page {page_number + 1} was empty at offset "
                    f"{len(rows)} of a stated count of {page.count}"
                )
            rows.extend(page.rows)
        raise FredError(
            f"releases/dates: {start.isoformat()}..{end.isoformat()} needs more than "
            f"{RELEASE_DATES_MAX_PAGES} pages of {page_limit}; refused rather than "
            "spending the FRED budget on a count that is probably wrong"
        )
