"""The rules tier: deterministic headline patterns, and who a match attaches to.

Phase 3 design, *Testing*: *"``rules.py``: every pattern has a positive and a
negative headline; the rule set is deterministic -- same headline, same
label."* Decision 21's *Attribution off the watch list* is tested below it.

The governing principle is decision 21 / PRD section 9's *"silence beats a
wrong label"*: every positive here asserts **which ticker** got the label, and
every regression headline asserts the right ticker or no label at all.

Headlines marked REAL are read verbatim out of the recorded fixtures under
``tests/fixtures/{alpaca,finnhub,massive}/`` (looked up by prefix, so curly
quotes survive). None of the 73 recorded headlines matches any pattern, so
every *positive* headline is SYNTHETIC, written in the vendors' house styles
(Benzinga's ``Q3 EPS $1.57 Beats $1.43 Estimate``; Reuters' sentence case).
Headlines marked DRAFT-BUG are ones the step-0 draft in
``scripts/probe_phase3.py`` labelled wrongly; AUDIT-<id> are ones the unit
5.1b audit found the first version of this module labelling wrongly, and
AUDIT2-/AUDIT3-<id> the second and third audits' (the third on a realistic
corpus). Each pins its fix.
"""

import json
import random
import string
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from corollary.data.news.article import NewsArticle, NewsFeed
from corollary.data.news.labels import Direction, LabelSource, SentimentTier
from corollary.data.news.rules import (
    LEGAL_AND_CLASS_SUFFIXES,
    MAX_HEADLINE_CHARS,
    RULE_PATTERNS,
    WATCH_COMPANY_NAMES,
    WORD_NAMES,
    NameBook,
    RuleFamily,
    RulePattern,
    RulesLabelling,
    label_headline,
    normalise_company_name,
    rules_labels,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _headlines(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("headline", "title") and isinstance(value, str):
                yield value
            else:
                yield from _headlines(value)
    elif isinstance(node, list):
        for value in node:
            yield from _headlines(value)


@cache
def _recorded_headlines() -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for vendor in ("alpaca", "finnhub", "massive"):
        for path in sorted((FIXTURES / vendor).glob("*news*.json")):
            for headline in _headlines(json.loads(path.read_text(encoding="utf-8"))):
                seen[headline] = None
    return tuple(seen)


def real(prefix: str) -> str:
    """REAL: the one recorded headline starting with ``prefix``."""
    found = [h for h in _recorded_headlines() if h.startswith(prefix)]
    assert len(found) == 1, f"expected one recorded headline starting {prefix!r}, got {found}"
    return found[0]


@cache
def _recorded_asset_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for path in sorted((FIXTURES / "alpaca").glob("p4_assets_active_*.json")):
        for asset in json.loads(path.read_text(encoding="utf-8"))["body"]:
            names[asset["symbol"]] = asset["name"]
    return names


#: A watch universe and asset list. The asset names are the broker's real ones
#: where recorded (``tests/fixtures/alpaca/p4_assets_active_sample.json``,
#: ``tests/fixtures/sec/alpaca_asset_*.json``); the rest are written in the
#: same style -- including the ones that collide with ordinary words or with a
#: watch name, which is what the audit regressions need.
WATCH = frozenset(
    {
        "AAPL", "NVDA", "META", "JPM", "CAT", "ORCL", "MU", "MSFT", "WMT", "COST",
        "CVX", "HD", "MRK", "BRK.B", "ABBV", "BAC", "LLY",
    }
)  # fmt: skip
ASSET_NAMES: dict[str, str] = {
    "AAPL": "Apple Inc. Common Stock",
    "NVDA": "NVIDIA Corporation Common Stock",
    "META": "Meta Platforms, Inc. Class A Common Stock",
    "JPM": "JPMorgan Chase & Co.",
    "CAT": "Caterpillar, Inc.",
    "ORCL": "Oracle Corporation Common Stock",
    "MU": "Micron Technology, Inc. Common Stock",
    "MSFT": "Microsoft Corporation Common Stock",
    "BAC": "Bank of America Corporation",
    "FSLY": "Fastly, Inc. Class A Common Stock",
    "TGT": "Target Corporation Common Stock",
    "MS": "Morgan Stanley",
    "CRL": "Charles River Laboratories International, Inc. Common Stock",
    "COIN": "Coinbase Global, Inc. Class A Common Stock",
    "AZO": "AutoZone, Inc. Common Stock",
    "HES": "Hess Corporation Common Stock",
    "SBUX": "Starbucks Corporation Common Stock",
    "CMG": "Chipotle Mexican Grill, Inc. Common Stock",
    "BCS": "Barclays PLC American Depositary Shares",
    "MCO": "Moody's Corporation Common Stock",
    "BA": "Boeing Company (The) Common Stock",
    "NDAQ": "Nasdaq, Inc. Common Stock",
    "DOW": "Dow Inc. Common Stock",
    "NWSA": "News Corporation Class A Common Stock",
    "BOX": "Box, Inc. Class A Common Stock",
    "SE": "Sea Limited American Depositary Shares",
    "APLE": "Apple Hospitality REIT, Inc. Common Stock",
    "MMAT": "Meta Materials Inc. Common Stock",
    "BHLB": "Berkshire Hills Bancorp, Inc. Common Stock",
    "EPS": "WisdomTree U.S. LargeCap Fund",
    "AI": "C3.ai, Inc. Class A Common Stock",
    "IT": "Gartner, Inc. Common Stock",
    "HR": "Healthcare Realty Trust Incorporated Common Stock",
    "IPO": "Renaissance IPO ETF",
    "DE": "Deere & Company Common Stock",
    "WFC": "Wells Fargo & Company Common Stock",
    "MKC": "McCormick & Company, Incorporated Non-Voting Common Stock",
    "INTC": "Intel Corporation Common Stock",
    "RKLB": "Rocket Lab Corporation Common Stock",
    "REGN": "Regeneron Pharmaceuticals, Inc. Common Stock",
    "REPL": "Replimune Group, Inc. Common Stock",
    "SMMT": "Summit Therapeutics Inc. Common Stock",
    "VRTX": "Vertex Pharmaceuticals Incorporated Common Stock",
    "VERX": "Vertex, Inc. Class A Common Stock",
    "NKE": "NIKE, Inc. Common Stock",
    "AKAM": "Akamai Technologies, Inc. Common Stock",
    "BX": "Blackstone Inc. Common Stock",
    "MSTR": "Strategy Inc Class A Common Stock",
    # AUDIT3: banks and rating agencies that publish forecasts of their own,
    # the macro funds a data release is tagged with, and the recall shapes.
    "DB": "Deutsche Bank AG Common Stock",
    "GS": "Goldman Sachs Group, Inc. (The) Common Stock",
    "NFLX": "Netflix, Inc. Common Stock",
    "F": "Ford Motor Company Common Stock",
    "NVO": "Novo Nordisk A/S Common Stock",
    "SPY": "SPDR S&P 500 ETF Trust",
    "QQQ": "Invesco QQQ Trust, Series 1",
}
#: The fund and market tickers among them: what the caller derives from the
#: curated universe's ``fund`` flag. EPS and IPO are funds
#: too, which is the point -- nothing in the labeller knows SPY by name.
FUNDS = frozenset({"SPY", "QQQ", "EPS", "IPO"})
BOOK = NameBook(watch=WATCH, watch_names=WATCH_COMPANY_NAMES, asset_names=ASSET_NAMES, funds=FUNDS)


def article(
    headline: str, feed: NewsFeed = NewsFeed.FINNHUB_MARKET, tickers: tuple[str, ...] = ()
) -> NewsArticle:
    """SYNTHETIC wrapper: only the headline, feed and tags matter to the rules tier."""
    vendor = {"alpaca_news": "alpaca", "massive_news": "massive"}.get(feed.value, "finnhub")
    return NewsArticle(
        vendor=vendor,
        vendor_id="synthetic-1",
        feed=feed,
        url="https://example.invalid/a",
        headline=headline,
        summary=None,
        publisher="Synthetic",
        published_at=datetime(2026, 9, 24, 12, 55, tzinfo=timezone.utc),
        tickers=tickers,
    )


def result_of(
    headline: str, feed: NewsFeed = NewsFeed.FINNHUB_MARKET, tags: tuple[str, ...] = (), book: NameBook = BOOK
) -> RulesLabelling:
    return rules_labels(article(headline, feed, tags), book)


def fired(headline: str) -> list[str]:
    """The rule_ids that match ``headline``, before attribution."""
    return [m.rule_id for m in label_headline(headline, NewsFeed.FINNHUB_MARKET, (), BOOK).matches]


def labelled(
    headline: str, feed: NewsFeed = NewsFeed.FINNHUB_MARKET, tags: tuple[str, ...] = (), book: NameBook = BOOK
) -> dict[str, str]:
    """``{ticker: "rule_id direction"}`` for every label on ``headline``."""
    return {
        label.ticker: f"{label.rule_id} {label.direction.value}"
        for label in result_of(headline, feed, tags, book).labels
    }


# ------------------------------------------------------------- the pattern set


def test_the_eight_flagged_families_each_have_a_bullish_and_bearish_or_single_pattern() -> None:
    assert {p.family for p in RULE_PATTERNS} == set(RuleFamily)
    assert len(RuleFamily) == 8
    assert [p.rule_id for p in RULE_PATTERNS] == [
        "earnings_beat",
        "earnings_miss",
        "guidance_raised",
        "guidance_cut",
        "analyst_upgrade",
        "analyst_downgrade",
        "mna_target",
        "secondary_offering",
        "buyback",
        "executive_departure",
        "fda_approval",
        "fda_rejection",
    ]
    assert all(p.direction in (Direction.BULLISH, Direction.BEARISH) for p in RULE_PATTERNS)


def test_every_current_pattern_is_discovery_flagged_and_a_new_one_is_not_by_default() -> None:
    """Decision 21: *"A pattern step 5 adds later is unflagged until someone flags it."*"""
    from corollary.data.news.rules import Shape

    assert all(p.discovery_flagged for p in RULE_PATTERNS)
    later = RulePattern(
        rule_id="later",
        family=RuleFamily.BUYBACK,
        direction=Direction.BULLISH,
        shapes=(Shape(r"\bsomething\b"),),
    )
    assert later.discovery_flagged is False


def test_a_pattern_refuses_a_neutral_direction_a_blank_rule_id_and_no_shapes() -> None:
    from corollary.data.news.rules import Shape

    one = (Shape("x"),)
    with pytest.raises(ValueError, match="direction"):
        RulePattern(rule_id="x", family=RuleFamily.FDA, direction=Direction.NEUTRAL, shapes=one)
    with pytest.raises(ValueError, match="rule_id"):
        RulePattern(rule_id="", family=RuleFamily.FDA, direction=Direction.BULLISH, shapes=one)
    with pytest.raises(ValueError, match="shape"):
        RulePattern(rule_id="x", family=RuleFamily.FDA, direction=Direction.BULLISH, shapes=())


#: (rule_id, positive headline, the labels it must produce). SYNTHETIC. An empty
#: dict is a headline whose subject is not in the book: it must fire and stay
#: unlabelled, which the test checks too.
POSITIVE: list[tuple[str, str, dict[str, str]]] = [
    ("earnings_beat", "Apple Q3 EPS $1.57 Beats $1.43 Estimate, Sales $94.04B Beat $89.53B Estimate",
     {"AAPL": "earnings_beat bullish"}),
    ("earnings_beat", "Micron Technology tops Wall Street expectations on AI memory demand",
     {"MU": "earnings_beat bullish"}),
    ("earnings_miss", "Nike Q1 EPS $0.70 Misses $0.82 Estimate", {}),  # NIKE is all caps in the asset list
    ("earnings_miss", "Starbucks quarterly sales fall short of estimates", {"SBUX": "earnings_miss bearish"}),
    ("guidance_raised", "Oracle Raises FY27 Revenue Guidance", {"ORCL": "guidance_raised bullish"}),
    ("guidance_raised", "Fastly boosts first-quarter outlook", {"FSLY": "guidance_raised bullish"}),
    ("guidance_cut", "Starbucks Lowers Fiscal 2027 Revenue Guidance", {"SBUX": "guidance_cut bearish"}),
    ("guidance_cut", "Intel withdraws full-year forecast", {"INTC": "guidance_cut bearish"}),
    ("analyst_upgrade", "Goldman Sachs Upgrades Apple to Buy", {"AAPL": "analyst_upgrade bullish"}),
    # AUDIT-H2: this was MS bullish -- the broker after "at" was read as the subject.
    ("analyst_upgrade", "Nvidia upgraded at Morgan Stanley", {"NVDA": "analyst_upgrade bullish"}),
    ("analyst_upgrade", "Fastly Stock Upgraded: Here's Why", {"FSLY": "analyst_upgrade bullish"}),
    ("analyst_upgrade", "Goldman Sachs Upgrades Fastly, Raises Price Target", {"FSLY": "analyst_upgrade bullish"}),
    ("analyst_downgrade", "Morgan Stanley Downgrades Fastly to Underweight", {"FSLY": "analyst_downgrade bearish"}),
    ("analyst_downgrade", "Apple downgraded by Jefferies on iPhone demand", {"AAPL": "analyst_downgrade bearish"}),
    ("mna_target", "Hess Agrees to Be Acquired by Chevron for $53 Billion", {"HES": "mna_target bullish"}),
    ("mna_target", "Fastly receives unsolicited takeover offer", {"FSLY": "mna_target bullish"}),
    ("mna_target", "Chevron to Acquire Hess for $53 Billion", {"HES": "mna_target bullish"}),
    ("mna_target", "Chevron Agrees to Buy Hess in $53 Billion All-Stock Deal", {"HES": "mna_target bullish"}),
    ("secondary_offering", "Rocket Lab Announces Pricing of Secondary Offering",
     {"RKLB": "secondary_offering bearish"}),
    ("secondary_offering", "Coinbase (COIN) Prices $1.5B Common Stock Offering",
     {"COIN": "secondary_offering bearish"}),
    ("buyback", "Apple Board Authorizes $110 Billion Share Buyback", {"AAPL": "buyback bullish"}),
    ("buyback", "Fastly Announces $100M Repurchase Program", {"FSLY": "buyback bullish"}),
    ("executive_departure", "Starbucks CEO Laxman Narasimhan Steps Down", {"SBUX": "executive_departure bearish"}),
    ("executive_departure", "Intel CFO to leave the company in January", {"INTC": "executive_departure bearish"}),
    ("fda_approval", "FDA Approves Eli Lilly's Alzheimer's Drug Kisunla", {"LLY": "fda_approval bullish"}),
    ("fda_approval", "Vertex Pharmaceuticals Receives FDA Approval for Journavx", {"VRTX": "fda_approval bullish"}),
    ("fda_approval", "FDA Grants Accelerated Approval to Summit Therapeutics' Ivonescimab",
     {"SMMT": "fda_approval bullish"}),
    ("fda_rejection", "Regeneron Pharmaceuticals Receives Complete Response Letter From FDA",
     {"REGN": "fda_rejection bearish"}),
    ("fda_rejection", "Replimune (REPL) Receives CRL For RP1", {"REPL": "fda_rejection bearish"}),
    ("fda_rejection", "FDA Rejects Lykos' MDMA Therapy", {}),  # Lykos is private
]


@pytest.mark.parametrize(("rule_id", "headline", "expected"), POSITIVE)
def test_each_pattern_fires_on_its_positive_headline_and_labels_its_subject(
    rule_id: str, headline: str, expected: dict[str, str]
) -> None:
    assert rule_id in fired(headline)
    assert labelled(headline) == expected
    if not expected:
        assert [m.rule_id for m in result_of(headline).unattributed] == [rule_id]


def test_every_pattern_has_a_positive_and_a_negative_headline() -> None:
    ids = {p.rule_id for p in RULE_PATTERNS}
    assert {r for r, _, _ in POSITIVE} == ids
    assert {r for r, _ in NEGATIVE} == ids
    # A positive that attributes to nobody proves the pattern, not the binding.
    assert {r for r, _, expected in POSITIVE if expected} == ids


#: (rule_id, headline it must NOT fire on). Lazily resolved so REAL headlines
#: come out of the fixtures at test time.
NEGATIVE: list[tuple[str, str]] = [
    ("earnings_beat", "REAL:UEC Gears Up to Report Q4 Earnings"),
    ("earnings_beat", "Top Analyst Forecasts for the Week Ahead"),  # "Top" as an adjective
    ("earnings_miss", "REAL:Astrana Health Affirms FY2026 Sales Guidance"),
    ("earnings_miss", "REAL:USA S&P Global Manufacturing PMI For September"),
    ("guidance_raised", "REAL:DA Davidson Maintains Neutral on Fastly, Raises Price Target"),
    # DRAFT-BUG: a price-target raise is a rating action, not company guidance
    ("guidance_raised", "Morgan Stanley Raises Price Target on Nvidia, Cites Strong Outlook"),
    ("guidance_cut", "REAL:AutoZone Analysts Cut Their Forecasts After Q4 Results"),
    ("guidance_cut", "REAL:These Analysts Slash Their Forecasts On KB Home"),
    # DRAFT-BUG: same, in the bearish direction
    ("guidance_cut", "DA Davidson Lowers Price Target on AutoZone, Sees Softer Outlook"),
    ("analyst_upgrade", "REAL:B of A Securities Reiterates Buy on Apple"),
    # DRAFT-BUG: a product being upgraded is not a rating change
    ("analyst_upgrade", "Tesla Model Y Gets Upgraded Battery Pack"),
    # AUDIT-H2: "to Add" is a verb phrase, not a rating
    ("analyst_upgrade", "Microsoft Upgrades Azure Data Centers to Add Nvidia Chips"),
    ("analyst_downgrade", "REAL:Bank of America Just Told Investors to Sell UPS"),
    # DRAFT-BUG: same
    ("analyst_downgrade", "Apple Watch Ultra Downgraded Display Draws Complaints"),
    ("mna_target", "REAL:MGM Resorts shares sink 9% after Barry Diller"),
    ("mna_target", "REAL:'Activist Toms Capital urges Devon Energy"),
    # AUDIT-M3: a terminated agreement is not a pending acquisition
    ("mna_target", "Fastly Terminates Agreement to Be Acquired by Akamai"),
    ("secondary_offering", "REAL:Brightline Interactive Announces 1-For-8 Reverse Stock Split"),
    ("secondary_offering", "REAL:SoftBank Launches $11.1 Billion Bond Sale"),
    # AUDIT-M3: a cancelled offering is not an offering
    ("secondary_offering", "Fastly Cancels Proposed Common Stock Offering"),
    ("buyback", "REAL:Here's How Much $1000 Invested In Apple 20 Years Ago"),
    # DRAFT-BUG: suspending a buyback is not a bullish buyback
    ("buyback", "Intel Suspends Share Buyback Program to Fund Foundry"),
    ("executive_departure", "REAL:World"),  # "...Wealth Fund CEO Expects US Stock Returns To Slow"
    # DRAFT-BUG: a CEO speaking about an exit is not a CEO exiting
    ("executive_departure", "Nvidia CEO Jensen Huang Says Company Will Not Exit China"),
    ("fda_approval", "Vertex Seeks FDA Approval for Pain Drug"),
    # DRAFT-BUG: a designation is not an approval
    ("fda_approval", "FDA Grants Breakthrough Therapy Designation to Summit's Ivonescimab"),
    # AUDIT-M3: failing to win approval, and a competitor's biosimilar
    ("fda_approval", "Vertex Fails to Win FDA Approval for Pain Drug"),
    ("fda_approval", "FDA Approves Biosimilar to AbbVie's Humira"),
    ("fda_rejection", "REAL:Quantum Stocks Catch a Bid After IonQ"),
    # DRAFT-BUG: CRL is Charles River Laboratories' ticker
    ("fda_rejection", "Charles River Laboratories (CRL) Q3 EPS $2.59 Beats $2.47 Estimate"),
]


def _resolve(headline: str) -> str:
    return real(headline.removeprefix("REAL:")) if headline.startswith("REAL:") else headline


@pytest.mark.parametrize(("rule_id", "headline"), NEGATIVE)
def test_each_pattern_stays_silent_on_its_negative_headline(rule_id: str, headline: str) -> None:
    assert rule_id not in fired(_resolve(headline))


def test_no_recorded_headline_fires_a_pattern() -> None:
    """REAL: all 73 recorded headlines. A change that starts matching one is
    worth a look, so this pins the set at none rather than leaving it implicit."""
    assert len(_recorded_headlines()) >= 70
    assert {h: fired(h) for h in _recorded_headlines() if fired(h)} == {}


def test_charles_river_beat_is_a_bullish_crl_label_not_a_conflict() -> None:
    """The DRAFT-BUG above, end to end: the draft read (CRL) as an FDA rejection,
    which collided with the beat and left the headline unlabelled. ``(CRL)``
    directly follows Charles River's own name, so it is a mention here."""
    headline = "Charles River Laboratories (CRL) Q3 EPS $2.59 Beats $2.47 Estimate"
    assert labelled(headline) == {"CRL": "earnings_beat bullish"}


# ------------------------------------------------------------- one label, its fields


def test_a_rules_label_carries_its_rule_source_tier_and_the_matched_phrase() -> None:
    result = result_of("Oracle Q1 EPS $1.47 Misses $1.48 Estimate")
    (label,) = result.labels
    assert label.ticker == "ORCL"
    assert label.source is LabelSource.RULES
    assert label.tier is SentimentTier.RULES
    assert label.direction is Direction.BEARISH
    assert label.rule_id == "earnings_miss"
    assert label.reasoning is not None
    assert "earnings_miss" in label.reasoning
    assert '"Misses $1.48 Estimate"' in label.reasoning
    assert "subject" in label.reasoning
    (match,) = result.matches
    assert match.tickers == ("ORCL",)


def test_two_agreeing_patterns_give_one_label_naming_both_phrases() -> None:
    """UNIQUE (article_id, ticker, source): one row. The first pattern in
    declared order is the rule_id; the reasoning keeps every phrase. The second
    clause has no subject of its own and takes the first clause's."""
    result = result_of("Oracle Q1 EPS Beats Estimate, Raises FY27 Revenue Guidance")
    (label,) = result.labels
    assert label.ticker == "ORCL"
    assert label.rule_id == "earnings_beat"
    assert label.reasoning is not None
    assert "earnings_beat" in label.reasoning and "guidance_raised" in label.reasoning
    assert [m.rule_id for m in result.matches] == ["earnings_beat", "guidance_raised"]
    assert [m.tickers for m in result.matches] == [("ORCL",), ("ORCL",)]
    assert result.conflicting == ()


def test_a_bullish_and_bearish_match_gives_no_label_and_is_returned_as_a_conflict() -> None:
    result = result_of("Oracle Q1 EPS Beats Estimate, Lowers Full-Year Guidance")
    assert result.labels == ()
    assert [m.rule_id for m in result.conflicting] == ["earnings_beat", "guidance_cut"]
    assert result.unattributed == ()


def test_a_match_that_attributes_to_no_ticker_is_returned_not_dropped() -> None:
    result = result_of("Nike Q1 EPS $0.70 Misses $0.82 Estimate")
    assert result.labels == ()
    assert [m.rule_id for m in result.unattributed] == ["earnings_miss"]
    assert result.unattributed[0].phrase == "Misses $0.82 Estimate"
    assert result.unattributed[0].tickers == ()
    assert result.unattributed[0].attribution  # says why
    assert result.conflicting == ()


def test_a_headline_no_pattern_matches_returns_nothing_at_all() -> None:
    result = result_of(real("Meta nears first new high"))
    assert (result.labels, result.matches, result.conflicting, result.unattributed) == ((), (), (), ())


def test_same_headline_same_labels_and_the_book_order_does_not_matter() -> None:
    headline = "Apple Beats Estimates, Nvidia Raises Outlook; Fastly Announces $100M Repurchase Program"
    once = result_of(headline)
    assert once == result_of(headline)
    reversed_book = NameBook(
        watch=frozenset(sorted(WATCH, reverse=True)),
        watch_names=dict(reversed(list(WATCH_COMPANY_NAMES.items()))),
        asset_names=dict(reversed(list(ASSET_NAMES.items()))),
        funds=frozenset(sorted(FUNDS, reverse=True)),
    )
    assert once == rules_labels(article(headline), reversed_book)
    assert [label.ticker for label in once.labels] == ["AAPL", "FSLY", "NVDA"]
    assert [label.rule_id for label in once.labels] == ["earnings_beat", "buyback", "guidance_raised"]


def test_a_headline_over_the_cap_is_not_matched_and_says_so() -> None:
    """A 50,000-character headline is no headline. It is not cut down and
    labelled -- a veto past the cut would be invisible -- but returned empty
    with ``truncated`` set."""
    headline = ("Apple (AAPL) Raises Q3 Beats, " * 1700) + "x" * 1000
    assert len(headline) > 50_000
    started = time.perf_counter()
    result = result_of(headline)
    assert time.perf_counter() - started < 0.5
    assert result.truncated is True
    assert (result.labels, result.matches, result.vetoed) == ((), (), ())


def test_the_cap_permits_a_headline_at_exactly_the_cap_and_refuses_one_over_it() -> None:
    lead = "Apple Raises Full-Year Outlook; "
    at_cap = lead + "x" * (MAX_HEADLINE_CHARS - len(lead))
    assert len(at_cap) == MAX_HEADLINE_CHARS
    assert labelled(at_cap) == {"AAPL": "guidance_raised bullish"}
    assert result_of(at_cap).truncated is False
    over = at_cap + "x"
    assert labelled(over) == {}
    assert result_of(over).truncated is True


def test_whitespace_runs_are_collapsed_before_matching() -> None:
    """AUDIT-M-A: nothing upstream collapses inner whitespace, so the labeller
    does. The phrase in the reasoning is the collapsed text."""
    result = result_of("Apple   Raises\tFull-Year\n  Outlook ")
    (label,) = result.labels
    assert (label.ticker, label.rule_id) == ("AAPL", "guidance_raised")
    assert result.matches[0].phrase == "Raises Full-Year Outlook"
    spaced = "Chevron to acquire" + " " * 4_000 + "Hess for $53 Billion"
    started = time.perf_counter()
    assert labelled(spaced) == {"HES": "mna_target bullish"}
    assert time.perf_counter() - started < 0.5


#: Every trigger word of every shape, so a mixed input reaches every regex.
_TRIGGERS = (
    "Apple Inc. (NASDAQ: AAPL) Apple's Q3 EPS $1.57 beats tops misses falls short of below the "
    "Wall Street estimates raises its full-year guidance cuts outlook upgrades Fastly to Buy, raises "
    "price target downgrades upgraded at Barclays stock downgraded agrees to be acquired receives "
    "takeover offer from to acquire acquires buys Hess for $53 Billion in deal secondary offering "
    "common stock offering of share buyback repurchase program CEO Tim Cook steps down to retire "
    "FDA approves Merck's grants accelerated approval to wins FDA approval for complete response "
    "letter CRL issues rejects ; , : ? "
)


def _pathological_inputs() -> dict[str, str]:
    words = _TRIGGERS.split()
    return {
        "4k-space runs": (" " * 4_000).join(words),
        "50k-space runs": (" " * 50_000).join(words[:6]),
        "50k mixed": (_TRIGGERS * (50_000 // len(_TRIGGERS) + 1))[:50_000],
        "50k mixed, double spaced": (_TRIGGERS.replace(" ", "  ") * (50_000 // len(_TRIGGERS) + 1))[:50_000],
        "one 50k word": "upgrades " + "a" * 50_000 + " to Buy",
        "one 50k number": "beats $" + "1" * 50_000 + " estimates",
        "50k brackets": "FDA approves " + "(" * 50_000,
    }


@pytest.mark.parametrize("name", sorted(_pathological_inputs()))
def test_every_regex_is_bounded_on_raw_pathological_input(name: str) -> None:
    """AUDIT-M-A: the labeller normalises first, but every shape, veto and
    near-miss is bounded on its own, on raw input -- the defence does not rest
    on one function remembering to normalise. Each must finish in 0.5 s; the
    unbounded object slot took 12.3 s at 4,000 spaces."""
    text = _pathological_inputs()[name]
    for pattern in RULE_PATTERNS:
        for shape in pattern.shapes:
            started = time.perf_counter()
            shape.search(text)
            assert time.perf_counter() - started < 0.5, (pattern.rule_id, shape.regex[:40])
        started = time.perf_counter()
        pattern.vetoed_by(text)
        pattern.near_miss_in(text)
        assert time.perf_counter() - started < 0.5, (pattern.rule_id, "veto/near-miss")
    started = time.perf_counter()
    result_of(text)
    assert time.perf_counter() - started < 0.5


def test_a_headline_just_under_the_cap_made_of_every_trigger_is_labelled_in_bounded_time() -> None:
    text = (_TRIGGERS * 3)[: MAX_HEADLINE_CHARS - 1]
    started = time.perf_counter()
    for feed in (NewsFeed.FINNHUB_MARKET, NewsFeed.ALPACA_NEWS):
        result_of(text, feed, ("FSLY",))
    assert time.perf_counter() - started < 0.5


# ------------------------------------------------------------- attribution: tags


@pytest.mark.parametrize("feed", [NewsFeed.ALPACA_NEWS, NewsFeed.MASSIVE_NEWS])
def test_a_single_tag_from_a_per_article_feed_attributes_to_that_tag(feed: NewsFeed) -> None:
    result = result_of("Company Raises Full-Year Guidance", feed, ("FSLY",))
    assert [(label.ticker, label.rule_id) for label in result.labels] == [("FSLY", "guidance_raised")]
    assert "only tag" in (result.labels[0].reasoning or "")


def test_a_single_tag_on_a_finnhub_company_row_does_not_attribute() -> None:
    """The tag is the symbol Finnhub was queried for, not its reading of the
    article -- it returned Oracle and Grab headlines for ALAB."""
    result = result_of("Company Raises Full-Year Guidance", NewsFeed.FINNHUB_COMPANY, ("FSLY",))
    assert result.labels == ()
    assert [m.rule_id for m in result.unattributed] == ["guidance_raised"]


def test_two_tags_attribute_to_neither_without_a_name() -> None:
    """A roundup tagged with several tickers is a label on none of them."""
    assert labelled("Company Raises Full-Year Guidance", NewsFeed.ALPACA_NEWS, ("FSLY", "AZO")) == {}


def test_the_single_tag_rule_counts_equity_tags_only() -> None:
    """A crypto pair is not an equity tag, so COIN is the article's only one."""
    tags = ("BTCUSD", "COIN")
    assert labelled("Company Prices $1.5B Common Stock Offering", NewsFeed.ALPACA_NEWS, tags) == {
        "COIN": "secondary_offering bearish"
    }


def test_a_named_subject_wins_over_a_single_tag_that_is_a_different_company() -> None:
    """The headline says Apple was upgraded. Labelling the tag too would put a
    bullish label on a company the headline does not say anything about."""
    assert labelled("Goldman Sachs Upgrades Apple to Buy", NewsFeed.ALPACA_NEWS, ("FSLY",)) == {
        "AAPL": "analyst_upgrade bullish"
    }


def test_a_single_tag_never_attributes_to_a_company_the_headline_names_in_another_role() -> None:
    """Hess is not in this book, so the subject slot is unnamed -- but the lone
    tag is the acquirer, named after "by", and the label must not land on it."""
    book = NameBook(watch=frozenset({"CVX"}), watch_names=WATCH_COMPANY_NAMES, asset_names={}, funds=())
    result = result_of("Hess Agrees to Be Acquired by Chevron", NewsFeed.ALPACA_NEWS, ("CVX",), book)
    assert result.labels == ()
    assert [m.rule_id for m in result.unattributed] == ["mna_target"]


# ------------------------------------------------------------- attribution: names


def test_a_broker_named_before_a_rating_verb_is_not_the_subject() -> None:
    """Watch table (JPMorgan Chase) and off-watch asset name (Morgan Stanley) alike."""
    assert labelled("JPMorgan Chase Upgrades Meta Platforms to Overweight") == {"META": "analyst_upgrade bullish"}
    assert labelled("Morgan Stanley Downgrades Fastly to Underweight") == {"FSLY": "analyst_downgrade bearish"}
    result = result_of("Morgan Stanley Downgrades Fastly to Underweight")
    assert ("MS",) in [s.tickers for s in result.suppressed]


def test_a_word_ticker_is_not_matched_as_a_bare_word() -> None:
    """CAT is a word; NVDA is not. Both are on the watch list."""
    assert labelled("CAT Raises Full-Year Outlook") == {}
    assert labelled("NVDA Raises Full-Year Outlook") == {"NVDA": "guidance_raised bullish"}
    assert labelled("Caterpillar (CAT) Raises Full-Year Outlook") == {"CAT": "guidance_raised bullish"}


def test_off_the_watch_list_a_bare_symbol_is_not_a_mention_but_an_explicit_one_is() -> None:
    assert labelled("FSLY Raises Full-Year Outlook") == {}
    assert labelled("Edge Cloud Firm (NYSE: FSLY) Raises Full-Year Outlook") == {"FSLY": "guidance_raised bullish"}
    assert labelled("$FSLY Raises Full-Year Outlook") == {"FSLY": "guidance_raised bullish"}


def test_off_the_watch_list_the_normalised_asset_name_attributes() -> None:
    assert labelled("Fastly Raises Full-Year Outlook") == {"FSLY": "guidance_raised bullish"}
    assert labelled("AutoZone Q4 EPS Misses Estimate") == {"AZO": "earnings_miss bearish"}


def test_a_watch_ticker_without_a_hand_kept_name_falls_back_to_its_asset_name() -> None:
    book = NameBook(watch=frozenset({"FSLY"}), watch_names={}, asset_names=ASSET_NAMES, funds=FUNDS)
    assert labelled("Fastly Raises Full-Year Outlook", book=book) == {"FSLY": "guidance_raised bullish"}


def test_a_name_must_end_at_a_word_boundary() -> None:
    """DRAFT-BUG: the draft matched ``\\bApple`` with no trailing boundary,
    so Applebee's was Apple. A possessive still counts."""
    assert labelled("Applebee's Owner Raises Full-Year Guidance") == {}
    assert labelled("Apple's Q3 EPS Beats Estimate") == {"AAPL": "earnings_beat bullish"}


def test_a_name_that_is_an_ordinary_word_never_attributes() -> None:
    """Target Corporation normalises to "Target", which is in every price-target headline."""
    result = result_of("Goldman Sachs Upgrades Fastly to Buy, Raises Price Target")
    assert {label.ticker for label in result.labels} == {"FSLY"}
    assert any(s.tickers == ("TGT",) and "ordinary word" in s.reason for s in result.suppressed)


def test_names_match_case_sensitively() -> None:
    """REAL: "Cat owners ..." is not Caterpillar, and lowercase "apple" is fruit."""
    assert labelled("apple growers raise outlook for the harvest") == {}


# ------------------------------------------------------------- AUDIT-H1: the subject, not every name


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        # The acquirer, after "by", is never the target.
        ("Hess Agrees to Be Acquired by Chevron for $53 Billion", {"HES": "mna_target bullish"}),
        ("Chevron to Acquire Hess for $53 Billion", {"HES": "mna_target bullish"}),
        # "Chipotle" is not "Chipotle Mexican Grill"; the destination is never labelled.
        ("Chipotle CEO Brian Niccol to Leave for Starbucks", {}),
        # Each rating verb binds its own object. "Upgrades Apple," has no rating
        # word, so it binds nothing; "Downgrades Nvidia to Sell" binds Nvidia.
        ("Goldman Sachs Upgrades Apple, Downgrades Nvidia to Sell", {"NVDA": "analyst_downgrade bearish"}),
        # The raise belongs to Walmart, not to the Costco clause after the comma.
        ("Walmart Raises Outlook, Costco Shares Slide", {"WMT": "guidance_raised bullish"}),
        # The speaker is not the subject.
        ("Microsoft Says Apple Beats Estimates", {"AAPL": "earnings_beat bullish"}),
    ],
)
def test_audit_h1_the_label_attaches_to_the_subject_of_the_matched_phrase(
    headline: str, expected: dict[str, str]
) -> None:
    assert labelled(headline) == expected


@pytest.mark.parametrize(
    "headline",
    [
        "Apple and Nvidia Beat Estimates",
        "Microsoft, Apple Beat Estimates",
        "Goldman Sachs Upgrades Apple and Nvidia to Buy",
    ],
)
def test_audit_h1_a_phrase_with_several_candidate_subjects_labels_none(headline: str) -> None:
    result = result_of(headline)
    assert result.labels == ()
    assert len(result.unattributed) == 1
    assert "ambiguous" in result.unattributed[0].attribution


# ------------------------------------------------------------- AUDIT-H2: the rater is not the subject


@pytest.mark.parametrize(
    ("headline", "expected", "rater"),
    [
        ("Nvidia upgraded at Morgan Stanley", {"NVDA": "analyst_upgrade bullish"}, "MS"),
        ("Apple downgraded to Neutral at BofA", {"AAPL": "analyst_downgrade bearish"}, "BAC"),
        ("Apple upgraded to Buy from Hold at Barclays", {"AAPL": "analyst_upgrade bullish"}, "BCS"),
        ("Boeing Downgraded by Moody's on Cash Burn", {"BA": "analyst_downgrade bearish"}, "MCO"),
        ("Morgan Stanley Bullish on Apple, Upgrades to Overweight", {}, "MS"),
        ("Morgan Stanley's Analyst Upgrades Apple to Overweight", {"AAPL": "analyst_upgrade bullish"}, "MS"),
    ],
)
def test_audit_h2_a_broker_named_around_a_rating_action_is_never_labelled(
    headline: str, expected: dict[str, str], rater: str
) -> None:
    assert labelled(headline) == expected
    assert any(s.tickers == (rater,) for s in result_of(headline).suppressed)


def test_audit_h2_an_upgrade_without_a_rating_word_is_not_a_rating_change() -> None:
    result = result_of("Microsoft Upgrades Azure Data Centers to Add Nvidia Chips")
    assert result.labels == ()
    assert result.matches == ()
    assert [v.rule_id for v in result.vetoed] == ["analyst_upgrade"]


# ------------------------------------------------------------- AUDIT-H3: names that are market words


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Micron (Nasdaq: MU) Beats Estimates on AI Demand", {"MU": "earnings_beat bullish"}),
        ("Nasdaq Hits Record as Nvidia Tops Estimates", {"NVDA": "earnings_beat bullish"}),
        ("Dow Jumps 400 Points as Nvidia Tops Estimates", {"NVDA": "earnings_beat bullish"}),
        ("Nvidia Stock Rises on News It Beat Estimates", {}),
        ("Disney's 'Inside Out 2' Box Office Tops Expectations", {}),
        ("Maersk Cuts Outlook on Red Sea Disruption", {}),
        ("Nvidia's AI Strategy Tops Expectations", {}),
        ("Vertex Receives FDA Approval for Journavx", {}),  # Vertex, Inc. is a tax-software company
    ],
)
def test_audit_h3_a_company_whose_name_is_a_market_word_is_not_named_by_that_word(
    headline: str, expected: dict[str, str]
) -> None:
    assert labelled(headline) == expected


def test_audit_h3_an_exchange_prefix_is_never_a_company_name() -> None:
    book = NameBook(
        watch=frozenset(),
        watch_names={},
        asset_names={"CBOE": "Cboe Global Markets", "FSLY": ASSET_NAMES["FSLY"]},
        funds=(),
    )
    assert labelled("Fastly (Cboe Global Markets: FSLY) Beats Estimates", book=book) == {}
    assert labelled("Cboe Global Markets-listed Fastly Beats Estimates", book=book) == {
        "FSLY": "earnings_beat bullish"
    }


def test_audit_h3_every_single_word_recorded_asset_name_that_is_a_common_word_is_listed() -> None:
    """The audit of the recorded asset fixtures: of their single-word
    normalised names, these are ordinary English or market words."""
    singles = {normalise_company_name(n) for n in _recorded_asset_names().values()}
    singles = {n for n in singles if " " not in n}
    common = {"Carnival", "Flex"}
    assert common <= singles
    assert common <= WORD_NAMES
    assert {"Nasdaq", "Dow", "News", "Box", "Sea", "Target"} <= WORD_NAMES


# ------------------------------------------------------------- AUDIT-M1: a longer name covers a shorter one


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Apple Hospitality REIT Cuts Full-Year Outlook", {"APLE": "guidance_cut bearish"}),
        ("Meta Materials Prices $10M Common Stock Offering", {"MMAT": "secondary_offering bearish"}),
        ("Merck KGaA Lowers Full-Year Forecast", {}),
        ("Berkshire Hills Bancorp Q3 EPS Misses Estimate", {"BHLB": "earnings_miss bearish"}),
    ],
)
def test_audit_m1_a_watch_name_that_starts_a_longer_company_name_is_not_the_watch_company(
    headline: str, expected: dict[str, str]
) -> None:
    assert labelled(headline) == expected


