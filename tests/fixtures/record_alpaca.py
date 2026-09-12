"""Record real Alpaca responses into ``tests/fixtures/alpaca/``.

Run by hand, never by the test suite::

    uv run python tests/fixtures/record_alpaca.py

**The suite itself makes no live calls.** It replays what this script
captured, through an ``httpx.MockTransport``. That split is the point: real
shapes, deterministic tests, no rate budget spent and no dependence on market
hours.

Run one half or both::

    uv run python tests/fixtures/record_alpaca.py            # everything
    uv run python tests/fixtures/record_alpaca.py trading    # the trading host
    uv run python tests/fixtures/record_alpaca.py market     # data.alpaca.markets

Three safety properties, enforced below rather than merely intended:

* **No credential is ever written.** Every recorded body is scanned for the
  key id and the secret before it is saved; a hit aborts the run without
  writing.
* **No account identifier is written.** ``account_id``, ``account_number`` and
  ``client_order_id`` are redacted outright -- see the scrubber below for why
  the *other* ids are not. Field-level redaction is not enough on its own: the
  account number also turns up in the middle of a ``FEE`` row's
  ``description``, so a second, substring pass blanks it out of free text
  before the scan runs. See :data:`REDACTED_SUBSTRING`.
* **Nothing is placed, cancelled or modified.** GET only, and
  ``test_record_alpaca.py`` asserts this file contains no other verb.

``python-dotenv`` loads ``.env`` at runtime. This script reads it; nothing
under ``corollary/`` does -- the process environment is the engine's input.
"""

import asyncio
import json
import os
import re
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable, Final, Sequence

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from corollary.data.providers.alpaca import (  # noqa: E402
    DATA_BASE_URL,
    AlpacaCredentials,
    FeedConfig,
    FeedConfigError,
)

OUTPUT_DIR = Path(__file__).resolve().parent / "alpaca"

#: An expiry five weeks out, where Alpaca populates IV and greeks on every
#: strike. The near-dated chain only carries them near the money.
GREEKS_EXPIRY = "2026-10-16"

#: The window the adjusted-contract hunt runs over.
ADJUSTED_FROM = "2026-09-01"
ADJUSTED_TO = "2028-12-31"

#: Adjusted contracts had to be hunted for -- none of the liquid names carry
#: one. These two batches are where GME1 and XRX1 turned up.
ADJUSTED_CANDIDATES = (
    ["VMW", "ATVI", "SIRI", "LUMN", "AMC", "GME"],
    ["VTRS", "ZBH", "XRX", "HPE", "CTVA", "FTV"],
)

#: Redacted outright, at any depth. The **account number** is the one
#: genuinely sensitive field on any of these responses, and ``client_order_id``
#: is ours to invent rather than Alpaca's to report, so neither carries
#: information a fixture needs.
_REDACT_KEYS = frozenset({"account_id", "account_number", "client_order_id"})

REDACTED = "00000000-0000-0000-0000-000000000000"

#: Substituted for an identifier found *inside* free text rather than as a
#: field of its own. Readable, because it lands mid-sentence: a fixture
#: reading ``"CAT fee for proceed of 15 trades on 2026-09-10 by <redacted>"``
#: still says what happened, where the UUID-shaped :data:`REDACTED` in that
#: position would read as data.
#:
#: This exists because the field-level rule above is **not sufficient**, which
#: was found the hard way rather than reasoned about: the first trading
#: recording wrote the account number into ``activities_non_trade.json`` eight
#: times, in the ``description`` of every ``FEE`` row, while
#: ``account_number`` itself was dutifully redacted one object away.
REDACTED_SUBSTRING = "<redacted>"

