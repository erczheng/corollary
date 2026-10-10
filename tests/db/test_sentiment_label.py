"""Phase 3 step 5's ``sentiment_label`` table (migration 0013).

Design, *Database*: ``sentiment_label(id, article_id, ticker, source, tier,
direction, reasoning, rule_id, labeled_at)``, UNIQUE ``(article_id, ticker,
source)``. ``direction`` in ``bullish | bearish | neutral``; ``tier`` in
``rules | vendor``, CHECK-constrained. No ``confidence`` column.

Every CHECK runs against **both** ``create_all()`` and ``alembic upgrade
head``: autogenerate does not compare CHECK text, so the model/migration
match test cannot see a CHECK one side lacks.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, delete, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corollary.data.news.labels import SOURCE_TIER, Direction, LabelSource, SentimentTier
from corollary.db.models import (
    SENTIMENT_DIRECTIONS,
    SENTIMENT_SOURCES,
    SENTIMENT_TIERS,
    NewsArticle,
    SentimentLabelRow,
)
from corollary.db.session import create_db_engine, sqlite_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

T0 = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)


def _config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(params=["models", "migration"])
def any_engine(request: pytest.FixtureRequest, db_path: Path) -> Iterator[Engine]:
    url = sqlite_url(db_path)
    if request.param == "migration":
        command.upgrade(_config(url), "head")
        eng = create_db_engine(url)
    else:
        from corollary.db.models import Base

        eng = create_db_engine(url)
        Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _article(sess: Session, vendor_id: str = "1") -> int:
    row = NewsArticle(
        vendor="alpaca",
        vendor_id=vendor_id,
        feed="alpaca_news",
        canonical_id=None,
        url=f"https://example.com/story/{vendor_id}",
        url_key=f"https://example.com/story/{vendor_id}",
        headline="Acme beats estimates",
        headline_key="acme beats estimates",
        summary=None,
        publisher="Benzinga",
        published_at=T0,
        ingested_at=T0 + timedelta(minutes=1),
    )
    sess.add(row)
    sess.flush()
    return row.id


def _label(article_id: int, **overrides: object) -> SentimentLabelRow:
    fields: dict[str, object] = {
        "article_id": article_id,
        "ticker": "ACME",
        "source": "rules",
        "tier": "rules",
        "direction": "bullish",
        "reasoning": 'earnings_beat matched "Acme beats estimates"',
        "rule_id": "earnings_beat",
        "labeled_at": T0,
    }
    fields.update(overrides)
    return SentimentLabelRow(**fields)


# --------------------------------------------------------------- vocabularies


def test_vocabularies_are_the_labeller_enums() -> None:
    """The CHECK lists are derived from the enums the labellers build with."""
    assert set(SENTIMENT_DIRECTIONS) == {d.value for d in Direction} == {"bullish", "bearish", "neutral"}
    assert set(SENTIMENT_TIERS) == {t.value for t in SentimentTier} == {"rules", "vendor"}
    assert set(SENTIMENT_SOURCES) == {s.value for s in LabelSource} == {"rules", "massive"}


def test_the_table_has_exactly_the_specs_columns(any_engine: Engine) -> None:
    columns = {c["name"] for c in inspect(any_engine).get_columns("sentiment_label")}
    assert columns == {
        "id", "article_id", "ticker", "source", "tier", "direction",
        "reasoning", "rule_id", "labeled_at",
    }  # fmt: skip
    assert "confidence" not in columns


# --------------------------------------------------------------- CHECKs


_ACCEPTED = [
    {},
    {"direction": "bearish", "rule_id": "earnings_miss"},
    {"direction": "neutral"},
    {"reasoning": None},
    {"source": "massive", "tier": "vendor", "rule_id": None, "reasoning": "Vendor text."},
    {"source": "massive", "tier": "vendor", "rule_id": None, "reasoning": None, "direction": "neutral"},
]

_REFUSED = [
    ({"direction": "mixed"}, "ck_sentiment_label_direction"),
    ({"direction": "positive"}, "ck_sentiment_label_direction"),
    ({"tier": "llm"}, "ck_sentiment_label_tier"),
    ({"source": "polygon", "tier": "vendor", "rule_id": None}, "ck_sentiment_label_source"),
    ({"source": "massive", "tier": "rules", "rule_id": None}, "ck_sentiment_label_source_tier"),
    ({"source": "rules", "tier": "vendor"}, "ck_sentiment_label_source_tier"),
    ({"rule_id": None}, "ck_sentiment_label_rule_id"),
    ({"rule_id": ""}, "ck_sentiment_label_rule_id"),
    ({"source": "massive", "tier": "vendor", "rule_id": "earnings_beat"}, "ck_sentiment_label_rule_id"),
    ({"ticker": ""}, "ck_sentiment_label_ticker"),
    ({"ticker": "acme"}, "ck_sentiment_label_ticker"),
]


@pytest.mark.parametrize("overrides", _ACCEPTED)
def test_valid_labels_are_accepted_and_round_trip(any_engine: Engine, overrides: dict[str, object]) -> None:
    with Session(any_engine) as sess:
        article_id = _article(sess)
        sess.add(_label(article_id, **overrides))
        sess.commit()
    with Session(any_engine) as sess:
        stored = sess.scalars(select(SentimentLabelRow)).one()
        expected = {**_label(article_id).__dict__, **overrides}
        for name in ("ticker", "source", "tier", "direction", "reasoning", "rule_id"):
            assert getattr(stored, name) == expected[name]
        assert stored.labeled_at == T0
        assert stored.labeled_at.tzinfo is not None


@pytest.mark.parametrize(("overrides", "constraint"), _REFUSED)
def test_invalid_labels_are_refused(any_engine: Engine, overrides: dict[str, object], constraint: str) -> None:
    with Session(any_engine) as sess:
        article_id = _article(sess)
        sess.add(_label(article_id, **overrides))
        with pytest.raises(IntegrityError, match=constraint):
            sess.commit()


def test_every_source_tier_pairing_the_labellers_build_is_accepted(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        article_id = _article(sess)
        for source, tier in SOURCE_TIER.items():
            sess.add(
                _label(
                    article_id,
                    source=source.value,
                    tier=tier.value,
                    rule_id="earnings_beat" if source is LabelSource.RULES else None,
                )
            )
        sess.commit()


# --------------------------------------------------------------- uniqueness


def test_one_label_per_article_ticker_source(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        article_id = _article(sess)
        sess.add(_label(article_id))
        sess.commit()
        sess.add(_label(article_id, direction="bearish", rule_id="earnings_miss"))
        with pytest.raises(IntegrityError, match="UNIQUE"):
            sess.commit()


def test_the_two_sources_each_label_one_ticker(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        article_id = _article(sess)
        sess.add(_label(article_id))
        sess.add(_label(article_id, source="massive", tier="vendor", rule_id=None, direction="bearish"))
        sess.add(_label(article_id, ticker="OTHR"))
        sess.commit()
        assert len(sess.scalars(select(SentimentLabelRow)).all()) == 3


# --------------------------------------------------------------- the article FK


def test_a_label_must_name_a_stored_article(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(_label(999))
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            sess.commit()


def test_a_labelled_article_cannot_be_deleted(any_engine: Engine) -> None:
    """No ``ON DELETE`` action: labels are kept indefinitely (decision 21), so
    deleting their article is refused rather than cascading the labels away or
    leaving them orphaned. Retention never tries -- it keeps labelled groups."""
    with Session(any_engine) as sess:
        article_id = _article(sess)
        sess.add(_label(article_id))
        sess.commit()
        # Refused at the end of the DELETE statement itself, not at commit.
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            sess.execute(delete(NewsArticle).where(NewsArticle.id == article_id))


def test_an_unlabelled_article_still_deletes(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        labelled = _article(sess, "1")
        unlabelled = _article(sess, "2")
        sess.add(_label(labelled))
        sess.commit()
        sess.execute(delete(NewsArticle).where(NewsArticle.id == unlabelled))
        sess.commit()
        assert sess.get(NewsArticle, labelled) is not None


# --------------------------------------------------------------- migration


def test_0013_downgrades_to_0012_and_back(db_path: Path) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0012")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert "sentiment_label" not in tables
    assert {"news_article", "news_article_ticker", "isin_ticker"} <= tables

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert "sentiment_label" in tables
