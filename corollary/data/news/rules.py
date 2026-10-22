"""The rules tier: deterministic headline patterns, and the ticker each match attaches to. Pure.

Phase 3 design, decision 4: *"Every headline in the store is labelled by every
source that can reach it: the rules tier on every headline ..."* Decision 21
names the eight families every current pattern belongs to -- earnings
beat/miss, guidance raised/cut, analyst upgrade/downgrade, M&A, secondary
offering, buyback, executive departure, FDA approval/rejection -- and leaves
the patterns themselves to step 5.

The governing principle is decision 21 / PRD section 9: **"silence beats a
wrong label."** Every label is graded by the self-audit, and every off-watch
label can put a ticker on the Movers panel. A missed label is a false
negative; a wrong ticker or a wrong direction is the failure. So wherever this
module cannot tell, it emits nothing -- and returns what it saw, so the
re-measurement can count what silence cost.

Provenance
----------

:data:`RULE_PATTERNS` is step 5's v1 set: the step-0 draft
(``scripts/probe_phase3.py`` ``DRAFT_PATTERNS``) moved into the engine, each
pattern given a stable ``rule_id``, its family and a discovery flag, then
hardened twice -- where the draft was plainly wrong on a test headline
(``DRAFT-BUG`` in ``tests/data/news/test_rules.py``) and where the unit 5.1b
audit found this module wrong (``AUDIT-<id>``, ``AUDIT2-<id>``, and ``AUDIT3-<id>``
from the third, on a realistic corpus). The draft stays in the probe
script unchanged: the spec states that re-running ``probe_phase3.py labels``
reproduces step 0's measurement, which needs the draft, not these.

Shapes: every phrase is bound to one subject
--------------------------------------------

*"A rules label attaches to a ticker under step 5's rule: the headline names
the ticker."* That rule is the floor, not a licence: a headline names its
subject, but it also names acquirers, brokers, speakers and rivals. So a
pattern is a set of :class:`Shape` s, and each shape says where its subject
sits:

* **Subject before the phrase** (most shapes): the subject is the nearest
  named company *before* the matched phrase, and the text between the two
  must be made only of the shape's closed list of filler tokens -- *Q3 EPS
  $1.57*, *Board Authorizes $110 Billion*, a ``(TICKER)``. Anything else in
  between (a clause boundary ``,`` ``;``, *as*, *after*, *says*, another noun)
  means the phrase has no named subject.
* **Object after the verb** (a shape whose regex has a named group
  ``object``): *Upgrades <X> to Buy*, *FDA approves <X>'s*, *to acquire <X>
  for $...*. The object must be exactly one named company, and the object
  slot is one to eight space-separated tokens, never a free character run. A
  name after *by* / *at* / *from* is never an object. An acquisition also
  needs its **actor**: one named company, alone at the start of its clause,
  followed by nothing but *to* / *will* / *agrees to* / *has agreed to*.

Every shape is written as a short contiguous span, and a phrase whose span
names another company, or (for an offering) is followed by one, binds nobody.
The second 5.1b audit (``AUDIT2-<id>``) showed that patching phrasings one at
a time does not converge, so where a shape was the source of an error it was
narrowed rather than given another veto: the earnings verb reaches its
expectation over at most four closed filler tokens; *receives a takeover
offer* is contiguous; an executive departure is *<Company> <title> (<name of
up to three words>) <verb>* with a closed tail, or *<title> to <verb>* with no
name at all; the third FDA approval shape ends its clause or names a product.

A phrase whose subject is a coordinated list (*Apple and Nvidia Beat*), or
whose object names more than one company, is **ambiguous** and labels
nobody. A phrase with no subject of its own that directly follows a bound
phrase across ``,`` / ``;`` / *and* takes that phrase's subject: *Oracle Beats
Estimate, Raises Guidance*.

Names
-----

A company is named by any of:

* an explicit symbol -- ``$TICKER``, ``NYSE:``/``NASDAQ:TICKER``, or
  ``(EXCHANGE: TICKER)`` -- for any symbol in the book;
* a bare ``(TICKER)`` only when it directly follows that ticker's own name
  (Zacks style, *Nvidia (NVDA)*), or the ticker is one of the article's own
  tags. A parenthetical that expands the preceding words' initials --
  *Complete Response Letter (CRL)*, *Earnings Per Share (EPS)* -- is not the
  company's name and so is not a mention. Where the two coincide, as in
  *Charles River Laboratories (CRL)*, the words *are* the company's name and
  the mention stands;
* on the **watch universe** only, the bare uppercase symbol, if it is three or
  more letters and not an ordinary word (:data:`WORD_TICKERS`);
* a company name, case-sensitively and as a whole word: the hand-kept
  :data:`WATCH_COMPANY_NAMES` for a watch ticker, otherwise the asset-list
  ``name`` passed through :func:`normalise_company_name`.

Then, and each recorded in :attr:`RulesLabelling.suppressed`:

* a normalised name that is an ordinary English or market word
  (:data:`WORD_NAMES`) is never a mention;
* where two names overlap, the longer wins (*Apple Hospitality REIT* is not
  Apple, *Meta Materials* is not Meta);
* a name used as an exchange prefix (*(Nasdaq: MU)*, *Nasdaq-listed*) is not a
  mention;
* a **rater** -- a name before *upgrades/downgrades/reiterates/raises price
  target*, before *'s analyst*, before *bullish/bearish on*, or after *at/by/
  from* following a rating verb -- is never a subject.

Tags
----

*"An article that carries exactly one equity tag from a feed that tags per
article (Alpaca ``symbols``, Massive ``tickers``) attributes to that ticker.
That single-tag rule never applies to ``finnhub_company`` rows."* Here it
applies to a phrase that has **no** named subject, and only when the headline
does not name the tagged company in any way: not as a mention, not as any
name set aside (a rater, an ordinary word, a name covered by a longer one),
not by the first word of its own name. It never applies at all when a
capitalised word follows *by*, *from*, *at* or *for* -- a name in some other
role, which may be the tag's. A lone tag that the headline names as the
acquirer or the broker is the wrong company. It never applies to a fund or
market tag -- the ``funds`` a :class:`NameBook` is built with -- since a macro
release (*US Consumer Prices Rise 0.2%, Below Expectations*) is tagged with
SPY alone, and its phrase is about the economy, often in the opposite
direction to the fund. And it never applies to an earnings phrase
(:data:`NO_SINGLE_TAG_FAMILIES`, 5.2 audit M1), whatever the tag: macro
releases speak beat/miss/estimates, and the asset list carries every listed
ETF while ``funds`` is only the curated universe's, so a release tagged DIA,
VOO, TLT, XLF or USO alone would otherwise land as an earnings label on that
fund. An earnings phrase with no named subject is unattributed, and says so.

Input
-----

Every whitespace run is collapsed to one space and the ends stripped before
anything is matched, and every phrase is taken from that text. A headline
longer than :data:`MAX_HEADLINE_CHARS` afterwards is not matched at all and
comes back with ``truncated`` set: cutting it down to label it could cut off
the veto. Each regex is also bounded on raw input in its own right, which
``test_every_regex_is_bounded_on_raw_pathological_input`` checks at 50,000
characters. Not every repeated run has a fixed upper bound. There are
bounded lazy runs (the deal-terms ``in ... deal`` window, the FDA approval's
80-character tail, the rater window before a name); unbounded whitespace
(``\\s+`` / ``\\s*``) between tokens in :func:`_filler`, ``_TO_EXPECTATION``,
the guidance, departure, M&A and rater shapes; and unbounded runs *inside* a
single token -- digits in the money and number tokens, word characters in a
name word, a wire prefix, the vetoes and the *upgraded at/by* shapes. Every
such run is a single character class followed by a fixed token or a boundary,
never nested, so none can backtrack super-linearly; the 50,000-character
test above (including one 50k word and one 50k number) is what pins that.
After normalisation every whitespace run is a single space.

A question is not an event: any ``?`` in the headline, or a headline led by an
auxiliary (*Did*, *Does*, *Will*, *Can*, *Is*, ...), labels nothing.

One headline, one direction
---------------------------

Every pattern that fires is reported in :attr:`RulesLabelling.matches`, each
with the tickers it bound and how. A headline matching a bullish **and** a
bearish pattern gets **no** rules label and is returned in
:attr:`RulesLabelling.conflicting`. Otherwise each bound ticker gets one label
(``sentiment_label`` is UNIQUE ``(article_id, ticker, source)``): ``rule_id``
is its first match in :data:`RULE_PATTERNS` order and ``reasoning`` names
every phrase bound to it. A match bound to nobody is returned in
:attr:`RulesLabelling.unattributed`. A phrase stopped by a veto, a preview or
negation, or a near-miss of a pattern's strict shape is returned in
:attr:`RulesLabelling.vetoed` with its reason. Nothing is dropped silently.

Two names in one span are labelled together: *Alphabet* is GOOGL and GOOG,
which are one company's share classes.

Known limits
------------

What the narrowed shapes still get wrong or miss. The third 5.1b audit
measured them on a realistic corpus of about 310 synthetic headlines in the
vendors' styles and the 73 recorded ones: after its fixes the one wrong label
left is a wrong ticker, the single-tag supplier/peer case below. Everything
else here is a silence, or a label on a plausible-but-arguable reading of an
adversarial headline.

* **The single-tag rule on a supplier or a peer -- a known wrong ticker.** A
  vendor tags a story about a supplier, a customer or a rival with the
  company it matters to, and when the headline names nobody the book knows,
  the lone tag takes the label: *Hon Hai CEO Steps Down* tagged AAPL labels
  Apple bearish, *Foxconn Cuts Outlook* likewise. That is the spec's rule,
  applied as written; a fund or market tag is exempt (AUDIT3-M1), and an
  earnings phrase never takes it (5.2 audit M1) -- so *SK Hynix Beats
  Estimates* tagged NVDA is silent, as is *Company Beats Estimates* tagged
  with the company it is about.
* **Guidance followed by anything but an allowed tail.** After the guidance
  noun only the clause's end, *after/amid/as/again*, or *for <period>* may
  follow (``_GUIDANCE_TAIL``, a whitelist). That is what keeps a bank's
  forecast *for* the economy, or a rating agency's outlook *on* a company,
  off the forecaster (AUDIT3-H1). Any other tail silences the label, so it
  also silences a company's own guidance given a reason or a level: *TSMC
  Raises Full-Year Revenue Outlook On AI Demand*, *Salesforce Lowers FY25
  Revenue Guidance Below Estimates*, *Apple Raises Full-Year Guidance To
  $2.50*. Those are missed labels, never wrong ones.
* **Names the normaliser leaves long.** A name is matched only as the asset
  list spells it after :func:`normalise_company_name`, case-sensitively. A
  suffix not in :data:`LEGAL_AND_CLASS_SUFFIXES` (*Unsponsored ADR*, *GDR*,
  *Series A*) stays in the name, so *Hon Hai* or *SoftBank* is not a mention
  of *Hon Hai Precision Industry Co., Ltd. Unsponsored GDR*; a short form
  (*TSMC* for *Taiwan Semiconductor Manufacturing Company*), a brand
  (*Foxconn*), or a different capitalisation (*Nike* for *NIKE*) is not a
  mention either. Each is a silence, not a wrong label.

* **Unnamed acquirers.** *Thoma Bravo to Acquire Fastly for $2 Billion* labels
  nobody when Thoma Bravo is not in the book: title case makes *Activist
  Urges Oracle* look just like a name, so an unnamed actor is never trusted.
* **Named executives with future departures.** *Starbucks CEO Laxman
  Narasimhan to Step Down* is silent; only *CEO to step down*, with no name,
  is read. *Steps down as CEO* is silent too, since *as* may name another role.
* **Function-word names.** The name slot refuses a closed set of pronouns,
  auxiliaries and speech verbs; a denial written without any of them
  (*Apple CEO Dismisses Resigns*) is ungrammatical, but would read.
* **Whose buyback.** *Apple Board Approves Fastly Share Buyback* labels
  Fastly, the company whose buyback it is, whoever approved it.
* **A rejected offer.** *Fastly Receives Takeover Offer, Board Says No* labels
  Fastly bullish; only *rejects*-style vetoes are recognised.
* **Mixed headlines.** A beat and a miss anywhere in one headline silence both
  patterns, even when they belong to different companies.
* **All-caps headlines.** Company names match case-sensitively, so in an
  all-caps headline only a bare watch symbol (*NVDA*) or an explicit symbol is
  a mention: *APPLE CEO STEPS DOWN* is silent. An executive's name is read in
  title case only, so *NVDA CEO JENSEN HUANG STEPS DOWN* is silent too.

Pure: no I/O, no clock. Same inputs, same output.
"""

