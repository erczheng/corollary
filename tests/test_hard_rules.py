"""The *structural* halves of CLAUDE.md's nine hard rules, in one file.

Every rule in this file is proven by reading the tree rather than by running
anything, because every rule in this file is an **absence**: a type nothing
declares, a method nothing defines, a verb nothing calls, a package nothing
imports. An absence has no runtime to observe, so the only way to test it is
to parse the source and find nothing.

**Why this file exists at all.** These guards used to live in files named
after their *subject* -- ``test_no_order_path`` for the broker,
``test_account_mode`` for the API, ``test_settings_routes`` for settings,
``test_record_alpaca`` for the recorder -- and the result was the same
assertion written eight times, each one scoped to whatever its file happened
to be about. Eight copies is not eight times the coverage: it is one rule
with seven blind spots, because a module nobody wrote a subject-file for was
covered by nothing. A ninth pass would have found a ninth gap. So the guards
are named after the *rule* and scoped to the *package*, and a new module is
covered the moment it is written, by nobody's deliberate act.

Each guard here collects offenders across every module in scope and asserts
the collection is empty, rather than being parametrised per module: one test
per rule that names the file and line that broke it is both shorter and
stricter than one test per file. ``_sources`` raises if a scope path has gone
stale, because a guard whose scope has evaporated passes in silence, which is
the exact failure mode this file was written to end.

Everything here parses with :mod:`ast`. That is not fastidiousness. The only
places ``BrokerExecution``, ``submit_order`` and *MCP* legitimately appear in
this codebase are prose -- the interface docstring explaining why the type is
absent, the risk manager's stub explaining what it will one day own, and the
``BrokerAuthError`` message explaining that the Alpaca MCP server holds
non-paper keys. A substring test fails on the very documentation that records
the rule, which is the fastest way to get a rule deleted. So: names that are
*bound*, names that are *used*, calls that are *made*. Text is ignored by
construction, and :func:`test_the_mcp_guard_ignores_prose` pins that it is.

What is here and what is not:

* **Rule 1** (all orders go through the risk manager) -- the whole structural
  set, below.
* **Rule 3** (no MCP in the order path or the data pipeline) -- below, and
  new; it had no test of any kind before this file.
* **Rule 2** (backtests cannot touch live credentials) -- *absent, and known
  to be.* ``corollary/backtest/`` is a stub, so rule 2 holds today only
  because there is nothing to violate it. It is a Phase 6 entry condition,
  not a gap to paper over with a test that would pass on an empty directory.
* **Rules 4, 5, 7, 8, 9** have runtimes, so they are tested by behaviour where
  they live -- ``tests/engine/test_runtime.py``, ``tests/api/test_engine_routes.py``,
  ``tests/db/``. Only structural residue belongs here.
* **Rule 6** (secrets live in the environment) is tested at the boundaries
  that could leak one -- ``tests/data/providers/test_alpaca_credentials.py``
  and the log-scrubbing tests. There is no absence to read off the tree.
"""

# --------------------------------------------------------------------------
# Why everything here carries ``@pytest.mark.risk``
# --------------------------------------------------------------------------
#
# The line -- what earns the marker and what does not -- is stated once, at
# the top of ``tests/engine/test_runtime.py``. This file applies it; it does
# not restate it. Read that note before adding anything here.
#
# It reaches every test in this file, with nothing left bare, for one reason:
# rule 1 and rule 3 are the two money rules with no runtime to test. There is
# no order path in Phase 2, so nothing here can be proven by placing an order
# and watching it be refused. The guarantee *is* the absence. A failure here
# means the absence ended -- a write verb reached the vendor surface, a type a
# route could annotate against came into existence, a name the risk manager is
# supposed to own appeared somewhere else, or a nondeterministic tool call
# appeared in the data pipeline. Each of those is an order placed outside
# ``RiskManager.approve()`` waiting for a caller, and none of them announce
# themselves at runtime. Silent is the criterion, and these are silent.
#
# The two meta-tests at the bottom are tagged for the same reason rather than
# a different one: a guard that has stopped catching anything is worse than no
# guard, because it reports safety it is no longer checking.
#
# --------------------------------------------------------------------------
# One amendment to that line, approved, and stated here because the canonical
# copy is held by another agent as this is written
# --------------------------------------------------------------------------
#
# The line in ``test_runtime.py`` says the marker means "this test protects a
# money-safety rule". Read strictly, a leaked API key is a *security* failure
# and not a money-safety one, and on that reading rule 6's guards would never
# qualify. That reading is wrong and the wording should say so: **a leaked
# credential is in scope for this marker.** A leaked key is somebody else
# placing orders in this account, which is the worst money outcome available
# in this project -- strictly worse than any limit this engine could compute
# incorrectly, because it takes the engine out of the loop entirely.
#
# ``tests/engine/test_runtime.py`` needs these same words added to its note.
# It is not edited from here because another agent holds that file; the
# orchestrator reconciles the two copies.

import ast
import inspect
import re
from pathlib import Path

import pytest

from corollary.data.providers.alpaca import AlpacaQuoteStream
from corollary.engine.execution import alpaca as alpaca_module
from corollary.engine.execution import interface as interface_module
from corollary.engine.execution.alpaca import AlpacaBroker, AlpacaTradeUpdateStream
from corollary.engine.execution.interface import BrokerAccount
from corollary.sockets import VendorStream

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = REPO_ROOT / "corollary"

FIXTURES = REPO_ROOT / "tests" / "fixtures"

