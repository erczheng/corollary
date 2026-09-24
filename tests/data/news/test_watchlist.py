"""The watch universe (decisions 12 and 21) -- pure.

Every input is synthetic. The Markets universe is passed in as an argument,
exactly as the route layer will pass ``UNIVERSE``; nothing here imports the API.
"""

from datetime import date
from decimal import Decimal

import pytest

from corollary.data.news.watchlist import (
    MARKET_TICKER,
    WATCH_UNIVERSE_CAP,
    Membership,
    SkippedSymbol,
    WatchRefusal,
    can_add_manual,
    can_remove_manual,
    leaders_from_seed,
    watch_universe,
)
from corollary.data.seeds import SPDR_FUNDS, SPDR_SECTORS, SpdrHolding, SpdrSeed

MARKETS = ("AAPL", "MSFT", "NVDA", "SPY")


def synthetic_symbols(count: int, prefix: str = "Q") -> list[str]:
    """``count`` distinct, well-formed equity symbols: QAAA, QAAB, ..."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = []
    for i in range(count):
        a, rest = divmod(i, 26 * 26)
        b, c = divmod(rest, 26)
        out.append(f"{prefix}{letters[a]}{letters[b]}{letters[c]}")
    return out


def seed(per_fund: int = 6) -> SpdrSeed:
    rows = []
    for etf in SPDR_FUNDS:
        for rank in range(per_fund):
            rows.append(
                SpdrHolding(
                    etf=etf,
                    sector=SPDR_SECTORS[etf],
                    symbol=f"{etf}{'ABCDEFGH'[rank]}",
                    weight=Decimal(20 - rank),
                )
            )
    return SpdrSeed(as_of=date(2026, 9, 22), rows=tuple(rows))


# --- the leaders, from an optional seed ---------------------------------------


def test_the_leaders_are_the_seeds_top_five_per_fund() -> None:
    leaders = leaders_from_seed(seed())
    assert leaders.seed_missing is False
    assert len(leaders.symbols) == 5 * len(SPDR_FUNDS)
    assert "XLKA" in leaders.symbols
    assert "XLKE" in leaders.symbols
    assert "XLKF" not in leaders.symbols  # the sixth-heaviest is not a leader


def test_an_absent_seed_gives_no_leaders_and_says_so() -> None:
    leaders = leaders_from_seed(None)
    assert leaders.symbols == frozenset()
    assert leaders.seed_missing is True


# --- the union ----------------------------------------------------------------


def test_the_universe_is_the_union_plus_market() -> None:
    universe = watch_universe(
        markets=MARKETS, leaders=["NVDA", "XOM"], positions=["TSLA"], manual=["PLTR"]
    )
    assert set(universe.symbols) == {"AAPL", "MSFT", "NVDA", "SPY", "XOM", "TSLA", "PLTR", MARKET_TICKER}
    assert MARKET_TICKER in universe


def test_polled_symbols_omit_market() -> None:
    universe = watch_universe(markets=MARKETS, leaders=[], positions=[], manual=[])
    assert MARKET_TICKER not in universe.polled_symbols
    assert set(universe.polled_symbols) == set(MARKETS)


def test_every_member_carries_every_reason_it_is_there() -> None:
    universe = watch_universe(
        markets=MARKETS, leaders=["NVDA"], positions=["NVDA", "TSLA"], manual=["TSLA"]
    )
    assert universe.reasons("NVDA") == frozenset(
        {Membership.MARKETS, Membership.SECTOR_LEADER, Membership.POSITION}
    )
    assert universe.reasons("TSLA") == frozenset({Membership.POSITION, Membership.MANUAL})
    assert universe.reasons(MARKET_TICKER) == frozenset({Membership.MARKET})
    assert universe.reasons("ZZZZ") == frozenset()
    assert universe.manual == frozenset({"TSLA"})


def test_symbols_are_normalised_uppercase_with_a_dotted_class_share() -> None:
    universe = watch_universe(markets=[" aapl "], leaders=["BRK/B"], positions=["brk-b"], manual=[])
    assert "AAPL" in universe
    assert "BRK.B" in universe
    assert universe.reasons("brk.b") == frozenset({Membership.SECTOR_LEADER, Membership.POSITION})


def test_the_symbols_come_out_in_one_order_whatever_the_input_order() -> None:
    a = watch_universe(markets=MARKETS, leaders=["XOM", "CVX"], positions=["TSLA"], manual=["PLTR"])
    b = watch_universe(
        markets=list(reversed(MARKETS)), leaders=["CVX", "XOM"], positions=["TSLA"], manual=["PLTR"]
    )
    assert a == b
    assert a.symbols == b.symbols
    assert list(a.symbols) == sorted(a.symbols)


@pytest.mark.parametrize("bad", ["AAPL1", "NOT A SYMBOL", "", "AAPL241220C00150000"])
@pytest.mark.parametrize("source", ["markets", "leaders", "manual"])
def test_a_malformed_symbol_from_our_own_sources_is_refused(bad: str, source: str) -> None:
    inputs: dict[str, list[str]] = {"markets": [], "leaders": [], "positions": [], "manual": []}
    inputs[source] = [bad]
    with pytest.raises(ValueError):
        watch_universe(**inputs)


@pytest.mark.parametrize("source", ["markets", "leaders", "manual"])
def test_market_is_not_accepted_from_our_own_sources(source: str) -> None:
    inputs: dict[str, list[str]] = {"markets": [], "leaders": [], "positions": [], "manual": []}
    inputs[source] = ["market"]
    with pytest.raises(ValueError, match="MARKET"):
        watch_universe(**inputs)


def test_a_broker_position_symbol_that_is_not_an_equity_is_skipped_and_reported() -> None:
    """A GME1 assignment delivering GME.WS warrants must not take the universe down."""
    universe = watch_universe(
        markets=MARKETS, leaders=["XOM"], positions=["TSLA", "GME.WS", "AAPL1"], manual=["PLTR"]
    )
    assert set(universe.polled_symbols) == set(MARKETS) | {"XOM", "TSLA", "PLTR"}
    assert "GME.WS" not in universe
    assert [item.raw for item in universe.skipped_positions] == ["AAPL1", "GME.WS"]
    assert all("not an equity symbol" in item.reason for item in universe.skipped_positions)
    assert all(isinstance(item, SkippedSymbol) for item in universe.skipped_positions)


def test_market_as_a_position_symbol_is_skipped_not_raised() -> None:
    universe = watch_universe(markets=MARKETS, leaders=[], positions=["market"], manual=[])
    assert universe.reasons(MARKET_TICKER) == frozenset({Membership.MARKET})
    assert [item.raw for item in universe.skipped_positions] == ["market"]
    assert "MARKET" in universe.skipped_positions[0].reason


def test_skipped_positions_are_deterministic_and_deduplicated() -> None:
    a = watch_universe(
        markets=MARKETS, leaders=[], positions=["GME.WS", "CASH_USD", "GME.WS"], manual=[]
    )
    b = watch_universe(markets=MARKETS, leaders=[], positions=["CASH_USD", "GME.WS"], manual=[])
    assert a == b
    assert [item.raw for item in a.skipped_positions] == ["CASH_USD", "GME.WS"]


def test_a_clean_universe_skips_nothing() -> None:
    universe = watch_universe(markets=MARKETS, leaders=[], positions=["TSLA"], manual=[])
    assert universe.skipped_positions == ()


def test_the_universe_is_frozen() -> None:
    universe = watch_universe(markets=MARKETS, leaders=[], positions=[], manual=[])
    with pytest.raises(TypeError):
        universe.members["NEW"] = frozenset({Membership.MANUAL})  # type: ignore[index]


# --- the cap counts the universe before position underlyings ------------------


def test_the_cap_is_100() -> None:
    assert WATCH_UNIVERSE_CAP == 100


def test_the_count_before_positions_excludes_position_only_members_and_market() -> None:
    universe = watch_universe(
        markets=MARKETS, leaders=["XOM"], positions=["TSLA", "AAPL"], manual=["PLTR"]
    )
    # AAPL MSFT NVDA SPY XOM PLTR -- TSLA is position-only, MARKET is not a symbol.
    assert universe.count_before_positions == 6


def test_adding_the_hundredth_symbol_is_permitted() -> None:
    universe = watch_universe(markets=synthetic_symbols(99), leaders=[], positions=[], manual=[])
    assert universe.count_before_positions == 99
    check = can_add_manual(universe, "PLTR")
    assert check.allowed is True
    assert check.refusal is None
    assert check.ticker == "PLTR"


def test_one_past_the_ceiling_is_refused_and_the_refusal_names_it() -> None:
    universe = watch_universe(
        markets=synthetic_symbols(90), leaders=[], positions=[], manual=synthetic_symbols(10, "R")
    )
    assert universe.count_before_positions == 100
    check = can_add_manual(universe, "PLTR")
    assert check.allowed is False
    assert check.refusal is WatchRefusal.CAP_REACHED
    assert "100" in check.detail
    assert "PLTR" in check.detail


def test_position_underlyings_do_not_count_toward_the_cap() -> None:
    universe = watch_universe(
        markets=synthetic_symbols(99), leaders=[], positions=synthetic_symbols(10, "P"), manual=[]
    )
    assert len(universe.polled_symbols) == 109
    assert can_add_manual(universe, "PLTR").allowed is True


def test_a_position_only_ticker_may_be_watched_and_counts_toward_the_cap() -> None:
    """Watching it keeps it after the position closes, so it is a pre-position member."""
    below = watch_universe(markets=synthetic_symbols(99), leaders=[], positions=["TSLA"], manual=[])
    assert can_add_manual(below, "TSLA").allowed is True

    at_cap = watch_universe(markets=synthetic_symbols(100), leaders=[], positions=["TSLA"], manual=[])
    check = can_add_manual(at_cap, "TSLA")
    assert check.refusal is WatchRefusal.CAP_REACHED


# --- other add refusals -------------------------------------------------------


def test_adding_an_existing_manual_watch_is_refused() -> None:
    universe = watch_universe(markets=MARKETS, leaders=[], positions=[], manual=["PLTR"])
    assert can_add_manual(universe, "pltr").refusal is WatchRefusal.ALREADY_MANUAL


def test_adding_a_markets_or_leader_member_is_refused_naming_why() -> None:
    universe = watch_universe(markets=MARKETS, leaders=["XOM"], positions=[], manual=[])
    markets = can_add_manual(universe, "AAPL")
    assert markets.refusal is WatchRefusal.ALREADY_MEMBER
    assert "markets" in markets.detail
    leader = can_add_manual(universe, "XOM")
    assert leader.refusal is WatchRefusal.ALREADY_MEMBER
    assert "sector_leader" in leader.detail


def test_market_cannot_be_added() -> None:
    universe = watch_universe(markets=MARKETS, leaders=[], positions=[], manual=[])
    assert can_add_manual(universe, "market").refusal is WatchRefusal.RESERVED


@pytest.mark.parametrize("bad", ["AAPL1", "NOT A SYMBOL", "", "AAPL241220C00150000"])
def test_a_malformed_ticker_cannot_be_added(bad: str) -> None:
    universe = watch_universe(markets=MARKETS, leaders=[], positions=[], manual=[])
    assert can_add_manual(universe, bad).refusal is WatchRefusal.INVALID_SYMBOL


def test_add_normalises_the_ticker() -> None:
    universe = watch_universe(markets=MARKETS, leaders=[], positions=[], manual=[])
    check = can_add_manual(universe, " brk/b ")
    assert check.allowed is True
    assert check.ticker == "BRK.B"


# --- removal ------------------------------------------------------------------


def test_a_manual_watch_can_be_removed() -> None:
    universe = watch_universe(markets=MARKETS, leaders=[], positions=[], manual=["PLTR"])
    check = can_remove_manual(universe, "pltr")
    assert check.allowed is True
    assert check.ticker == "PLTR"


def test_a_manual_watch_that_is_also_a_position_can_be_removed() -> None:
    universe = watch_universe(markets=MARKETS, leaders=[], positions=["PLTR"], manual=["PLTR"])
    assert can_remove_manual(universe, "PLTR").allowed is True


@pytest.mark.parametrize(
    ("ticker", "reason"),
    [("AAPL", "markets"), ("XOM", "sector_leader"), ("TSLA", "position")],
)
def test_a_non_manual_member_cannot_be_removed_and_the_refusal_says_why(
    ticker: str, reason: str
) -> None:
    universe = watch_universe(markets=MARKETS, leaders=["XOM"], positions=["TSLA"], manual=[])
    check = can_remove_manual(universe, ticker)
    assert check.allowed is False
    assert check.refusal is WatchRefusal.NOT_MANUAL
    assert reason in check.detail


def test_market_cannot_be_removed_and_is_refused_as_reserved_like_the_add_path() -> None:
    universe = watch_universe(markets=MARKETS, leaders=[], positions=[], manual=[])
    removal = can_remove_manual(universe, "market")
    assert removal.refusal is WatchRefusal.RESERVED
    assert removal.refusal is can_add_manual(universe, "market").refusal


def test_an_absent_ticker_cannot_be_removed() -> None:
    universe = watch_universe(markets=MARKETS, leaders=[], positions=[], manual=[])
    assert can_remove_manual(universe, "PLTR").refusal is WatchRefusal.NOT_MEMBER


def test_a_malformed_ticker_cannot_be_removed() -> None:
    universe = watch_universe(markets=MARKETS, leaders=[], positions=[], manual=[])
    assert can_remove_manual(universe, "AAPL1").refusal is WatchRefusal.INVALID_SYMBOL
