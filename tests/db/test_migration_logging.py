"""Running a migration must not switch off the loggers already built.

Separate from ``test_migrations.py`` because this is not a claim about the
schema. That file asks whether the migrated database matches the models; this
one asks what running a migration does to the rest of the process, which is a
different question that happens to have the same trigger.

The bug it pins was real and shipped: ``env.py`` called
``fileConfig(config.config_file_name)``, and ``logging.config.fileConfig``
defaults to ``disable_existing_loggers=True`` -- so every logger that existed
at that moment was switched off and stayed off.

It surfaced as a test-order artifact. A ``caplog`` assertion in
``tests/engine/test_ledger.py`` passed alone and failed in a full run, because
``test_migrations.py`` had run ``command.upgrade`` first. The assertion read as
"the code did not log" when the code had logged into a disabled logger.

**In production it would not have surfaced at all.** Decision 1 puts the engine
and the API in one process, so the natural home for ``upgrade head`` is the
FastAPI lifespan -- and there it would kill every ``corollary.*`` logger for the
life of the process. Rule 8: "A rejected order records the rule that rejected
it, the inputs, and the timestamp. Silent rejection is a bug." Nothing raises,
nothing is misconfigured, and the rejection log is simply empty.
"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from corollary.db.session import sqlite_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

#: Two of the engine's loggers, named rather than sampled: these are the ones
#: rule 8's rejection records travel through.
ENGINE_LOGGERS = ("corollary.engine.ledger", "corollary.engine.grouping")


def test_migrating_does_not_silence_the_engines_loggers(db_path: Path) -> None:
    loggers = [logging.getLogger(name) for name in ENGINE_LOGGERS]
    saved = [logger.disabled for logger in loggers]
    for logger in loggers:
        logger.disabled = False
    try:
        cfg = Config(str(ALEMBIC_INI))
        cfg.set_main_option("sqlalchemy.url", sqlite_url(db_path))
        command.upgrade(cfg, "head")

        still_on = {logger.name: logger.disabled for logger in loggers}
        assert still_on == {name: False for name in ENGINE_LOGGERS}
    finally:
        for logger, was in zip(loggers, saved):
            logger.disabled = was


def test_the_guard_would_notice(db_path: Path) -> None:
    """The test above is only worth having if the old default would fail it.

    Proves the assertion is live rather than vacuous: run ``fileConfig`` the
    way ``env.py`` used to, and the logger really does go dead.
    """
    from logging.config import fileConfig

    logger = logging.getLogger(ENGINE_LOGGERS[0])
    saved = logger.disabled
    logger.disabled = False
    try:
        fileConfig(str(ALEMBIC_INI))  # the old call, without the keyword
        assert logger.disabled is True, (
            "the old default no longer disables loggers, so the guard in "
            "env.py may no longer be needed -- check before removing it"
        )
    finally:
        logger.disabled = saved
        fileConfig(str(ALEMBIC_INI), disable_existing_loggers=False)