#: The one-shot fixture recorders. Not part of the shipped package, but each
#: one holds live credentials and talks to a live host, so they are on the
#: vendor surface for the purposes of "nothing reaches a vendor with a write
#: verb" even though they are not on it for anything else.
#:
#: **Discovered rather than listed, and that is the fix for a real miss.**
#: This was a single path spelled ``record_alpaca.py``. ``record_finnhub.py``
#: then arrived -- a second script loading ``.env`` and calling a live vendor
#: with real credentials -- and was in the scope of no guard at all, while
#: its own docstring claimed a test asserted it contained no write verb. A
#: named list is a list somebody forgets to extend; a glob covers the third
#: recorder on the day it is written, by nobody's deliberate act.
#:
#: :data:`KNOWN_RECORDERS` is the floor under the glob, checked by
#: :func:`recorders` at every point of use. A pattern that matched nothing --
#: the directory renamed, the naming convention changed -- would report
#: success over an empty scope, which is this file's own stated failure mode.
#: ``_sources`` cannot help here the way it helps a named path: a glob never
#: yields a path that does not exist, so its staleness raise sees nothing
#: wrong with a scope that has quietly emptied.
#:
#: **The glob is deliberately non-recursive.** A future
#: ``tests/fixtures/recorders/record_x.py`` would fall outside it, and so
#: outside every guard below -- a recorder lives beside these two or the
#: pattern changes with it. What ``KNOWN_RECORDERS`` does catch is a *move*
#: of the two that exist, which is the case that matters: that is the one
#: where the credentials are already written and the guard is the thing that
#: goes missing. A recorder nobody has written yet leaks nothing.
RECORDER_GLOB = "record_*.py"
RECORDERS = tuple(sorted(FIXTURES.glob(RECORDER_GLOB)))

#: The recorders that exist today. The glob must find at least these.
KNOWN_RECORDERS = frozenset({"record_alpaca.py", "record_finnhub.py"})

#: The package's own two vendor directories -- the files that reach Alpaca
#: through a ``self._client`` attribute.
#:
#: Named rather than sliced off the front of the vendor surface by position. A
#: third package root added ahead of the recorders would silently shrink a
#: ``[:2]`` back to one directory, and ``_sources`` could not help: every path
#: in the slice still exists, so nothing raises and the guard reports success
#: over half its scope. That is this file's own stated failure mode -- a guard
#: whose scope has evaporated passes in silence.
#: ``corollary/sockets.py`` is the third entry and it is a *file*, which
#: :func:`_sources` handles. It is here because step 8d part 2 moved the
#: actual vendor I/O out of the two directories above and into it: it holds
#: the only code in the tree that knows what a real socket is, opens one, and
#: writes to one. For a while that left the one module performing vendor I/O
#: outside the scope of both write-verb guards -- the transport moved out of
#: the guarded directories as part of the change that created it, and the
#: behavioural assertions that replaced the file-scope check live in three
#: other files where nothing can see that they are the whole of it.
VENDOR_PACKAGES = (
    PACKAGE / "engine" / "execution",
    PACKAGE / "data" / "providers",
    PACKAGE / "sockets.py",
)

#: Every ``action`` a vendor websocket may put on the wire.
#:
#: ``auth`` and ``listen`` carry no instruction, and a ``subscribe`` is how a
#: *read* is scoped -- so none of the three changes anything at the vendor.
#: The guard over this is structural and file-scoped, which is what
#: :data:`WRITE_VERBS` cannot be for a socket: ``self._codec.transmit(...)``
#: is a name no HTTP verb list knows, and an ``{"action": "cancel"}`` frame on
#: the trading socket would be an order placed outside
#: ``RiskManager.approve()`` under a green gate.
SOCKET_ACTIONS = frozenset({"auth", "subscribe", "listen"})

#: The methods a ``VendorSocket`` may be asked to perform, and the whole
#: protocol: two directions, a read and a close.
SOCKET_PROTOCOL = frozenset({"send_text", "send_bytes", "recv", "close"})

#: What may be called on the ``websockets`` connection itself -- the library's
#: own API, which spells its write ``send``. Pinned so the exemption in
#: :func:`test_nothing_on_the_vendor_surface_issues_a_non_get_request` cannot
#: quietly widen into a second write path.
WEBSOCKET_CONNECTION_CALLS = frozenset({"send", "recv", "close"})

def recorders() -> tuple[Path, ...]:
    """Every fixture recorder, with the glob's floor checked *here*.

    A function rather than a constant, because the check has to run wherever
    the scope is used -- including from ``tests/fixtures/test_record_alpaca
    .py``, which walks these same files for the prose half of the same rule.
    The floor used to be asserted only inside
    ``test_every_fixture_recorder_is_inside_the_vendor_surface`` below, which
    made one test load-bearing for three guards across two files: skip it or
    delete it and all three quietly shrink back to :data:`VENDOR_PACKAGES`
    with nothing failing. A guard whose scope can evaporate in silence is the
    thing this file exists to refuse, so it does not get to live in this file.

    That test stays, because a named ``-m risk`` gate with a written reason
    is worth more than a raise nobody reads -- but it now *states* what this
    function enforces rather than being the only thing enforcing it.
    """
    found = {path.name for path in RECORDERS}
    missing = sorted(KNOWN_RECORDERS - found)
    if missing:
        raise AssertionError(
            f"the recorder glob {RECORDER_GLOB!r} under {_where(FIXTURES)} "
            f"found {sorted(found)}, which is missing {missing}. A script "
            "that loads .env and calls a live vendor is in no guard's scope "
            "until it is in this one."
        )
    return RECORDERS


