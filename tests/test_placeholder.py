"""Smoke tests: the package imports, and the risk gate collects something.

``uv run python -m pytest -m risk`` is mandatory before any engine change,
so what the marker selects has to be worth running. It selects **the tests
that protect a money-safety rule** -- which is a property of the test, not
of the module it lives in. When ``RiskManager`` lands its limits will be
the largest group by far: every ceiling proving it rejects, and proving it
permits at the boundary. Today the first group is rule 9's dead-man's
switch, in ``tests/engine/test_runtime.py``, whose header states the line
between what is tagged there and what is not.

The test below is not that content and never was -- it pins the
*registration*. Under ``--strict-markers`` a typo in the marker name is a
collection error rather than a quietly empty run, and this keeps ``-m
risk`` from ever passing by selecting nothing at all.
"""

import pytest

from corollary.api import app


def test_package_imports() -> None:
    assert app.title == "Corollary"


@pytest.mark.risk
def test_risk_marker_is_registered() -> None:
    """The gate's floor: ``-m risk`` always collects at least this one."""
    assert True