def test_audit_m1_without_the_longer_name_in_the_book_the_watch_company_still_is_not_labelled() -> None:
    book = NameBook(watch=WATCH, watch_names=WATCH_COMPANY_NAMES, asset_names={}, funds=())
    for headline in (
        "Apple Hospitality REIT Cuts Full-Year Outlook",
        "Meta Materials Prices $10M Common Stock Offering",
        "Berkshire Hills Bancorp Q3 EPS Misses Estimate",
    ):
        assert labelled(headline, book=book) == {}, headline


def test_audit_m1_a_covered_name_is_recorded_as_suppressed() -> None:
    result = result_of("Apple Hospitality REIT Cuts Full-Year Outlook")
    assert any(s.tickers == ("AAPL",) and "longer" in s.reason for s in result.suppressed)


# ------------------------------------------------------------- AUDIT-M2: a bracketed acronym is not a mention


@pytest.mark.parametrize(
    "headline",
    [
        "Replimune Receives Complete Response Letter (CRL) From FDA for RP1",
        "Earnings Per Share (EPS) Beats Estimates at Fastly",
        "Artificial Intelligence (AI) Revenue Beats Estimates",
        "Information Technology (IT) Spending Tops Forecasts",
        "Initial Public Offering (IPO) Activity Tops Expectations",
        "Human Resources (HR) Software Spending Beats Estimates",
    ],
)
def test_audit_m2_a_parenthetical_acronym_is_not_a_ticker_mention(headline: str) -> None:
    assert labelled(headline) == {}