def vendor_surface() -> tuple[Path, ...]:
    """Everything in the tree that may speak to a data vendor at all.

    The package's two vendor directories plus every recorder. Not a constant,
    so the recorder half can never silently be empty: see :func:`recorders`.
    """
    return VENDOR_PACKAGES + recorders()

#: Verbs that change something at the other end. ``request`` and ``send`` are
#: here because both take the method as an argument, so an audit that only
#: looked for ``.post(`` would miss ``.request("POST", ...)``.
#:
#: **This list is about HTTP, and the websockets are named so that it stays
#: that way.** Three vendor sockets landed on this surface in step 8d part 2,
#: and a websocket has to transmit -- an ``auth`` frame, a ``subscribe``, a
#: ``listen``. None of the three changes anything at the vendor: a subscribe
#: is how a *read* is scoped. So ``corollary.sockets.VendorSocket`` spells its
#: two directions ``send_text`` and ``send_bytes`` rather than overloading
#: ``send``, which keeps ``httpx``'s method-taking ``send`` catchable here
#: without excusing the sockets.
#:
#: What the sockets transmit is pinned by *behaviour* instead, which is
#: stronger than a name check: ``tests/data/providers/test_alpaca_stream.py``
#: asserts the market-data client's whole transmitted list is ``auth`` and
#: ``subscribe`` frames and nothing else, and
#: ``tests/engine/execution/test_trade_update_stream.py`` does the same for
#: ``auth`` and ``listen``. An order placed over a socket would fail both.
WRITE_VERBS = frozenset({"post", "put", "patch", "delete", "request", "send"})

#: Fragments of a method name that would mean the broker can change something.
WRITE_SHAPED = (
    "submit",
    "place",
    "cancel",
    "replace",
    "close_position",
    "close_all",
    "liquidate",
    "exercise",
)

#: An MCP client, in every spelling that is an import or an identifier.
#:
#: The prefix is anchored and the suffix is not, and that asymmetry is the
#: whole design. ``(?:^|[._])`` requires the token to *start* a word, which is
#: what keeps ``dmcpx`` out. The suffix admits a separator, the end of the
#: string, **or another letter**, because the idiomatic Python spelling of an
#: MCP client is a class name and a class name runs the letters together.
#: Requiring ``[._]`` or end-of-string there missed ``MCPClient``,
#: ``McpClient``, ``McpSession`` and ``mcpclient`` outright -- so
#: ``from vendor_tools import MCPClient`` passed :func:`_mcp_offenders`
#: entirely, with no string anywhere in it, and that is MCP in the data
#: pipeline under a green gate. ``re.IGNORECASE`` is what makes ``A-Z`` cover
#: the lowercase run-on too; it is also what makes ``MCP_SESSION`` match.
#:
#: So ``mcp``, ``fastmcp``, ``mcp.client.session``, ``mcp_client``,
#: ``call_mcp_tool``, ``MCP_SESSION``, ``MCPClient`` and ``McpSession`` all
#: match. ``dmcpx`` and ``amcp`` still do not.
MCP_TOKEN = re.compile(
    r"(?:^|[._])(?:fast)?mcp(?:[._A-Z]|$)|modelcontextprotocol", re.IGNORECASE
)