import bisect
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from corollary.data.news.article import NewsArticle, NewsFeed
from corollary.data.news.labels import (
    Direction,
    LabelSource,
    SentimentLabel,
    SentimentTier,
)

__all__ = [
    "LEGAL_AND_CLASS_SUFFIXES",
    "MAX_HEADLINE_CHARS",
    "NO_SINGLE_TAG_FAMILIES",
    "RULE_PATTERNS",
    "SINGLE_TAG_FEEDS",
    "WATCH_COMPANY_NAMES",
    "WORD_NAMES",
    "WORD_TICKERS",
    "NameBook",
    "RuleFamily",
    "RuleMatch",
    "RulePattern",
    "RulesLabelling",
    "Shape",
    "SuppressedName",
    "VetoedMatch",
    "label_headline",
    "normalise_company_name",
    "normalise_headline",
    "rules_labels",
]


class RuleFamily(StrEnum):
    """Decision 21's eight discovery-flagged families."""

    EARNINGS = "earnings"
    GUIDANCE = "guidance"
    ANALYST_RATING = "analyst_rating"
    MNA = "mna"
    SECONDARY_OFFERING = "secondary_offering"
    BUYBACK = "buyback"
    EXECUTIVE_DEPARTURE = "executive_departure"
    FDA = "fda"


# --------------------------------------------------------------------------
# Filler tokens: what may sit between a subject and its phrase
# --------------------------------------------------------------------------


def _filler(*alternatives: str, limit: int = 10) -> str:
    """A gap of at most ``limit`` whitespace-separated tokens from a closed list.

    Optionally led by a possessive attached to the name (*Apple's*). Each token
    must end at a non-word character, so ``and`` never matches inside a word.
    """
    tokens = "|".join(alternatives)
    return rf"(?:['’]s?)?(?:\s+(?:{tokens})(?!\w)){{0,{limit}}}\s*"


#: A bracketed symbol, with or without an exchange prefix. Case-sensitive.
_T_PAREN: Final = (
    r"(?-i:\((?:(?:NYSE American|NYSE|NASDAQ|Nasdaq|AMEX|OTC|Cboe|CBOE)\s?:\s?)?[A-Z][A-Z.]{0,6}\))"
)
_T_LEGAL: Final = r"&\s?Company|&\s?Co\.?|Inc\.?|Corp\.?|Co\.|Company|Ltd\.?|plc|N\.V\.|S\.A\.|AG|SE|LLC"
_T_MONEY: Final = (
    r"[-−]?(?:US)?\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:[KMBT]|bn|mln|million|billion|trillion))?"
)
_T_NUMBER: Final = r"[-−]?\d[\d,]*(?:\.\d+)?%?"
_T_PERIOD: Final = (
    r"Q[1-4]|[1-4]Q|H[12]|FY\s?'?\d{2,4}|(?:first|second|third|fourth)[- ]quarter"
    r"|(?:first|second)[- ]half|fiscal|quarterly|quarter|full[- ]year|annual|year[- ]end"
)

#: The default gap: only a bracketed symbol or a legal form. Nothing else.
_GAP_BARE: Final = _filler(_T_PAREN, _T_LEGAL, limit=3)
_GAP_EARNINGS: Final = _filler(
    _T_PAREN, _T_LEGAL, _T_MONEY, _T_NUMBER, _T_PERIOD,
    r"adjusted|adj\.|GAAP|non-GAAP|core|EPS|earnings|profits?|revenues?|sales|results|net"
    r"|income|margins?|comparable|same[- ]store|per[- ]share|operating|of|and"
    r"|reports?|reported|posts?|posted|delivers?|delivered",
    limit=12,
)  # fmt: skip
_GAP_GUIDANCE: Final = _filler(_T_PAREN, _T_LEGAL, r"also|again|now|further", limit=4)
_GAP_RATED: Final = _filler(_T_PAREN, _T_LEGAL, r"stock|shares|is|gets|got|was|just|again", limit=4)
_GAP_BUYBACK: Final = _filler(
    _T_PAREN, _T_LEGAL, _T_MONEY, _T_NUMBER,
    r"board|of|directors?|authori[sz]es?|authori[sz]ed|approves?|approved|announces?|announced"
    r"|unveils?|unveiled|launches?|launched|expands?|expanded|boosts?|boosted|increases?"
    r"|increased|raises?|raised|ups|adds?|added|sets?|new|additional|accelerated|up|to|a|an|its"
    r"|billion|million|for|in",
    limit=12,
)  # fmt: skip
_GAP_OFFERING: Final = _filler(
    _T_PAREN, _T_LEGAL, _T_MONEY, _T_NUMBER,
    r"prices?|priced|pricing|of|announces?|announced|launches?|launched|commences?|commenced"
    r"|files?|filed|for|proposes?|proposed|plans?|upsized?|upsizes|completes?|completed|closes?"
    r"|closed|a|an|its|public|underwritten|registered|direct|overnight|million|billion|shares?"
    r"|common|class|ordinary|depositary|ADS",
    limit=12,
)  # fmt: skip
_GAP_EXECUTIVE: Final = _filler(
    _T_PAREN, _T_LEGAL, r"longtime|long-time|veteran|interim|embattled|outgoing", limit=4
)
#: After an object's name: a bracketed symbol or legal form, and for a rating "stock".
_TRAIL_BARE: Final = _GAP_BARE
_TRAIL_RATED: Final = _filler(_T_PAREN, _T_LEGAL, r"stock|shares", limit=3)

#: One token of an object slot: a run of up to 40 characters with no space and
#: no clause punctuation, or one bracketed group (*(NASDAQ: AAPL)*).
_OBJECT_TOKEN: Final = r"(?:[^\s,;:?!()]{1,40}|\([^()]{1,24}\))"
#: The object slot of a verb: one to eight tokens, each separated by exactly one
#: whitespace character. Bounded by token count, never by a lazy character run,
#: so a run of whitespace ends the object instead of being absorbed into it --
#: the 5.1b re-audit's M-A, where a lazy ``{1,60}?`` over spaces took 12 s on a
#: 4,000-space headline.
_OBJECT: Final = rf"(?P<object>{_OBJECT_TOKEN}(?:\s{_OBJECT_TOKEN}){{0,7}}?)"
#: The object of a possessive shape: tokens hold no apostrophe, so the object
#: ends at the first possessive (*Eli Lilly*'s, never *Eli Lilly's Alzheimer*'s).
_OBJECT_TOKEN_PLAIN: Final = r"(?:[^\s,;:?!()'’]{1,40}|\([^()]{1,24}\))"
_POSSESSOR: Final = rf"(?P<object>{_OBJECT_TOKEN_PLAIN}(?:\s{_OBJECT_TOKEN_PLAIN}){{0,7}}?)"
#: A possessive straight after an object, which is how FDA headlines name the
#: sponsor -- unless the possessed noun is not the sponsor's product: *Merck's
#: Rival Drug*, *Merck's Plan to Withdraw*, *Merck's Petition*.
_POSSESSIVE_NEXT: Final = (
    r"(?=['’]s?\s(?!(?:rivals?|competitors?|competing|cop(?:y|ies)|plans?|requests?|petitions?"
    r"|proposals?|challenges?|bids?|decisions?|move)\b))"
)
#: The end of a clause: the end of the headline, clause punctuation, or a dash.
_CLAUSE_END: Final = r"\s*(?:$|[,;:(]|[-–—]\s)"