def test_audit_m2_a_bracketed_symbol_counts_after_its_own_name_with_an_exchange_or_as_a_tag() -> None:
    assert labelled("Gartner (IT) Tops Estimates") == {"IT": "earnings_beat bullish"}
    assert labelled("Healthcare Realty (HR) Raises Full-Year Outlook") == {"HR": "guidance_raised bullish"}
    assert labelled("Software Maker (NYSE: AI) Beats Estimates") == {"AI": "earnings_beat bullish"}
    assert labelled("Software Maker (AI) Beats Estimates", NewsFeed.ALPACA_NEWS, ("AI", "MSFT")) == {
        "AI": "earnings_beat bullish"
    }
    assert labelled("Software Maker (AI) Beats Estimates") == {}


# ------------------------------------------------------------- AUDIT-M3: negations, previews, cancellations


@pytest.mark.parametrize(
    ("headline", "rule_id"),
    [
        ("Apple Fails to Beat Estimates for Third Straight Quarter", "earnings_beat"),
        ("Will Nvidia (NVDA) Beat Estimates Again in Its Next Earnings Report?", "earnings_beat"),
        ("Oracle (ORCL) Expected to Beat Earnings Estimates: Should You Buy?", "earnings_beat"),
        ("Vertex Fails to Win FDA Approval for Pain Drug", "fda_approval"),
        ("FDA Approves Biosimilar to AbbVie's Humira", "fda_approval"),
        ("Fastly Terminates Agreement to Be Acquired by Akamai", "mna_target"),
        ("Fastly Cancels Proposed Common Stock Offering", "secondary_offering"),
        ("Oracle Earnings Preview: Can It Raise Guidance Again", "guidance_raised"),
        ("Blackstone Prices Secondary Offering of Apple Shares", "secondary_offering"),
    ],
)
def test_audit_m3_a_negated_previewed_or_cancelled_event_is_vetoed_and_recorded(
    headline: str, rule_id: str
) -> None:
    result = result_of(headline)
    assert result.labels == ()
    assert rule_id not in [m.rule_id for m in result.matches]
    assert rule_id in [v.rule_id for v in result.vetoed]
    assert all(v.reason for v in result.vetoed)