#: Pseudonymised **stably and injectively** rather than redacted, because
#: something now reads them.
#:
#: This whole set used to collapse onto :data:`REDACTED`, on the stated
#: grounds that "nothing in this codebase reads them". Step 4 made that false
#: in three places at once:
#:
#: * The multi-leg join is *two-hop*. A fill's ``order_id`` is the **leg's**
#:   id; the parent ``mleg`` id appears nowhere on it, so the only route to
#:   the parent is the leg-id to parent-id map built from ``legs[]``. Collapse
#:   every id onto one constant and that map is a map from one key to itself,
#:   which groups nothing -- silently, and in a fixture that still replays
#:   green.
#: * The activity ``id`` is composite (``<stamp>::<uuid>``) and is both the
#:   resume cursor for ingestion -- Alpaca's ``page_token`` *is* the last
#:   row's id -- and the unique key on the ``fill`` table. One constant leaves
#:   nothing to be unique on and no cursor to resume from. (Its 17-digit stamp
#:   sorts chronologically; the whole id does **not**, because stamps repeat
#:   within a millisecond and the UUID half then breaks the tie arbitrarily.
#:   Measured, not assumed -- see
#:   ``tests/engine/execution/test_alpaca_activities.py``.)
#: * ``group_id`` is the **only** linkage a non-trade activity gets -- it has
#:   no ``order_id`` at all -- so it is what pairs an ``OPEXC`` with the
#:   ``OPTRD`` carrying the money.
#:
#: ``uuid5`` in a fixed namespace gives both properties the relation needs:
#: the same input always yields the same output (so a re-recording diffs
#: readably) and distinct inputs yield distinct outputs (so a relation stays a
#: relation). It is not the real id, which is the point.
_PSEUDONYM_KEYS = frozenset(
    {
        "id",
        "order_id",
        "asset_id",
        "underlying_asset_id",
        "group_id",
        "replaced_by",
        "replaces",
        # Added 2026-09-11, and it is the same bug as ``group_id`` one field
        # over. A ``FEE`` row's ``execution_id`` is the **UUID half of the
        # paired fill's composite activity id** -- measured, 15 of 15 on this
        # account. Pseudonymising ``id`` while leaving ``execution_id`` raw
        # broke the relation in exactly one direction: the fee kept a real id,
        # the fill got a ``uuid5`` one, and nothing could ever join them again.
        #
        # It cost a real conclusion before it was caught. Step 5 measured "all
        # 19 fees unattributed" off these fixtures and reported the fee chain's
        # second hop as unreachable on this account. That was an artifact of
        # this set, not a fact about Alpaca -- the same ``uuid5`` applied to
        # both sides restores all 15 matches.
        "execution_id",
    }
)

#: Arbitrary and fixed. Fixed is the requirement: changing it re-writes every
#: id in every fixture, which is a large diff and no loss of meaning.
_PSEUDONYM_NAMESPACE = uuid.UUID("1e4f9c62-0a3b-4d5e-9f80-7c6b5a4d3e2f")


def _pseudonym(value: str) -> str:
    """A stable, injective stand-in that keeps the value's *shape*.

    A composite activity id keeps its timestamp half, so it still matches
    ``<stamp>::<uuid>`` and its stamps still order the way the real ones did.
    Everything else is a bare UUID in and a bare UUID out.
    """
    head, separator, tail = value.rpartition("::")
    if separator:
        return f"{head}{separator}{uuid.uuid5(_PSEUDONYM_NAMESPACE, tail)}"
    return str(uuid.uuid5(_PSEUDONYM_NAMESPACE, value))


def _redact_substrings(text: str, identifiers: Sequence[str]) -> str:
    """Blank each identifier wherever it appears inside ``text``.

    Case-insensitive on purpose. The scan in :func:`save` is case-*sensitive*,
    so redacting more broadly than the scan checks means the scan can only
    ever fail on something the redaction genuinely missed.
    """
    for identifier in identifiers:
        if identifier:
            text = re.sub(
                re.escape(identifier), REDACTED_SUBSTRING, text, flags=re.IGNORECASE
            )
    return text


def _scrub_field(key: str, value: Any, identifiers: Sequence[str]) -> Any:
    if isinstance(value, str):
        if key in _REDACT_KEYS:
            return REDACTED
        if key in _PSEUDONYM_KEYS:
            return _pseudonym(value)
    return scrub(value, identifiers)