#: The rating words a stock is upgraded or downgraded *to*. Closed. "Add" is
#: absent on purpose: *"Upgrades Azure Data Centers to Add Nvidia Chips"*.
_UP_RATING: Final = (
    r"strong buy|buy|overweight|outperform|accumulate|positive|neutral|hold|equal[- ]weight"
    r"|market perform|sector perform|peer perform|in[- ]line|market weight|sector weight"
    r"|sector outperform|market outperform"
)
_DOWN_RATING: Final = (
    r"strong sell|sell|underweight|underperform|reduce|negative|neutral|hold|equal[- ]weight"
    r"|market perform|sector perform|peer perform|in[- ]line|market weight|sector weight"
    r"|sector underperform|market underperform"
)
#: Function words that can never be part of a person's name between an
#: executive's title and the departure verb. A closed grammatical class --
#: pronouns, auxiliaries, modals, negation, speech -- not a list of synonyms:
#: it is what stops *Denies He Resigns* or *Will Not Step Down* reading as a
#: name followed by a departure. Compared case-sensitively.
_NAME_STOP: Final = (
    r"He|She|They|It|Its|His|Her|Who|That|This|Will|Would|Could|Should|May|Might|Must|Can"
    r"|Cannot|Not|Never|No|Is|Was|Are|Has|Have|Had|Does|Did|Says|Said|To|And|Or|But|Also"
)
#: One word of an executive's name: capitalised letters, an initial, or a hyphenated pair.
_NAME_WORD: Final = rf"(?-i:(?!(?:{_NAME_STOP})\b)(?:[A-Z][a-z]+(?:-[A-Z][a-z]+)?|[A-Z]\.))"
#: A departure that has happened, in headline present tense.
_DEPARTS_FINITE: Final = r"(?:resigns|steps\s+down|departs|retires|exits|is\s+ousted|ousted)\b"
#: A departure announced for the future. Only straight after the title: after a
#: name slot, *Refuses to Step Down* and *Rejects Calls to Step Down* would be
#: indistinguishable from *Tim Cook to Step Down* in title case.
_DEPARTS_TO: Final = r"to\s+(?:step\s+down|retire|leave|depart|exit|resign)\b"
#: What may follow a departure: the end of the clause, or a closed set of
#: timing words. Never *from <board>*, *as <other role>*, *for <destination>*.
_DEPARTS_TAIL: Final = (
    rf"(?={_CLAUSE_END}|\s+(?:after|amid|effective|immediately|following|over|this\s+year"
    r"|next\s+year|the\s+company|the\s+firm)\b)"
)

#: The longest gap between a subject and its phrase that is even looked at.
_MAX_GAP: Final = 80


@dataclass(frozen=True, slots=True)
class Shape:
    """One way a pattern's phrase is written, and where its subject sits.

    ``regex`` compiles case-insensitively. If it has a named group ``object``,
    the subject is the one named company filling that group, followed by at
    most ``gap``; otherwise the subject is the nearest named company before the
    match, separated from it by exactly ``gap`` (fully matched).
    """

    regex: str
    gap: str = _GAP_BARE
    #: An object shape only: the actor must be one named company, alone at the
    #: start of its clause, separated from the match by only ``gap``.
    actor: bool = False
    #: No named company may appear anywhere after the phrase.
    alone_after: bool = False
    _compiled: re.Pattern[str] = field(init=False, repr=False, compare=False)
    _gap: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_compiled", re.compile(self.regex, re.IGNORECASE))
        object.__setattr__(self, "_gap", re.compile(self.gap, re.IGNORECASE))

    @property
    def binds_object(self) -> bool:
        return "object" in self._compiled.groupindex

    def search(self, headline: str) -> re.Match[str] | None:
        return self._compiled.search(headline)

    def gap_fits(self, text: str) -> bool:
        return len(text) <= _MAX_GAP and self._gap.fullmatch(text) is not None


@dataclass(frozen=True, slots=True)
class RulePattern:
    """One headline pattern: its shapes, and what stops it firing.

    ``veto``, when set, is searched over the whole headline (case-insensitive);
    if it matches, the pattern does not fire and the phrase is recorded with
    ``veto_reason``. ``near_miss``, when set, is a looser regex: if no shape
    matched but it does, the phrase is recorded with ``near_miss_reason`` so
    the re-measurement can see what the strict shapes turned away.
    ``discovery_flagged`` defaults to False: decision 21, *"A pattern step 5
    adds later is unflagged until someone flags it."*
    """

    rule_id: str
    family: RuleFamily
    direction: Direction
    shapes: tuple[Shape, ...]
    veto: str | None = None
    veto_reason: str = ""
    near_miss: str | None = None
    near_miss_reason: str = ""
    discovery_flagged: bool = False
    _veto: re.Pattern[str] | None = field(init=False, repr=False, compare=False)
    _near_miss: re.Pattern[str] | None = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.rule_id or self.rule_id != self.rule_id.strip():
            raise ValueError(f"a pattern needs a non-blank, trimmed rule_id: {self.rule_id!r}")
        if self.direction not in (Direction.BULLISH, Direction.BEARISH):
            raise ValueError(f"a rules pattern's direction is bullish or bearish, not {self.direction}")
        if not self.shapes:
            raise ValueError(f"pattern {self.rule_id} needs at least one shape")
        veto = None if self.veto is None else re.compile(self.veto, re.IGNORECASE)
        near = None if self.near_miss is None else re.compile(self.near_miss, re.IGNORECASE)
        object.__setattr__(self, "_veto", veto)
        object.__setattr__(self, "_near_miss", near)

    def vetoed_by(self, headline: str) -> str | None:
        if self._veto is not None and self._veto.search(headline):
            return self.veto_reason or f"{self.rule_id} veto"
        return None

    def near_miss_in(self, headline: str) -> re.Match[str] | None:
        return None if self._near_miss is None else self._near_miss.search(headline)


#: Bare "top" only straight after a plural results noun (Zacks' *Q3 Earnings
#: and Revenues Top Estimates*); anywhere else it is an adjective (*Top Analyst
#: Forecasts*).
_BEAT_VERB: Final = (
    r"\b(?:beats?|tops|topped|surpass(?:es|ed)?|exceeds?|exceeded"
    r"|(?:(?<=earnings\s)|(?<=revenues\s)|(?<=results\s)|(?<=sales\s)|(?<=profits\s))top)\b"
)
_MISS_VERB: Final = r"\b(?:miss(?:es|ed)?|falls?\s+short\s+of|trails?|below)\b"
_EXPECTATION: Final = r"\b(?:estimates?|expectations|consensus|views?|forecasts?)\b"
#: What may sit between a beat/miss verb and the expectation it beats: a
#: closed list, at most four tokens, so the span can never reach another
#: clause or another company (*Surpasses Microsoft in Cloud; Analysts Cut
#: Estimates*).
_EXPECTATION_FILLER: Final = (
    rf"{_T_MONEY}|{_T_NUMBER}|the|its|analysts?['’]?|wall\s+street(?:['’]s?)?|street(?:['’]s?)?"
    r"|consensus|quarterly|Q[1-4]|EPS|earnings|revenues?|sales|profits?|and"
)
_TO_EXPECTATION: Final = rf"(?:\s+(?:{_EXPECTATION_FILLER})(?!\w)){{0,4}}\s+{_EXPECTATION}"
#: A loss beating its estimate is good news and a loss above it is bad, so a
#: beat/miss over a loss would read the direction backwards.
_LOSS: Final = r"\b(?:loss(?:es)?|LPS|deficit)\b"
_GUIDANCE_QUALIFIER: Final = (
    r"its|full[- ]year|annual|fiscal|FY\s?'?\d{2,4}|\d{4}(?:[-/]\d{2,4})?|Q[1-4]|[1-4]Q|H[12]"
    r"|(?:first|second|third|fourth)[- ]quarter|(?:first|second)[- ]half|current[- ]quarter"
    r"|quarterly|year|revenue|sales|earnings|profit|EPS|margin|adjusted|core|operating|organic"
    r"|comparable|same[- ]store|long[- ]term|and"
)
_GUIDANCE_NOUN: Final = r"(?:guidance|outlook|forecasts?)\b"
#: What may follow a guidance noun and leave it the company's own (AUDIT3-H1):
#: the end of the clause, or a timing word. Never *for* or *on* a thing --
#: *Morgan Stanley cuts its forecast for euro zone growth*, *Moody's lowers
#: outlook on Boeing* -- since a bank or a rating agency publishes forecasts
#: of the economy and of other companies, and its name sits exactly where a
#: company's own would.
_GUIDANCE_TIMING: Final = rf"(?:{_CLAUSE_END}|\s+(?:after|amid|as|again)\b)"
#: The one thing *for* may introduce: the period the guidance covers, which
#: must itself end the clause or be followed by a timing word -- *for 2025*
#: is a period, *for 2025 GDP growth* is not.
_GUIDANCE_PERIOD: Final = (
    r"(?:the\s+)?(?:full[- ]year|fiscal(?:[- ]year)?|year|quarter|current[- ]quarter"
    r"|(?:first|second|third|fourth)[- ]quarter|(?:first|second)[- ]half|Q[1-4]|FY\s?'?\d{2,4})"
    r"(?:\s+(?:20\d{2}|FY\s?'?\d{2,4}))?"
    r"|20\d{2}"
)
_GUIDANCE_TAIL: Final = rf"(?={_GUIDANCE_TIMING}|\s+for\s+(?:{_GUIDANCE_PERIOD}){_GUIDANCE_TIMING})"
_RAISE_VERB: Final = r"\b(?:raises?|raised|boosts?|boosted|lifts?|lifted|ups|hikes?|hiked|increases?|increased)"
_CUT_VERB: Final = (
    r"\b(?:cuts?|lowers?|lowered|slashes?|slashed|reduces?|reduced|trims?|trimmed|withdraws?"
    r"|withdrew|pulls?|pulled|suspends?|suspended)"
)
_TITLE: Final = (
    r"\b(?:co-)?(?:ceo|cfo|coo|chief\s+executive|chief\s+financial|chief\s+operating|chairman)\b"
)
#: The broad departure vocabulary, used only by the near-miss record.
_DEPARTS: Final = (
    r"\b(?:resigns?|steps?\s+down|departs?|to\s+leave|leaving|exits?|ousted|fired(?!\s+up)"
    r"|to\s+retire|retires?)\b"
)
_TAKEN: Final = r"(?:acquired|bought(?!\s+back)|taken\s+private)"
#: A deal term straight after an acquisition's object: *for $53 Billion*, *in ... deal*.
_DEAL_TERMS: Final = (
    r"(?=\s+for\s+(?:about\s+|roughly\s+|nearly\s+|around\s+|up\s+to\s+)?(?:US)?[$€£]\s?\d"
    r"|\s+in\s+[^,;:]{0,30}?\bdeal\b)"
)
_CRL: Final = r"(?:complete\s+response\s+letter|(?-i:CRL))\b"

