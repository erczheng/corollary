"""Feed configuration: three variables, read once, and no defaults.

The rule CLAUDE.md spends a paragraph on is the reason this file is longer
than the code it tests. Defaulting ``ALPACA_STOCK_FEED_HISTORICAL`` to IEX
measures a strategy's ``min_avg_volume`` against roughly 2.5% of real US
equity volume, so a 5,000,000 threshold silently filters on a fortieth of what
the strategy author wrote. Nothing errors. The scanner just returns a
different universe than the document says it should.

So: **a missing variable raises.** Not a warning, not a fallback.
"""

import ast
import os
from collections.abc import Mapping

import pytest

from corollary.data.providers.alpaca import (
    ALPACA_OPTIONS_FEED_ENV,
    ALPACA_STOCK_FEED_HISTORICAL_ENV,
    ALPACA_STOCK_FEED_REALTIME_ENV,
    FeedConfig,
    FeedConfigError,
)

#: What CLAUDE.md documents for the Basic plan.
BASIC_PLAN: Mapping[str, str] = {
    ALPACA_OPTIONS_FEED_ENV: "indicative",
    ALPACA_STOCK_FEED_HISTORICAL_ENV: "sip",
    ALPACA_STOCK_FEED_REALTIME_ENV: "iex",
}


def test_all_three_resolve_from_the_environment() -> None:
    feeds = FeedConfig.from_env(BASIC_PLAN)
    assert feeds.options == "indicative"
    assert feeds.stock_historical == "sip"
    assert feeds.stock_realtime == "iex"


def test_the_algo_trader_plus_upgrade_is_three_values_and_nothing_else() -> None:
    """CLAUDE.md: upgrading "sets all three and changes nothing else"."""
    feeds = FeedConfig.from_env(
        {
            ALPACA_OPTIONS_FEED_ENV: "opra",
            ALPACA_STOCK_FEED_HISTORICAL_ENV: "sip",
            ALPACA_STOCK_FEED_REALTIME_ENV: "sip",
        }
    )
    assert (feeds.options, feeds.stock_historical, feeds.stock_realtime) == (
        "opra",
        "sip",
        "sip",
    )


@pytest.mark.parametrize(
    "missing",
    [
        ALPACA_OPTIONS_FEED_ENV,
        ALPACA_STOCK_FEED_HISTORICAL_ENV,
        ALPACA_STOCK_FEED_REALTIME_ENV,
    ],
)
def test_a_missing_variable_raises_and_names_itself(missing: str) -> None:
    env = dict(BASIC_PLAN)
    del env[missing]
    with pytest.raises(FeedConfigError) as caught:
        FeedConfig.from_env(env)
    assert missing in str(caught.value)


@pytest.mark.parametrize(
    "missing",
    [
        ALPACA_OPTIONS_FEED_ENV,
        ALPACA_STOCK_FEED_HISTORICAL_ENV,
        ALPACA_STOCK_FEED_REALTIME_ENV,
    ],
)
def test_a_blank_variable_is_missing_rather_than_empty(missing: str) -> None:
    """``ALPACA_STOCK_FEED_REALTIME=`` in a .env file is an unset variable.

    ``.env.example`` ships every name with an empty value, so a half-filled
    copy of it is the likeliest way this goes wrong. An empty string reaching
    the query parameter would be a 400 from the vendor with no clue why.
    """
    env = dict(BASIC_PLAN)
    env[missing] = "   "
    with pytest.raises(FeedConfigError) as caught:
        FeedConfig.from_env(env)
    assert missing in str(caught.value)