# ------------------------------------------------------------- AUDIT-M4: the guidance verb acts on guidance


@pytest.mark.parametrize(
    "headline",
    [
        "Tariffs Increase Pressure on Apple Outlook",
        "Fed Rate Cuts Brighten Outlook for Home Depot",
        "Oracle Raises $18 Billion in Bond Sale, Reaffirms Outlook",
    ],
)
def test_audit_m4_a_guidance_verb_that_does_not_act_on_the_company_s_guidance_is_not_guidance(
    headline: str,
) -> None:
    result = result_of(headline)
    assert result.labels == ()
    assert {m.rule_id for m in result.matches} & {"guidance_raised", "guidance_cut"} == set()


# ------------------------------------------------------------- AUDIT-L3 and L4


def test_audit_l3_an_ampersand_company_name_attributes() -> None:
    assert labelled("Deere & Co. Q3 EPS $4.75 Beats $3.89 Estimate") == {"DE": "earnings_beat bullish"}
    assert labelled("Wells Fargo Raises Full-Year Outlook") == {"WFC": "guidance_raised bullish"}
    assert labelled("McCormick Lowers Full-Year Outlook") == {"MKC": "guidance_cut bearish"}


def test_audit_l4_nothing_is_suppressed_silently() -> None:
    """A veto, a near-miss and a suppressed name each leave a record."""
    vetoed = result_of("Intel Suspends Share Buyback Program to Fund Foundry").vetoed
    assert [(v.rule_id, v.reason != "") for v in vetoed] == [("buyback", True)]
    near_miss = result_of("Morgan Stanley Raises Price Target on Nvidia, Cites Strong Outlook").vetoed
    assert "guidance_raised" in [v.rule_id for v in near_miss]
    word = result_of("Nvidia Stock Rises on News It Beat Estimates").suppressed
    assert any(s.tickers == ("NWSA",) for s in word)


