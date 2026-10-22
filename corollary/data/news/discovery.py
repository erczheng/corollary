"""Decision 21's discovery candidates -- *Movers in the news* -- derived on read. Pure.

A ticker is a candidate, over the page's lookback, when all three hold
(decision 21, *What makes a discovery candidate*):

1. **It is outside the watch universe**, at read time. Candidates are derived
   on read and never stored, so a ticker the owner watches leaves at once.
2. **A high-signal article names it**: a rules label from a
   ``discovery_flagged`` pattern is attributed to it, or a Massive
   ``positive``/``negative`` insight names it. ``neutral`` and ``mixed`` never
   qualify -- :data:`~corollary.data.news.vendor.MASSIVE_SENTIMENT_DIRECTION`
   stores both as ``neutral``, so "directional" is the whole test.
   Attribution is the labeller's (``rules.py``, ``vendor.py``): a label row
   *is* the attribution, and this module adds no rule of its own.
3. **It passes the tradeability filter**, as the ``ticker_tradeability``
   cache already decided it. **Only the cached ``passes`` decides.** The
   boundaries (ADV, last close, completed sessions, IPO dates) are
   ``tradeability.py``'s and are not re-implemented here, and no vendor is
   called: a ticker with no cached verdict is *awaiting a check*, counted and
   not served.

Decisions this module makes
---------------------------

* **One reason per (source, rule, direction)**, carrying every article it
  rests on, newest first. **Conflicting directions are kept side by side,
  never netted** -- an ``analyst_downgrade`` and a Massive ``positive`` on the
  same ticker are two reasons with two directions.
* **Articles are canonical articles.** The caller maps each label to its
  duplicate group's canonical row (decision 3), so one story carried by three
  vendors is one article here. **The caller also picks the label**: one per
  (group, ticker, source), by the feed's rule
  (:func:`corollary.data.news.signals.group_labels` -- the canonical row's,
  else the lowest member id's). Two labels from one source for one ticker on
  one canonical article are refused with ``ValueError``: choosing between them
  here would be a second rule, and the feed and Movers would then show
  different reasoning for the same story.
* **A ``rule_id`` no shipped pattern carries is unflagged**: decision 21's
  *"A pattern step 5 adds later is unflagged until someone flags it"* read the
  other way round -- a retired pattern's stored label is no one's flag.
* **Ranking** (decision 21, confirmed by the owner as written, Q22):
  a candidate with any rules reason outranks every Massive-only one,
  regardless of recency; within each group, the newest qualifying article
  first; ties by ticker, so the order is total and the same inputs always give
  the same output whatever order they arrived in. The API serves this order
  and the client does not re-rank.

No I/O, no clock: the route reads the rows and the time and passes them in.
"""

from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from corollary.data.news.labels import SOURCE_TIER, Direction, LabelSource, SentimentTier
from corollary.data.news.rules import RULE_PATTERNS, RuleFamily, RulePattern
from corollary.data.news.tradeability import TradeabilityResult
from corollary.data.news.watchlist import MARKET_TICKER
from corollary.wire import require_aware

__all__ = [
    "DIRECTIONAL",
    "FLAGGED_PATTERNS",
    "CachedVerdict",
    "Candidate",
    "CandidateArticle",
    "Discovery",
    "DiscoverySignal",
    "Evidence",
    "RankGroup",
    "Reason",
    "discover",
    "qualifies",
    "verdict_from_result",
]

#: The two directions a discovery signal may carry. ``neutral`` never qualifies.
DIRECTIONAL: Final[frozenset[Direction]] = frozenset({Direction.BULLISH, Direction.BEARISH})

#: Every shipped pattern by ``rule_id``. Read-only.
FLAGGED_PATTERNS: Final[Mapping[str, RulePattern]] = MappingProxyType(
    {pattern.rule_id: pattern for pattern in RULE_PATTERNS}
)

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=timezone.utc)
#: Source order inside a candidate's reasons: rules first, as in the ranking.
_SOURCE_ORDER: Final[Mapping[LabelSource, int]] = MappingProxyType(
    {LabelSource.RULES: 0, LabelSource.MASSIVE: 1}
)


@dataclass(frozen=True, slots=True)
class DiscoverySignal:
    """One stored label, joined to its duplicate group's canonical article.

    ``article_id``, ``published_at``, ``headline``, ``url`` and ``publisher``
    are the **canonical** row's; ``ticker``, ``source``, ``direction``,
    ``rule_id`` and ``reasoning`` are the ``sentiment_label`` row's.
    """

    article_id: int
    published_at: datetime
    headline: str
    url: str
    publisher: str | None
    ticker: str
    source: LabelSource
    direction: Direction
    rule_id: str | None
    reasoning: str | None

    def __post_init__(self) -> None:
        require_aware(self.published_at, "published_at")


