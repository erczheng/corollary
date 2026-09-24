"""The watch universe (decisions 12 and 21) -- pure.

    watch universe = Markets universe ∪ open-position underlyings
                     ∪ decision 6's sector leaders ∪ manual watches ∪ MARKET

It decides what the watch tier polls (Finnhub ``/company-news``, one symbol
per request) and which directional labels the self-audit grades uncapped.

**Inputs are plain symbols, never imports.** The Markets universe is passed in
by the route layer (``api/routes/markets.py``'s ``UNIVERSE``) rather than
imported here, so ``corollary/data`` never depends on ``corollary/api``. The
leaders come from the SPDR seed through :func:`leaders_from_seed`, which
answers an empty set *and a flag* when the seed has not been built yet -- a
stated absence, not a silently smaller universe.

**``MARKET`` is a member but not a symbol.** It is the ticker value an article
tagged to nothing is stored under, so it belongs to the graded set; nobody
polls it, so :attr:`WatchUniverse.polled_symbols` leaves it out. It is also
refused as an input symbol and as a manual watch: it is always present already.

**The cap counts the universe before position underlyings** *(assumption:
100)*. That is every symbol with a membership other than
:attr:`Membership.POSITION`, and not ``MARKET``: at launch 26 Markets names ∪
the leaders measured 66 symbols, leaving 34 manual watches. Position
underlyings come and go with trades and must never be what blocks a watch.

**A position-only ticker may be added as a manual watch, and then it counts.**
The manual membership is what keeps it watched after the position closes, so
it is a pre-position member like any other manual watch.

**A Markets or leader member may not be added as a manual watch.** It would
change nothing that is polled or graded, and an audit-log row and a Discord
notice for a no-op are noise in the logs that matter.

**A bad position symbol is skipped and reported, never raised.** Position
underlyings come from the broker, not from this app: an exercised or assigned
adjusted contract can leave the account holding a deliverable such as
``GME.WS`` warrants, which is no equity symbol this app polls. Raising on it
would take the whole universe down -- the watch tier and the self-audit's
grading set with it -- over one row nobody can act on here. So it lands in
:attr:`WatchUniverse.skipped_positions` with the reason, for the caller to
log. The other three sources are this app's own code and database, and a bad
symbol there is still a bug that raises.

**Only manual watches are removable.** Removing a manual watch that is also a
position underlying is allowed; the ticker stays in the universe through the
position until it closes. The route layer additionally checks that an added
ticker is an active US equity in the asset list -- that needs the list, so it
is not decided here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from corollary.data.seeds import (
    EQUITY_SYMBOL_RE,
    LEADERS_PER_FUND,
    SpdrSeed,
    normalize_symbol,
)

__all__ = [
    "MARKET_TICKER",
    "WATCH_UNIVERSE_CAP",
    "Membership",
    "SectorLeaders",
    "WatchChangeCheck",
    "WatchRefusal",
    "SkippedSymbol",
    "WatchUniverse",
    "can_add_manual",
    "can_remove_manual",
    "leaders_from_seed",
    "watch_universe",
]

#: The ticker value for an article tagged to no ticker (``types.ts``).
MARKET_TICKER: Final = "MARKET"

#: The most symbols manual watches may bring the universe to, counted before
#: position underlyings *(assumption)*. Inclusive: the 100th is permitted.
WATCH_UNIVERSE_CAP: Final = 100


class Membership(StrEnum):
    """Why a symbol is in the watch universe. A symbol may have several."""

    MARKETS = "markets"
    SECTOR_LEADER = "sector_leader"
    POSITION = "position"
    MANUAL = "manual"
    MARKET = "market"


class WatchRefusal(StrEnum):
    """Why an add or remove was refused. The route maps these to status codes."""

    INVALID_SYMBOL = "invalid_symbol"
    RESERVED = "reserved"
    ALREADY_MANUAL = "already_manual"
    ALREADY_MEMBER = "already_member"
    CAP_REACHED = "cap_reached"
    NOT_MEMBER = "not_member"
    NOT_MANUAL = "not_manual"


@dataclass(frozen=True, slots=True)
class SectorLeaders:
    """Decision 6's leaders, and whether the seed they come from exists."""

    symbols: frozenset[str]
    #: True when the SPDR seed has not been built: the caller surfaces it.
    seed_missing: bool