# ------------------------------------------------------------- name normalisation


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [
        # REAL names from the recorded asset list.
        ("Apple Inc. Common Stock", "Apple"),
        ("Meta Platforms, Inc. Class A Common Stock", "Meta Platforms"),
        ("NVIDIA Corporation Common Stock", "NVIDIA"),
        ("JPMorgan Chase & Co.", "JPMorgan Chase"),
        ("Eaton Corporation, plc Ordinary Shares", "Eaton"),
        ("Carnival Corporation Ltd.", "Carnival"),
        ("Seagate Technology Holdings PLC Ordinary Shares (Ireland)", "Seagate Technology Holdings"),
        ("Willis Towers Watson Public Limited Company Ordinary Shares", "Willis Towers Watson"),
        ("BERKSHIRE HATHAWAY Class B", "BERKSHIRE HATHAWAY"),
        ("LyondellBasell Industries N.V. Class A", "LyondellBasell Industries"),
        ("Bunge Global SA", "Bunge Global"),
        ("AMC ENTERTAINMENT HOLDINGS, INC.", "AMC ENTERTAINMENT HOLDINGS"),
        ("Mastercard Incorporated", "Mastercard"),
        ("Garmin Ltd", "Garmin"),
        ("Royal Caribbean Group", "Royal Caribbean Group"),
        ("State Street SPDR S&P 500 ETF Trust", "State Street SPDR S&P 500 ETF Trust"),
        # A right is not the common stock: its name is left long so it rarely matches.
        ("AIB Acquisition Corporation Rights", "AIB Acquisition Corporation Rights"),
        # SYNTHETIC
        ("Alphabet Inc. Class C Capital Stock", "Alphabet"),
        ("The Walt Disney Company", "Walt Disney"),
        ("Arm Holdings plc American Depositary Shares", "Arm Holdings"),
        ("Boeing Company (The) Common Stock", "Boeing"),
        ("Inc.", "Inc."),  # never stripped to nothing
        # AUDIT-L3: a trailing "&" left by stripping "Company" goes too.
        ("Deere & Company Common Stock", "Deere"),
        ("Wells Fargo & Company Common Stock", "Wells Fargo"),
        ("McCormick & Company, Incorporated Non-Voting Common Stock", "McCormick"),
        ("Johnson & Johnson Common Stock", "Johnson & Johnson"),
    ],
)
def test_asset_names_are_normalised_by_stripping_legal_and_class_suffixes(raw: str, normalised: str) -> None:
    assert normalise_company_name(raw) == normalised


def test_the_suffix_list_is_closed_and_carries_the_spec_s_examples() -> None:
    assert isinstance(LEGAL_AND_CLASS_SUFFIXES, tuple)
    for named in ("Inc.", "Corporation", "Common Stock", "Class A"):
        assert named in LEGAL_AND_CLASS_SUFFIXES
    for kept in ("Holdings", "Group", "Trust", "Rights", "Warrants", "Units"):
        assert kept not in LEGAL_AND_CLASS_SUFFIXES
    # Longest first, so "& Co." is tried before "Co." and "Public Limited Company" before "Company".
    assert LEGAL_AND_CLASS_SUFFIXES.index("& Co.") < LEGAL_AND_CLASS_SUFFIXES.index("Co.")
    assert LEGAL_AND_CLASS_SUFFIXES.index("Public Limited Company") < LEGAL_AND_CLASS_SUFFIXES.index("Company")