@dataclass(frozen=True, slots=True)
class CachedVerdict:
    """One ``ticker_tradeability`` row, as the read path needs it.

    ``failures`` are the stored tokens as strings, not
    :class:`~corollary.data.news.tradeability.TradeabilityFailure` members: a
    row written under an older token (Q10 retired ``insufficient_history``)
    must still read as the failure it is, and only ``passes`` is consulted.
    """

    ticker: str
    session_date: date
    passes: bool
    avg_volume_20d: int | None
    last_close: Decimal | None
    sessions_available: int
    failures: tuple[str, ...]
    checked_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.checked_at, "checked_at")
        if self.passes and self.failures:
            raise ValueError(f"{self.ticker}: a passing verdict lists failures {self.failures}")
        if not self.passes and not self.failures:
            raise ValueError(f"{self.ticker}: a failing verdict must say why")
        if self.passes and (self.avg_volume_20d is None or self.last_close is None):
            # A pass with no ADV or close would serve a candidate with no
            # numbers; the filter cannot produce one (both are checked).
            raise ValueError(f"{self.ticker}: a passing verdict carries its ADV and last close")


def verdict_from_result(result: TradeabilityResult, *, checked_at: datetime) -> CachedVerdict:
    """The verdict a stored :class:`TradeabilityResult` reads back as."""
    return CachedVerdict(
        ticker=result.ticker,
        session_date=result.session_date,
        passes=result.passes,
        avg_volume_20d=result.avg_volume_20d,
        last_close=result.last_close,
        sessions_available=result.sessions_available,
        failures=tuple(failure.value for failure in result.failures),
        checked_at=checked_at,
    )


@dataclass(frozen=True, slots=True)
class Evidence:
    """One article a reason rests on, with the label's own reasoning text."""

    article_id: int
    published_at: datetime
    #: The rules tier's matched phrase, or Massive's ``sentiment_reasoning`` verbatim.
    reasoning: str | None


@dataclass(frozen=True, slots=True)
class Reason:
    """One (source, rule, direction) a candidate was flagged by."""

    source: LabelSource
    tier: SentimentTier
    #: The pattern, for a rules reason; ``None`` for Massive.
    rule_id: str | None
    family: RuleFamily | None
    direction: Direction
    #: Newest first, ties by article id descending. Never empty.
    evidence: tuple[Evidence, ...]

    @property
    def article_ids(self) -> tuple[int, ...]:
        return tuple(item.article_id for item in self.evidence)

    @property
    def latest_at(self) -> datetime:
        return self.evidence[0].published_at


@dataclass(frozen=True, slots=True)
class CandidateArticle:
    """A canonical article behind at least one of a candidate's reasons."""

    id: int
    published_at: datetime
    headline: str
    url: str
    publisher: str | None


#: ``0``: at least one rules reason. ``1``: Massive only.
RankGroup = int
_RULES_GROUP: Final[RankGroup] = 0
_MASSIVE_ONLY_GROUP: Final[RankGroup] = 1


@dataclass(frozen=True, slots=True)
class Candidate:
    """One *Movers in the news* row."""

    ticker: str
    #: Rules first, then Massive; within a source newest first, then rule id
    #: and direction -- a total order.
    reasons: tuple[Reason, ...]
    #: The distinct directions among the reasons, sorted. Two members means
    #: the reasons disagree, which is shown, never netted.
    directions: tuple[Direction, ...]
    #: Newest first, ties by id descending.
    articles: tuple[CandidateArticle, ...]
    #: The newest qualifying article's time -- the within-group sort key.
    latest_at: datetime
    rank_group: RankGroup
    #: The cached verdict's numbers, exactly as stored.
    avg_volume_20d: int
    sessions_available: int
    last_close: Decimal
    tradeability_session_date: date
    tradeability_checked_at: datetime

    @property
    def has_rules_reason(self) -> bool:
        return self.rank_group == _RULES_GROUP


@dataclass(frozen=True, slots=True)
class Discovery:
    """The ranked candidates, and why every other signalled name is absent."""

    candidates: tuple[Candidate, ...]
    #: Off-watch tickers carrying at least one qualifying signal in the lookback.
    signalled: int
    #: Watched tickers that carried a qualifying signal (not counted in ``signalled``).
    watched_excluded: int
    #: Signalled tickers whose cached verdict fails.
    failed_tradeability: int
    #: Signalled tickers with no cached verdict yet, sorted.
    awaiting_check: tuple[str, ...]


def qualifies(signal: DiscoverySignal, patterns: Mapping[str, RulePattern]) -> bool:
    """Condition 2: a flagged rules label, or a directional Massive insight."""
    if signal.ticker == MARKET_TICKER or signal.direction not in DIRECTIONAL:
        return False
    if signal.source is LabelSource.RULES:
        if signal.rule_id is None:
            return False
        pattern = patterns.get(signal.rule_id)
        return pattern is not None and pattern.discovery_flagged
    return signal.source is LabelSource.MASSIVE


def _micros(at: datetime) -> int:
    """An aware instant as integer microseconds since the epoch -- an exact sort key."""
    return (at - _EPOCH) // timedelta(microseconds=1)


def _reason_key(reason: Reason) -> tuple[int, int, str, str]:
    return (
        _SOURCE_ORDER[reason.source],
        -_micros(reason.latest_at),
        reason.rule_id or "",
        reason.direction.value,
    )