#: Step 5's v1 patterns, in the order that picks a label's ``rule_id``.
RULE_PATTERNS: Final[tuple[RulePattern, ...]] = (
    # "tops" as a verb only: "Top Analyst Forecasts" is a roundup, not a beat.
    # The verb reaches its expectation over closed filler only. A loss, or a
    # verb of the other direction anywhere in the headline, means silence.
    RulePattern(
        rule_id="earnings_beat",
        family=RuleFamily.EARNINGS,
        direction=Direction.BULLISH,
        shapes=(Shape(_BEAT_VERB + _TO_EXPECTATION, _GAP_EARNINGS),),
        veto=_LOSS + "|" + _MISS_VERB,
        veto_reason="a loss inverts the direction, or the headline also reports a miss",
        discovery_flagged=True,
    ),
    RulePattern(
        rule_id="earnings_miss",
        family=RuleFamily.EARNINGS,
        direction=Direction.BEARISH,
        shapes=(Shape(_MISS_VERB + _TO_EXPECTATION, _GAP_EARNINGS),),
        veto=_LOSS + r"|\bbeating\b|" + _BEAT_VERB,
        veto_reason="a loss inverts the direction, or the headline also reports a beat",
        discovery_flagged=True,
    ),
    # The verb's object is the guidance noun, after only qualifiers: never a
    # price target, a currency amount or "pressure on". After the noun, only
    # the clause's end, a timing word or "for <period>" (AUDIT3-H1).
    RulePattern(
        rule_id="guidance_raised",
        family=RuleFamily.GUIDANCE,
        direction=Direction.BULLISH,
        shapes=(
            Shape(_RAISE_VERB + rf"(?:\s+(?:{_GUIDANCE_QUALIFIER})(?!\w)){{0,6}}\s+" + _GUIDANCE_NOUN + _GUIDANCE_TAIL, _GAP_GUIDANCE),
        ),
        near_miss=_RAISE_VERB + r"\b.{0,40}\b" + _GUIDANCE_NOUN,
        near_miss_reason="the raise verb does not act on the company's own guidance",
        discovery_flagged=True,
    ),  # fmt: skip
    RulePattern(
        rule_id="guidance_cut",
        family=RuleFamily.GUIDANCE,
        direction=Direction.BEARISH,
        shapes=(
            Shape(_CUT_VERB + rf"(?:\s+(?:{_GUIDANCE_QUALIFIER})(?!\w)){{0,6}}\s+" + _GUIDANCE_NOUN + _GUIDANCE_TAIL, _GAP_GUIDANCE),
        ),
        near_miss=_CUT_VERB + r"\b.{0,40}\b" + _GUIDANCE_NOUN,
        near_miss_reason="the cut verb does not act on the company's own guidance",
        discovery_flagged=True,
    ),  # fmt: skip
    # A rating change needs rating context: a rating word or a price target.
    RulePattern(
        rule_id="analyst_upgrade",
        family=RuleFamily.ANALYST_RATING,
        direction=Direction.BULLISH,
        shapes=(
            Shape(rf"\bupgrades?\s+{_OBJECT}\s+to\s+(?:an?\s+)?(?:{_UP_RATING})\b", _TRAIL_RATED),
            Shape(
                rf"\bupgrades?\s+{_OBJECT}\s*,\s*(?:and\s+)?(?:raises|lifts|boosts|hikes|ups)\s+"
                r"(?:its\s+|the\s+)?(?:price\s+target|PT)\b",
                _TRAIL_RATED,
            ),
            Shape(
                rf"\bupgraded\s+(?:to\s+(?:an?\s+)?(?:{_UP_RATING})\b|(?:at|by)\s+(?-i:[A-Z])[\w&'’.-]*)",
                _GAP_RATED,
            ),
            Shape(r"\b(?:stock|shares)\s+upgraded\b", _GAP_BARE),
        ),
        near_miss=r"\bupgrade[sd]?\b",
        near_miss_reason="an upgrade with no rating word or price target bound to one company",
        discovery_flagged=True,
    ),
    RulePattern(
        rule_id="analyst_downgrade",
        family=RuleFamily.ANALYST_RATING,
        direction=Direction.BEARISH,
        shapes=(
            Shape(rf"\bdowngrades?\s+{_OBJECT}\s+to\s+(?:an?\s+)?(?:{_DOWN_RATING})\b", _TRAIL_RATED),
            Shape(
                rf"\bdowngrades?\s+{_OBJECT}\s*,\s*(?:and\s+)?(?:cuts|lowers|slashes|trims)\s+"
                r"(?:its\s+|the\s+)?(?:price\s+target|PT)\b",
                _TRAIL_RATED,
            ),
            Shape(
                rf"\bdowngraded\s+(?:to\s+(?:an?\s+)?(?:{_DOWN_RATING})\b|(?:at|by)\s+(?-i:[A-Z])[\w&'’.-]*)",
                _GAP_RATED,
            ),
            Shape(r"\b(?:stock|shares)\s+downgraded\b", _GAP_BARE),
        ),
        near_miss=r"\bdowngrade[sd]?\b",
        near_miss_reason="a downgrade with no rating word or price target bound to one company",
        discovery_flagged=True,
    ),
    # The target, before "to be acquired" or "receives a takeover offer", or the
    # object of an acquisition that is the headline's main event. Never a name
    # after "by": that is the acquirer.
    RulePattern(
        rule_id="mna_target",
        family=RuleFamily.MNA,
        direction=Direction.BULLISH,
        shapes=(
            Shape(
                rf"\b(?:agrees?\s+to\s+be\s+{_TAKEN}"
                rf"|(?:reaches?|strikes?|signs?)\s+(?:a\s+)?deal\s+to\s+be\s+{_TAKEN}"
                rf"|to\s+be\s+{_TAKEN})\b",
                _GAP_BARE,
            ),
            # Contiguous: "receives (a) takeover offer", then the end of the
            # clause or "from <bidder>". "Receives EU Nod for ... Takeover Offer"
            # is the bidder receiving something else.
            Shape(
                r"\breceives?\s+(?:an?\s+)?(?:unsolicited\s+)?(?:takeover|buyout|acquisition)\s+"
                rf"(?:offer|bid|proposal)\b(?={_CLAUSE_END}|\s+(?:from|worth|valued)\b)",
                _GAP_BARE,
            ),
            # "<Actor> to / will / agrees to / has agreed to acquire <Target> for $..."
            # or "<Actor> acquires / buys <Target> ...", the actor named alone at
            # the start of its clause. Any other word before the verb -- offer,
            # plan, bid, says, no, urges -- leaves the actor unnamed and the
            # target unlabelled.
            Shape(
                r"\b(?:(?:(?:agrees?|has\s+agreed)\s+to|to|will)\s+(?:acquire|buy)|acquires|buys)\s+"
                rf"{_OBJECT}{_DEAL_TERMS}",
                _TRAIL_BARE,
                actor=True,
            ),
        ),
        veto=r"\b(?:terminat\w*|cancel\w*|scrap(?:s|ped|ping)?|abandon\w*|walks?\s+away|walked\s+away"
        r"|calls?\s+off|called\s+off|ends?\s+(?:talks|deal|agreement|merger)|rejects?|rejected"
        r"|block(?:s|ed|ing)?|collaps\w*|stake|talks|consider\w*|explor\w*|weighs?|weighing|mulls?"
        r"|mulling|rumou?r\w*|reportedly|sues)\b",
        veto_reason="the deal is terminated, blocked, partial or only reported",
        near_miss=r"\b(?:acquired\s+by|takeover|buyout|to\s+acquire|acquires)\b",
        near_miss_reason="M&A language with no target shape",
        discovery_flagged=True,
    ),
    RulePattern(
        rule_id="secondary_offering",
        family=RuleFamily.SECONDARY_OFFERING,
        direction=Direction.BEARISH,
        shapes=(
            Shape(
                r"\b(?:secondary|follow-on)(?:\s+public|\s+stock|\s+share)?\s+offering\b"
                r"|\b(?:common\s+)?(?:stock|share)\s+offering\b",
                _GAP_OFFERING,
                alone_after=True,
            ),
        ),
        # "of <anything but a share word>" after the offering, its stock or its
        # shares: Offering of Fastly Shares, Common Stock of Fastly.
        veto=r"\b(?:cancel\w*|terminat\w*|withdraw\w*|withdrew|postpon\w*|scrap(?:s|ped|ping)?"
        r"|abandon\w*|pulls?|pulled|suspend\w*|delay\w*)\b"
        r"|\b(?:offering|stock|shares)\s+of\s+(?!(?:\d|\$|common\b|ordinary\b|its\b|class\b|shares\b"
        r"|american\b|depositary\b))",
        veto_reason="the offering is cancelled, or is of another company's shares",
        discovery_flagged=True,
    ),
    RulePattern(
        rule_id="buyback",
        family=RuleFamily.BUYBACK,
        direction=Direction.BULLISH,
        shapes=(
            Shape(
                r"\b(?:share|stock)\s+(?:buybacks?|repurchases?)\b"
                r"|\brepurchase\s+(?:program|plan|authori[sz]ation)\b",
                _GAP_BUYBACK,
            ),
        ),
        # AUDIT3-L1: an expected buyback, or one still to be announced, has not
        # happened. *Analysts Expect Apple To Announce* sits too far before the
        # phrase for the negation window, which looks only just before it.
        veto=r"\b(?:suspend(?:s|ed|ing)?|halt(?:s|ed|ing)?|paus(?:e|es|ed|ing)|terminat(?:e|es|ed|ing)"
        r"|scrap(?:s|ped|ping)?|cancel(?:s|ed|led|ing|ling)?)\b"
        r"|\bexpect(?:s|ed|ing)?\s(?:[^\s,;:]{1,40}\s){0,6}to\b"
        r"|\bto\s(?:announce|unveil|launch|approve|authori[sz]e|consider|propose)\b",
        veto_reason="the buyback is suspended, halted or cancelled, or only expected",
        discovery_flagged=True,
    ),
    RulePattern(
        rule_id="executive_departure",
        family=RuleFamily.EXECUTIVE_DEPARTURE,
        direction=Direction.BEARISH,
        # "<Company> CEO (<Name, up to three words>) resigns / steps down / ...",
        # or "<Company> CEO to step down": contiguous, with nothing else between
        # the title and the verb, and nothing after but the clause's end or a
        # timing word.
        shapes=(
            Shape(
                _TITLE + rf"(?:(?:\s+{_NAME_WORD}){{0,3}}\s+{_DEPARTS_FINITE}|\s+{_DEPARTS_TO})" + _DEPARTS_TAIL,
                _GAP_EXECUTIVE,
            ),
        ),
        veto=r"\b(?:calls?\s+for|calling\s+for|urges?|urged|demands?|pressure[sd]?|push(?:es|ing)?\s+for"
        r"|asks?|asked|seeks?|denies|denied|rumou?r\w*|former)\b|\bex-",
        veto_reason="somebody else wants the executive gone, or the executive is a former one",
        near_miss=_TITLE + r".{0,40}" + _DEPARTS,
        near_miss_reason="a departure that is not the title's own, contiguous, completed departure",
        discovery_flagged=True,
    ),
    RulePattern(
        rule_id="fda_approval",
        family=RuleFamily.FDA,
        direction=Direction.BULLISH,
        shapes=(
            Shape(rf"\bfda\s+(?:approves?|approved|clears?|cleared)\s+{_POSSESSOR}{_POSSESSIVE_NEXT}", _TRAIL_BARE),
            Shape(
                r"\bfda\s+grants?\s+(?:full\s+|accelerated\s+|traditional\s+)?(?:approval|clearance)\s+"
                rf"(?:to|for)\s+{_POSSESSOR}{_POSSESSIVE_NEXT}",
                _TRAIL_BARE,
            ),
            Shape(
                r"\b(?:wins?|won|receives?|received|gets?|got|secures?|secured|granted|earns?|earned"
                r"|lands?|landed)\s+(?:full\s+|accelerated\s+|traditional\s+)?fda\s+"
                # The noun ends its clause or is followed by "for <product>", and
                # nothing after it in the clause says delay, trial or study:
                # *FDA Approval Delay*, *FDA Clearance to Begin Trial*.
                rf"(?:approval|clearance|nod)\b(?={_CLAUSE_END}|\s+for\s)"
                r"(?![^,;:]{0,80}?\b(?:delay\w*|trials?|stud(?:y|ies)|IND|phase|begin\w*|start\w*"
                r"|test\w*|postpon\w*|extension)\b)",
                _GAP_BARE,
            ),
        ),
        veto=r"\b(?:biosimilars?|generics?|copycat|cop(?:y|ies)|interchangeable)\b",
        veto_reason="an approval of a copy of another company's drug",
        near_miss=r"\bfda\s+(?:grants?|approval|clearance|approves?|clears?)\b",
        near_miss_reason="FDA language with no approval shape bound to one company",
        discovery_flagged=True,
    ),  # fmt: skip
    RulePattern(
        rule_id="fda_rejection",
        family=RuleFamily.FDA,
        direction=Direction.BEARISH,
        shapes=(
            Shape(
                rf"\b(?:receives?|received|gets?|got|hit\s+with)\s+(?:an?\s+)?(?:fda\s+)?{_CRL}",
                _GAP_BARE,
            ),
            Shape(
                rf"\bfda\s+(?:issues?|issued|sends?|sent)\s+(?:an?\s+)?{_CRL}\s+(?:to|for)\s+{_OBJECT}"
                r"(?=['’](?:s\b|\s)|\s*[,;:(]|\s*$|\s+(?:for|on|over)\b)",
                _TRAIL_BARE,
            ),
            Shape(
                r"\bfda\s+(?:rejects?|rejected|(?:declines?|declined|refuses?|refused)\s+to\s+approve"
                rf"|turns?\s+down|turned\s+down)\s+{_POSSESSOR}{_POSSESSIVE_NEXT}",
                _TRAIL_BARE,
            ),
        ),
        near_miss=rf"\b{_CRL}",
        near_miss_reason="a response letter with no recipient bound to one company",
        discovery_flagged=True,
    ),  # fmt: skip
)

