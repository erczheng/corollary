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
from pathlib import Path, PurePath, PurePosixPath

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

#: The hand-run scripts under ``scripts/``. Same standing as the recorders,
#: and in scope for the same reason: ``scripts/probe_phase3.py`` loads keyed
#: credentials for Alpaca, Finnhub, FRED, Massive and StockTwits and calls
#: every one of them live. It sat outside every guard in this file -- the
#: exact miss :data:`RECORDER_GLOB`'s note records for ``record_finnhub.py``
#: -- until the Phase 3 step 0 audit named it, and the spec has step 5 edit it
#: again to re-measure while holding those keys.
#:
#: Globbed, not listed, so the next script is covered on the day it is
#: written; non-recursive, for the reason given for the recorders. The floor
#: :data:`KNOWN_SCRIPTS` is checked by :func:`scripts` at every point of use,
#: because a glob over a renamed directory returns nothing and ``_sources``
#: would see nothing wrong with that.
SCRIPTS = REPO_ROOT / "scripts"
SCRIPT_GLOB = "*.py"
SCRIPT_FILES = tuple(sorted(SCRIPTS.glob(SCRIPT_GLOB)))

#: The scripts that exist today. The glob must find at least these.
KNOWN_SCRIPTS = frozenset({"probe_phase3.py"})

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
#: ``auth`` and ``listen`` carry no instruction, and ``subscribe`` /
#: ``unsubscribe`` are how a *read* is scoped -- so none of the four changes
#: anything at the vendor. ``unsubscribe`` was added when the Markets
#: viewport hint became a mid-session re-plan: converging on a new plan by
#: sending the difference costs one gap in the marks, where re-sending the
#: whole list would cost one on every position underlying the socket carries.
#: It narrows a read and can place nothing.
#:
#: **One set governs all three sockets, the trading socket included.** The
#: guard is structural, which is what :data:`WRITE_VERBS` cannot be for a
#: socket -- ``self._codec.transmit(...)`` is a name no HTTP verb list knows,
#: and an ``{"action": "cancel"}`` frame on the trading socket would be an
#: order placed outside ``RiskManager.approve()`` under a green gate. It is
#: **not** file-scoped, and this said it was for as long as it took one
#: widening to reach it:
#: :func:`test_no_vendor_socket_frame_carries_an_action_outside_the_allowlist`
#: walks :func:`vendor_surface` -- ``engine/execution`` and
#: ``data/providers`` entire, ``sockets.py``, every fixture recorder and
#: every ``scripts/*.py`` --
#: so an action added here for a *quote* stream is one the **order** socket
#: may also say. ``unsubscribe`` is safe under that reading because it
#: narrows a read wherever it is sent, and because Alpaca's trading stream
#: speaks ``listen``/``unlisten`` and would not answer it. The next addition
#: gets the same question asked of the trading socket, in writing, before it
#: goes in -- which is exactly what a reader quoting *"file-scoped"* would
#: have skipped.
SOCKET_ACTIONS = frozenset({"auth", "subscribe", "unsubscribe", "listen"})

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


def scripts() -> tuple[Path, ...]:
    """Every ``scripts/*.py``, with the glob's floor checked *here*.

    The same shape as :func:`recorders` and for the same reason: the check
    has to run wherever the scope is used, so no single test is load-bearing
    for the guards that walk it.
    """
    found = {path.name for path in SCRIPT_FILES}
    missing = sorted(KNOWN_SCRIPTS - found)
    if missing:
        raise AssertionError(
            f"the script glob {SCRIPT_GLOB!r} under {_where(SCRIPTS)} found "
            f"{sorted(found)}, which is missing {missing}. A script that loads "
            ".env and calls a live vendor is in no guard's scope until it is "
            "in this one."
        )
    return SCRIPT_FILES


def vendor_surface() -> tuple[Path, ...]:
    """Everything in the tree that may speak to a data vendor at all.

    The package's two vendor directories plus every recorder and every
    ``scripts/*.py``. Not a constant, so neither globbed half can silently be
    empty: see :func:`recorders` and :func:`scripts`.
    """
    return VENDOR_PACKAGES + recorders() + scripts()

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
#: asserts the market-data client's whole transmitted list is ``auth``,
#: ``subscribe`` and ``unsubscribe`` frames and nothing else -- the last of
#: those is how a mid-session re-plan converges, and it narrows a read -- and
#: ``tests/engine/execution/test_trade_update_stream.py`` does the same for
#: ``auth`` and ``listen``. An order placed over a socket would fail both.
#:
#: ``stream`` is here for the same reason as ``request``: ``httpx``'s
#: ``client.stream("POST", url)`` takes the method as an argument. And the
#: guard flags a *reference* to any of these names, not only a direct call --
#: ``f = client.post; f(...)``, ``functools.partial(client.post, ...)`` -- so
#: a data field cannot share a name with one: ``SubscriptionPlan``'s socket
#: field is ``stream_kind`` and the recorders read ``response.url``, not
#: ``response.request.url``, for exactly that reason.
WRITE_VERBS = frozenset(
    {"post", "put", "patch", "delete", "request", "send", "stream"}
)