def _sources(*roots: Path) -> list[Path]:
    """Every module in scope, with a stale scope raising rather than passing.

    A guard pointed at a directory that has since been renamed finds no
    offenders and reports success. That is the failure mode this whole file
    exists to end, so a missing root is an error here rather than an empty
    list.

    **This protects named roots only.** A scope assembled by a glob --
    :data:`RECORDERS` -- never hands this function a path that does not
    exist: the glob simply returns fewer of them, or none, and every one it
    does return is real. That scope is checked by :func:`recorders` instead,
    and anything built out of a pattern needs the same treatment.
    """
    found: set[Path] = set()
    for root in roots:
        if root.is_dir():
            found.update(root.rglob("*.py"))
        elif root.is_file():
            found.add(root)
        else:
            raise AssertionError(f"guard scope has gone stale: {root} does not exist")
    return sorted(found)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _where(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


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


#: An identifier, wherever one is hiding inside a quoted annotation.
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _annotation_names(tree: ast.Module) -> set[str]:
    """Names reachable only through a *quoted* annotation.

    This was found by injecting the violation rather than by reading the code:
    ``def _dep() -> "BrokerExecution"`` passed every guard in the tree, old and
    new, because a quoted forward reference parses to an ``ast.Constant`` and
    never to an ``ast.Name``. That is not an exotic spelling -- it is the
    *only* way to annotate against a type that does not exist yet, which is
    precisely the state ``BrokerExecution`` is in until Phase 6. A route
    holding that annotation is the exact shape rule 1 forbids.

    Scoped to annotation positions, so ordinary strings stay invisible and the
    prose that documents these rules keeps passing.
    """
    names: set[str] = set()
    annotations: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                annotations.append(node.returns)
        elif isinstance(node, ast.arg) and node.annotation is not None:
            annotations.append(node.annotation)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    for annotation in annotations:
        for inner in ast.walk(annotation):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                names.update(IDENTIFIER.findall(inner.value))
    return names


def _referenced_names(tree: ast.Module) -> set[str]:
    """Every name or attribute this module *uses*, quoted annotations included.

    **The ``ast.alias`` branch is load-bearing and was missing for one
    revision.** An import binds its name without that name ever appearing as
    an ``ast.Name``, so ``from x import submit_order as _submit`` followed by
    ``_submit(order)`` reads, to a walk over ``ast.Name``, as a call to
    ``_submit`` and nothing else. Both halves of the alias are collected:
    ``from x import submit_order`` hides the forbidden name in ``alias.name``,
    and ``import x as submit_order`` hides it in ``alias.asname``.

    Nothing escapes either spelling *today*, because for such an import to
    resolve the name must be **defined** somewhere under ``corollary/`` and the
    definition site is caught by this same guard. That compensation ends in
    Phase 6. The moment rule 1's guard is relaxed from "``submit_order``
    appears nowhere" to "``submit_order`` appears only inside
    ``RiskManager``", ``from corollary.engine.execution.alpaca import
    submit_order as _place`` in a route is a live rule-1 bypass with a green
    gate. Do not narrow this back.

    It is also what makes a deletion elsewhere honest: the ``_identifiers``
    helper removed from ``tests/api/test_account_mode.py`` walked ``ast.alias``,
    and its removal was justified by this function containing it. For one
    revision that claim was false and the two shapes above were uncovered.
    """
    names: set[str] = _annotation_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name)
            if node.asname:
                names.add(node.asname)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def _imported_roots(tree: ast.Module) -> set[str]:
    """The top-level package of every import, however it is spelled."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _mcp_offenders(tree: ast.Module) -> list[str]:
    """Imports of, and references to, an MCP client. Never string content.

    The limit is stated rather than hidden: this reads identifiers, so
    anything naming MCP in a *string* passes. The identifier half is
    complete, and deliberately so -- ``MCP_TOKEN``'s suffix admits a
    following letter precisely so that ``MCPClient`` and ``McpSession`` are
    identifiers this catches rather than shapes it excuses -- which leaves a
    residual class that is **string-mediated and nothing else**. That class
    is wider than a shell-out, and its members are arguably the likelier
    spellings. All of these are confirmed missed, and every one of them
    carries the name in a string literal:

    * ``importlib.import_module("mcp")`` and ``__import__("mcp")`` -- an
      import whose module name is data rather than syntax.
    * ``client.call_tool("mcp__alpaca__get_quote", {})`` -- an already-open
      session, reached by tool name. ``mcp__alpaca__*`` is the vocabulary
      CLAUDE.md itself uses, which makes it the spelling nearest to hand.
    * ``subprocess.run(["npx", "alpaca-mcp-server"])`` -- launched by name.
    * ``httpx.get("http://127.0.0.1:9000/mcp/tools/quote")`` -- spoken to over
      plain HTTP, with no MCP identifier anywhere in the module.

    The trade is still the right one. Reading strings is exactly what would
    make this guard fail on the three places that discuss MCP correctly, and a
    guard that fails on correct code is a guard somebody deletes. The import
    and the call are the shapes that put MCP *in* the order path; a server
    reached through a string is a code review, not a gate.
    """
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if MCP_TOKEN.search(alias.name) or (
                    alias.asname and MCP_TOKEN.search(alias.asname)
                ):
                    hits.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [alias.name for alias in node.names]
            # ``asname`` for the same reason the ``ast.Import`` branch above
            # checks it, and it was missing here alone: ``from corollary.tools
            # import client as mcp_sess`` carries the token in neither the
            # module nor the imported name.
            renames = [alias.asname for alias in node.names if alias.asname]
            if (
                MCP_TOKEN.search(module)
                or any(MCP_TOKEN.search(n) for n in names)
                or any(MCP_TOKEN.search(n) for n in renames)
            ):
                hits.append(f"line {node.lineno}: from {module} import {names}")
        elif isinstance(node, ast.Name) and MCP_TOKEN.search(node.id):
            hits.append(f"line {node.lineno}: {node.id}")
        elif isinstance(node, ast.Attribute) and MCP_TOKEN.search(node.attr):
            hits.append(f"line {node.lineno}: .{node.attr}")
    return sorted(set(hits))


def _brokers_defined_in_the_package() -> list[type]:
    """``BrokerAccount`` and every implementation of it that we ship.

    Discovered rather than listed, with the limit named rather than implied:
    ``__subclasses__`` sees a broker the day it is **imported by the test
    session**, which is not the day it is written.
    ``corollary/engine/execution/__init__.py`` imports no submodules, so a
    ``BrokerAccount`` subclass in a file no test imports is invisible here.

    That is still strictly more than the ``AlpacaBroker``-only check this
    replaced, and the caller's assertion that ``AlpacaBroker`` *was*
    discovered is what stops the mechanism degrading to an empty list in
    silence. But a second broker needs an import to be seen; if one ever lands
    without a test that imports it, walking the package's modules is the fix,
    not a longer list here.

    Filtered to classes defined under ``corollary/`` on purpose: a test double
    declared in some other test module is a subclass too, and a guard whose
    result depends on which test files were imported first is a guard that
    fails on Tuesdays.
    """
    seen: dict[str, type] = {}
    stack: list[type] = [BrokerAccount]
    while stack:
        cls = stack.pop()
        if cls.__module__.startswith("corollary.") and cls.__name__ not in seen:
            seen[cls.__name__] = cls
        stack.extend(cls.__subclasses__())
    return [seen[name] for name in sorted(seen)]


# --------------------------------------------------------------------------
# Rule 1 -- all orders go through the risk manager
#
# The Phase 2 design splits ``BrokerInterface`` in two: ``BrokerAccount``
# (account, positions, orders, activities -- implemented now) and
# ``BrokerExecution`` (submit, cancel, replace -- *"does not exist until Phase
# 6"*). The second half is **absent** rather than stubbed because *"API routes
# depend on ``BrokerAccount`` only, so ``submit_order`` is not in a type they
# can reach. That keeps rule 1 structural rather than disciplinary."* An empty
# ``Protocol`` is a type a route can reach; a stub raising
# ``NotImplementedError`` is a method a route can call. Both would turn the
# guarantee back into a convention, and rule 1 exists instead of a convention.
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_no_module_declares_a_broker_execution_type() -> None:
    """Not as a class, not as a Protocol, not as an alias, not as a stub.

    The two ``hasattr`` checks are the runtime half and they are not
    redundant: a name injected into a module's ``globals()`` is bound without
    ever being parsed as a definition, and the two execution modules are the
    ones where that would matter.
    """
    assert not hasattr(interface_module, "BrokerExecution")
    assert not hasattr(alpaca_module, "BrokerExecution")

    declared = {
        _where(path)
        for path in _sources(PACKAGE)
        if "BrokerExecution" in _defined_names(_tree(path))
    }
    assert declared == set(), f"BrokerExecution is declared in {sorted(declared)}"


@pytest.mark.risk
def test_no_module_uses_broker_execution_as_code() -> None:
    """A prose mention is fine; a use is not.

    The interface's docstring *should* say the other half arrives in Phase 6 --
    that is the record of the decision. What it may not do is name a type
    anything can annotate against.

    The two tests overlap at the AST level and no longer pretend otherwise:
    now that ``_referenced_names`` walks ``ast.alias``, ``import x as
    BrokerExecution`` fails both. What the test above still holds alone is its
    runtime half -- a name injected into a module's ``globals()`` is bound
    without ever being parsed as anything -- and a failure there names
    *declaration* where a failure here names *use*, which is the first thing
    you want to know.
    """
    used = {
        _where(path)
        for path in _sources(PACKAGE)
        if "BrokerExecution" in _referenced_names(_tree(path))
    }
    assert used == set(), f"BrokerExecution is used as code in {sorted(used)}"


@pytest.mark.risk
def test_no_module_defines_or_calls_submit_order() -> None:
    """There is no order path in this phase at all.

    Every module, not only the ones somebody thought to write a subject-named
    test file for.
    """
    offenders = {
        _where(path)
        for path in _sources(PACKAGE)
        if "submit_order" in _referenced_names(_tree(path))
    }
    assert offenders == set(), f"submit_order is reachable from {sorted(offenders)}"


@pytest.mark.risk
def test_no_broker_exposes_a_write_shaped_method() -> None:
    """A name is not a guarantee, but an absent method is."""
    brokers = _brokers_defined_in_the_package()
    assert AlpacaBroker in brokers, "the broker under test was not discovered"

    offenders = [
        f"{cls.__name__}.{name}"
        for cls in brokers
        for name, _ in inspect.getmembers(cls, callable)
        if not name.startswith("__") and any(word in name for word in WRITE_SHAPED)
    ]
    assert offenders == [], f"a broker can change something: {offenders}"


@pytest.mark.risk
def test_every_fixture_recorder_is_inside_the_vendor_surface() -> None:
    """The floor under :data:`RECORDERS`' glob.

    Two guards below, and one in ``tests/fixtures/test_record_alpaca.py``,
    run over the recorders and would pass over an empty scope. This is the
    named statement that the scope is populated and contains the scripts
    anybody would name if asked -- so renaming the convention, or moving the
    recorders, fails here rather than quietly somewhere downstream.

    It no longer *is* the protection: :func:`recorders` raises on the same
    condition at every point of use, so deleting or skipping this test
    shrinks nobody's scope. It is the copy of that rule with a reason
    attached, which a raise inside a helper cannot be.

    It is also the missing half of a claim that was written down as done:
    ``record_finnhub.py`` documented a ``test_record_finnhub.py`` asserting
    it contained no write verb, and that file never existed. Generalising
    this file's guard was preferred to writing that one, because a per-vendor
    copy is how the eight-copies-with-seven-blind-spots problem in the module
    docstring started.
    """
    found = {path.name for path in recorders()}
    assert found >= KNOWN_RECORDERS, (
        f"the recorder glob found {sorted(found)}, which is missing "
        f"{sorted(KNOWN_RECORDERS - found)}. A script that loads .env and "
        "calls a live vendor is in no guard's scope until it is in this one."
    )
    assert set(recorders()) <= set(vendor_surface())


@pytest.mark.risk
def test_nothing_on_the_vendor_surface_issues_a_non_get_request() -> None:
    """The files that touch Alpaca are read-only in this phase.

    Scoped to the vendor surface rather than to the whole package on purpose:
    ``api/routes/`` will legitimately declare ``@router.post`` in step 7, and
    a guard that broke on that is one somebody silences. What must stay true
    is that nothing *reaches Alpaca* with a verb that changes anything.

    The recorders under ``tests/fixtures/`` are in scope here and nowhere
    else: they are the only other things in the tree holding live
    credentials, and a recorder that could POST is a write path to the broker
    sitting outside ``RiskManager.approve()``. **All** of them are in scope,
    found by :data:`RECORDERS`, because the second one was in scope of
    nothing while its own docstring said otherwise.

    One test rather than one per verb: the failure message names the verb and
    the line, so parametrising bought identity in the report and nothing in
    coverage, and the gate is the thing that has to stay short.

    **One exemption, and it is as narrow as it can be made:**
    ``self._connection.send(...)`` inside ``corollary/sockets.py``.
    ``websockets`` spells a frame write ``send`` and that name cannot be
    changed from here, while ``WRITE_VERBS`` needs ``send`` for ``httpx``,
    whose ``send`` takes the method as an argument and would carry a POST past
    this guard. So the exemption is keyed on the *receiver* -- the attribute
    holding a ``ClientConnection`` -- and on the file, and it is bounded from
    the other side by
    :func:`test_the_websocket_connection_is_only_ever_read_written_and_closed`,
    which enumerates everything called on that attribute and insists it is
    the transport protocol and nothing else. A websocket frame changes nothing
    at the vendor; what those frames may *say* is
    :func:`test_the_vendor_sockets_transmit_no_action_but_auth_subscribe_and_listen`.
    """
    offenders = []
    for path in _sources(*vendor_surface()):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in WRITE_VERBS
                and not _is_websocket_frame_write(path, node.func)
            ):
                offenders.append(f"{_where(path)}:{node.lineno} .{node.func.attr}()")
    assert offenders == [], f"a write verb reaches the vendor: {sorted(offenders)}"


def _is_websocket_frame_write(path: Path, func: ast.Attribute) -> bool:
    """``self._connection.send(...)`` in ``corollary/sockets.py``, and nothing else."""
    return (
        path.name == "sockets.py"
        and func.attr == "send"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "_connection"
    )


@pytest.mark.risk
def test_the_websocket_connection_is_only_ever_read_written_and_closed() -> None:
    """The positive form of the exemption above, keyed the same way.

    ``self._client`` has one of these and the socket needs its own: the
    transport's whole use of the ``websockets`` library is three calls, and
    enumerating them is what makes ``.send()`` being allowed a statement about
    frames rather than a hole. A fourth call -- anything that reconfigures the
    connection, or a second write path -- fails here.
    """
    calls = set()
    for path in _sources(*VENDOR_PACKAGES):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_connection"
            ):
                calls.add(node.func.attr)
    assert calls == WEBSOCKET_CONNECTION_CALLS, calls


@pytest.mark.risk
def test_the_vendor_sockets_transmit_no_action_but_auth_subscribe_and_listen() -> None:
    """What the three sockets may *say*, as a file-scope invariant.

    The behavioural assertions in ``tests/data/providers/test_alpaca_stream
    .py`` and ``tests/engine/execution/test_trade_update_stream.py`` are good
    and they stay -- they read back the whole transmitted list, which is
    stronger than any name check for the clients they cover. What they cannot
    do is cover a client nobody has written yet, and three counts of that gap
    were open at once: the vendor files transmit through
    ``self._codec.transmit(...)``, a name no guard knows;
    :data:`WRITE_SHAPED` is applied only to discovered *brokers*, and
    ``AlpacaTradeUpdateStream`` is a ``VendorStream`` rather than a broker; and
    the transport had left the guarded directories entirely. So a ``cancel``
    method transmitting ``{"action": "cancel", "order_id": ...}`` on the
    trading socket tripped nothing.

    This reads the frames instead of the call sites, which is what makes it
    independent of how a message reaches the wire: every ``action`` key in a
    dict literal anywhere on the vendor surface must carry one of
    :data:`SOCKET_ACTIONS`, spelled as a literal. A non-literal value fails
    too, deliberately -- an action assembled from a variable is exactly how
    this guard would be got round, and there is no reason for one.
    """
    offenders = []
    for path in _sources(*vendor_surface()):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and key.value == "action"):
                    continue
                if isinstance(value, ast.Constant) and value.value in SOCKET_ACTIONS:
                    continue
                offenders.append(
                    f"{_where(path)}:{node.lineno} action={ast.unparse(value)}"
                )
    assert offenders == [], f"a socket frame carries an unknown action: {offenders}"


@pytest.mark.risk
def test_no_vendor_socket_exposes_a_write_shaped_method() -> None:
    """:data:`WRITE_SHAPED`, applied to the sockets as well as the brokers.

    ``test_no_broker_exposes_a_write_shaped_method`` discovers ``BrokerAccount``
    subclasses, and the three stream clients are none: they subclass
    ``VendorStream``. So a ``cancel`` on the ``trade_updates`` client -- the
    one socket already authenticated against the *trading* host -- was in
    neither list. An absent method is still the only real guarantee.
    """
    discovered = _vendor_streams_defined_in_the_package()
    assert AlpacaQuoteStream in discovered, "the market-data client was not discovered"
    assert (
        AlpacaTradeUpdateStream in discovered
    ), "the trade_updates client was not discovered"

    offenders = [
        f"{cls.__name__}.{name}"
        for cls in discovered
        for name, _ in inspect.getmembers(cls, callable)
        if not name.startswith("__") and any(word in name for word in WRITE_SHAPED)
    ]
    assert offenders == [], f"a vendor socket can change something: {offenders}"


def _vendor_streams_defined_in_the_package() -> list[type]:
    """``VendorStream`` and every implementation of it that we ship.

    Same mechanism and same stated limit as
    :func:`_brokers_defined_in_the_package`: ``__subclasses__`` sees a class
    the day the test session imports it, so the caller asserts that the two
    that exist *were* discovered rather than trusting an empty list.
    """
    seen: dict[str, type] = {}
    stack: list[type] = [VendorStream]
    while stack:
        cls = stack.pop()
        if cls.__module__.startswith("corollary.") and cls.__name__ not in seen:
            seen[cls.__name__] = cls
        stack.extend(cls.__subclasses__())
    return [seen[name] for name in sorted(seen)]


@pytest.mark.risk
def test_the_only_http_call_on_the_vendor_surface_is_get() -> None:
    """The positive form of the test above, so the list cannot go stale.

    A new verb Alpaca invents would not be in ``WRITE_VERBS`` and the negative
    test would pass in silence. This one enumerates what *is* called on a
    client and insists it is only ``get``. Scoped to ``VENDOR_PACKAGES``
    because it keys on ``self._client``: a recorder owns its ``httpx`` client
    as a local, and is covered by the negative form above, which runs over
    the whole surface including every recorder.

    ``corollary/sockets.py`` is inside this scope and contributes nothing to
    the set, because it holds no HTTP client at all -- the websocket half of
    the same question is
    :func:`test_the_websocket_connection_is_only_ever_read_written_and_closed`.
    """
    client_calls = set()
    for path in _sources(*VENDOR_PACKAGES):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_client"
            ):
                client_calls.add(node.func.attr)
    assert client_calls == {"get", "aclose"}, client_calls


@pytest.mark.risk
def test_the_vendor_sdk_is_imported_nowhere() -> None:
    """CLAUDE.md caps ``alpaca`` at two files; Phase 2 imports it in none.

    ``alpaca-py`` annotates its market-data models ``bid_price: float``,
    ``open: float`` and so on, so the SDK converts every price to an IEEE
    double on ingest -- at the *entry* boundary, where the loss is
    unrecoverable. The two vendor files use ``httpx`` plus ``corollary.wire``
    instead, and so does the recorder. Importing
    ``corollary.data.providers.alpaca`` is a different thing and is allowed
    everywhere: that module is ours.
    """
    offenders = {
        _where(path)
        for path in _sources(PACKAGE, *RECORDERS)
        if "alpaca" in _imported_roots(_tree(path))
    }
    assert offenders == set(), f"the vendor SDK is imported in {sorted(offenders)}"


# --------------------------------------------------------------------------
# Rule 3 -- MCP servers are never in the order path or the data pipeline
#
# *"MCP is a protocol for language models to call tools. It belongs in two
# places: the Research chat tab, and your own tooling while developing. The
# engine talks to Alpaca over REST and WebSocket. Do not route orders, quotes,
# or scanner inputs through an MCP server -- it is slow, nondeterministic, and
# unauditable after the fact."*
#
# Nondeterministic and unauditable are the words that make this a money rule
# rather than an architecture preference. A quote that arrived through a
# language model's tool call cannot be reproduced from the audit log, so a
# position sized against it cannot be explained afterwards -- and rule 8 says
# a rejection records its inputs. Inputs that came from an MCP server are not
# inputs anyone can re-derive. The scanner's determinism requirement fails the
# same way, one layer up.
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_no_module_imports_or_invokes_an_mcp_client() -> None:
    """Rule 3, structurally: the engine's data has one provenance.

    MCP appears in prose in three places -- two docstrings and a
    ``BrokerAuthError`` message, all of them explaining that the Alpaca MCP
    server holds non-paper keys and answers 401 on every trading endpoint.
    Those are the record of an afternoon lost to it and they stay. What may
    not appear is an import or a call.
    """
    offenders = {
        _where(path): hits
        for path in _sources(PACKAGE)
        if (hits := _mcp_offenders(_tree(path)))
    }
    assert offenders == {}, f"MCP is reachable from the engine: {offenders}"


# The two guards below prove the one above reads *code* and not *text*, in
# both directions -- a mention passes, an import fails. They loop over their
# cases rather than being parametrised over them for the same reason the
# write-verb guard is one test and not six: the gate has a hard ceiling, each
# case names itself in the failure message, and eleven more node ids buy
# nothing the message does not already say.

#: Every way MCP is legitimately discussed in this codebase today, plus the
#: near-miss that a sloppier pattern would catch.
PROSE_THAT_IS_NOT_A_DEPENDENCY = {
    "docstring": '"""Unprobed: the Alpaca MCP server holds non-paper keys."""',
    "error-message": (
        'raise BrokerAuthError("the Alpaca MCP server holds non-paper keys")'
    ),
    "comment": "# never route quotes through an MCP server\nx = 1",
    "letters-inside-another-word": (
        "def compute_dmcpx(value: int) -> int:\n    return value"
    ),
}

#: Every shape an MCP client actually arrives in.
WAYS_TO_REACH_AN_MCP_CLIENT = {
    "import": "import mcp",
    "import-fastmcp": "import fastmcp",
    "import-as": "import mcp_client as tools",
    "from-import": "from mcp.client.session import ClientSession",
    "imported-name": "from corollary.tools import mcp_session",
    "from-import-renamed": "from corollary.tools import client as mcp_sess",
    "call-on-a-name": 'mcp_client.call_tool("quotes", {})',
    "call-on-an-attribute": "session.mcp_invoke(payload)",
    # The four below ran the letters into the next word and so matched
    # nothing at all until ``MCP_TOKEN`` grew its ``A-Z`` suffix. The first
    # is the one that had a constructible failure: an import and a call, in
    # the data pipeline, with no string in either.
    "camelcase-import": "from vendor_tools import MCPClient",
    "camelcase-call": 'MCPClient().call_tool("get_quote", {"symbol": symbol})',
    "titlecase-name": "session = McpSession(endpoint)",
    "lowercase-run-on": 'mcpclient.call_tool("quotes", {})',
}


@pytest.mark.risk
def test_the_mcp_guard_ignores_prose() -> None:
    """A mention is not a dependency.

    The real mentions in this codebase are the reason the guard reads
    identifiers instead of text. If this fails, the guard has begun failing on
    correct documentation, and the next person to hit it will weaken the guard
    rather than the docs.
    """
    caught = {
        name: hits
        for name, source in PROSE_THAT_IS_NOT_A_DEPENDENCY.items()
        if (hits := _mcp_offenders(ast.parse(source)))
    }
    assert caught == {}, f"the guard fired on prose: {caught}"


@pytest.mark.risk
def test_the_mcp_guard_catches_an_import_or_a_call() -> None:
    """And the other direction: the guard has to actually catch something.

    A guard that no longer fires reports safety it is not checking, which is
    worse than no guard at all.
    """
    missed = sorted(
        name
        for name, source in WAYS_TO_REACH_AN_MCP_CLIENT.items()
        if not _mcp_offenders(ast.parse(source))
    )
    assert missed == [], f"an MCP client would go unnoticed: {missed}"


#: An import binds a name without that name ever appearing as an ``ast.Name``,
#: and either half of the alias can carry the forbidden one. Pinned as cases so
#: that narrowing ``_referenced_names`` fails here in a second, rather than only
#: under an injected violation nobody thinks to run. Each case carries the name
#: it must be caught by, because two different names are forbidden.
IMPORTS_THAT_ARE_USES = {
    "bare-from-import": (
        "from corollary.engine.risk import submit_order",
        "submit_order",
    ),
    "aliased-from-import": (
        "from corollary.engine.risk import submit_order as _submit\n"
        "def place(order):\n    return _submit(order)",
        "submit_order",
    ),
    "aliased-module": (
        "import corollary.engine.risk as submit_order",
        "submit_order",
    ),
    "aliased-type-in-an-annotation": (
        "from corollary.engine.execution.interface import BrokerExecution as Broker\n"
        "def dep() -> Broker: ...",
        "BrokerExecution",
    ),
}


@pytest.mark.risk
def test_an_aliased_import_counts_as_a_use() -> None:
    """Rule 1's Phase 6 tripwire, pinned while it still costs nothing.

    Each of these passed the whole gate for one revision. They are harmless
    *today* only because the name they import has to be defined somewhere
    under ``corollary/`` for the import to resolve, and the definition site is
    caught. The day rule 1's guard is relaxed to "``submit_order`` appears
    only inside ``RiskManager``" -- which is what Phase 6 makes it -- the
    second case is a live bypass with a green gate.
    """
    missed = sorted(
        name
        for name, (source, forbidden) in IMPORTS_THAT_ARE_USES.items()
        if forbidden not in _referenced_names(ast.parse(source))
    )
    assert missed == [], f"an aliased import would go unnoticed: {missed}"


#: A quoted annotation is a use; a string that is merely a string is not.
#: Both halves are load-bearing, which is why they are pinned together.
ANNOTATIONS_THAT_ARE_USES = {
    "return": 'def dep() -> "BrokerExecution": ...',
    "argument": "def dep(broker: 'BrokerExecution') -> None: ...",
    "variable": "broker: 'BrokerExecution'",
    "inside-a-subscript": 'def dep() -> list["BrokerExecution"]: ...',
    "inside-a-union": "def dep(b: 'BrokerExecution | None') -> None: ...",
}

STRINGS_THAT_ARE_NOT_USES = {
    "docstring": '"""BrokerExecution does not exist until Phase 6."""',
    "message": 'raise TypeError("BrokerExecution is absent by design")',
    "value": 'PHASE_SIX = "BrokerExecution"',
}


@pytest.mark.risk
def test_a_quoted_annotation_counts_as_a_use_and_prose_does_not() -> None:
    """Found by injection, not by reading, and pinned so it cannot come back.

    ``def dep() -> "BrokerExecution"`` passed every rule-1 guard in this
    tree -- the four that lived in subject-named files as well as the
    consolidated ones -- because a quoted forward reference parses to an
    ``ast.Constant`` and never to an ``ast.Name``. It is also the only way to
    annotate against a type that does not exist yet, so it is the *likeliest*
    spelling of the violation, not an exotic one.

    The second half matters just as much: reading every string would make the
    guard fail on the docstrings that explain why the type is absent, and a
    guard that fails on correct documentation gets weakened until it catches
    nothing.

    Both halves are collected before either is asserted. Asserting ``missed``
    first short-circuits: a regression in the first direction would stop the
    second from running at all, so the half that is still correct goes
    unreported for exactly as long as the other one is broken -- and "the
    guard fired on prose" is the failure that gets a guard deleted.
    """
    missed = sorted(
        name
        for name, source in ANNOTATIONS_THAT_ARE_USES.items()
        if "BrokerExecution" not in _referenced_names(ast.parse(source))
    )
    caught = sorted(
        name
        for name, source in STRINGS_THAT_ARE_NOT_USES.items()
        if "BrokerExecution" in _referenced_names(ast.parse(source))
    )
    assert (missed, caught) == ([], []), (
        f"a quoted annotation would go unnoticed: {missed}; "
        f"the guard fired on prose: {caught}"
    )