#: A phrase straight after one of these words is not the event: *Fails to
#: Beat*, *Expected to Beat*, *Could Raise*. Searched on the 40 characters
#: before a match.
_NEGATED: Final = re.compile(
    r"(?:\b(?:fails?|failed|failing|unable|expected|expects?|likely|unlikely|poised|set|seen"
    r"|aims?|hopes?|looks?|tries|trying|seeks?|seeking|could|may|might|will|would|should|can"
    r"|cannot|can['’]t|won['’]t|wouldn['’]t|didn['’]t|doesn['’]t|don['’]t|did|does|not|never"
    r"|no|to)\s+){1,3}\Z",
    re.IGNORECASE,
)
#: A headline that asks rather than reports: led by an auxiliary verb. Any
#: ``?`` anywhere in the headline is a question too (*Beats Estimates? Not Quite*).
_QUESTION_LEAD: Final = re.compile(
    r"\s*(?:did|does|do|will|can|could|should|would|is|are|was|has|have)\b", re.IGNORECASE
)
#: A headline that previews rather than reports.
_PREVIEW: Final = re.compile(
    r"\bshould\s+you\s+(?:buy|sell|hold)\b|\bahead\s+of\b|\bpreviews?\b|\bwhat\s+to\s+expect\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RuleMatch:
    """One pattern that fired on a headline, the text it fired on, and who it bound.

    ``tickers`` is empty when the phrase bound nobody; ``attribution`` then
    says why (no named subject, or ambiguous), and otherwise how.
    """

    rule_id: str
    family: RuleFamily
    direction: Direction
    discovery_flagged: bool
    phrase: str
    tickers: tuple[str, ...] = ()
    attribution: str = ""


@dataclass(frozen=True, slots=True)
class VetoedMatch:
    """A phrase a pattern turned away, and the reason."""

    rule_id: str
    phrase: str
    reason: str


@dataclass(frozen=True, slots=True)
class SuppressedName:
    """A name in the headline that was found and deliberately not used as a mention."""

    tickers: tuple[str, ...]
    text: str
    reason: str


@dataclass(frozen=True, slots=True)
class RulesLabelling:
    """One headline's rules labels, and everything that produced none."""

    #: One per attributed ticker, sorted by ticker.
    labels: tuple[SentimentLabel, ...]
    #: Every pattern that fired, in :data:`RULE_PATTERNS` order.
    matches: tuple[RuleMatch, ...]
    #: Every match, when bullish and bearish both fired -- and then no labels.
    conflicting: tuple[RuleMatch, ...]
    #: Every match that bound no ticker (no conflict).
    unattributed: tuple[RuleMatch, ...]
    #: Every phrase a veto, a preview or negation, or a near-miss turned away.
    vetoed: tuple[VetoedMatch, ...] = ()
    #: Every name found and not used, with why. Computed only when a pattern
    #: fired or was turned away.
    suppressed: tuple[SuppressedName, ...] = ()
    #: True when the headline, whitespace collapsed, was longer than
    #: :data:`MAX_HEADLINE_CHARS`. Such a headline is not matched at all: a
    #: label read off a cut-down headline could miss the veto past the cut.
    truncated: bool = False


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

#: The legal-form and share-class suffixes stripped from an asset-list name.
#: Closed: anything not here stays in the name. Longest first, so ``& Co.``
#: is tried before ``Co.``. Compared case-insensitively, and only as a whole
#: trailing word (preceded by a space or comma). *Holdings*, *Group*, *Trust*
#: are part of how a company is named and are kept; *Rights*, *Warrants* and
#: *Units* are other securities and are kept so their long names rarely match.
LEGAL_AND_CLASS_SUFFIXES: Final[tuple[str, ...]] = (
    "American Depositary Shares",
    "American Depository Shares",
    "Non-Voting Common Stock",
    "American Depositary Share",
    "Public Limited Company",
    "Depositary Shares",
    "Ordinary Shares",
    "Common Shares",
    "Common Stock",
    "Capital Stock",
    "Incorporated",
    "Corporation",
    "Limited",
    "Company",
    "Class A",
    "Class B",
    "Class C",
    "& Co.",
    "L.L.C.",
    "Corp.",
    "Corp",
    "Inc.",
    "Inc",
    "Ltd.",
    "Ltd",
    "N.V.",
    "S.A.",
    "L.P.",
    "LLC",
    "plc",
    "Oyj",
    "ASA",
    "A/S",
    "Co.",
    "LP",
    "NV",
    "SA",
    "SE",
    "AG",
    "AB",
)

#: A trailing parenthetical, e.g. ``Ordinary Shares (Ireland)``.
_TRAILING_PARENTHETICAL: Final = re.compile(r"\s*\([^()]*\)\s*$")
#: A conjunction left dangling by stripping ``Company`` from ``Deere & Company``.
_TRAILING_CONJUNCTION: Final = re.compile(r"(?:\s+(?:&|and))+$")


def normalise_company_name(name: str) -> str:
    """``name`` with its trailing legal and share-class suffixes stripped.

    Strips, repeatedly from the end: a trailing parenthetical, then any of
    :data:`LEGAL_AND_CLASS_SUFFIXES`, with the commas and spaces before it, and
    a trailing ``&``/``and`` that leaves behind; and a leading ``The``. Never
    strips a name to nothing -- ``"Inc."`` stays.
    """
    current = " ".join(name.split())
    while True:
        before = current
        current = _TRAILING_PARENTHETICAL.sub("", current).rstrip(" ,")
        current = _TRAILING_CONJUNCTION.sub("", current).rstrip(" ,") or current
        lowered = current.lower()
        for suffix in LEGAL_AND_CLASS_SUFFIXES:
            cut = len(current) - len(suffix)
            if cut > 0 and lowered.endswith(suffix.lower()) and current[cut - 1] in " ,":
                stripped = current[:cut].rstrip(" ,")
                if stripped:
                    current = stripped
                break
        if current == before:
            break
    if current.startswith("The ") and len(current) > len("The "):
        current = current[len("The ") :]
    return current


#: **Hand-kept company names for the watch universe**, moved from the step-0
#: draft (``scripts/probe_phase3.py`` ``COMPANY_NAMES``) unchanged. Matched
#: case-sensitively, as whole words.
WATCH_COMPANY_NAMES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType({
    "AAPL": ("Apple",), "MSFT": ("Microsoft",), "NVDA": ("Nvidia", "NVIDIA"),
    "AMZN": ("Amazon",), "GOOGL": ("Alphabet", "Google"), "GOOG": ("Alphabet", "Google"),
    "META": ("Meta Platforms", "Meta"), "AVGO": ("Broadcom",), "TSLA": ("Tesla",),
    "LLY": ("Eli Lilly", "Lilly"), "WMT": ("Walmart",), "JPM": ("JPMorgan Chase",),
    "UNH": ("UnitedHealth",), "XOM": ("Exxon", "ExxonMobil"), "COST": ("Costco",),
    "HD": ("Home Depot",), "ARM": ("Arm Holdings",), "ALAB": ("Astera Labs",),
    "RDDT": ("Reddit",), "RBRK": ("Rubrik",), "CRWV": ("CoreWeave",),
    "CRCL": ("Circle Internet",), "ARKK": ("ARK Innovation",),
    "ORCL": ("Oracle",), "BRK.B": ("Berkshire",), "V": ("Visa",), "MA": ("Mastercard",),
    "BAC": ("Bank of America", "BofA"), "JNJ": ("Johnson & Johnson", "J&J"),
    "ABBV": ("AbbVie",), "MRK": ("Merck",), "MCD": ("McDonald's", "McDonald’s"),
    "BKNG": ("Booking Holdings",), "NFLX": ("Netflix",), "DIS": ("Disney",),
    "GE": ("GE Aerospace", "General Electric"), "CAT": ("Caterpillar",),
    "RTX": ("Raytheon", "Pratt & Whitney"), "UBER": ("Uber",), "GEV": ("GE Vernova",),
    "PG": ("Procter & Gamble", "P&G"), "KO": ("Coca-Cola",), "PM": ("Philip Morris",),
    "CVX": ("Chevron",), "COP": ("ConocoPhillips",), "WMB": ("Williams Companies",),
    "NEE": ("NextEra",), "SO": ("Southern Company", "Southern Co"),
    "CEG": ("Constellation Energy",), "DUK": ("Duke Energy",), "VST": ("Vistra",),
    "WELL": ("Welltower",), "PLD": ("Prologis",), "AMT": ("American Tower",),
    "EQIX": ("Equinix",), "SPG": ("Simon Property",), "LIN": ("Linde",),
    "SHW": ("Sherwin-Williams",), "NEM": ("Newmont",), "ECL": ("Ecolab",),
    "APD": ("Air Products",),
})  # fmt: skip

#: Tickers that are also words, so a bare uppercase match proves nothing (the draft's).
WORD_TICKERS: Final[frozenset[str]] = frozenset(
    {"ARM", "CAT", "WELL", "LIN", "DIS", "COST", "UBER", "META"}
)

#: Normalised asset names that are ordinary English or market words, never
#: matched as a company name. An explicit ``(TICKER)`` and the single-tag rule
#: still attribute. Closed and incomplete by nature -- there is no dictionary
#: here to tell *Fastly* from *Toast* -- so add one when a headline shows it.
#:
#: Sources: the draft's eight; the 5.1b audit's five (Nasdaq, Dow, News, Box,
#: Sea); the two common words among the recorded asset fixtures' single-word
#: names (Carnival, Flex); and listed companies whose normalised name is a
#: common word or collides with a better-known company (Vertex, Inc. against
#: Vertex Pharmaceuticals; Bullish, the exchange, against "bullish on").
WORD_NAMES: Final[frozenset[str]] = frozenset({
    "Target", "People", "Block", "Gap", "Southern", "Progressive", "Match", "Snap",
    "Nasdaq", "Dow", "News", "Box", "Sea",
    "Carnival", "Flex",
    "Amplitude", "Ball", "Bullish", "Coherent", "Compass", "Crocs", "Elastic", "Fox",
    "Lemonade", "Mosaic", "Noble", "Premier", "Root", "Shell", "Strategy", "Tapestry",
    "Toast", "Vertex",
})  # fmt: skip

#: Feeds that tag per article, so a lone tag is the vendor's reading of it.
SINGLE_TAG_FEEDS: Final[frozenset[NewsFeed]] = frozenset(
    {NewsFeed.ALPACA_NEWS, NewsFeed.MASSIVE_NEWS}
)

#: Families a phrase with no named subject never attributes by the single-tag
#: rule (5.2 audit M1). Macro data releases are written in earnings language --
#: *Jobless Claims Below Expectations*, *Retail Sales Top Estimates* -- and
#: carry a lone index or ETF tag. ``funds`` covers only the curated universe,
#: while the asset list carries every listed ETF (DIA, VOO, TLT, XLF, USO...),
#: so the fund exemption cannot be what stops it: an earnings phrase must name
#: its company, or it attributes to nobody.
NO_SINGLE_TAG_FAMILIES: Final[frozenset[RuleFamily]] = frozenset({RuleFamily.EARNINGS})

#: Explicit symbol mentions. ``px`` is a bracketed symbol's exchange prefix.
_EXPLICIT_SYMBOL: Final = re.compile(
    r"\((?:(?P<px>NYSE American|NYSE|NASDAQ|Nasdaq|AMEX|OTC|Cboe|CBOE)\s?:\s?)?(?P<p>[A-Z][A-Z.]{0,6})\)"
    r"|\$(?P<c>[A-Z][A-Z.]{0,6})\b"
    r"|\b(?:NYSE|NASDAQ|Nasdaq|AMEX)\s?:\s?(?P<x>[A-Z][A-Z.]{0,6})\b"
)
#: Straight after a name: it is rating somebody (the draft's list, extended).
_RATER_AFTER: Final = re.compile(
    r"(?:\s+(?:Securities|Research|Capital|Analysts?))?\s+(?:upgrades|downgrades|initiates"
    r"|reiterates|maintains|resumes|reinstates|assumes|(?:raises|lowers|cuts|boosts|trims|lifts"
    r"|hikes|slashes)\s+(?:its\s+|the\s+)?(?:price\s+target|PT))\b"
    r"|['’]s?\s+(?:analysts?|strategists?)\b"
    r"|\s+(?:is\s+|turns\s+|stays\s+|remains\s+)?(?:bullish|bearish)\s+on\b",
    re.IGNORECASE,
)
#: Straight before a name: *upgraded ... at|by|from <Broker>*.
_RATER_BEFORE: Final = re.compile(
    r"\b(?:up|down)grad(?:es|ed|e)\b[^,;]{0,60}?\b(?:at|by|from)\s+\Z", re.IGNORECASE
)
#: Names joined to the subject by one of these make it a list, not a subject.
_COORDINATED: Final = re.compile(r"(?:\band|&|/|\bor|\bvs\.?|\bversus|\+)\s*\Z", re.IGNORECASE)
_COMMA_ONLY: Final = re.compile(r"\s*,\s*")
#: What may join a phrase with no subject to the bound phrase before it.
_CHAINED: Final = re.compile(r"\s*(?:,|;|&|,?\s*and)\s*", re.IGNORECASE)


def _name_form(name: str) -> str:
    return rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])"