# ------------------------------------------------------------- the 5.1b re-audit (round 2)
#
# AUDIT2-<id> are headlines the second adversarial audit found the round-1
# module labelling wrongly. Each pins the outcome of the narrowed shape that
# replaced the one at fault.


@pytest.mark.parametrize(
    "headline",
    [
        # H-A: the verb-to-expectation span crossed a clause into another company.
        "Oracle Surpasses Microsoft in Cloud; Analysts Cut Estimates",
        "Apple Beats Back Lawsuit as Analysts Trim Estimates",
        "Fastly Trails Rivals Despite Beating Estimates",
        "Microsoft Tops Apple in Market Value as Analysts Raise Estimates",
        "Apple Trails Microsoft in AI Race as Analysts Cut Estimates",
        "Nvidia Exceeds $4 Trillion Value Even as Analysts Slash Forecasts",
        "Apple Tops Nvidia Again: Estimates Cut",
        "Oracle Beats Microsoft to Cloud Deal; Analysts Raise Estimates",
        # H-B: a loss inverts the direction, so a loss beat or miss is silent.
        "Fastly Q3 Net Loss Exceeds Estimates",
        "Apple Q3 Loss Below Estimates",
        "Fastly Q3 Loss Beats Estimates",
        "Fastly Q3 Losses Surpass Estimates",
        "Apple Reports Q3 Loss Below Estimates",
        # A beat and a miss in one headline is mixed, not either.
        "Apple Revenue Beats Estimates, EPS Misses",
        "Apple Q3 EPS Beats Estimate, Guidance Below Views",
        "Apple Beats Estimates; Nvidia Misses",
    ],
)
def test_audit2_h_a_h_b_an_earnings_phrase_never_crosses_a_clause_or_reads_a_loss(headline: str) -> None:
    assert labelled(headline) == {}


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Oracle Exceeds Expectations", {"ORCL": "earnings_beat bullish"}),
        ("Apple Q3 Sales Below Estimates", {"AAPL": "earnings_miss bearish"}),
        ("Apple Q3 Revenue Tops Wall Street Estimates", {"AAPL": "earnings_beat bullish"}),
        ("Oracle Q1 EPS Beats Analysts' Estimates", {"ORCL": "earnings_beat bullish"}),
    ],
)
def test_audit2_h_a_an_earnings_phrase_still_fires_over_closed_filler(headline: str, expected: dict[str, str]) -> None:
    assert labelled(headline) == expected


def test_audit2_h_b_a_loss_beat_is_recorded_as_vetoed_not_dropped() -> None:
    result = result_of("Fastly Q3 Net Loss Exceeds Estimates")
    assert result.matches == ()
    assert [v.rule_id for v in result.vetoed] == ["earnings_beat"]
    assert "loss" in result.vetoed[0].reason


@pytest.mark.parametrize(
    "headline",
    [
        "Oracle Withdraws Offer to Acquire Fastly for $2 Billion",
        "Oracle Drops Plan to Acquire Fastly for $2 Billion",
        "Oracle Shelves Plan to Acquire Fastly for $2 Billion",
        "Oracle Ditches Bid to Acquire Fastly for $2 Billion",
        "Oracle No Longer Plans to Acquire Fastly for $2 Billion",
        "Apple Says It Has No Plans to Acquire Fastly for $2 Billion",
        "Activist Urges Oracle to Acquire Fastly for $2 Billion",
        "Akamai Outbids Oracle to Acquire Fastly for $2 Billion",
        "Oracle Said to Acquire Fastly for $2 Billion",
        "Oracle Wins Bid to Acquire Fastly for $2 Billion",
        "Oracle Loses Bid to Acquire Fastly for $2 Billion",
        "Oracle Offers to Acquire Fastly for $2 Billion",
        "Oracle Plans to Acquire Fastly for $2 Billion",
        "Private Equity Firm to Acquire Fastly for $2 Billion",  # the acquirer is not named
    ],
)
def test_audit2_h_c_an_acquisition_that_is_not_the_main_event_labels_nobody(headline: str) -> None:
    result = result_of(headline)
    assert result.labels == ()
    assert all(m.rule_id != "mna_target" or not m.tickers for m in result.matches)


def test_audit2_h_c_a_withdrawn_offer_records_why_the_target_went_unlabelled() -> None:
    (match,) = result_of("Activist Urges Oracle to Acquire Fastly for $2 Billion").unattributed
    assert match.rule_id == "mna_target"
    assert "actor" in match.attribution


@pytest.mark.parametrize(
    "headline",
    [
        "Chevron to Acquire Hess for $53 Billion",
        "Chevron Agrees to Acquire Hess for $53 Billion",
        "Chevron Has Agreed to Acquire Hess for $53 Billion",
        "Chevron Will Acquire Hess for $53 Billion",
        "Chevron Acquires Hess for $53 Billion",
        "Chevron Buys Hess for $53 Billion",
        "Chevron (NYSE: CVX) to Acquire Hess for $53 Billion",
        "BRIEF-Chevron To Acquire Hess For $53 Billion",
        "Oil Prices Slip; Chevron to Acquire Hess for $53 Billion",
    ],
)
def test_audit2_h_c_a_named_actor_straight_before_the_verb_labels_the_target(headline: str) -> None:
    assert labelled(headline) == {"HES": "mna_target bullish"}


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Oracle Receives EU Nod for Fastly Takeover Offer", {}),
        ("Oracle Receives Financing for Fastly Takeover Bid", {}),
        ("Oracle Receives Takeover Bid for Fastly", {}),
        ("Oracle Receives Shareholder Backing for Fastly Takeover Bid", {}),
        ("Fastly Receives Takeover Offer From Oracle", {"FSLY": "mna_target bullish"}),
        ("Fastly Receives a Buyout Proposal", {"FSLY": "mna_target bullish"}),
    ],
)
def test_audit2_h_d_receives_a_takeover_offer_is_contiguous(headline: str, expected: dict[str, str]) -> None:
    assert labelled(headline) == expected


@pytest.mark.parametrize(
    "headline",
    [
        # Denials: the gap between title and verb is closed, so none can fit.
        "Apple CEO Refuses to Step Down",
        "Apple CEO Rejects Calls to Step Down",
        "Apple CEO Tim Cook Isn't Leaving",
        "Apple CEO Tim Cook Dismisses Talk He Will Retire",
        "Apple CEO Tim Cook Won't Step Down",
        "Apple CEO Isn't Stepping Down",
        "Apple CEO Will Not Step Down, Company Says",
        "Apple CEO Tim Cook Denies He Resigns",
        "Apple CEO Tim Cook Vows Never to Retire",
        "Apple CEO Not Planning to Retire",
        # Other companies' executives, former ones, and other roles.
        "Apple CEO Tim Cook Praises Oracle Chairman, Who Retires",
        "Former Intel CEO Resigns From Oracle Board",
        "Former Apple CFO Steps Down as Fastly Director",
        "Fastly Ex-CEO Resigns From Apple Board",
        "Apple Board Member and Former Fastly CEO Resigns",
        "Apple's CFO Steps Down From Fastly Board",
        "Fastly Names Apple CFO, Who Steps Down From Apple",
        "Chipotle CEO Brian Niccol to Leave for Starbucks",
        "Fastly CEO Departs for Apple",
        # PLAUSIBLE: a change of one role, and an exit that is not from the job.
        "Fastly CEO Steps Down as Chairman, Remains CEO",
        "Fastly CEO Exits Apple Stake",
        "Fastly CEO Retires From Board But Stays On",
        "Fastly CEO Fired Up About AI",
    ],
)
def test_audit2_h_e_a_departure_is_the_title_s_own_contiguous_completed_departure(headline: str) -> None:
    assert labelled(headline) == {}


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Starbucks CEO Laxman Narasimhan Steps Down", {"SBUX": "executive_departure bearish"}),
        ("Fastly CEO to Retire, Names Successor", {"FSLY": "executive_departure bearish"}),
        ("Apple CEO Says Fastly CFO Steps Down", {"FSLY": "executive_departure bearish"}),
        ("Fastly CEO Steps Down After Apple Deal", {"FSLY": "executive_departure bearish"}),
        ("Intel CFO Resigns, Effective Immediately", {"INTC": "executive_departure bearish"}),
        ("Fastly's CEO Steps Down", {"FSLY": "executive_departure bearish"}),
    ],
)
def test_audit2_h_e_a_departure_still_fires_on_the_narrow_shape(headline: str, expected: dict[str, str]) -> None:
    assert labelled(headline) == expected


#: A book that also knows Toast, whose name is an ordinary word.
_TAG_BOOK = NameBook(
    watch=WATCH,
    watch_names=WATCH_COMPANY_NAMES,
    asset_names={**ASSET_NAMES, "TOST": "Toast, Inc. Class A Common Stock"},
    funds=FUNDS,
)


@pytest.mark.parametrize(
    ("headline", "tag"),
    [
        # H-F: the acquirer is suppressed as a word name, so it looked unnamed.
        ("Adenza Agrees to Be Acquired by Nasdaq for $10.5 Billion", "NDAQ"),
        ("Shipt to Be Acquired by Target", "TGT"),
        ("Startup Receives Takeover Offer From Toast", "TOST"),
        # ... covered by a longer name.
        ("Startup Receives Takeover Offer From Apple Hospitality REIT", "AAPL"),
        # ... named only by the first word of its name.
        ("Biotech Startup Receives Complete Response Letter; Regeneron Partner", "REGN"),
        # ... or not named at all, in a role after by / from / at / for.
        ("Startup Agrees to Be Acquired by Private Equity Firm", "BX"),
    ],
)
def test_audit2_h_f_the_single_tag_rule_never_lands_on_a_company_named_in_another_role(
    headline: str, tag: str
) -> None:
    assert labelled(headline, NewsFeed.ALPACA_NEWS, (tag,), _TAG_BOOK) == {}


def test_audit2_h_f_the_single_tag_rule_still_applies_to_a_headline_with_no_name_in_it() -> None:
    assert labelled("Company Agrees to Be Acquired", NewsFeed.ALPACA_NEWS, ("FSLY",), _TAG_BOOK) == {
        "FSLY": "mna_target bullish"
    }


