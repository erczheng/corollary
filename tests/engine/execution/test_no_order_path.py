"""Rule 1, for the broker's own shape.

The Phase 2 design splits ``BrokerInterface`` in two:

* ``BrokerAccount`` -- account, positions, orders, activities. Implemented
  here, in this phase.
* ``BrokerExecution`` -- submit, cancel, replace. *"Does not exist until Phase
  6."*

The reason the second half is **absent** rather than stubbed is quoted in the
design: *"API routes depend on ``BrokerAccount`` only, so ``submit_order`` is
not in a type they can reach. That keeps rule 1 structural rather than
disciplinary."* An empty ``Protocol`` is a type a route can reach. A stub
raising ``NotImplementedError`` is a method a route can call. Both would turn
the guarantee back into a convention, and rule 1 exists instead of a
convention.

**Most of what was here now lives in ``tests/test_hard_rules.py``.** The
tree-wide guards -- no module declares or uses ``BrokerExecution``, nothing
defines or calls ``submit_order``, no broker exposes a write-shaped method,
no write verb reaches the vendor surface, the vendor SDK is imported nowhere
-- were scoped to this file's subject when they are facts about the whole
package, and the same assertion had been copied into three other subject-named
files with three different scopes. They are one guard each now, scoped to
``corollary/``, and they cover more than these did: every module, including
the ones nobody has written a test file for yet.

What stays here is what a tree-wide guard cannot express -- the *shape of
these specific types*: which methods ``BrokerAccount`` declares, that
``AlpacaBroker`` satisfies it, and that ``sim.py`` is still empty. A guard
that walks the package can prove a name is absent; it cannot prove a
particular abstract base declares exactly five methods and no sixth.
"""

# --------------------------------------------------------------------------
# Why one of these carries ``@pytest.mark.risk`` and two do not
# --------------------------------------------------------------------------
#
# The line -- what earns the marker and what does not -- is stated once, at the
# top of ``tests/engine/test_runtime.py``, and ``tests/test_hard_rules.py``
# explains why it reaches tests that read source rather than run it. This file
# applies both; it restates neither.
#
# ``test_broker_account_declares_exactly_the_read_surface`` is tagged: the set
# of methods on the read half *is* rule 1's structural guarantee expressed as
# a type, and a sixth method appearing there is how the guarantee would end --
# silently, with nothing failing at runtime until something called it.
#
# Two are left bare. ``test_alpaca_broker_satisfies_broker_account`` is
# conformance: if it fails the broker will not instantiate, which is loud, at
# startup, and before any money moves. ``test_no_sim_broker_exists_yet`` pins
# a *phase scope* rather than a rule -- a ``SimBroker`` arriving early is
# drift to report, not an order path, since a simulated broker places nothing
# by construction -- and it is expected to be deleted rather than satisfied
# when Phase 6 arrives.

import ast
import inspect
from pathlib import Path

import pytest

from corollary.engine.execution.alpaca import AlpacaBroker
from corollary.engine.execution.interface import BrokerAccount

PACKAGE = Path(__file__).resolve().parents[3] / "corollary"


def _defined_names(path: Path) -> set[str]:
    """Every name the module *binds*. Parsed, so prose about it does not count.

    ``sim.py`` says in its docstring what will one day be in it, and a
    substring test would fail on that sentence -- which is the fastest way to
    get a rule deleted.

    ``ast.AnnAssign`` is walked alongside ``ast.Assign`` because an annotated
    binding is still a binding: ``SimBroker: TypeAlias = ...`` declares the
    name just as surely as ``SimBroker = ...`` does, and dropping the branch
    let that one spelling through.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return names


@pytest.mark.risk
def test_broker_account_declares_exactly_the_read_surface() -> None:
    """Adding a method here is a deliberate act, and this is the record of it."""
    assert BrokerAccount.__abstractmethods__ == frozenset(
        {"account", "positions", "orders", "activities", "portfolio_history"}
    )


def test_alpaca_broker_satisfies_broker_account() -> None:
    assert issubclass(AlpacaBroker, BrokerAccount)
    assert not inspect.isabstract(AlpacaBroker)


def test_no_sim_broker_exists_yet() -> None:
    """Out of scope: *"No ``SimBroker``, Parquet, DuckDB or backtest worker."*"""
    assert "SimBroker" not in _defined_names(PACKAGE / "engine" / "execution" / "sim.py")