def test_the_historical_error_explains_the_volume_consequence() -> None:
    """The one variable whose silent default would be worst says why.

    A message that only says "set ALPACA_STOCK_FEED_HISTORICAL" invites
    someone to set it to ``iex`` to make the error go away.
    """
    env = dict(BASIC_PLAN)
    del env[ALPACA_STOCK_FEED_HISTORICAL_ENV]
    with pytest.raises(FeedConfigError) as caught:
        FeedConfig.from_env(env)
    message = str(caught.value).lower()
    assert "volume" in message
    assert "sip" in message


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        (ALPACA_OPTIONS_FEED_ENV, "sip"),  # a stock feed in the options slot
        (ALPACA_OPTIONS_FEED_ENV, "indicitive"),  # a plausible typo
        (ALPACA_STOCK_FEED_HISTORICAL_ENV, "spi"),
        (ALPACA_STOCK_FEED_HISTORICAL_ENV, "opra"),  # an options feed
        (ALPACA_STOCK_FEED_HISTORICAL_ENV, "delayed_sip"),  # latest-only
        (ALPACA_STOCK_FEED_REALTIME_ENV, "indicative"),
    ],
)
def test_a_value_the_endpoint_does_not_accept_is_refused_up_front(
    variable: str, value: str
) -> None:
    """A typo should fail at construction, not as a 400 mid-poll.

    ``delayed_sip`` is the interesting one: it is a real Alpaca feed, valid on
    the *latest* endpoints and absent from the historical bars enum. Accepting
    it here would produce a 400 only once someone asked for bars.
    """
    env = dict(BASIC_PLAN)
    env[variable] = value
    with pytest.raises(FeedConfigError) as caught:
        FeedConfig.from_env(env)
    assert variable in str(caught.value)
    assert value in str(caught.value)


def test_values_are_normalised_but_not_guessed() -> None:
    env = dict(BASIC_PLAN)
    env[ALPACA_OPTIONS_FEED_ENV] = "  INDICATIVE  "
    assert FeedConfig.from_env(env).options == "indicative"


def test_from_env_reads_os_environ_when_given_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in BASIC_PLAN.items():
        monkeypatch.setenv(name, value)
    assert FeedConfig.from_env().options == "indicative"


def test_the_real_environment_is_the_source_and_not_the_data_feed_table() -> None:
    """No database import anywhere in the provider module.

    Step 2 seeded a ``data_feed`` table with these same three values. Which of
    ``.env`` and the table wins at runtime is an open step-7 decision, and
    reading both here would decide it by accident. The provider reads the
    environment; the table is not consulted.
    """
    import corollary.data.providers.alpaca as provider_module

    source = provider_module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    for forbidden in ("corollary.db", "DataFeed", "sqlalchemy"):
        assert forbidden not in text, (
            f"{forbidden!r} appears in the provider; the .env-vs-data_feed "
            "precedence is an open step-7 decision and must not be settled here"
        )


def test_the_environment_variable_names_are_exactly_claude_mds() -> None:
    assert ALPACA_OPTIONS_FEED_ENV == "ALPACA_OPTIONS_FEED"
    assert ALPACA_STOCK_FEED_HISTORICAL_ENV == "ALPACA_STOCK_FEED_HISTORICAL"
    assert ALPACA_STOCK_FEED_REALTIME_ENV == "ALPACA_STOCK_FEED_REALTIME"


def test_the_names_are_all_present_in_env_example() -> None:
    """A variable the provider requires must be discoverable without .env.

    ``.claude/settings.json`` denies reading the real ``.env``, so
    ``.env.example`` is the only map anyone has.
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    ))))
    with open(os.path.join(root, ".env.example"), encoding="utf-8") as handle:
        example = handle.read()
    for name in BASIC_PLAN:
        assert f"{name}=" in example


def test_the_recorder_resolves_feeds_the_same_way_the_provider_does() -> None:
    """The one script outside the provider that used to know a feed name.

    ``tests/fixtures/record_alpaca.py`` built a ``FeedConfig`` by hand with
    ``or``-fallbacks to the three Basic-plan values, sitting next to the very
    environment variable names those defaults were meant to make unnecessary.
    Defensible in a hand-run script and indefensible as a thing to copy from:
    three lines like that are exactly what someone pastes when ``from_env``
    raises on them somewhere it matters. It calls ``from_env`` and lets it
    raise.

    Checked over the parsed source rather than the raw text, so the *history*
    can stay written in a comment while the *value* cannot come back.
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    ))))
    path = os.path.join(root, "tests", "fixtures", "record_alpaca.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()

    assert "FeedConfig.from_env()" in source
    tree = ast.parse(source)
    feed_names = set(BASIC_PLAN.values())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value in feed_names:
            raise AssertionError(
                f"{node.value!r} appears as a literal in the recorder at line "
                f"{node.lineno}. Feed names are configuration; the recorder "
                "reads the same three variables the engine does."
            )