#: Calls that look an attribute up by a name given as data. A non-literal name
#: is refused outright (the guard cannot know it is not a write verb); a
#: literal that *is* a write verb is refused as the reference it spells.
ATTRIBUTE_LOOKUPS = frozenset({"getattr", "attrgetter", "methodcaller"})
DUNDER_LOOKUPS = frozenset({"__getattribute__", "__getattr__"})

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
def test_every_script_is_inside_the_vendor_surface() -> None:
    """The floor under :data:`SCRIPT_FILES`' glob, stated with its reason.

    ``scripts/probe_phase3.py`` holds five vendors' live keys and was in no
    guard's scope when it was written. :func:`scripts` raises on a missing
    known script at every point of use, so this test is the named copy of
    that rule rather than the only thing enforcing it.
    """
    found = {path.name for path in scripts()}
    assert found >= KNOWN_SCRIPTS, (
        f"the script glob found {sorted(found)}, which is missing "
        f"{sorted(KNOWN_SCRIPTS - found)}."
    )
    assert set(scripts()) <= set(vendor_surface())


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
    nothing while its own docstring said otherwise. So is every
    ``scripts/*.py`` (:func:`scripts`), for the same reason a third time.

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
    :func:`test_no_vendor_socket_frame_carries_an_action_outside_the_allowlist`.

    **A second exemption, for an HTTP write that never reaches a broker (Q21,
    owner decision 2026-09-30):** one ``self._client.post(...)`` call site in
    ``corollary/data/providers/openfigi.py``, to exactly
    :data:`OPENFIGI_EXEMPT_URL`. See :data:`OPENFIGI_EXEMPT_PATH` for the
    reasoning and :func:`_openfigi_mapping_posts` for the mechanism. The
    purpose stated above survives it unchanged: nothing reaches *Alpaca* with
    a verb that changes anything.

    The check itself is :func:`_write_verb_offenders`, a pure function over a
    path and its source, so the tests below can prove the exemption's edges
    on synthetic source without any real file having to break first.
    """
    offenders = []
    for path in _sources(*vendor_surface()):
        offenders.extend(
            _write_verb_offenders(_rel(path), path.read_text(encoding="utf-8"))
        )
    assert offenders == [], f"a write verb reaches the vendor: {sorted(offenders)}"


def _rel(path: Path) -> str:
    """``path`` relative to the repo, POSIX-spelled -- the key the exemption matches."""
    return path.relative_to(REPO_ROOT).as_posix()


def _lookup_name(node: ast.Call) -> tuple[str, ast.expr | None] | None:
    """``(lookup, name-argument)`` if ``node`` looks an attribute up by name.

    ``getattr(obj, name)``, ``operator.attrgetter(name)``,
    ``operator.methodcaller(name, ...)``, ``obj.__getattribute__(name)``. The
    name argument is ``None`` when the call is missing it or splats it --
    which is as unknowable as a variable, and is treated as one.
    """
    func = node.func
    called = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else None
    )
    if called is None:
        return None
    if called == "getattr":
        index = 1
    elif called in ATTRIBUTE_LOOKUPS or called in DUNDER_LOOKUPS:
        index = 0
    else:
        return None
    args = node.args
    if len(args) <= index or any(isinstance(a, ast.Starred) for a in args[: index + 1]):
        return called, None
    return called, args[index]


def _write_verb_offenders(rel: str, source: str) -> list[str]:
    """Every write-verb call or reference in ``source`` that no exemption covers.

    Three shapes are refused:

    * a call ``x.<verb>(...)`` -- the original check;
    * an *uncalled* reference ``x.<verb>`` in any context -- because
      ``f = client.post; f(...)`` and ``functools.partial(client.post, ...)``
      are the same write with one more line in between;
    * a by-name lookup (:func:`_lookup_name`) whose name is not a string
      literal, or is a literal write verb -- ``getattr(client, verb)``.

    Both exemptions cover a *direct call* and nothing else: the websocket
    frame write ``self._connection.send(...)`` in ``sockets.py``, and the one
    OpenFIGI call node. An uncalled ``self._connection.send`` is refused
    like any other reference.

    ``rel`` is the repo-relative POSIX path of the file the source came from;
    both exemptions are keyed on it. Pure, so the real tree and the synthetic
    sources in the exemption tests go through exactly the same code.
    """
    tree = ast.parse(source, filename=rel)
    exempt = _openfigi_mapping_posts(rel, tree)
    called_funcs = {
        id(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    lookups = ATTRIBUTE_LOOKUPS | DUNDER_LOOKUPS
    offenders = []
    for node in ast.walk(tree):
        # A lookup function itself passed around uncalled -- ``g = getattr`` --
        # would carry a by-name lookup past the check below.
        spelled = (
            node.id
            if isinstance(node, ast.Name)
            else node.attr
            if isinstance(node, ast.Attribute)
            else None
        )
        if spelled in lookups and id(node) not in called_funcs:
            offenders.append(f"{rel}:{node.lineno} {spelled} (uncalled reference)")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in WRITE_VERBS
            and not _is_websocket_frame_write(PurePosixPath(rel), node.func)
            and id(node) not in exempt
        ):
            offenders.append(f"{rel}:{node.lineno} .{node.func.attr}()")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in WRITE_VERBS
            and id(node) not in called_funcs
        ):
            offenders.append(f"{rel}:{node.lineno} .{node.attr} (uncalled reference)")
        if isinstance(node, ast.Call):
            lookup = _lookup_name(node)
            if lookup is None:
                continue
            called, name = lookup
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                offenders.append(f"{rel}:{node.lineno} {called}(<non-literal name>)")
            elif name.value in WRITE_VERBS:
                offenders.append(f"{rel}:{node.lineno} {called}({name.value!r})")
    return offenders


# --------------------------------------------------------------------------
# The one HTTP write exemption: OpenFIGI's mapping endpoint (spec Q21)
# --------------------------------------------------------------------------

#: **Owner decision 2026-09-30, option A** (spec Q21, resolving the open part
#: of Q14/Q17/Q20). The guard's stated purpose is *"nothing reaches Alpaca
#: with a verb that changes anything"*. OpenFIGI is not a broker host: its
#: mapping endpoint turns ISINs into FIGIs and tickers and is POST-only. An
#: exemption scoped to that one endpoint keeps the purpose while changing the
#: guard's wording from "no write verb on the vendor surface" to "no write
#: verb on the vendor surface except this one, which cannot reach a broker".
#:
#: Exactly one file, exactly one URL, at most one call site -- no wildcard,
#: no host-only match. Any other non-GET anywhere still fails, including a
#: second POST in this same file, a POST to any other OpenFIGI path, and a
#: POST to the mapping URL from any other file. The exempted file is held
#: structurally unable to reach the broker by
#: :func:`test_the_openfigi_provider_cannot_reach_the_broker`.
OPENFIGI_EXEMPT_PATH = "corollary/data/providers/openfigi.py"

#: The one URL, compared for exact string equality against the **inline
#: literal** at the call site. Not a name, not a module constant: a module
#: global can be rebound at runtime in ways the AST cannot count, so the only
#: URL the exemption believes is the one spelled in the call itself.
OPENFIGI_EXEMPT_URL = "https://api.openfigi.com/v3/mapping"

#: The one verb, called on the one receiver (``self._client``).
OPENFIGI_EXEMPT_VERB = "post"

#: The exempt call's keywords, exactly: no more, no fewer. ``json`` is the
#: body, ``headers`` carries the optional key, and ``follow_redirects`` must
#: be spelled, as the literal ``False``, so a 3xx can never carry the key-
#: bearing request to another host. Anything else -- ``auth``, ``cookies``,
#: ``extensions``, a ``**`` splat -- is a way the call could change shape.
OPENFIGI_EXEMPT_KEYWORDS = frozenset({"json", "headers", "follow_redirects"})

#: How many call sites may use the exemption. A second one -- even to the same
#: URL -- voids it for every site in the file, so both are reported.
OPENFIGI_EXEMPT_MAX_CALL_SITES = 1

#: Builtins that can rebind a name, or reach code, without a node the AST can
#: count. Any reference to one in the exempted file voids the exemption: the
#: call site's static shape -- receiver, verb, literal URL -- would no longer
#: be the whole story.
DYNAMIC_REBINDERS = frozenset({"globals", "vars", "setattr", "exec", "eval", "locals"})


def _is_mapping_post_shape(node: ast.Call) -> bool:
    """``self._client.post("<the mapping URL>", json=..., headers=..., follow_redirects=False)``.

    One positional argument, which must be the URL as an inline ``str``
    literal; keywords exactly :data:`OPENFIGI_EXEMPT_KEYWORDS`, with no splat,
    and ``follow_redirects`` the literal ``False`` (by identity, so ``0`` is
    not ``False`` here).
    """
    func = node.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr == OPENFIGI_EXEMPT_VERB
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "_client"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "self"
    ):
        return False
    if len(node.args) != 1:
        return False
    url = node.args[0]
    if not (
        isinstance(url, ast.Constant)
        and isinstance(url.value, str)
        and url.value == OPENFIGI_EXEMPT_URL
    ):
        return False
    names = [kw.arg for kw in node.keywords]
    if None in names or len(names) != len(set(names)):
        return False
    if set(names) != OPENFIGI_EXEMPT_KEYWORDS:
        return False
    redirects = next(kw.value for kw in node.keywords if kw.arg == "follow_redirects")
    return isinstance(redirects, ast.Constant) and redirects.value is False


def _openfigi_mapping_posts(rel: str, tree: ast.Module) -> set[int]:
    """The ``id()`` of each exempt call node in ``tree`` -- empty unless all conditions hold.

    The path must be exactly :data:`OPENFIGI_EXEMPT_PATH`; the file must
    reference none of :data:`DYNAMIC_REBINDERS`; and no more than
    :data:`OPENFIGI_EXEMPT_MAX_CALL_SITES` calls may match the shape. Past the
    cap the exemption is void for *every* site, so all of them are reported.
    """
    if rel != OPENFIGI_EXEMPT_PATH:
        return set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and node.id in DYNAMIC_REBINDERS) or (
            isinstance(node, ast.Attribute) and node.attr in DYNAMIC_REBINDERS
        ):
            return set()
    sites = {
        id(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_mapping_post_shape(node)
    }
    if len(sites) > OPENFIGI_EXEMPT_MAX_CALL_SITES:
        return set()
    return sites


def _is_websocket_frame_write(path: PurePath, func: ast.Attribute) -> bool:
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
def test_no_vendor_socket_frame_carries_an_action_outside_the_allowlist() -> None:
    """What the three sockets may *say*, across the whole vendor surface.

    **Three sockets, one allowlist.** The scope is :func:`vendor_surface` --
    ``engine/execution`` and ``data/providers`` entire, ``sockets.py``, every
    fixture recorder and every ``scripts/*.py`` -- so this is not the quote streams' guard with
    the trading socket alongside: it is one set governing all three, and an
    action added to :data:`SOCKET_ACTIONS` for one of them is an action the
    **order** socket may also carry. The name this test used to have listed
    the allowlist's members, which went stale the first time one was added
    and read as though the enumeration were the rule.

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

    **The OpenFIGI mapping POST (Q21) is subtracted by the same predicate**
    the negative form uses, :func:`_openfigi_mapping_posts` -- so one exempt
    ``self._client.post`` in ``openfigi.py`` leaves this set unchanged, and a
    second post anywhere, or one that misses the exemption's shape, adds
    ``"post"`` and fails.
    """
    client_calls = set()
    for path in _sources(*VENDOR_PACKAGES):
        tree = _tree(path)
        exempt = _openfigi_mapping_posts(_rel(path), tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_client"
                and id(node) not in exempt
            ):
                client_calls.add(node.func.attr)
    assert client_calls == {"get", "aclose"}, client_calls


# --------------------------------------------------------------------------
# The OpenFIGI exemption's edges (Q21), proven on synthetic source
#
# Every source below goes through _write_verb_offenders, the same function
# the real-tree guard calls, so an edge proven here is an edge the gate holds.
# --------------------------------------------------------------------------

#: The exemption's one admitted shape: the exact URL as an inline literal,
#: and exactly the three pinned keywords.
_URL = '"https://api.openfigi.com/v3/mapping"'
_KW = "json=jobs, headers=self._headers(), follow_redirects=False"
ADMITTED = (
    "class P:\n"
    "    async def map(self, jobs):\n"
    f"        return await self._client.post({_URL}, {_KW})\n"
)


def _post_with(args: str) -> str:
    """The admitted class with the post call's argument list replaced."""
    return (
        "class P:\n"
        "    async def map(self, jobs, **kw):\n"
        f"        return await self._client.post({args})\n"
    )


@pytest.mark.risk
@pytest.mark.parametrize(
    "source",
    [
        ADMITTED,
        # Keyword order is not part of the shape.
        _post_with(f"{_URL}, follow_redirects=False, headers=self._headers(), json=jobs"),
    ],
    ids=["literal", "keywords_reordered"],
)
def test_the_openfigi_exemption_admits_its_one_call_site(source: str) -> None:
    assert _write_verb_offenders(OPENFIGI_EXEMPT_PATH, source) == []


#: The URL passed by a module constant -- even an exact, ``Final``, bound-once
#: one. A module global can be rebound at runtime in ways the AST cannot
#: count, so the exemption believes only the literal at the call site.
_MAPPING_CONSTANT = (
    "from typing import Final\n"
    'OPENFIGI_MAPPING_URL: Final = "https://api.openfigi.com/v3/mapping"\n'
)

#: Sources at the exempted path that must still trip the guard. Each is the
#: admitted file with one thing changed, or with one thing added.
TRIPS_IN_THE_EXEMPTED_FILE = {
    # (a) a POST to the broker, beside the admitted call
    "alpaca_order_beside_it": ADMITTED
    + "    async def order(self, body):\n"
    '        return await self._client.post("https://paper-api.alpaca.markets/v2/orders", json=body)\n',
    "alpaca_order_alone": _post_with(
        f'"https://paper-api.alpaca.markets/v2/orders", {_KW}'
    ),
    # (b) another path on api.openfigi.com, or the mapping URL spelled otherwise
    "openfigi_search": _post_with(f'"https://api.openfigi.com/v3/search", {_KW}'),
    "trailing_slash": _post_with(f'"https://api.openfigi.com/v3/mapping/", {_KW}'),
    "http_scheme": _post_with(f'"http://api.openfigi.com/v3/mapping", {_KW}'),
    "f_string": 'BASE = "https://api.openfigi.com"\n'
    + _post_with(f'f"{{BASE}}/v3/mapping", {_KW}'),
    "concatenation": 'BASE = "https://api.openfigi.com"\n'
    + _post_with(f'BASE + "/v3/mapping", {_KW}'),
    "attribute_url": _post_with(f"self.url, {_KW}"),
    # (finding 2) the URL by name: never resolved, however it is bound
    "constant": _MAPPING_CONSTANT + _post_with(f"OPENFIGI_MAPPING_URL, {_KW}"),
    "constant_rebound_via_globals": _MAPPING_CONSTANT
    + _post_with(f"OPENFIGI_MAPPING_URL, {_KW}")
    + 'globals()["OPENFIGI_MAPPING_URL"] = "https://paper-api.alpaca.markets/v2/orders"\n',
    # a dynamic rebinder anywhere in the file voids the exemption
    "setattr_anywhere": ADMITTED + 'setattr(P, "x", 1)\n',
    "builtins_setattr_anywhere": "import builtins\n"
    + ADMITTED
    + 'builtins.setattr(P, "x", 1)\n',
    # a second call site -- even to the same URL
    "two_call_sites_same_url": ADMITTED
    + "    async def again(self, jobs):\n"
    f"        return await self._client.post({_URL}, {_KW})\n",
    # (finding 3) the keyword set is exact, and follow_redirects is literally False
    "no_keywords": _post_with(_URL),
    "missing_follow_redirects": _post_with(f"{_URL}, json=jobs, headers=self._headers()"),
    "missing_headers": _post_with(f"{_URL}, json=jobs, follow_redirects=False"),
    "missing_json": _post_with(f"{_URL}, headers=self._headers(), follow_redirects=False"),
    "follow_redirects_true": _post_with(
        f"{_URL}, json=jobs, headers=self._headers(), follow_redirects=True"
    ),
    "follow_redirects_zero": _post_with(
        f"{_URL}, json=jobs, headers=self._headers(), follow_redirects=0"
    ),
    "follow_redirects_variable": _post_with(
        f"{_URL}, json=jobs, headers=self._headers(), follow_redirects=self.redirects"
    ),
    "extra_auth": _post_with(f"{_URL}, {_KW}, auth=self.auth"),
    "extra_extensions": _post_with(f"{_URL}, {_KW}, extensions={{}}"),
    "extra_content": _post_with(f"{_URL}, {_KW}, content=b''"),
    "kwargs_splat": _post_with(f"{_URL}, {_KW}, **kw"),
    "kwargs_splat_alone": _post_with(f"{_URL}, **kw"),
    "url_keyword": _post_with(f"url={_URL}, {_KW}"),
    "two_positionals": _post_with(f"{_URL}, jobs, {_KW}"),
    "starred_url": _post_with(f"*[{_URL}], {_KW}"),
    # other write verbs, whatever their URL
    "request_post": "class P:\n"
    "    async def map(self, jobs):\n"
    f'        return await self._client.request("POST", {_URL}, json=jobs)\n',
    "send": "class P:\n"
    "    async def map(self, request):\n"
    "        return await self._client.send(request)\n",
    "put_to_mapping": "class P:\n"
    "    async def map(self, jobs):\n"
    f"        return await self._client.put({_URL}, {_KW})\n",
    # (finding 4) httpx's streaming request takes the method as an argument
    "stream_post": "class P:\n"
    "    async def map(self, jobs):\n"
    f'        async with self._client.stream("POST", {_URL}, json=jobs) as r:\n'
    "            return r\n",
    # the shape bent: another receiver
    "module_level_httpx_post": "import httpx\n"
    "def map(jobs):\n"
    f"    return httpx.post({_URL}, json=jobs, follow_redirects=False)\n",
    "other_receiver": "class P:\n"
    "    async def map(self, jobs):\n"
    f"        return await self._broker.post({_URL}, {_KW})\n",
    # (finding 5) the write verb referenced rather than called, or looked up by name
    "bound_method_alias": ADMITTED
    + "    async def again(self, jobs):\n"
    "        f = self._client.post\n"
    f"        return await f({_URL}, {_KW})\n",
    "functools_partial": "import functools\n"
    + ADMITTED
    + "    def later(self):\n"
    "        return functools.partial(self._client.post, 'x')\n",
    "getattr_non_literal": ADMITTED
    + "    async def again(self, verb):\n"
    f"        return await getattr(self._client, verb)({_URL})\n",
    "getattr_literal_write_verb": ADMITTED
    + "    async def again(self):\n"
    f'        return await getattr(self._client, "post")({_URL})\n',
    "getattr_aliased": ADMITTED + "g = getattr\n",
}


@pytest.mark.risk
@pytest.mark.parametrize(
    "source",
    list(TRIPS_IN_THE_EXEMPTED_FILE.values()),
    ids=list(TRIPS_IN_THE_EXEMPTED_FILE),
)
def test_the_openfigi_exemption_does_not_cover_anything_else_in_its_file(
    source: str,
) -> None:
    assert _write_verb_offenders(OPENFIGI_EXEMPT_PATH, source) != []


@pytest.mark.risk
def test_a_second_call_site_voids_the_exemption_for_both() -> None:
    """Past the cap, every site is reported -- not just the later one."""
    offenders = _write_verb_offenders(
        OPENFIGI_EXEMPT_PATH, TRIPS_IN_THE_EXEMPTED_FILE["two_call_sites_same_url"]
    )
    assert len(offenders) == 2, offenders


#: Write verbs reached without a direct call, anywhere on the surface (finding
#: 5). Checked at non-OpenFIGI paths, so that exemption is not in play.
WRITE_VERBS_REACHED_INDIRECTLY = {
    "bound_method_alias": "def f(client):\n    p = client.post\n    return p('u')\n",
    "partial": "import functools\n"
    "def f(client):\n    return functools.partial(client.post, 'u')\n",
    "passed_as_callback": "def f(client, run):\n    return run(client.delete, 'u')\n",
    "stream_reference": "def f(client):\n    s = client.stream\n    return s('POST', 'u')\n",
    "getattr_variable": "def f(client, verb):\n    return getattr(client, verb)('u')\n",
    "getattr_literal_post": "def f(client):\n    return getattr(client, 'post')('u')\n",
    "getattr_literal_stream": "def f(client):\n"
    "    return getattr(client, 'stream')('POST', 'u')\n",
    "getattr_with_default": "def f(client, verb):\n    return getattr(client, verb, None)\n",
    "getattr_splat": "def f(client, a):\n    return getattr(*a)\n",
    "attrgetter_variable": "from operator import attrgetter\n"
    "def f(client, verb):\n    return attrgetter(verb)(client)('u')\n",
    "attrgetter_literal_put": "import operator\n"
    "def f(client):\n    return operator.attrgetter('put')(client)('u')\n",
    "methodcaller_literal_patch": "import operator\n"
    "def f(client):\n    return operator.methodcaller('patch', 'u')(client)\n",
    "dunder_getattribute": "def f(client, verb):\n"
    "    return client.__getattribute__(verb)('u')\n",
    "getattr_aliased": "g = getattr\ndef f(client):\n    return g(client, 'post')('u')\n",
    "websocket_send_uncalled": "class S:\n"
    "    def f(self):\n        return self._connection.send\n",
}


@pytest.mark.risk
@pytest.mark.parametrize(
    "source",
    list(WRITE_VERBS_REACHED_INDIRECTLY.values()),
    ids=list(WRITE_VERBS_REACHED_INDIRECTLY),
)
def test_a_write_verb_reached_without_a_direct_call_trips_the_guard(source: str) -> None:
    """``f = client.post; f(...)`` is the same write; so is ``getattr(client, verb)``.

    Also checked at ``corollary/sockets.py`` itself: that file's exemption
    covers the direct frame write ``self._connection.send(...)`` and nothing
    else, so an uncalled reference there is refused like any other.
    """
    assert _write_verb_offenders("corollary/sockets.py", source) != []
    assert _write_verb_offenders("corollary/data/providers/finnhub.py", source) != []


@pytest.mark.risk
@pytest.mark.parametrize(
    "source",
    [
        "def f(r):\n    return getattr(r, 'status_code', None)\n",
        "import operator\n"
        "def f(rows):\n    return sorted(rows, key=operator.attrgetter('symbol'))\n",
        "def f(client):\n    return client.get('u')\n",
        "class S:\n    async def f(self):\n        await self._connection.send('x')\n",
    ],
    ids=["getattr_literal_read", "attrgetter_literal_read", "get_call", "websocket_frame_write"],
)
def test_a_read_or_a_literal_non_write_lookup_does_not_trip(source: str) -> None:
    """The indirect-reference check is not simply refusing every lookup."""
    assert _write_verb_offenders("corollary/sockets.py", source) == []


@pytest.mark.risk
@pytest.mark.parametrize(
    "rel",
    [
        "corollary/data/providers/finnhub.py",
        "corollary/data/providers/alpaca.py",
        "corollary/engine/execution/alpaca.py",
        "tests/fixtures/record_openfigi.py",
        "scripts/probe_phase3.py",
        # near-misses on the one path
        "corollary/data/providers/openfigi_v2.py",
        "corollary/data/openfigi.py",
        "corollary\\data\\providers\\openfigi.py",
        "/corollary/data/providers/openfigi.py",
    ],
)
def test_the_openfigi_exemption_holds_in_exactly_one_file(rel: str) -> None:
    """(c) The admitted source, verbatim, anywhere else trips the guard."""
    assert _write_verb_offenders(rel, ADMITTED) != []


@pytest.mark.risk
def test_the_real_openfigi_provider_uses_the_exemption_exactly_once() -> None:
    """The exemption is live, and used by exactly one call site.

    An exemption nothing uses is a hole waiting for a caller; one used twice
    would already fail the guard. Pinned here so the count is a stated fact
    about the tree rather than an inference from a green gate.
    """
    path = REPO_ROOT / OPENFIGI_EXEMPT_PATH
    assert path.is_file(), f"the exempted file {OPENFIGI_EXEMPT_PATH} does not exist"
    sites = _openfigi_mapping_posts(OPENFIGI_EXEMPT_PATH, _tree(path))
    assert len(sites) == OPENFIGI_EXEMPT_MAX_CALL_SITES == 1


# --------------------------------------------------------------------------
# The exempted file cannot reach the broker (Q21, owner condition 2)
# --------------------------------------------------------------------------

#: Module prefixes the exempted file may not import, directly or transitively:
#: the broker package, the vendor SDK, and the Alpaca market-data provider
#: (which holds the Alpaca credentials).
BROKER_REACH = (
    "corollary.engine.execution",
    "alpaca",
    "corollary.data.providers.alpaca",
)

#: Calls that import a module named by a runtime string.
DYNAMIC_IMPORTERS = frozenset({"import_module", "__import__"})


def _targets_broker(module: str) -> bool:
    return any(module == p or module.startswith(p + ".") for p in BROKER_REACH)


def _module_imports(
    module: str, tree: ast.Module, *, is_package: bool
) -> tuple[set[str], list[str]]:
    """Every module name ``tree`` imports, plus the dynamic imports it cannot name.

    ``from a.b import c`` yields both ``a.b`` and ``a.b.c``, because ``c`` may
    be a submodule. Relative imports resolve against ``module``'s package --
    which for a package's own ``__init__.py`` (``is_package``) is ``module``
    itself, and for a plain module file is its parent. Resolving an
    ``__init__``'s ``from . import alpaca`` against the parent put it one
    level too high (``corollary.data.alpaca``), where it matched nothing the
    reach check refuses. Keyword-only and required, so no caller can forget
    which kind of file it read. A
    call to ``importlib.import_module``/``__import__`` with a literal yields
    that literal; with anything else it is reported, because a module chosen
    at runtime is one this check cannot see.
    """
    names: set[str] = set()
    dynamic: list[str] = []
    package = module.split(".") if is_package else module.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                stem = ".".join(base + ([node.module] if node.module else []))
            else:
                stem = node.module or ""
            names.add(stem)
            names.update(f"{stem}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            called = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if called in DYNAMIC_IMPORTERS:
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value)
                else:
                    dynamic.append(f"{module}:{node.lineno} {called}(<non-literal>)")
    return names, dynamic


def _broker_reach_offenders(module: str, source: str, *, is_package: bool = False) -> list[str]:
    """What, in one file's own text, could reach the broker or its credentials.

    Imports of :data:`BROKER_REACH`; dynamic imports; and any string literal
    or identifier mentioning ``alpaca`` in any case -- which is what makes an
    ``ALPACA_*`` environment read, an Alpaca host, or an attribute walk to
    ``corollary.data.providers.alpaca`` unspellable here.
    """
    tree = ast.parse(source)
    names, offenders = _module_imports(module, tree, is_package=is_package)
    offenders = list(offenders)
    offenders += [f"imports {name}" for name in sorted(names) if _targets_broker(name)]
    for node in ast.walk(tree):
        text: str | None = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.Name):
            text = node.id
        elif isinstance(node, ast.Attribute):
            text = node.attr
        elif isinstance(node, ast.alias):
            text = node.asname
        if text is not None and "alpaca" in text.lower():
            offenders.append(f"line {getattr(node, 'lineno', '?')}: {text[:60]!r}")
    return offenders


def _module_file(module: str) -> Path | None:
    """The source file for a ``corollary.*`` module, or ``None`` if it is not one."""
    base = REPO_ROOT.joinpath(*module.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _transitive_corollary_imports(module: str) -> dict[str, set[str]]:
    """Every module reachable from ``module`` through ``corollary.*`` imports.

    Keyed by importing module, valued by what it imports (including
    third-party names, so the SDK is seen wherever it is imported). Parent
    packages are walked too, because importing ``a.b.c`` executes
    ``a/__init__.py`` and ``a/b/__init__.py``.
    """
    seen: dict[str, set[str]] = {}
    queue = [module]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        path = _module_file(current)
        if path is None:
            continue
        names, _ = _module_imports(
            current, _tree(path), is_package=path.name == "__init__.py"
        )
        seen[current] = names
        parts = current.split(".")
        parents = [".".join(parts[:i]) for i in range(1, len(parts))]
        queue.extend(n for n in names | set(parents) if n.startswith("corollary"))
    return seen


@pytest.mark.risk
def test_the_openfigi_provider_cannot_reach_the_broker() -> None:
    """The exempted file imports nothing that could place, or authenticate, an order.

    Owner condition 2 of Q21: no import from ``corollary/engine/execution``,
    from the ``alpaca`` SDK, or from ``data/providers/alpaca.py``; no
    ``ALPACA_*`` variable read. Checked on the file's own text and over the
    transitive closure of its ``corollary.*`` imports, so a helper module
    that imports the broker cannot carry it in by the back door.

    *What this does not catch:* a forbidden name assembled at runtime from
    fragments. That is a deliberate evasion rather than an accident, and the
    dynamic-import rule refuses the only way such a name could be imported.
    """
    module = OPENFIGI_EXEMPT_PATH.removesuffix(".py").replace("/", ".")
    path = REPO_ROOT / OPENFIGI_EXEMPT_PATH
    direct = _broker_reach_offenders(module, path.read_text(encoding="utf-8"))
    assert direct == [], f"{OPENFIGI_EXEMPT_PATH} can reach the broker: {direct}"

    closure = _transitive_corollary_imports(module)
    assert module in closure, "the closure walk did not start from the file"
    reached = sorted(
        f"{importer} -> {name}"
        for importer, names in closure.items()
        for name in names
        if _targets_broker(name)
    )
    assert reached == [], f"{OPENFIGI_EXEMPT_PATH} reaches the broker transitively: {reached}"


#: Sources the reach check must refuse -- one per way in.
REACHES_THE_BROKER = {
    "import_execution": "import corollary.engine.execution.alpaca\n",
    "from_execution": "from corollary.engine.execution.alpaca import AlpacaBroker\n",
    "from_engine_import_execution": "from corollary.engine import execution\n",
    "sdk": "import alpaca\n",
    "sdk_submodule": "from alpaca.trading.client import TradingClient\n",
    "alpaca_provider": "from corollary.data.providers.alpaca import AlpacaProvider\n",
    "providers_import_alpaca": "from corollary.data.providers import alpaca\n",
    "relative_alpaca": "from . import alpaca\n",
    "relative_alpaca_module": "from .alpaca import AlpacaProvider\n",
    "import_module_literal": "import importlib\n"
    'importlib.import_module("corollary.engine.execution.alpaca")\n',
    "import_module_dynamic": "import importlib\nname = 'x'\nimportlib.import_module(name)\n",
    "dunder_import": '__import__("alpaca")\n',
    "env_read": 'import os\nkey = os.environ["ALPACA_PAPER_API_KEY"]\n',
    "getenv_read": 'import os\nkey = os.getenv("ALPACA_PAPER_SECRET_KEY")\n',
    "alpaca_host": 'URL = "https://paper-api.alpaca.markets/v2/orders"\n',
}


@pytest.mark.risk
@pytest.mark.parametrize(
    "source", list(REACHES_THE_BROKER.values()), ids=list(REACHES_THE_BROKER)
)
def test_the_broker_reach_check_refuses_every_way_in(source: str) -> None:
    module = OPENFIGI_EXEMPT_PATH.removesuffix(".py").replace("/", ".")
    assert _broker_reach_offenders(module, source) != []


@pytest.mark.risk
def test_the_broker_reach_check_passes_a_clean_provider() -> None:
    """The check is not simply refusing everything."""
    module = OPENFIGI_EXEMPT_PATH.removesuffix(".py").replace("/", ".")
    clean = (
        "import httpx\n"
        "from corollary.ratelimit import default_limiter\n"
        "from corollary.data.providers.interface import ProviderError\n"
        'KEY_ENV = "OPENFIGI_API_KEY"\n'
    )
    assert _broker_reach_offenders(module, clean) == []


@pytest.mark.risk
@pytest.mark.parametrize(
    ("module", "is_package", "source", "expected"),
    [
        # A plain module file: relative imports resolve against its parent.
        ("corollary.data.providers.openfigi", False, "from . import interface\n",
         "corollary.data.providers.interface"),
        ("corollary.data.providers.openfigi", False, "from .. import macro\n",
         "corollary.data.macro"),
        # A package __init__: its package is the module itself.
        ("corollary.data.providers", True, "from . import alpaca\n",
         "corollary.data.providers.alpaca"),
        ("corollary.data.providers", True, "from .alpaca import AlpacaProvider\n",
         "corollary.data.providers.alpaca"),
        ("corollary.data.providers", True, "from .. import wire\n",
         "corollary.data.wire"),
        ("corollary.engine", True, "from .execution import alpaca\n",
         "corollary.engine.execution.alpaca"),
    ],
    ids=["module_dot", "module_dotdot", "init_dot", "init_dot_module", "init_dotdot",
         "engine_init"],
)
def test_relative_imports_resolve_against_the_right_package(
    module: str, is_package: bool, source: str, expected: str
) -> None:
    names, dynamic = _module_imports(module, ast.parse(source), is_package=is_package)
    assert expected in names, sorted(names)
    assert dynamic == []


@pytest.mark.risk
@pytest.mark.parametrize(
    "source",
    ["from . import alpaca\n", "from .alpaca import AlpacaProvider\n"],
    ids=["dot_alpaca", "dot_alpaca_module"],
)
def test_a_package_init_reaching_the_alpaca_provider_is_refused(source: str) -> None:
    """Finding 1: resolved against the parent, ``corollary.data.alpaca`` passed."""
    assert _broker_reach_offenders("corollary.data.providers", source, is_package=True) != []


@pytest.mark.risk
def test_the_closure_walk_sees_a_relative_import_in_a_parent_package_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: ``providers/__init__.py`` importing ``.alpaca`` is reached.

    Importing ``corollary.data.providers.openfigi`` executes the package's
    ``__init__.py``, so a broker import there is one the exempted file carries
    in. A synthetic tree under ``tmp_path`` stands in for the real one.
    """
    providers = tmp_path / "corollary" / "data" / "providers"
    providers.mkdir(parents=True)
    (tmp_path / "corollary" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "corollary" / "data" / "__init__.py").write_text("", encoding="utf-8")
    (providers / "__init__.py").write_text(
        "from .alpaca import AlpacaProvider\n", encoding="utf-8"
    )
    (providers / "openfigi.py").write_text("import httpx\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "REPO_ROOT", tmp_path)

    closure = _transitive_corollary_imports("corollary.data.providers.openfigi")
    reached = sorted(
        f"{importer} -> {name}"
        for importer, names in closure.items()
        for name in names
        if _targets_broker(name)
    )
    assert "corollary.data.providers -> corollary.data.providers.alpaca" in reached, reached
    assert all(r.startswith("corollary.data.providers -> ") for r in reached), reached


#: What ``self._client`` in the exempted file may be constructed with.
#: Everything that could carry the key elsewhere or reshape the request --
#: ``base_url``, ``headers``, ``event_hooks``, ``auth``, ``follow_redirects``,
#: ``mounts``, ``cookies`` -- is absent by construction.
OPENFIGI_CLIENT_KEYWORDS = frozenset({"timeout", "transport"})


def _openfigi_client_offenders(source: str) -> list[str]:
    """Why the exempted file's ``_client`` binding is not the one pinned shape.

    Exactly one binding of any ``*._client`` attribute (assignment, annotated
    or augmented assignment, ``del``), whose value is ``httpx.AsyncClient(...)``
    with no positional argument, no splat and keywords within
    :data:`OPENFIGI_CLIENT_KEYWORDS`. And no ``__dict__`` / ``__setattr__``
    reference, which could bind it without an attribute node.
    """
    tree = ast.parse(source)
    offenders: list[str] = []
    bindings: list[ast.Attribute] = []
    values: list[ast.expr | None] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"__dict__", "__setattr__"}:
            offenders.append(f"line {node.lineno}: .{node.attr}")
        if isinstance(node, ast.Attribute) and node.attr == "_client":
            if not isinstance(node.ctx, ast.Load):
                bindings.append(node)
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Attribute) and t.attr == "_client" for t in node.targets):
                values.append(node.value)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Attribute) and node.target.attr == "_client":
                values.append(node.value if isinstance(node, ast.AnnAssign) else None)
    if len(bindings) != 1 or len(values) != 1:
        return offenders + [f"{len(bindings)} bindings of ._client, not exactly 1"]
    value = values[0]
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "AsyncClient"
        and isinstance(value.func.value, ast.Name)
        and value.func.value.id == "httpx"
    ):
        shown = ast.unparse(value) if value is not None else "<augmented>"
        return offenders + [f"._client is bound to {shown!r}, not httpx.AsyncClient(...)"]
    if value.args:
        offenders.append("httpx.AsyncClient(...) takes a positional argument")
    for kw in value.keywords:
        if kw.arg is None or kw.arg not in OPENFIGI_CLIENT_KEYWORDS:
            offenders.append(f"httpx.AsyncClient(...) takes {kw.arg or '**splat'}=")
    return offenders


@pytest.mark.risk
def test_the_openfigi_provider_builds_its_own_bare_client() -> None:
    """Finding 3: no injected client can carry the key, or the request, elsewhere.

    The exempt call pins its own keywords, but an ``httpx.AsyncClient`` handed
    in from outside could still bring a ``base_url``, default headers, event
    hooks, auth or redirect-following with it. So the provider constructs its
    client itself, exactly once, from ``timeout`` and an optional test
    ``transport`` -- and this holds it there.
    """
    path = REPO_ROOT / OPENFIGI_EXEMPT_PATH
    assert _openfigi_client_offenders(path.read_text(encoding="utf-8")) == []


_BARE_CLIENT = (
    "import httpx\n"
    "class P:\n"
    "    def __init__(self, transport=None):\n"
    "        self._client = httpx.AsyncClient(timeout=15.0, transport=transport)\n"
)


@pytest.mark.risk
@pytest.mark.parametrize(
    "source",
    [
        # injected
        "class P:\n    def __init__(self, client):\n        self._client = client\n",
        "import httpx\nclass P:\n    def __init__(self, client=None):\n"
        "        self._client = client or httpx.AsyncClient()\n",
        # built with something that reshapes the request
        _BARE_CLIENT.replace("timeout=15.0", "base_url='https://x'"),
        _BARE_CLIENT.replace("timeout=15.0", "headers={'a': 'b'}"),
        _BARE_CLIENT.replace("timeout=15.0", "event_hooks={}"),
        _BARE_CLIENT.replace("timeout=15.0", "follow_redirects=True"),
        _BARE_CLIENT.replace("timeout=15.0", "auth=('u', 'p')"),
        _BARE_CLIENT.replace("timeout=15.0", "**kw"),
        # bound twice, or rebound some other way
        _BARE_CLIENT + "    def swap(self, c):\n        self._client = c\n",
        _BARE_CLIENT + "    def swap(self):\n        del self._client\n",
        _BARE_CLIENT + "    def swap(self, c):\n        self.__dict__['_client'] = c\n",
        _BARE_CLIENT + "    def swap(self, c):\n        object.__setattr__(self, '_client', c)\n",
        # never bound
        "class P:\n    pass\n",
    ],
    ids=["injected", "injected_or_default", "base_url", "headers", "event_hooks",
         "follow_redirects", "auth", "splat", "rebound", "deleted", "dunder_dict",
         "dunder_setattr", "unbound"],
)
def test_the_client_shape_check_refuses_every_other_construction(source: str) -> None:
    assert _openfigi_client_offenders(source) != []


@pytest.mark.risk
def test_the_client_shape_check_passes_the_bare_client() -> None:
    assert _openfigi_client_offenders(_BARE_CLIENT) == []


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

    The recorders and ``scripts/*.py`` are walked through :func:`recorders`
    and :func:`scripts`, not the bare glob constants, so an emptied glob
    raises here instead of shrinking the scope to the package alone.
    """
    offenders = {
        _where(path)
        for path in _sources(PACKAGE, *recorders(), *scripts())
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