def leaders_from_seed(
    seed: SpdrSeed | None, per_fund: int = LEADERS_PER_FUND
) -> SectorLeaders:
    """The seed's top ``per_fund`` holdings of each fund, as one set.

    ``None`` -- the seed not yet built -- gives an empty set with
    ``seed_missing`` set, never an error: the universe still works without
    leaders, and the caller says why it is smaller.
    """
    if seed is None:
        return SectorLeaders(symbols=frozenset(), seed_missing=True)
    symbols: set[str] = set()
    for held in seed.leaders(per_fund=per_fund).values():
        symbols.update(normalize_symbol(symbol) for symbol in held)
    return SectorLeaders(symbols=frozenset(symbols), seed_missing=False)


@dataclass(frozen=True, slots=True, order=True)
class SkippedSymbol:
    """An input symbol left out of the universe, and why. For the caller to log."""

    #: Exactly as it arrived, so a log line shows what the broker sent.
    raw: str
    reason: str


@dataclass(frozen=True, slots=True)
class WatchUniverse:
    """Every member and every reason it is a member. Read-only."""

    #: symbol -> its memberships. ``MARKET`` is keyed like any other member.
    members: Mapping[str, frozenset[Membership]]
    #: Position symbols that could not be members, sorted and de-duplicated.
    #: Empty in the ordinary case; non-empty is something the caller logs.
    skipped_positions: tuple[SkippedSymbol, ...] = ()
    _symbols: tuple[str, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        frozen = MappingProxyType(dict(sorted(self.members.items())))
        object.__setattr__(self, "members", frozen)
        object.__setattr__(self, "_symbols", tuple(frozen))
        object.__setattr__(
            self, "skipped_positions", tuple(sorted(set(self.skipped_positions)))
        )

    def __contains__(self, ticker: object) -> bool:
        return isinstance(ticker, str) and normalize_symbol(ticker) in self.members

    @property
    def symbols(self) -> tuple[str, ...]:
        """Every member, ``MARKET`` included, sorted."""
        return self._symbols

    @property
    def polled_symbols(self) -> tuple[str, ...]:
        """Every member but ``MARKET``: what the watch tier requests, sorted."""
        return tuple(symbol for symbol in self._symbols if symbol != MARKET_TICKER)

    @property
    def manual(self) -> frozenset[str]:
        return frozenset(
            symbol for symbol, reasons in self.members.items() if Membership.MANUAL in reasons
        )

    @property
    def count_before_positions(self) -> int:
        """What the cap counts: members with a reason other than position, less ``MARKET``."""
        return sum(
            1
            for symbol, reasons in self.members.items()
            if symbol != MARKET_TICKER and reasons - {Membership.POSITION}
        )

    def reasons(self, ticker: str) -> frozenset[Membership]:
        """Why ``ticker`` is a member; empty when it is not one."""
        return self.members.get(normalize_symbol(ticker), frozenset())


def _symbol_problem(raw: str, source: Membership) -> str | None:
    """Why ``raw`` cannot be a ``source`` member, or ``None`` when it can."""
    symbol = normalize_symbol(raw)
    if symbol == MARKET_TICKER:
        return f"{MARKET_TICKER} is not a symbol and cannot be a {source} member"
    if not EQUITY_SYMBOL_RE.fullmatch(symbol):
        return f"{raw!r} is not an equity symbol and cannot be a {source} member"
    return None


def watch_universe(
    markets: Iterable[str],
    leaders: Iterable[str],
    positions: Iterable[str],
    manual: Iterable[str],
) -> WatchUniverse:
    """Build the universe from its four symbol sources, plus ``MARKET``.

    ``markets``, ``leaders`` and ``manual`` come from this app's own code or
    database: a malformed symbol or ``MARKET`` in any of them is a bug to
    surface, and raises ``ValueError``. ``positions`` come from the broker, so
    a symbol there that cannot be a member -- a warrant deliverable such as
    ``GME.WS``, or ``MARKET`` -- is **skipped, never raised**, and returned in
    :attr:`WatchUniverse.skipped_positions` with the reason. The caller logs
    each one; the rest of the universe is unaffected.
    """
    members: dict[str, set[Membership]] = {MARKET_TICKER: {Membership.MARKET}}
    skipped: list[SkippedSymbol] = []
    sources: tuple[tuple[Membership, Iterable[str]], ...] = (
        (Membership.MARKETS, markets),
        (Membership.SECTOR_LEADER, leaders),
        (Membership.POSITION, positions),
        (Membership.MANUAL, manual),
    )
    for reason, symbols in sources:
        for raw in symbols:
            problem = _symbol_problem(raw, reason)
            if problem is None:
                members.setdefault(normalize_symbol(raw), set()).add(reason)
            elif reason is Membership.POSITION:
                skipped.append(SkippedSymbol(raw=raw, reason=problem))
            else:
                raise ValueError(problem)
    return WatchUniverse(
        members={symbol: frozenset(reasons) for symbol, reasons in members.items()},
        skipped_positions=tuple(skipped),
    )


@dataclass(frozen=True, slots=True)
class WatchChangeCheck:
    """The answer to "may this ticker be added / removed?"."""

    #: Normalised. The raw input when it could not be normalised to a symbol.
    ticker: str
    refusal: WatchRefusal | None
    #: A sentence a route can return and a log line can carry.
    detail: str

    @property
    def allowed(self) -> bool:
        return self.refusal is None


def _reason_list(reasons: frozenset[Membership]) -> str:
    return ", ".join(sorted(reasons))


def can_add_manual(universe: WatchUniverse, ticker: str) -> WatchChangeCheck:
    """Whether ``ticker`` may be added as a manual watch. See the module docstring."""
    symbol = normalize_symbol(ticker)
    if symbol == MARKET_TICKER:
        return WatchChangeCheck(
            symbol, WatchRefusal.RESERVED, f"{MARKET_TICKER} is always watched and is not a symbol"
        )
    if not EQUITY_SYMBOL_RE.fullmatch(symbol):
        return WatchChangeCheck(
            ticker, WatchRefusal.INVALID_SYMBOL, f"{ticker!r} is not an equity symbol"
        )
    reasons = universe.reasons(symbol)
    if Membership.MANUAL in reasons:
        return WatchChangeCheck(
            symbol, WatchRefusal.ALREADY_MANUAL, f"{symbol} is already a manual watch"
        )
    if reasons - {Membership.POSITION}:
        return WatchChangeCheck(
            symbol,
            WatchRefusal.ALREADY_MEMBER,
            f"{symbol} is already watched ({_reason_list(reasons)}); a manual watch would change nothing",
        )
    count = universe.count_before_positions
    if count + 1 > WATCH_UNIVERSE_CAP:
        return WatchChangeCheck(
            symbol,
            WatchRefusal.CAP_REACHED,
            f"adding {symbol} would bring the watch universe to {count + 1} symbols before "
            f"position underlyings; the ceiling is {WATCH_UNIVERSE_CAP}",
        )
    return WatchChangeCheck(symbol, None, f"{symbol} may be added as a manual watch")


def can_remove_manual(universe: WatchUniverse, ticker: str) -> WatchChangeCheck:
    """Whether ``ticker`` may be removed. Only a manual watch may.

    ``MARKET`` is refused as :attr:`WatchRefusal.RESERVED`, exactly as the add
    path refuses it: it is always watched, and was never a manual watch.
    """
    symbol = normalize_symbol(ticker)
    if symbol == MARKET_TICKER:
        return WatchChangeCheck(
            symbol, WatchRefusal.RESERVED, f"{MARKET_TICKER} is always watched and is not a symbol"
        )
    if not EQUITY_SYMBOL_RE.fullmatch(symbol):
        return WatchChangeCheck(
            ticker, WatchRefusal.INVALID_SYMBOL, f"{ticker!r} is not an equity symbol"
        )
    reasons = universe.reasons(symbol)
    if not reasons:
        return WatchChangeCheck(
            symbol, WatchRefusal.NOT_MEMBER, f"{symbol} is not in the watch universe"
        )
    if Membership.MANUAL not in reasons:
        return WatchChangeCheck(
            symbol,
            WatchRefusal.NOT_MANUAL,
            f"{symbol} is not a manual watch ({_reason_list(reasons)}); only manual watches are removable",
        )
    return WatchChangeCheck(symbol, None, f"{symbol} may be removed from the manual watches")