def _evidence_key(item: Evidence) -> tuple[int, int]:
    return (-_micros(item.published_at), -item.article_id)


def _rank_key(candidate: Candidate) -> tuple[int, int, str]:
    """Rules-first, then newest, then ticker. Q22: confirmed by the owner as written."""
    return (candidate.rank_group, -_micros(candidate.latest_at), candidate.ticker)


def _reasons(
    signals: Iterable[DiscoverySignal], patterns: Mapping[str, RulePattern]
) -> tuple[Reason, ...]:
    grouped: dict[tuple[LabelSource, str | None, Direction], dict[int, Evidence]] = {}
    for signal in signals:
        key = (signal.source, signal.rule_id, signal.direction)
        # ``discover`` refused a second label per (source, ticker, article),
        # so each article appears here once per reason.
        grouped.setdefault(key, {})[signal.article_id] = Evidence(
            signal.article_id, signal.published_at, signal.reasoning
        )
    reasons = []
    for (source, rule_id, direction), by_article in grouped.items():
        pattern = patterns.get(rule_id) if rule_id is not None else None
        reasons.append(
            Reason(
                source=source,
                tier=SOURCE_TIER[source],
                rule_id=rule_id,
                family=pattern.family if pattern is not None else None,
                direction=direction,
                evidence=tuple(sorted(by_article.values(), key=_evidence_key)),
            )
        )
    return tuple(sorted(reasons, key=_reason_key))


def _articles(signals: Iterable[DiscoverySignal]) -> tuple[CandidateArticle, ...]:
    by_id: dict[int, CandidateArticle] = {}
    for signal in signals:
        by_id.setdefault(
            signal.article_id,
            CandidateArticle(
                id=signal.article_id,
                published_at=signal.published_at,
                headline=signal.headline,
                url=signal.url,
                publisher=signal.publisher,
            ),
        )
    return tuple(
        sorted(by_id.values(), key=lambda a: (-_micros(a.published_at), -a.id))
    )


def discover(
    signals: Sequence[DiscoverySignal],
    *,
    watched: Container[str],
    verdicts: Mapping[str, CachedVerdict],
    since: datetime | None,
    patterns: Mapping[str, RulePattern] = FLAGGED_PATTERNS,
) -> Discovery:
    """Decision 21's candidates from stored labels, the watch universe and cached verdicts.

    ``signals`` may hold anything stored; what does not qualify is ignored.
    ``since`` is the lookback's first instant (``None`` for everything);
    ``verdicts`` is the latest cached verdict per ticker. A verdict filed
    under another ticker's key is refused: it would serve one name's numbers
    under another.
    """
    if since is not None:
        require_aware(since, "since")
    for key, filed in verdicts.items():
        if filed.ticker != key:
            raise ValueError(f"the verdict filed under {key} is for {filed.ticker}")

    seen: set[tuple[LabelSource, str, int]] = set()
    for signal in signals:
        label_key = (signal.source, signal.ticker, signal.article_id)
        if label_key in seen:
            raise ValueError(
                f"two {signal.source.value} labels for {signal.ticker} on canonical article "
                f"{signal.article_id}: the caller picks one per group, ticker and source "
                "(signals.group_labels), and this layer never chooses between them"
            )
        seen.add(label_key)

    by_ticker: dict[str, list[DiscoverySignal]] = {}
    for signal in signals:
        if since is not None and signal.published_at < since:
            continue
        if not qualifies(signal, patterns):
            continue
        by_ticker.setdefault(signal.ticker, []).append(signal)

    candidates: list[Candidate] = []
    watched_excluded = 0
    failed = 0
    awaiting: list[str] = []
    for ticker in sorted(by_ticker):
        if ticker in watched:
            watched_excluded += 1
            continue
        verdict = verdicts.get(ticker)
        if verdict is None:
            awaiting.append(ticker)
            continue
        if not verdict.passes:
            failed += 1
            continue
        # ``CachedVerdict`` refuses a pass without these; restated for mypy.
        assert verdict.avg_volume_20d is not None and verdict.last_close is not None
        own = by_ticker[ticker]
        reasons = _reasons(own, patterns)
        articles = _articles(own)
        candidates.append(
            Candidate(
                ticker=ticker,
                reasons=reasons,
                directions=tuple(sorted({r.direction for r in reasons}, key=lambda d: d.value)),
                articles=articles,
                latest_at=articles[0].published_at,
                rank_group=(
                    _RULES_GROUP
                    if any(r.source is LabelSource.RULES for r in reasons)
                    else _MASSIVE_ONLY_GROUP
                ),
                avg_volume_20d=verdict.avg_volume_20d,
                sessions_available=verdict.sessions_available,
                last_close=verdict.last_close,
                tradeability_session_date=verdict.session_date,
                tradeability_checked_at=verdict.checked_at,
            )
        )

    return Discovery(
        candidates=tuple(sorted(candidates, key=_rank_key)),
        signalled=len(by_ticker) - watched_excluded,
        watched_excluded=watched_excluded,
        failed_tradeability=failed,
        awaiting_check=tuple(awaiting),
    )