@pytest.mark.parametrize(
    "headline",
    [
        "Did Apple Beat Estimates Last Quarter? Here's What Happened",
        "Apple Beats Estimates? Not Quite",
        "Apple Q3 Earnings: Beats Estimates? Not Quite",
        "Apple vs. Nvidia: Which Beats Estimates?",
        "Does Apple Beat Estimates Every Quarter",
        "Is Fastly Raising Guidance Again",
    ],
)
def test_audit2_m_b_a_question_anywhere_in_the_headline_is_silent(headline: str) -> None:
    result = result_of(headline)
    assert result.labels == ()
    assert result.matches == ()


def test_audit2_m_b_a_question_is_recorded_as_one() -> None:
    (vetoed,) = result_of("Apple Beats Estimates? Not Quite").vetoed
    assert (vetoed.rule_id, vetoed.reason) == ("earnings_beat", "a question, not an event")


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        # M-C: the third approval shape's noun ends its clause or names a product.
        ("Merck Gets FDA Approval Delay", {}),
        ("Merck Gets FDA Nod Delayed", {}),
        ("Merck Receives FDA Clearance to Begin Trial", {}),
        ("Merck Gets FDA Approval to Start Phase 3 Trial", {}),
        ("Merck Gets FDA Clearance for Trial", {}),
        ("Merck Wins FDA Approval to Test Drug in Humans", {}),
        ("Merck Wins FDA Approval Extension", {}),
        ("Merck Receives FDA Approval Rejection", {}),
        ("Merck Wins FDA Approval", {"MRK": "fda_approval bullish"}),
        ("Merck Wins FDA Approval, Shares Rise", {"MRK": "fda_approval bullish"}),
        ("Merck Wins FDA Approval for Keytruda", {"MRK": "fda_approval bullish"}),
        # PLAUSIBLE: the possessed noun is not the sponsor's product.
        ("FDA Approves Merck's Plan to Withdraw Drug", {}),
        ("FDA Approves Merck's Request to Halt Trial", {}),
        ("FDA Grants Approval to Merck's Rival Drug From Pfizer", {}),
        ("FDA Approves Fastly's Copy of Merck's Keytruda", {}),
        ("FDA Rejects Merck's Petition to Block Rival Drug", {}),
        ("FDA Rejects Merck's Rival's Drug", {}),
        ("FDA Approves Merck's Keytruda", {"MRK": "fda_approval bullish"}),
        ("FDA Approves Merck’s Keytruda for New Use", {"MRK": "fda_approval bullish"}),
    ],
)
def test_audit2_m_c_an_fda_approval_is_of_the_sponsor_s_product_and_final(
    headline: str, expected: dict[str, str]
) -> None:
    assert labelled(headline) == expected


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        # M-D: "of <another company>" after the offering, its stock or its shares.
        ("Oracle Announces Secondary Offering of Common Stock of Fastly", {}),
        ("Oracle Prices Secondary Offering of Fastly Shares", {}),
        ("Fastly Announces Pricing of Secondary Offering by Oracle", {}),
        ("Oracle Announces Secondary Offering of Common Stock of Lykos", {}),  # Lykos is not in the book
        ("Fastly Prices Secondary Offering of 5,000,000 Shares", {"FSLY": "secondary_offering bearish"}),
        ("Fastly Announces Secondary Offering of Common Stock", {"FSLY": "secondary_offering bearish"}),
    ],
)
def test_audit2_m_d_an_offering_of_another_company_s_shares_labels_nobody(
    headline: str, expected: dict[str, str]
) -> None:
    assert labelled(headline) == expected


def test_audit2_an_offering_naming_a_company_after_it_records_why() -> None:
    (match,) = result_of("Fastly Announces Pricing of Secondary Offering by Oracle").unattributed
    assert "after the phrase" in match.attribution


# ------------------------------------------------------------- the 5.1b re-audit (round 3)
#
# AUDIT3-<id> are headlines the third audit, on a realistic corpus, found the
# round-2 module labelling wrongly. As before, each fix narrows the shape at
# fault rather than adding a veto.


#: AUDIT3-H1: a bank's or a rating agency's own forecast, of the economy or of
#: somebody else. Every one is a guidance phrase with a named subject -- the
#: forecaster -- and none is that company's guidance.
_FORECASTERS_FORECASTS = (
    "Morgan Stanley cuts its forecast for euro zone growth",
    "Morgan Stanley lowers outlook on Fastly",
    "Bank of America raises its forecast for US economic growth",
    "BofA raises its outlook for gold prices",
    "Wells Fargo raises forecast for 2025 GDP growth",
    "Moody's lowers outlook on Boeing to negative",
    "Barclays raises its forecast for Fed rate cuts this year",
    "Deutsche Bank lowers forecast for ECB rate cuts - Reuters",
    "Moody's lowers outlook on Israel to negative",
    "Moody's raises outlook on Greece to positive",
    "Moody's cuts outlook on US banks",
    "Wells Fargo raises its outlook for US housing starts",
    "Morgan Stanley cuts its outlook on Tesla",
    "Barclays lowers outlook on European banks",
    "Moody's raises its 2025 forecast for global growth",
    "Moody's cuts outlook for US banking system to negative",
    "Morgan Stanley raises forecast for Fed cuts to three this year",
    "BofA cuts forecast for Fed rate cuts, sees one in 2025",
    "Goldman Sachs raises its forecast for Brent crude",
    # A period phrase followed by more noun is not the company's period.
    "Walmart raises forecast for 2025 GDP growth",
)


@pytest.mark.parametrize("headline", _FORECASTERS_FORECASTS)
@pytest.mark.parametrize(
    ("feed", "tags"),
    [(NewsFeed.FINNHUB_MARKET, ()), (NewsFeed.FINNHUB_COMPANY, ("BA",)), (NewsFeed.ALPACA_NEWS, ("BA",))],
)
def test_audit3_h1_a_forecaster_s_own_forecast_is_nobody_s_guidance(
    headline: str, feed: NewsFeed, tags: tuple[str, ...]
) -> None:
    result = result_of(headline, feed, tags)
    assert result.labels == ()
    assert {m.rule_id for m in result.matches} & {"guidance_raised", "guidance_cut"} == set()


def test_audit3_h1_a_forecaster_s_forecast_is_recorded_as_a_near_miss() -> None:
    (vetoed,) = result_of("Morgan Stanley cuts its forecast for euro zone growth").vetoed
    assert vetoed.rule_id == "guidance_cut"
    assert "guidance" in vetoed.reason


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Wells Fargo Raises Full-Year Outlook", {"WFC": "guidance_raised bullish"}),
        ("Walmart raises annual forecast after strong quarter", {"WMT": "guidance_raised bullish"}),
        ("Walmart Boosts Outlook After Strong Q2", {"WMT": "guidance_raised bullish"}),
        ("Nvidia raises full-year forecast again", {"NVDA": "guidance_raised bullish"}),
        ("Oracle raises guidance for fiscal 2026", {"ORCL": "guidance_raised bullish"}),
        ("Apple Raises Outlook for Q4", {"AAPL": "guidance_raised bullish"}),
        ("Walmart raises forecast for 2025", {"WMT": "guidance_raised bullish"}),
        ("Microsoft lowers outlook for the second half", {"MSFT": "guidance_cut bearish"}),
        ("Fastly cuts outlook for the full year, shares slide", {"FSLY": "guidance_cut bearish"}),
        ("Intel cuts forecast for the quarter as PC demand slows", {"INTC": "guidance_cut bearish"}),
        ("Intel lowers forecast amid weak PC demand", {"INTC": "guidance_cut bearish"}),
    ],
)
def test_audit3_h1_a_company_s_own_guidance_still_fires(headline: str, expected: dict[str, str]) -> None:
    assert labelled(headline) == expected


@pytest.mark.parametrize(
    ("headline", "tag"),
    [
        ("US Consumer Prices Rise 0.2% In September, Below Expectations", "SPY"),
        ("US Core PCE Price Index Tops Estimates", "SPY"),
        ("US Unemployment Rate Falls To 4.1%, Below Expectations", "SPY"),
        ("US Initial Jobless Claims Fall To 210,000, Below Estimates", "SPY"),
        ("US Retail Sales Top Estimates In August", "QQQ"),
        ("Company Prices $1.5B Common Stock Offering", "IPO"),
    ],
)
@pytest.mark.parametrize("feed", [NewsFeed.ALPACA_NEWS, NewsFeed.MASSIVE_NEWS])
def test_audit3_m1_the_single_tag_rule_never_lands_on_a_fund(headline: str, tag: str, feed: NewsFeed) -> None:
    """A macro release tagged with a market fund is about the economy, and its
    direction for the fund is often the reverse of the phrase's."""
    result = result_of(headline, feed, (tag,))
    assert result.labels == ()
    assert all(m.tickers == () for m in result.matches)


def test_audit3_m1_which_tickers_are_funds_is_the_book_s_input_not_a_list_of_names() -> None:
    headline = "Company Prices $1.5B Common Stock Offering"
    zed = {"ZZZF": "Zed Equity ETF"}
    spy = {"SPY": "SPDR S&P 500 ETF Trust"}
    as_fund = NameBook(watch=frozenset(), watch_names={}, asset_names=zed, funds=frozenset({"ZZZF"}))
    not_fund = NameBook(watch=frozenset(), watch_names={}, asset_names=zed, funds=())
    spy_not_fund = NameBook(watch=frozenset(), watch_names={}, asset_names=spy, funds=())
    assert labelled(headline, NewsFeed.ALPACA_NEWS, ("ZZZF",), as_fund) == {}
    assert labelled(headline, NewsFeed.ALPACA_NEWS, ("ZZZF",), not_fund) == {"ZZZF": "secondary_offering bearish"}
    assert labelled(headline, NewsFeed.ALPACA_NEWS, ("SPY",), spy_not_fund) == {"SPY": "secondary_offering bearish"}


def test_audit3_m1_a_named_fund_is_still_a_mention() -> None:
    """Only the single-tag rule stays off funds: a headline that names one is read."""
    assert labelled("$SPY Prices $1.5B Common Stock Offering") == {"SPY": "secondary_offering bearish"}


