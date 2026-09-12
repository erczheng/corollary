"""Rule 1, enforced structurally rather than by discipline.

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

Every check here parses the source with :mod:`ast` rather than grepping it.
That is not fastidiousness: the *only* places ``BrokerExecution`` and
``submit_order`` legitimately appear in this codebase are prose -- the
interface's docstring explaining why the type is absent, and the risk
manager's stub explaining what it will one day own. A substring test would
fail on the very documentation that records the rule, which is the fastest
way to get a rule deleted. So: names that are *defined*, and calls that are
*made*. Text is ignored by construction.
"""

import ast
import inspect
from pathlib import Path

import pytest

from corollary.engine.execution import alpaca as alpaca_module
from corollary.engine.execution import interface as interface_module
from corollary.engine.execution.alpaca import AlpacaBroker
from corollary.engine.execution.interface import BrokerAccount

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE = REPO_ROOT / "corollary"
VENDOR_SURFACE = (
    PACKAGE / "engine" / "execution",
    PACKAGE / "data" / "providers",
)

#: Verbs that change something at the other end. ``request`` and ``send`` are
#: here because both take the method as an argument, so a GET-only audit that
#: only looked for ``.post(`` would miss ``.request("POST", ...)``.
WRITE_VERBS = frozenset({"post", "put", "patch", "delete", "request", "send"})


def _sources(*roots: Path) -> list[Path]:
    return sorted(path for root in roots for path in root.rglob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _defined_names(tree: ast.Module) -> set[str]:
    """Every name this module *binds* -- class, function, variable, import."""
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


def _referenced_names(tree: ast.Module) -> set[str]:
    """Every name or attribute this module *uses*."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def test_broker_execution_is_not_a_declared_type_anywhere() -> None:
    """Not as a class, not as a Protocol, not as an alias, not as a stub."""
    assert not hasattr(interface_module, "BrokerExecution")
    assert not hasattr(alpaca_module, "BrokerExecution")

    declared = {
        str(path.relative_to(REPO_ROOT))
        for path in _sources(PACKAGE)
        if "BrokerExecution" in _defined_names(_tree(path))
    }
    assert declared == set(), f"BrokerExecution is declared in {sorted(declared)}"


def test_broker_execution_is_not_referenced_as_code_anywhere() -> None:
    """A prose mention is fine; a use is not.

    The interface's docstring *should* say the other half arrives in Phase 6 —
    that is the record of the decision. What it may not do is name a type
    anything can annotate against.
    """
    used = {
        str(path.relative_to(REPO_ROOT))
        for path in _sources(PACKAGE)
        if "BrokerExecution" in _referenced_names(_tree(path))
    }
    assert used == set(), f"BrokerExecution is used as code in {sorted(used)}"


def test_nothing_in_the_package_defines_or_calls_submit_order() -> None:
    """There is no order path in this phase at all."""
    offenders = {
        str(path.relative_to(REPO_ROOT))
        for path in _sources(PACKAGE)
        if "submit_order" in _referenced_names(_tree(path))
    }
    assert offenders == set(), f"submit_order is reachable from {sorted(offenders)}"


def test_the_broker_exposes_no_write_shaped_method() -> None:
    """A name is not a guarantee, but an absent method is."""
    forbidden = (
        "submit",
        "place",
        "cancel",
        "replace",
        "close_position",
        "close_all",
        "liquidate",
        "exercise",
    )
    names = [name for name, _ in inspect.getmembers(AlpacaBroker, callable)]
    assert [
        name
        for name in names
        if any(word in name for word in forbidden) and not name.startswith("__")
    ] == []


def test_broker_account_declares_exactly_the_read_surface() -> None:
    """Adding a method here is a deliberate act, and this is the record of it."""
    assert BrokerAccount.__abstractmethods__ == frozenset(
        {"account", "positions", "orders", "activities", "portfolio_history"}
    )


def test_alpaca_broker_satisfies_broker_account() -> None:
    assert issubclass(AlpacaBroker, BrokerAccount)
    assert not inspect.isabstract(AlpacaBroker)


@pytest.mark.parametrize("verb", sorted(WRITE_VERBS))
def test_no_vendor_file_issues_a_non_get_request(verb: str) -> None:
    """The two files that touch Alpaca are read-only in this phase.

    Scoped to the vendor surface rather than the whole package on purpose:
    ``api/routes/`` will legitimately declare ``@router.post`` in step 7, and
    a test that broke on that would be noise someone silences. What must stay
    true is that nothing *reaches Alpaca* with a verb that changes anything.
    """
    offenders = []
    for path in _sources(*VENDOR_SURFACE):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == verb
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == [], f".{verb}() is called at {offenders}"


def test_the_only_http_call_on_the_vendor_surface_is_get() -> None:
    """The positive form of the test above, so the list cannot go stale.

    A new verb Alpaca invents would not be in ``WRITE_VERBS`` and the
    parametrised test would pass in silence. This one enumerates what *is*
    called on a client and insists it is only ``get``.
    """
    client_calls = set()
    for path in _sources(*VENDOR_SURFACE):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_client"
            ):
                client_calls.add(node.func.attr)
    assert client_calls == {"get", "aclose"}, client_calls


def test_the_alpaca_package_is_not_imported() -> None:
    """Step 3's decision, restated for the broker.

    ``alpaca-py`` annotates its market-data models ``bid_price: float``,
    ``open: float`` and so on, so the SDK converts every price to an IEEE
    double on ingest -- at the *entry* boundary, where the loss is
    unrecoverable. Both vendor files use ``httpx`` plus ``corollary.wire``
    instead.
    """
    for path in _sources(PACKAGE):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.split(".")[0] == "alpaca", (
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.split(".")[0] == "alpaca", (
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                )


def test_no_sim_broker_exists_yet() -> None:
    """Out of scope: *"No ``SimBroker``, Parquet, DuckDB or backtest worker."*"""
    sim = PACKAGE / "engine" / "execution" / "sim.py"
    assert "SimBroker" not in _defined_names(_tree(sim))
