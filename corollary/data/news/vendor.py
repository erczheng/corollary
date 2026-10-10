"""The vendor tier: Massive's per-ticker insights become sentiment labels. Pure.

Phase 3 design, decision 4: *"Every headline in the store is labelled by every
source that can reach it: ... Massive on the articles it carries an
``insights[]`` entry for. Each label is its own ``sentiment_label`` row."*
Decision 21: *"Massive insights need no attribution rule, because each one
names its own ticker."* *Testing*: *"one Massive article tagging several
tickers yields one label per ticker with its own direction; an article with no
insight for a ticker yields no row for it."*

The mapping
-----------

Massive's ``sentiment`` takes four values (step-0 probe, 2026-09-24):
``positive``, ``neutral``, ``negative`` and ``mixed``. The spec: *"``mixed``
... is neither directional value. The mapping has to say so explicitly."*

* ``positive`` -> ``bullish``; ``negative`` -> ``bearish``.
* ``neutral`` -> ``neutral``. Decision 4: *"A Massive ``neutral`` is a label
  and displays ``Neutral``, not ``Unclassified``"*.
* ``mixed`` -> ``neutral``. It is neither directional value, and
  ``sentiment_label.direction`` has exactly three, so the only non-directional
  one is ``neutral``. It is still a *label*, not an absence: decision 4 makes
  ``Unclassified`` mean that *"Massive carried no insight for that ticker on
  that article"*, and a ``mixed`` insight is an insight. The consequences the
  spec states hold either way -- it is never a discovery candidate (decision
  21: *"``neutral`` and ``mixed`` Massive insights never qualify"*) and is
  excluded from accuracy as a neutral (Q5). The vendor's word itself survives
  only in ``reasoning``, which Massive writes per insight.

Anything else is a vendor shape change. It is **never guessed at**: the
insight produces no label and is returned in :attr:`VendorLabelling.unmapped`
for the caller to log -- this module does no I/O.

One label per ticker
--------------------

``sentiment_label`` is UNIQUE ``(article_id, ticker, source)``. Should an
article carry several insights for one ticker:

* **All agree in direction** -- one label, from the first; the rest are
  returned in :attr:`VendorLabelling.duplicates`, collapsed but not dropped.
* **Two known values disagree** -- **no label** for that ticker, PRD section
  9's *"silence beats a wrong label"*, and the ticker is returned in
  :attr:`VendorLabelling.conflicting`.
* **A known value beside an unknown one** -- also conflicting, so also no
  label. The unknown value could be the vendor's word for the opposite
  direction, so the known one is not safe to publish alone. The unknown
  insight is still in :attr:`VendorLabelling.unmapped`.
* **Only unknown values** -- unmapped, and not conflicting: there is nothing
  known for them to disagree with.

No recorded response has shown any of these; they are handled so that none
can reach the table as a constraint violation, a silent pick, or a silent
drop (rule 8's spirit).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from corollary.data.news.article import NewsArticle, VendorInsight
from corollary.data.news.labels import (
    Direction,
    LabelSource,
    SentimentLabel,
    SentimentTier,
)

__all__ = ["MASSIVE_SENTIMENT_DIRECTION", "VendorLabelling", "vendor_labels"]

#: Massive's four ``sentiment`` values, each to its direction. Closed: a value
#: not here labels nothing.
MASSIVE_SENTIMENT_DIRECTION: Final[Mapping[str, Direction]] = MappingProxyType(
    {
        "positive": Direction.BULLISH,
        "negative": Direction.BEARISH,
        "neutral": Direction.NEUTRAL,
        "mixed": Direction.NEUTRAL,
    }
)


@dataclass(frozen=True, slots=True)
class VendorLabelling:
    """One article's vendor labels, and what was refused on the way."""

    #: One per ticker, in the vendor's insight order.
    labels: tuple[SentimentLabel, ...]
    #: Insights whose ``sentiment`` is not one of Massive's four values.
    unmapped: tuple[VendorInsight, ...]
    #: Tickers given insights that disagree -- two known directions, or a known
    #: value beside an unknown one -- so given no label. First-seen order.
    conflicting: tuple[str, ...]
    #: Insights collapsed into an earlier one for the same ticker that agrees.
    duplicates: tuple[VendorInsight, ...]


def vendor_labels(article: NewsArticle) -> VendorLabelling:
    """Massive's labels for ``article``: one per ticker its insights name.

    A ticker the article is tagged with but no insight names gets no label;
    an article with no insights gets none at all. Same article, same result.
    """
    grouped: dict[str, list[VendorInsight]] = {}
    unmapped: list[VendorInsight] = []
    for insight in article.insights:
        grouped.setdefault(insight.ticker, []).append(insight)
        if insight.sentiment not in MASSIVE_SENTIMENT_DIRECTION:
            unmapped.append(insight)

    labels: list[SentimentLabel] = []
    conflicting: list[str] = []
    duplicates: list[VendorInsight] = []
    for ticker, group in grouped.items():
        known = [i for i in group if i.sentiment in MASSIVE_SENTIMENT_DIRECTION]
        if not known:
            continue  # unknown values only: already in ``unmapped``
        directions = {MASSIVE_SENTIMENT_DIRECTION[i.sentiment] for i in known}
        if len(directions) > 1 or len(known) < len(group):
            conflicting.append(ticker)
            continue
        first = known[0]
        duplicates.extend(known[1:])
        labels.append(
            SentimentLabel(
                ticker=ticker,
                source=LabelSource.MASSIVE,
                tier=SentimentTier.VENDOR,
                direction=MASSIVE_SENTIMENT_DIRECTION[first.sentiment],
                reasoning=first.reasoning,
                rule_id=None,
            )
        )
    return VendorLabelling(
        labels=tuple(labels),
        unmapped=tuple(unmapped),
        conflicting=tuple(conflicting),
        duplicates=tuple(duplicates),
    )