#: A name form's first token: its leading run of ASCII letters and digits --
#: exactly the characters :func:`_name_form`'s boundaries test.
_NAME_TOKEN: Final = re.compile(r"[A-Za-z0-9]+")
#: A bare symbol's first token: its leading run of ASCII letters -- exactly the
#: characters a bare symbol's boundaries test (a digit may touch it).
_SYMBOL_TOKEN: Final = re.compile(r"[A-Za-z]+")


def _name_key(name: str) -> str | None:
    """The token a match of ``_name_form(name)`` must start with, or None if the
    name does not start with a letter or digit (then it is tried on every headline)."""
    run = _NAME_TOKEN.match(name)
    return None if run is None else "n:" + run.group(0)


def _symbol_key(symbol: str) -> str | None:
    run = _SYMBOL_TOKEN.match(symbol)
    return None if run is None else "s:" + run.group(0)


class _HeadlineTokens:
    """A headline's maximal letter-and-digit runs and letter runs, found once."""

    __slots__ = ("keys",)

    def __init__(self, headline: str) -> None:
        self.keys: frozenset[str] = frozenset(
            {"n:" + t for t in _NAME_TOKEN.findall(headline)} | {"s:" + t for t in _SYMBOL_TOKEN.findall(headline)}
        )


class _TokenIndex:
    """Which compiled name patterns a headline could possibly match (AUDIT3-M2).

    A name form is a literal bounded by ``(?<![A-Za-z0-9])`` and
    ``(?![A-Za-z0-9])``, so wherever it matches, the headline's maximal
    letter-and-digit run at the match's start *is* the name's first such run;
    a bare symbol likewise, over letters alone. So a pattern none of whose
    forms' first tokens is in the headline cannot match it, and is skipped.
    Every other pattern runs unchanged, in the order it was built -- so the
    mentions, and the order they are found in, are the unindexed book's. The
    headline is tokenised once, instead of one scan per ticker.
    """

    __slots__ = ("_by_key", "_always")

    def __init__(self) -> None:
        self._by_key: dict[str, list[int]] = {}
        self._always: list[int] = []

    def add(self, key: str | None, position: int) -> None:
        bucket = self._always if key is None else self._by_key.setdefault(key, [])
        if not bucket or bucket[-1] != position:
            bucket.append(position)

    def candidates(self, tokens: _HeadlineTokens) -> list[int]:
        found = set(self._always)
        for key in tokens.keys:
            found.update(self._by_key.get(key, ()))
        return sorted(found)


@dataclass(frozen=True, slots=True)
class _Mention:
    tickers: tuple[str, ...]
    start: int
    end: int
    text: str


def _is_exchange_prefix(headline: str, start: int, end: int) -> bool:
    """*(Nasdaq: MU)*, *NASDAQ:MU*, *Nasdaq-listed*: an exchange, not a company."""
    rest = headline[end : end + 8]
    if rest.startswith("-listed"):
        return True
    if start > 0 and headline[start - 1] == "(" and rest.lstrip(" ").startswith(":"):
        return True
    return len(rest) >= 2 and rest[0] == ":" and rest[1].isupper()


def _ends_with_words(text: str, words: str) -> bool:
    if not text.lower().endswith(words.lower()):
        return False
    cut = len(text) - len(words)
    return cut == 0 or not text[cut - 1].isalnum()