# 5.2 audit M1: the asset list carries every listed ETF, but ``funds`` is only
# the curated universe's five -- so a macro release tagged with any other ETF
# alone used to land as an earnings label on it. Names as the broker lists them.
_UNCURATED_ETFS = {
    "DIA": "SPDR Dow Jones Industrial Average ETF Trust",
    "VOO": "Vanguard S&P 500 ETF",
    "TLT": "iShares 20+ Year Treasury Bond ETF",
    "XLF": "Financial Select Sector SPDR Fund",
    "USO": "United States Oil Fund, LP",
}
_ETF_BOOK = NameBook(
    watch=WATCH,
    watch_names=WATCH_COMPANY_NAMES,
    asset_names={**ASSET_NAMES, **_UNCURATED_ETFS},
    funds=FUNDS,
)
#: What an unattributed earnings match says about why, in part.
_EARNINGS_REASON = "an earnings phrase never takes the article's only tag"
_MACRO_RELEASES = (
    "Jobless Claims Below Expectations",
    "US Consumer Prices Rise 0.2%, Below Expectations",
    "US Initial Jobless Claims Fall To 210,000, Below Estimates",
    "US Retail Sales Top Estimates In August",
    "US Core PCE Price Index Tops Estimates",
)


@pytest.mark.parametrize("tag", sorted(_UNCURATED_ETFS))
@pytest.mark.parametrize("headline", _MACRO_RELEASES)
@pytest.mark.parametrize("feed", [NewsFeed.ALPACA_NEWS, NewsFeed.MASSIVE_NEWS])
def test_5_2_m1_a_subjectless_earnings_phrase_never_lands_on_a_lone_tag(
    headline: str, tag: str, feed: NewsFeed
) -> None:
    """Macro releases speak beat/miss/estimates and carry a lone index or ETF
    tag, which the fund list cannot be relied on to cover: the earnings family
    never takes the single-tag rule, so the phrase attributes to nobody."""
    assert _ETF_BOOK.is_equity(tag) and not _ETF_BOOK.is_fund(tag)
    result = result_of(headline, feed, (tag,), _ETF_BOOK)
    assert result.labels == ()
    assert result.matches, "the phrase must still fire, or this test proves nothing"
    assert all(m.tickers == () for m in result.matches)
    assert result.unattributed == result.matches
    assert all(_EARNINGS_REASON in m.attribution for m in result.unattributed)


@pytest.mark.parametrize(
    ("headline", "tag", "expected"),
    [
        ("Intel Q3 EPS Beats Estimates", "INTC", {"INTC": "earnings_beat bullish"}),
        ("Netflix Misses Estimates", "NFLX", {"NFLX": "earnings_miss bearish"}),
        ("Intel Q3 EPS Beats Estimates", "XLF", {"INTC": "earnings_beat bullish"}),
    ],
)
def test_5_2_m1_an_earnings_phrase_with_a_named_subject_still_labels(
    headline: str, tag: str, expected: dict[str, str]
) -> None:
    assert labelled(headline, NewsFeed.ALPACA_NEWS, (tag,), _ETF_BOOK) == expected


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Company Raises Full-Year Guidance", "guidance_raised bullish"),
        ("Company Prices $1.5B Common Stock Offering", "secondary_offering bearish"),
        ("Company Agrees to Be Acquired", "mna_target bullish"),
    ],
)
def test_5_2_m1_the_single_tag_rule_still_applies_to_every_other_family(headline: str, expected: str) -> None:
    """Only earnings is excluded; the spec's single-tag rule stands for the rest."""
    assert labelled(headline, NewsFeed.ALPACA_NEWS, ("FSLY",), _ETF_BOOK) == {"FSLY": expected}


def test_5_2_m1_a_subjectless_earnings_phrase_on_a_lone_company_tag_is_unattributed() -> None:
    """The exclusion is by family, not by what the tag is: no lone tag, fund or
    company, takes an earnings label for a phrase whose subject is unnamed."""
    result = result_of("Company Beats Estimates", NewsFeed.ALPACA_NEWS, ("FSLY",), _ETF_BOOK)
    assert result.labels == ()
    assert [m.rule_id for m in result.unattributed] == ["earnings_beat"]
    assert _EARNINGS_REASON in result.unattributed[0].attribution


def _synthetic_book(size: int) -> NameBook:
    rng = random.Random(7)
    assets = dict(ASSET_NAMES)
    while len(assets) < size:
        symbol = "".join(rng.choice(string.ascii_uppercase) for _ in range(rng.randint(1, 5)))
        if symbol in assets:
            continue
        words = [
            "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(3, 10))).title()
            for _ in range(rng.randint(1, 3))
        ]
        assets[symbol] = " ".join(words) + rng.choice([" Inc. Common Stock", " Corporation", " Holdings, Inc."])
    return NameBook(watch=WATCH, watch_names=WATCH_COMPANY_NAMES, asset_names=assets, funds=FUNDS)


@cache
def _book_of_12000() -> NameBook:
    return _synthetic_book(12_000)


def _timed(call: Any) -> float:
    started = time.perf_counter()
    call()
    return time.perf_counter() - started


def test_audit3_m2_a_matched_full_length_headline_is_labelled_in_under_10ms_on_a_12000_ticker_book() -> None:
    """AUDIT3-M2: name lookup ran one regex per ticker, about 114 ms at 512
    characters on a 12,000-ticker book. Every article is labelled once it is
    stored, so the lookup is indexed and the headline scanned once."""
    book = _book_of_12000()
    base = (
        "Morgan Stanley Upgrades Apple to Overweight, Raises Price Target to $275 on Strong iPhone Cycle "
        "and Services Growth; "
    )
    headline = (base * 6)[:MAX_HEADLINE_CHARS]
    assert len(headline) == MAX_HEADLINE_CHARS
    assert labelled(headline, book=book) == {"AAPL": "analyst_upgrade bullish"}
    best = min(_timed(lambda: label_headline(headline, NewsFeed.FINNHUB_MARKET, (), book)) for _ in range(7))
    assert best < 0.010, f"{best * 1e3:.1f} ms"


def test_audit3_m2_the_large_book_reads_the_same_names_as_the_small_one() -> None:
    """The index changes how names are found, never which: these headlines
    read the same on the 12,000-ticker book as on the small one."""
    big = _book_of_12000()
    for headline in (
        *_FORECASTERS_FORECASTS,
        "Apple Hospitality REIT Cuts Full-Year Outlook",
        "Fastly (Nasdaq: FSLY) Beats Estimates",
        "Apple's Q3 EPS Beats Estimate",
        "Johnson & Johnson Q3 EPS Beats Estimate",
        "C3.ai Beats Estimates; NVDA Raises Outlook; Cboe Global Markets-listed Fastly",
    ):
        assert BOOK.mentions(headline) == big.mentions(headline), headline


@pytest.mark.parametrize(
    "headline",
    [
        "Analysts Expect Apple To Announce $100 Billion Share Buyback",
        "Apple Expected To Unveil $100 Billion Share Buyback",
        "Apple to Announce $90 Billion Share Buyback",
    ],
)
def test_audit3_l1_an_expected_buyback_is_not_a_buyback(headline: str) -> None:
    result = result_of(headline)
    assert result.labels == ()
    assert [v.rule_id for v in result.vetoed] == ["buyback"]


def test_audit3_l1_a_buyback_of_up_to_an_amount_still_fires() -> None:
    assert labelled("Apple Board Authorizes Up To $110 Billion Share Buyback") == {"AAPL": "buyback bullish"}


@pytest.mark.parametrize(
    ("headline", "expected", "feed", "tags"),
    [
        ("Netflix Q3 Earnings Top Estimates As Subscriber Growth Accelerates", {"NFLX": "earnings_beat bullish"},
         NewsFeed.ALPACA_NEWS, ("NFLX",)),
        ("Netflix (NFLX) Q3 Earnings and Revenues Top Estimates", {"NFLX": "earnings_beat bullish"},
         NewsFeed.MASSIVE_NEWS, ("NFLX",)),
        ("Ford (F) Surpasses Q3 Earnings and Revenue Estimates", {"F": "earnings_beat bullish"},
         NewsFeed.MASSIVE_NEWS, ("F",)),
        ("Oracle (ORCL) Misses Q1 Earnings and Revenue Estimates", {"ORCL": "earnings_miss bearish"},
         NewsFeed.MASSIVE_NEWS, ("ORCL",)),
        ("Apple Board Authorizes Additional $110B For Share Repurchases", {"AAPL": "buyback bullish"},
         NewsFeed.ALPACA_NEWS, ("AAPL",)),
        ("Apple authorizes additional $110B in share buybacks", {"AAPL": "buyback bullish"},
         NewsFeed.FINNHUB_COMPANY, ("AAPL",)),
        ("FDA Approves Novo Nordisk's Wegovy For Heart Risk Reduction", {"NVO": "fda_approval bullish"},
         NewsFeed.ALPACA_NEWS, ("NVO",)),
        # Still silent: a roundup, a paused buyback, a fund-tagged release.
        ("Top Analyst Forecasts For Tuesday: Nvidia, Apple, Microsoft", {}, NewsFeed.FINNHUB_MARKET, ()),
        ("Berkshire Hathaway Pauses Share Buybacks", {}, NewsFeed.ALPACA_NEWS, ("BRK.B",)),
        ("US Initial Jobless Claims Top Estimates", {}, NewsFeed.ALPACA_NEWS, ("SPY",)),
    ],
)  # fmt: skip
def test_audit3_bounded_recall(
    headline: str, expected: dict[str, str], feed: NewsFeed, tags: tuple[str, ...]
) -> None:
    assert labelled(headline, feed, tags) == expected


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [
        ("Novo Nordisk A/S", "Novo Nordisk"),
        ("Equinor ASA American Depositary Shares", "Equinor"),
        ("Volvo AB", "Volvo"),
        ("Nokia Oyj", "Nokia"),
    ],
)
def test_audit3_nordic_legal_forms_are_stripped(raw: str, normalised: str) -> None:
    assert normalise_company_name(raw) == normalised
