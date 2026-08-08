"""Phase 1 smoke tests: prove the pytest config and package layout work.

Real risk manager tests land in Phase 6. This file exists so `uv run
pytest -m risk` has something to collect and so a typo in the marker name
fails loudly (--strict-markers), per CLAUDE.md.
"""

import pytest

from corollary.api import app


def test_package_imports() -> None:
    assert app.title == "Corollary"


@pytest.mark.risk
def test_risk_marker_is_registered() -> None:
    """Placeholder — real risk manager tests replace this in Phase 6."""
    assert True