class NameBook:
    """Everything a headline can name: the watch universe and the asset list.

    Build once per asset-list refresh and reuse -- it compiles one pattern per
    ticker, and indexes each by the first token of its forms so a headline is
    tokenised once rather than scanned once per ticker (:class:`_TokenIndex`).
    ``watch_names`` is the hand-kept table for watch tickers (normally
    :data:`WATCH_COMPANY_NAMES`); ``asset_names`` maps every active US equity
    symbol to its broker ``name``; ``funds`` is the tickers the single-tag
    rule never lands on. The engine passes exactly the curated universe's
    ``fund`` flag (``UNIVERSE_FUND_SYMBOLS``: SPY, QQQ, IWM, XLE, ARKK) --
    not every ETF in the asset list, which is why the earnings family does not
    take the single-tag rule at all (:data:`NO_SINGLE_TAG_FAMILIES`). It is
    required, not defaulted: an empty default would quietly reopen AUDIT3-M1
    for a caller that forgot it. Order of any input does not matter.
    Read-only after construction.
    """

    __slots__ = ("_symbols", "_funds", "_by_name", "_word_named", "_own_names", "_index", "_word_index")

    def __init__(
        self,
        *,
        watch: Iterable[str],
        watch_names: Mapping[str, Sequence[str]],
        asset_names: Mapping[str, str],
        funds: Iterable[str],
    ) -> None:
        watched = frozenset(watch)
        self._symbols: frozenset[str] = watched | frozenset(asset_names)
        self._funds: frozenset[str] = frozenset(funds)
        patterns: list[tuple[str, re.Pattern[str]]] = []
        word_named: list[tuple[str, re.Pattern[str]]] = []
        own: dict[str, tuple[str, ...]] = {}
        index = _TokenIndex()
        word_index = _TokenIndex()
        for ticker in sorted(self._symbols):
            forms: list[str] = []
            names: Sequence[str] = ()
            keys: list[str | None] = []
            if ticker in watched:
                if len(ticker) >= 3 and ticker not in WORD_TICKERS:
                    forms.append(rf"(?<![A-Za-z.]){re.escape(ticker)}(?![A-Za-z])")
                    keys.append(_symbol_key(ticker))
                names = tuple(n for n in watch_names.get(ticker, ()) if n)
            normalised = normalise_company_name(asset_names[ticker]) if ticker in asset_names else ""
            own[ticker] = tuple(sorted({*names, *([normalised] if normalised else [])}))
            if not names and normalised:
                if normalised in WORD_NAMES:
                    word_index.add(_name_key(normalised), len(word_named))
                    word_named.append((ticker, re.compile(_name_form(normalised))))
                else:
                    names = (normalised,)
            # Longest first: an alternation takes its first match, so "Meta" before
            # "Meta Platforms" would never see the longer name.
            ordered = sorted(set(names), key=lambda n: (-len(n), n))
            forms.extend(_name_form(n) for n in ordered)
            keys.extend(_name_key(n) for n in ordered)
            if forms:
                for key in keys:
                    index.add(key, len(patterns))
                patterns.append((ticker, re.compile("|".join(forms))))
        self._by_name: tuple[tuple[str, re.Pattern[str]], ...] = tuple(patterns)
        self._word_named: tuple[tuple[str, re.Pattern[str]], ...] = tuple(word_named)
        self._own_names: Mapping[str, tuple[str, ...]] = MappingProxyType(own)
        self._index: _TokenIndex = index
        self._word_index: _TokenIndex = word_index

    def is_equity(self, symbol: str) -> bool:
        return symbol in self._symbols

    def is_fund(self, symbol: str) -> bool:
        """Whether ``symbol`` was passed in ``funds``: a fund or a market tag."""
        return symbol in self._funds

    def names_part_of(self, headline: str, symbol: str) -> bool:
        """Whether ``headline`` holds the first word of any of ``symbol``'s own names.

        Looser than a mention on purpose: the single-tag rule uses it to stay
        away from a tag the headline may be naming in some other role --
        *Regeneron Partner* for Regeneron Pharmaceuticals.
        """
        for name in self._own_names.get(symbol, ()):
            first = name.split()[0] if name.split() else ""
            if first and re.search(_name_form(first), headline):
                return True
        return False

    def _follows_own_name(self, headline: str, paren_start: int, symbol: str) -> bool:
        """Whether the words before ``(symbol)`` begin ``symbol``'s own company name."""
        before = normalise_company_name(headline[max(0, paren_start - 120) : paren_start])
        for name in self._own_names.get(symbol, ()):
            words = name.split()
            for count in range(1, len(words) + 1):
                if _ends_with_words(before, " ".join(words[:count])):
                    return True
        return False

    def mentions(
        self, headline: str, tags: Sequence[str] = ()
    ) -> tuple[tuple[_Mention, ...], tuple[SuppressedName, ...]]:
        """Every company ``headline`` names, in order, and every name it set aside."""
        tagged = frozenset(tags)
        found: list[_Mention] = []
        suppressed: list[tuple[int, SuppressedName]] = []
        for hit in _EXPLICIT_SYMBOL.finditer(headline):
            raw = hit.group("p") or hit.group("c") or hit.group("x") or ""
            symbol = raw.rstrip(".")
            if symbol not in self._symbols:
                continue
            bare_bracket = hit.group("p") is not None and hit.group("px") is None
            if bare_bracket and symbol not in tagged and not self._follows_own_name(headline, hit.start(), symbol):
                why = "a bracketed symbol that does not follow its own company's name"
                suppressed.append((hit.start(), SuppressedName((symbol,), hit.group(0), why)))
                continue
            found.append(_Mention((symbol,), hit.start(), hit.end(), hit.group(0)))
        # Only the tickers one of whose forms starts with a token the headline
        # holds can match it at all (see _TokenIndex); each is then run exactly
        # as before, in ticker order.
        tokens = _HeadlineTokens(headline)
        for position in self._index.candidates(tokens):
            ticker, pattern = self._by_name[position]
            for hit in pattern.finditer(headline):
                if _is_exchange_prefix(headline, hit.start(), hit.end()):
                    suppressed.append((hit.start(), SuppressedName((ticker,), hit.group(0), "an exchange prefix")))
                else:
                    found.append(_Mention((ticker,), hit.start(), hit.end(), hit.group(0)))
        for position in self._word_index.candidates(tokens):
            ticker, pattern = self._word_named[position]
            for hit in pattern.finditer(headline):
                why = "the company's name is an ordinary word"
                suppressed.append((hit.start(), SuppressedName((ticker,), hit.group(0), why)))

        # Longest span first; an exact repeat of a span merges its tickers;
        # anything overlapping an accepted span is covered by a longer name.
        found.sort(key=lambda m: (m.start - m.end, m.start, m.tickers))
        owner: dict[int, tuple[int, int]] = {}
        by_span: dict[tuple[int, int], list[str]] = {}
        texts: dict[tuple[int, int], str] = {}
        for mention in found:
            span = (mention.start, mention.end)
            if span in by_span:
                by_span[span].extend(t for t in mention.tickers if t not in by_span[span])
                continue
            covered = next((owner[i] for i in range(mention.start, mention.end) if i in owner), None)
            if covered is not None:
                if set(mention.tickers) - set(by_span[covered]):
                    why = "covered by a longer name"
                    suppressed.append((mention.start, SuppressedName(mention.tickers, mention.text, why)))
                continue
            by_span[span] = list(mention.tickers)
            texts[span] = mention.text
            for i in range(mention.start, mention.end):
                owner[i] = span

        accepted: list[_Mention] = []
        for (start, end), tickers in sorted(by_span.items()):
            mention = _Mention(tuple(sorted(tickers)), start, end, texts[(start, end)])
            window = headline[max(0, start - 80) : start]
            if _RATER_AFTER.match(headline, end) or _RATER_BEFORE.search(window):
                suppressed.append((start, SuppressedName(mention.tickers, mention.text, "the rater, not the subject")))
            else:
                accepted.append(mention)
        suppressed.sort(key=lambda pair: (pair[0], pair[1].tickers, pair[1].reason))
        return tuple(accepted), tuple(s for _, s in suppressed)


# --------------------------------------------------------------------------
# Labelling
# --------------------------------------------------------------------------

_SUBJECT: Final = "the subject of the matched phrase"
_OBJECT_OF: Final = "the object of the matched phrase"
_CHAINED_SUBJECT: Final = "the subject of the phrase before it"
_ONLY_TAG: Final = "the article's only tag"
_NO_SUBJECT: Final = "no named company in the subject position"
_NO_SUBJECT_EARNINGS: Final = (
    "no named company in the subject position, and an earnings phrase never takes the article's only tag"
)
_NOT_ONE_OBJECT: Final = "the object is not exactly one named company"
_AMBIGUOUS_LIST: Final = "ambiguous: the subject is one of several coordinated names"
_AMBIGUOUS_OBJECT: Final = "ambiguous: the object names more than one company"
_NAMES_INSIDE: Final = "the phrase itself names another company"
_NAMES_AFTER: Final = "another company is named after the phrase"
_NO_ACTOR: Final = "the actor is not one named company, alone at the start of its clause, straight before the verb"

#: The longest headline, after whitespace is collapsed, that is matched at all.
MAX_HEADLINE_CHARS: Final = 512

