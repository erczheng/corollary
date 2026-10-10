"""The sentiment label both labelling tiers produce -- one future ``sentiment_label`` row.

Phase 3 design, *Database*: ``sentiment_label(id, article_id, ticker, source,
tier, direction, reasoning, rule_id, labeled_at)``, UNIQUE ``(article_id,
ticker, source)``; ``direction`` in ``bullish | bearish | neutral``; ``tier`` in
``rules | vendor``, CHECK-constrained. Decision 13 names the two sources:
``rules`` and ``massive``.

:class:`SentimentLabel` carries exactly the columns a labeller decides. The
three it does not are left to the persistence step: ``id`` (the database's),
``article_id`` (the stored article's key, which a pure labeller never sees)
and ``labeled_at`` (the moment of writing, not of computing).

Shared by ``rules.py`` and ``vendor.py`` so the two tiers cannot drift apart on
what a label is. Pure: no I/O, no clock.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

__all__ = ["Direction", "LabelSource", "SentimentLabel", "SentimentTier"]


class Direction(StrEnum):
    """``sentiment_label.direction``. Exactly three values; there is no ``mixed``."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class SentimentTier(StrEnum):
    """Decision 17: ``'rules' | 'vendor'``. ``llm`` returns in Phase 4, as a migration."""

    RULES = "rules"
    VENDOR = "vendor"


class LabelSource(StrEnum):
    """``sentiment_label.source`` -- the unit demotion is graded per (decision 13)."""

    RULES = "rules"
    MASSIVE = "massive"


#: Which tier each source belongs to. A label claiming another pairing is refused.
#: Read-only: a caller re-pairing a source would change what every label means.
SOURCE_TIER: Final[Mapping[LabelSource, SentimentTier]] = MappingProxyType(
    {
        LabelSource.RULES: SentimentTier.RULES,
        LabelSource.MASSIVE: SentimentTier.VENDOR,
    }
)


@dataclass(frozen=True, slots=True)
class SentimentLabel:
    """One label on one ticker of one article, by one source.

    ``rule_id`` names the pattern for a rules label and is ``None`` for a
    vendor label; ``reasoning`` is the vendor's own text for a vendor label,
    published as-is (decision 17), and may be ``None`` when the vendor sent
    none. Construction refuses a source/tier mismatch, a rules label with no
    ``rule_id``, a vendor label with one, and a blank ticker.
    """

    ticker: str
    source: LabelSource
    tier: SentimentTier
    direction: Direction
    reasoning: str | None
    rule_id: str | None

    def __post_init__(self) -> None:
        if not self.ticker or self.ticker != self.ticker.strip():
            raise ValueError(f"a label's ticker must be non-blank and trimmed: {self.ticker!r}")
        if SOURCE_TIER[self.source] is not self.tier:
            raise ValueError(
                f"source {self.source.value} is tier {SOURCE_TIER[self.source].value}, "
                f"not {self.tier.value}"
            )
        if self.tier is SentimentTier.RULES and not self.rule_id:
            raise ValueError("a rules label must name the rule that produced it")
        if self.tier is SentimentTier.VENDOR and self.rule_id is not None:
            raise ValueError("a vendor label has no rule_id")