def scrub(value: Any, identifiers: Sequence[str] = ()) -> Any:
    """Redact, pseudonymise, and blank ``identifiers`` out of every string.

    ``identifiers`` is the caller's own account identifiers, which cannot be
    known from a field name: see :data:`REDACTED_SUBSTRING`. Defaults to
    nothing so the market half, which never sees them, is unchanged.
    """
    if isinstance(value, dict):
        return {
            key: _scrub_field(key, inner, identifiers)
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [scrub(item, identifiers) for item in value]
    if isinstance(value, str) and identifiers:
        return _redact_substrings(value, identifiers)
    return value


#: Wraps a ``Decimal`` on the way through ``json.dumps``, which has no hook
#: for emitting one as a bare JSON number: ``default=`` may only return
#: something *serializable*, so a Decimal comes back quoted. The sentinel is
#: ``\x01``, which cannot occur in a JSON string, so unwrapping it afterwards
#: cannot touch real data.
_NUMBER_SENTINEL = "\x01"
#: Built from the sentinel's *escaped* form: ``json.dumps`` writes a
#: control character as a ``\\u0001`` escape rather than as itself, so a
#: pattern containing the raw byte would never match. Derived from
#: ``json.dumps`` rather than typed out, so the two cannot drift.
_ESCAPED_SENTINEL = re.escape(json.dumps(_NUMBER_SENTINEL)[1:-1])
_SENTINEL_PATTERN = re.compile(
    f'"{_ESCAPED_SENTINEL}(-?[0-9][0-9.eE+-]*){_ESCAPED_SENTINEL}"'
)


def _tag_decimal(value: Any) -> str:
    if isinstance(value, Decimal):
        return f"{_NUMBER_SENTINEL}{value}{_NUMBER_SENTINEL}"
    raise TypeError(f"cannot serialise {type(value).__name__}: {value!r}")


def dumps_exact(payload: Any) -> str:
    """Serialise without any number ever having been a ``float``.

    The recorder used to do ``json.dumps(json.loads(response.text))``, so every
    price in every fixture was Python's shortest round-trip repr of an IEEE
    double rather than Alpaca's own digits. The *values* survive that for
    anything under about 17 significant figures, which is most of them -- but
    IV and greeks arrive with sixteen or seventeen, and the whole reason this
    module's provider owns its JSON decoder is that a double is not where a
    price should live even for one statement.

    So: parsed with ``parse_float=Decimal``, emitted from the Decimal's own
    string. A fixture now carries the digits Alpaca sent.
    """
    return _SENTINEL_PATTERN.sub(
        r"\1", json.dumps(payload, indent=2, sort_keys=True, default=_tag_decimal)
    )


def save(
    name: str,
    payload: Any,
    secrets: tuple[str, ...],
    *,
    identifiers: Sequence[str] = (),
) -> None:
    """Scrub, then scan, then write. **The order is the guarantee.**

    Redaction runs first and the scan runs on the redacted text, so the scan
    is not a substitute for the redaction -- it is the proof that the
    redaction worked. Anything in ``secrets`` that survives
    :func:`scrub` aborts the run with nothing written, which is what happens
    if an identifier turns up somewhere neither the field rule nor the
    substring pass reached.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    text = dumps_exact(scrub(payload, identifiers)) + "\n"
    for secret in secrets:
        if secret and secret in text:
            raise SystemExit(
                f"ABORTED: a credential appeared in {name}. Nothing was written."
            )
    (OUTPUT_DIR / f"{name}.json").write_text(text, encoding="utf-8")
    print(f"  wrote {name}.json ({len(text):,} bytes)")


# --------------------------------------------------------------------------
# Which halves to record
# --------------------------------------------------------------------------

#: The two hosts, under the two names the CLI accepts. Both are recorded when
#: no argument is given, which is what the module docstring promises.
SECTIONS: Final = ("market", "trading")


def parse_sections(argv: Sequence[str]) -> frozenset[str]:
    """Resolve the section arguments, or exit explaining the choices.

    An unrecognised name **exits** rather than being ignored, because being
    ignored is the worst available outcome here: ``tradng`` would match
    neither branch, both would be skipped, and the run would print ``done``
    having written not one file and spent not one request.
    """
    requested = [argument.strip().lower() for argument in argv if argument.strip()]
    if not requested:
        return frozenset(SECTIONS)
    unknown = sorted({name for name in requested if name not in SECTIONS})
    if unknown:
        raise SystemExit(
            "ABORTED before any request: unknown section "
            f"{', '.join(repr(name) for name in unknown)}. "
            f"Accepted: {', '.join(SECTIONS)}, or no argument at all for both."
        )
    return frozenset(requested)


# --------------------------------------------------------------------------
# The trading host
# --------------------------------------------------------------------------

#: One recorded GET. ``(base_url, path, params)`` in, ``{"status_code",
#: "body"}`` out, with the body already parsed at ``parse_float=Decimal``.
Getter = Callable[[str, str, dict[str, Any]], Awaitable[Any]]

#: Alpaca's ceiling on the activities endpoint -- ``maximum: 100`` in the
#: OpenAPI document, and the reason ingestion has to paginate at all.
ACTIVITIES_PAGE_MAX: Final = 100

#: A deliberately tiny page. This account has fewer rows than the ceiling, so
#: recording only at 100 would capture a history that fits in one page and
#: replay a pagination loop that never loops. At two, the recorded pages carry
#: the ``page_token`` values Alpaca actually accepts -- which is the id of the
#: last row of the previous page, not an opaque cursor of its own.
ACTIVITIES_PAGE_TINY: Final = 2

#: Orders: the default is 50 and the ceiling is 500. Stated rather than
#: defaulted -- a silently truncated order history is a two-hop join missing
#: its parents, and it looks exactly like a book with no spreads in it.
ORDERS_LIMIT: Final = 500

#: A month of daily equity. ``1D`` resolution has no window limit, so this is
#: a display choice rather than a constraint.
HISTORY_PERIOD: Final = "1M"
HISTORY_TIMEFRAME: Final = "1D"


def _rows(page: Any) -> list[Any]:
    """The array an activities response *is*. There is no envelope."""
    body = page["body"]
    return body if isinstance(body, list) else []


def _resume_token(page: Any) -> str | None:
    """The ``page_token`` that asks for whatever follows this page.

    Alpaca's own words: *"page_token represents the ID of the last item on
    your current page of results."* There is no separate cursor field, which
    is exactly why the composite activity id has to survive scrubbing -- the
    id **is** the pagination mechanism as well as the unique key on ``fill``.

    Read from the *unscrubbed* body, because this is a live request parameter
    and a pseudonym would 400.
    """
    rows = _rows(page)
    if not rows:
        return None
    last = rows[-1].get("id")
    return None if last is None else str(last)


def _account_identifiers(page: Any) -> tuple[str, ...]:
    """This account's own identifiers, for the substring pass in :func:`save`.

    ``account_number`` is redacted as a *field* already. It also arrives in
    the middle of free text, which a field-name rule cannot see: a live
    ``FEE`` row's ``description`` read *"CAT fee for proceed of 15 trades on
    2026-09-10 by PA0EXAMPLE00"*, so the one genuinely sensitive value on any
    of these responses was written into a fixture eight times, in prose, while
    the field one object away was correctly blanked. The number in that quote
    is a placeholder: a docstring explaining a redaction is not exempt from it,
    and this one carried the real value for a while, where none of the three
    guards below could see it.

    The account UUID is included for the same reason rather than because it
    was observed leaking -- it is the sibling identifier, and the cost of
    covering it is nothing.
    """
    body = page["body"]
    if not isinstance(body, dict):
        return ()
    found = [
        str(body[key]).strip()
        for key in ("account_number", "id")
        if isinstance(body.get(key), str) and str(body[key]).strip()
    ]
    print(f"  guarding {len(found)} account identifier(s) as substrings too")
    return tuple(found)


def _report_account(page: Any) -> None:
    """Print the facts about this account that must never be hardcoded.

    ``multiplier`` is the one that matters: PRD §8.6 asserts *"Paper is a
    margin account at 2x cash"*, and this account has reported ``'4'`` -- a
    PDT margin account, wrong in the direction that **overstates** capacity.
    So the recorder says what it found rather than confirming what was
    assumed. Same for the options level, which CLAUDE.md pins at 3 in prose.

    No key material is printed. These are balances and levels, and they are
    already on their way into a fixture.
    """
    body = page["body"]
    if not isinstance(body, dict):
        print(f"  ! account came back as {type(body).__name__}, not an object")
        return
    for field in (
        "status",
        "multiplier",
        "options_approved_level",
        "options_trading_level",
        "cash",
        "equity",
        "last_equity",
        "options_buying_power",
        "position_market_value",
    ):
        value = body.get(field, "<absent>")
        print(f"    {field} = {value!r} ({type(value).__name__})")
    for absent in ("pending_transfer_in", "pending_transfer_out"):
        if absent in body:
            print(f"    ! {absent} IS present -- the spec says it is not")


async def record_trading(
    get: Getter, credentials: AlpacaCredentials, secrets: tuple[str, ...]
) -> None:
    """Record the trading host into ``tests/fixtures/alpaca/``.

    Account, positions, orders and activities -- the four things
    ``BrokerAccount`` reads -- plus the portfolio history behind the equity
    curve. Every call is a GET; there is no order path in this phase at all,
    and ``test_record_alpaca.py`` asserts this file contains no other verb.
    """
    base = credentials.trading_base_url

    print("\nrecording the trading host:")
    # The account is fetched first because everything recorded after it is
    # guarded with the identifiers it carries -- see `_account_identifiers`.
    account = await get(base, "/v2/account", {})
    identifiers = _account_identifiers(account)
    guarded = secrets + identifiers

    def keep(name: str, payload: Any) -> None:
        """One `save`, with this account's identifiers guarded as substrings."""
        save(name, payload, guarded, identifiers=identifiers)

    keep("account", account)
    _report_account(account)

    # Thirteen rows from nine logical positions on the current recording --
    # four verticals contributing two each and five singles contributing one.
    # That ratio is the grouping problem in its plainest form, and it is why
    # step 6 exists. A short leg reports ``qty: "-1"`` *alongside*
    # ``side: "short"``,
    # with ``cost_basis`` and ``market_value`` both negative -- a credit is a
    # liability, and that has to survive into the fixture rather than being
    # tidied up on the way in.
    keep("positions", await get(base, "/v2/positions", {}))

    # ``nested=true`` is not a nicety. A fill carries its **leg's** order id;
    # the parent ``mleg`` id appears nowhere on it, so ``legs[]`` is the only
    # place a leg-id to parent-id map can come from. Without it the join is
    # one-hop and groups nothing -- silently, because on a simple order the
    # leg id and the parent id coincide and every single-leg test still
    # passes.
    keep(
        "orders_nested",
        await get(
            base,
            "/v2/orders",
            {
                "status": "all",
                "nested": "true",
                "limit": ORDERS_LIMIT,
                "direction": "asc",
            },
        ),
    )

    # No flat counterpart is recorded, and that is a finding rather than an
    # omission. The same request was issued three ways -- ``nested=true``,
    # ``nested=false`` and no ``nested`` at all -- and all three came back
    # **byte-identical**: 11 top-level orders, 4 of them ``mleg`` parents
    # carrying their 2 legs each, and no leg ever promoted to the top level.
    # So on this account and asset class Alpaca nests regardless.
    #
    # ``nested=true`` is still sent, because that is what the documentation
    # guarantees and the undocumented default is not ours to depend on. And
    # the negative control the one-hop test needs is built in the test itself,
    # by stripping ``legs[]`` off the recorded parents -- which is clearer
    # than a fixture that claims to be a flat response and is not one.

    # Everything on this account has filled, so this is a genuine empty-list
    # response rather than a hand-written ``[]``. Working Orders renders from
    # it, and an empty state is a state.
    keep(
        "orders_open",
        await get(base, "/v2/orders", {"status": "open", "limit": ORDERS_LIMIT}),
    )

    print("\nrecording activities (two shapes, two branches):")

    # Branch one -- trade activities. A ``FILL`` row carries ``order_id``, an
    # *unsigned* ``qty`` and a separate ``side``, and ``side`` takes three
    # values: ``sell_short`` opens a short, ``sell`` closes a long, and
    # ``buy`` is both BTO and BTC. Two of the four actions are
    # indistinguishable without the join to the order's ``position_intent``.
    fills = await get(
        base,
        "/v2/account/activities",
        {
            "activity_types": "FILL",
            "page_size": ACTIVITIES_PAGE_MAX,
            "direction": "asc",
        },
    )
    keep("activities_fill", fills)

    # Branch two -- non-trade activities. A different object entirely,
    # discriminated on ``activity_type``: no ``order_id`` at all, a *signed*
    # ``qty`` where one is present, and ``group_id`` as its only linkage. The
    # live ``JNLC`` came back with no ``symbol``, no ``qty``, no ``price`` and
    # no ``side``, which is why the branch must be modelled permissively --
    # the published ``NonTradeActivities`` schema is demonstrably incomplete
    # (``description`` appears live and is not in it).
    keep(
        "activities_non_trade",
        await get(
            base,
            "/v2/account/activities",
            {
                "category": "non_trade_activity",
                "page_size": ACTIVITIES_PAGE_MAX,
                "direction": "asc",
            },
        ),
    )

    # Both shapes in one array, which is what an unfiltered ingest sees and
    # the only fixture that can prove the discriminator is consulted at all.
    keep(
        "activities_mixed",
        await get(
            base,
            "/v2/account/activities",
            {"page_size": ACTIVITIES_PAGE_MAX, "direction": "asc"},
        ),
    )

    # Two real pages and a real terminator, at a page size of two. See
    # :data:`ACTIVITIES_PAGE_TINY`.
    page1 = await get(
        base,
        "/v2/account/activities",
        {
            "activity_types": "FILL",
            "page_size": ACTIVITIES_PAGE_TINY,
            "direction": "asc",
        },
    )
    keep("activities_fill_page1", page1)
    keep(
        "activities_fill_page2",
        await get(
            base,
            "/v2/account/activities",
            {
                "activity_types": "FILL",
                "page_size": ACTIVITIES_PAGE_TINY,
                "direction": "asc",
                "page_token": _resume_token(page1),
            },
        ),
    )
    # Past the end. The loop has to stop on an empty array, because this
    # endpoint has no ``next_page_token`` to tell it to.
    keep(
        "activities_end",
        await get(
            base,
            "/v2/account/activities",
            {
                "activity_types": "FILL",
                "page_size": ACTIVITIES_PAGE_TINY,
                "direction": "asc",
                "page_token": _resume_token(fills),
            },
        ),
    )

    print("\nrecording the equity curve:")
    # Money as **bare JSON numbers** here, not strings -- the one endpoint on
    # the trading host where that is true. ``get`` above parses with
    # ``parse_float=Decimal``, which is what keeps the whole curve off
    # doubles; a parser written on the reasonable belief that "the trading API
    # sends strings" would put every point on the Dashboard chart through one.
    keep(
        "portfolio_history",
        await get(
            base,
            "/v2/account/portfolio/history",
            {
                "period": HISTORY_PERIOD,
                "timeframe": HISTORY_TIMEFRAME,
                "cashflow_types": "ALL",
            },
        ),
    )


async def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")

    # Feeds come from the environment, through the same code path the engine
    # uses, and a missing one stops the run.
    #
    # This used to build a `FeedConfig` by hand with `or "indicative"` /
    # `or "sip"` / `or "iex"` fallbacks, on the reasoning that a hand-run
    # recorder is not the engine. True, and still the wrong shape: this was
    # the only place in the repository outside the provider where a feed
    # literal sat next to the name of the variable it was meant to make
    # unnecessary, and three lines are exactly what somebody copies when
    # `from_env` raises on them somewhere it matters. A recorder that cannot
    # find its configuration should say so, not invent it.
    try:
        feeds = FeedConfig.from_env()
    except FeedConfigError as exc:
        raise SystemExit(
            f"ABORTED before any request: {exc}\n\n"
            "Set the three feed variables in .env (the names are in "
            ".env.example) and run again."
        ) from exc
    print(f"recording with: {feeds}")

    sections = parse_sections(sys.argv[1:])
    print(f"recording sections: {', '.join(sorted(sections))}")

    credentials = AlpacaCredentials.paper_from_env()
    secrets = (credentials.key_id, credentials.secret_key)
    print(f"credentials: {credentials}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        headers = credentials.headers()

        async def get(base: str, path: str, params: dict[str, Any]) -> Any:
            response = await client.get(base + path, params=params, headers=headers)
            print(f"  GET {path} -> {response.status_code}")
            return {
                "status_code": response.status_code,
                # `parse_float=Decimal`, exactly as the provider's `_decode`
                # does. `response.json()` here would put every price through a
                # double before it ever reached the file.
                "body": (
                    json.loads(response.text, parse_float=Decimal)
                    if response.text
                    else None
                ),
            }

        if "market" in sections:
            print("\nrecording market data:")
            save(
                "stock_snapshots",
                await get(
                    DATA_BASE_URL,
                    "/v2/stocks/snapshots",
                    {"symbols": "NVDA,SPY,AAPL", "feed": feeds.stock_realtime},
                ),
                secrets,
            )
            save(
                "stock_bars_daily",
                await get(
                    DATA_BASE_URL,
                    "/v2/stocks/bars",
                    {
                        "symbols": "NVDA,SPY",
                        "timeframe": "1Day",
                        "start": "2026-08-03",
                        "end": "2026-08-14",
                        "feed": feeds.stock_historical,
                        "adjustment": "split",
                        "limit": 10000,
                        "sort": "asc",
                    },
                ),
                secrets,
            )

            # Two pages of one chain, so the pagination loop is replayed rather
            # than assumed.
            page1 = await get(
                DATA_BASE_URL,
                "/v1beta1/options/snapshots/NVDA",
                {"feed": feeds.options, "limit": 100, "root_symbol": "NVDA"},
            )
            save("option_chain_nvda_page1", page1, secrets)
            save(
                "option_chain_nvda_page2",
                await get(
                    DATA_BASE_URL,
                    "/v1beta1/options/snapshots/NVDA",
                    {
                        "feed": feeds.options,
                        "limit": 100,
                        "root_symbol": "NVDA",
                        "page_token": (page1["body"] or {}).get("next_page_token"),
                    },
                ),
                secrets,
            )

            # The ground-truth set for derived greeks: at this tenor Alpaca
            # populates IV and greeks on every strike, so ours can be checked
            # against theirs rather than only against a textbook.
            save(
                "option_chain_nvda_dated",
                await get(
                    DATA_BASE_URL,
                    "/v1beta1/options/snapshots/NVDA",
                    {
                        "feed": feeds.options,
                        "limit": 200,
                        "root_symbol": "NVDA",
                        "expiration_date": GREEKS_EXPIRY,
                        "strike_price_gte": 190,
                        "strike_price_lte": 250,
                    },
                ),
                secrets,
            )
            save(
                "stock_snapshot_nvda",
                await get(
                    DATA_BASE_URL,
                    "/v2/stocks/snapshots",
                    {"symbols": "NVDA", "feed": feeds.stock_realtime},
                ),
                secrets,
            )
            save(
                "option_chain_opra_denied",
                await get(
                    DATA_BASE_URL,
                    "/v1beta1/options/snapshots/NVDA",
                    {"feed": "opra", "limit": 10},
                ),
                secrets,
            )
            save(
                "stock_quotes_latest",
                await get(
                    DATA_BASE_URL,
                    "/v2/stocks/quotes/latest",
                    {"symbols": "NVDA,SPY", "feed": feeds.stock_realtime},
                ),
                secrets,
            )
            # Option *bars* are the only historical option data that exists:
            # there is no historical quotes endpoint at all, and trades reach
            # back 7 days. Anything older than a week is bars and nothing else.
            save(
                "option_bars_daily",
                await get(
                    DATA_BASE_URL,
                    "/v1beta1/options/bars",
                    {
                        "symbols": "NVDA261016C00220000",
                        "timeframe": "1Day",
                        "start": "2026-08-03",
                        "end": "2026-09-05",
                        # No `feed` here: this endpoint rejects one with a 400.
                        "limit": 10000,
                        "sort": "asc",
                    },
                ),
                secrets,
            )
            save(
                "option_quotes_latest",
                await get(
                    DATA_BASE_URL,
                    "/v1beta1/options/quotes/latest",
                    {"symbols": "NVDA260911C00205000", "feed": feeds.options},
                ),
                secrets,
            )

            print("\nrecording contract reference data (trading host):")
            save(
                "option_contracts_nvda",
                await get(
                    credentials.trading_base_url,
                    "/v2/options/contracts",
                    {"underlying_symbols": "NVDA", "status": "active", "limit": 100},
                ),
                secrets,
            )

            adjusted: list[Any] = []
            standard: list[Any] = []
            for batch in ADJUSTED_CANDIDATES:
                page = await get(
                    credentials.trading_base_url,
                    "/v2/options/contracts",
                    {
                        "underlying_symbols": ",".join(batch),
                        "status": "active",
                        "expiration_date_gte": ADJUSTED_FROM,
                        "expiration_date_lte": ADJUSTED_TO,
                        "limit": 10000,
                    },
                )
                for contract in (page["body"] or {}).get("option_contracts", []):
                    if contract.get("root_symbol") != contract["underlying_symbol"]:
                        adjusted.append(contract)
                    else:
                        standard.append(contract)

            seen: dict[str, int] = {}
            picked: list[Any] = []
            for contract in sorted(adjusted, key=lambda c: str(c["symbol"])):
                root = str(contract["root_symbol"])
                if seen.get(root, 0) < 3:
                    seen[root] = seen.get(root, 0) + 1
                    picked.append(contract)
            print(
                f"  adjusted found: {len(adjusted)} across roots {sorted(seen)}; "
                f"multipliers seen: {sorted({str(c['multiplier']) for c in adjusted})}"
            )
            save(
                "option_contracts_adjusted",
                {
                    "status_code": 200,
                    "body": {
                        "option_contracts": picked + standard[:6],
                        "next_page_token": None,
                    },
                },
                secrets,
            )

            # The same adjusted contract with its deliverables -- the field that
            # tells the truth when `multiplier` does not.
            save(
                "option_contracts_deliverables",
                await get(
                    credentials.trading_base_url,
                    "/v2/options/contracts",
                    {
                        "underlying_symbols": "GME",
                        "root_symbol": "GME1",
                        "status": "active",
                        "expiration_date_gte": ADJUSTED_FROM,
                        "expiration_date_lte": ADJUSTED_TO,
                        "show_deliverables": "true",
                        "limit": 5,
                    },
                ),
                secrets,
            )

        if "trading" in sections:
            await record_trading(get, credentials, secrets)

    print("\ndone. Fixtures are in tests/fixtures/alpaca/.")


if __name__ == "__main__":
    asyncio.run(main())