#: A wire-service prefix that starts a clause: *BRIEF-*, *UPDATE 2-*, *BUZZ-*.
_WIRE_PREFIX: Final = re.compile(r"[A-Z]{2,}(?: \d+)?-")
#: A preposition followed by a capitalised word: a name in a role (the
#: acquirer, the bidder, the broker, the destination) the book may not know.
_NAME_IN_A_ROLE: Final = re.compile(r"\b(?:by|from|at|for)\s+(?-i:[A-Z])", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Found:
    pattern: RulePattern
    shape: Shape
    phrase: str
    start: int
    end: int
    object_span: tuple[int, int] | None


def _find(headline: str) -> tuple[list[_Found], list[VetoedMatch]]:
    found: list[_Found] = []
    vetoed: list[VetoedMatch] = []
    question = "?" in headline or _QUESTION_LEAD.match(headline) is not None
    preview = "a question, not an event" if question else None
    if preview is None and _PREVIEW.search(headline):
        preview = "a preview, not an event"
    for pattern in RULE_PATTERNS:
        recorded = False
        hit: _Found | None = None
        for shape in pattern.shapes:
            match = shape.search(headline)
            if match is None:
                continue
            reason = preview or pattern.vetoed_by(headline)
            if reason is None and _NEGATED.search(headline, max(0, match.start() - 40), match.start()):
                reason = "negated or not yet happened"
            if reason is not None:
                vetoed.append(VetoedMatch(pattern.rule_id, match.group(0), reason))
                recorded = True
                continue
            span = match.span("object") if shape.binds_object else None
            hit = _Found(pattern, shape, match.group(0), match.start(), match.end(), span)
            break
        if hit is not None:
            found.append(hit)
        elif not recorded:
            near = pattern.near_miss_in(headline)
            if near is not None:
                vetoed.append(VetoedMatch(pattern.rule_id, near.group(0), pattern.near_miss_reason))
    return found, vetoed


def _first_of_run(headline: str, mentions: Sequence[_Mention], index: int) -> int:
    """Walk back over the same company named twice in a row: *Nvidia (NVDA)*."""
    first = index
    while (
        first > 0
        and mentions[first - 1].tickers == mentions[index].tickers
        and not headline[mentions[first - 1].end : mentions[first].start].strip()
    ):
        first -= 1
    return first


def _starts_clause(headline: str, start: int) -> bool:
    """Whether nothing but a clause boundary or a wire prefix precedes ``start``."""
    before = headline[:start].rstrip()
    return (
        not before
        or before[-1] in ";:|,"
        or before.endswith((" -", "–", "—"))
        or _WIRE_PREFIX.fullmatch(before) is not None
    )


def _bind_subject(headline: str, hit: _Found, mentions: Sequence[_Mention]) -> tuple[tuple[str, ...], str]:
    if any(hit.start <= m.start < hit.end for m in mentions):
        return (), _NAMES_INSIDE
    if hit.shape.alone_after and any(m.start >= hit.end for m in mentions):
        return (), _NAMES_AFTER
    ends = [m.end for m in mentions]
    index = bisect.bisect_right(ends, hit.start) - 1
    if index < 0:
        return (), _NO_SUBJECT
    nearest = mentions[index]
    if not hit.shape.gap_fits(headline[nearest.end : hit.start]):
        return (), _NO_SUBJECT
    first = _first_of_run(headline, mentions, index)
    start = mentions[first].start
    if _COORDINATED.search(headline, max(0, start - 12), start):
        return (), _AMBIGUOUS_LIST
    if first > 0 and _COMMA_ONLY.fullmatch(headline, mentions[first - 1].end, start):
        return (), _AMBIGUOUS_LIST
    return nearest.tickers, _SUBJECT


def _actor_named(headline: str, hit: _Found, mentions: Sequence[_Mention]) -> bool:
    """The acquirer: one named company, alone at the start of its clause, then the verb."""
    ends = [m.end for m in mentions]
    index = bisect.bisect_right(ends, hit.start) - 1
    if index < 0 or not hit.shape.gap_fits(headline[mentions[index].end : hit.start]):
        return False
    return _starts_clause(headline, mentions[_first_of_run(headline, mentions, index)].start)


def _bind_object(headline: str, hit: _Found, mentions: Sequence[_Mention]) -> tuple[tuple[str, ...], str]:
    assert hit.object_span is not None
    if hit.shape.actor and not _actor_named(headline, hit, mentions):
        return (), _NO_ACTOR
    start, end = hit.object_span
    inside = [m for m in mentions if m.start >= start and m.end <= end]
    if not inside or inside[0].start != start:
        return (), _NOT_ONE_OBJECT
    if len({m.tickers for m in inside}) > 1:
        return (), _AMBIGUOUS_OBJECT
    for before, after in zip(inside, inside[1:]):
        if headline[before.end : after.start].strip():
            return (), _NOT_ONE_OBJECT
    if not hit.shape.gap_fits(headline[inside[-1].end : end]):
        return (), _NOT_ONE_OBJECT
    return inside[0].tickers, _OBJECT_OF


def _bind_all(
    headline: str,
    found: Sequence[_Found],
    mentions: Sequence[_Mention],
    suppressed: Sequence[SuppressedName],
    feed: NewsFeed,
    tags: Sequence[str],
    book: NameBook,
) -> list[tuple[tuple[str, ...], str]]:
    bound = [
        _bind_object(headline, hit, mentions) if hit.object_span else _bind_subject(headline, hit, mentions)
        for hit in found
    ]
    # A phrase with no subject of its own, straight after a bound subject
    # phrase across "," / ";" / "and", takes that subject.
    for index in sorted(range(len(found)), key=lambda i: found[i].start):
        hit = found[index]
        if bound[index][1] != _NO_SUBJECT or hit.object_span is not None:
            continue
        before = [
            i for i in range(len(found))
            if i != index and found[i].end <= hit.start and bound[i][1] in (_SUBJECT, _CHAINED_SUBJECT)
        ]  # fmt: skip
        if before:
            previous = max(before, key=lambda i: found[i].end)
            if _CHAINED.fullmatch(headline, found[previous].end, hit.start):
                bound[index] = (bound[previous][0], _CHAINED_SUBJECT)
    # The single-tag rule, for a phrase with no named subject -- never one of
    # NO_SINGLE_TAG_FAMILIES, which stays unattributed and says why.
    only = _single_tag(headline, mentions, suppressed, feed, tags, book)
    if only is not None:
        bound = [
            (
                ((), _NO_SUBJECT_EARNINGS)
                if hit.pattern.family in NO_SINGLE_TAG_FAMILIES
                else ((only,), _ONLY_TAG)
            )
            if how == _NO_SUBJECT
            else (who, how)
            for hit, (who, how) in zip(found, bound)
        ]
    return bound


def _single_tag(
    headline: str,
    mentions: Sequence[_Mention],
    suppressed: Sequence[SuppressedName],
    feed: NewsFeed,
    tags: Sequence[str],
    book: NameBook,
) -> str | None:
    """The article's one equity tag, if the single-tag rule may use it here.

    Never a fund or a market tag (AUDIT3-M1): a vendor tags a macro release
    -- CPI, payrolls, jobless claims -- with SPY or QQQ alone, and the phrase
    that fires on it (*Below Expectations*) is about the economy, often in the
    opposite direction to the fund. Which tags are funds is the book's input.
    (The earnings family is kept off every lone tag by the caller,
    :func:`_bind_all`, through :data:`NO_SINGLE_TAG_FAMILIES`.)

    Never when the headline names that company in any way at all -- as a
    mention, as a name set aside for any reason (a rater, an ordinary word,
    covered by a longer name, an exchange prefix), or by the first word of its
    own name -- and never when a capitalised word follows *by*, *from*, *at* or
    *for*: that is a name in some other role, and it may be the tag's.
    """
    if feed not in SINGLE_TAG_FEEDS:
        return None
    equities = list(dict.fromkeys(t for t in tags if book.is_equity(t)))
    if len(equities) != 1:
        return None
    only = equities[0]
    if book.is_fund(only):
        return None
    named = {t for m in mentions for t in m.tickers} | {t for s in suppressed for t in s.tickers}
    if only in named or book.names_part_of(headline, only) or _NAME_IN_A_ROLE.search(headline):
        return None
    return only


def normalise_headline(headline: str) -> tuple[str, bool]:
    """``headline`` with every whitespace run collapsed to one space and the ends
    stripped, and whether the result is longer than :data:`MAX_HEADLINE_CHARS`."""
    text = " ".join(headline.split())
    return text, len(text) > MAX_HEADLINE_CHARS


def label_headline(
    headline: str, feed: NewsFeed, tickers: Sequence[str], book: NameBook
) -> RulesLabelling:
    """The rules tier on one headline, given the article's feed and its tags.

    The headline is normalised first (:func:`normalise_headline`), and every
    phrase is taken from the normalised text. One longer than
    :data:`MAX_HEADLINE_CHARS` is not matched at all: it returns empty, with
    ``truncated`` set.
    """
    headline, too_long = normalise_headline(headline)
    if too_long:
        return RulesLabelling(labels=(), matches=(), conflicting=(), unattributed=(), truncated=True)
    found, vetoed_list = _find(headline)
    vetoed = tuple(vetoed_list)
    if not found and not vetoed:
        return RulesLabelling(labels=(), matches=(), conflicting=(), unattributed=())
    mentions, suppressed = book.mentions(headline, tickers)
    if not found:
        return RulesLabelling(
            labels=(), matches=(), conflicting=(), unattributed=(), vetoed=vetoed, suppressed=suppressed
        )
    bound = _bind_all(headline, found, mentions, suppressed, feed, tickers, book)
    matches = tuple(
        RuleMatch(
            rule_id=hit.pattern.rule_id,
            family=hit.pattern.family,
            direction=hit.pattern.direction,
            discovery_flagged=hit.pattern.discovery_flagged,
            phrase=hit.phrase,
            tickers=who,
            attribution=how,
        )
        for hit, (who, how) in zip(found, bound)
    )
    if len({m.direction for m in matches}) > 1:
        return RulesLabelling(
            labels=(), matches=matches, conflicting=matches, unattributed=(), vetoed=vetoed, suppressed=suppressed
        )

    by_ticker: dict[str, list[RuleMatch]] = {}
    for match in matches:
        for ticker in match.tickers:
            by_ticker.setdefault(ticker, []).append(match)
    labels = tuple(
        SentimentLabel(
            ticker=ticker,
            source=LabelSource.RULES,
            tier=SentimentTier.RULES,
            direction=own[0].direction,
            reasoning="; ".join(f'{m.rule_id} matched "{m.phrase}"' for m in own) + f" ({own[0].attribution})",
            rule_id=own[0].rule_id,
        )
        for ticker, own in sorted(by_ticker.items())
    )
    unattributed = tuple(m for m in matches if not m.tickers)
    return RulesLabelling(
        labels=labels,
        matches=matches,
        conflicting=(),
        unattributed=unattributed,
        vetoed=vetoed,
        suppressed=suppressed,
    )


def rules_labels(article: NewsArticle, book: NameBook) -> RulesLabelling:
    """:func:`label_headline` on a fetched article's headline, feed and tags."""
    return label_headline(article.headline, article.feed, article.tickers, book)
