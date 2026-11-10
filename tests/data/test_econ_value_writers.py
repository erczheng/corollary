"""Only ``corollary/data/econ_store.py`` writes ``econ_release_value`` (U6d audit P3).

The first print is write-once (spec R6, Q23c), and ``store_first_print`` is
where that is enforced: it inserts, or returns the row already there. A second
writer anywhere else -- a route, a job, a backfill script under ``corollary/``
or ``scripts/`` -- could overwrite a first print and no test of the store would
notice. So this scans every module under ``corollary/`` **and** ``scripts/``
(where a backfill script would land) and fails on any other module that:

* **names** ``EconReleaseValue`` at all -- imports it, constructs it, hands it
  to ``insert``/``update``/``delete``, or reaches its ``__table__``. Reads go
  through ``econ_store`` too (``read_value``, ``read_economic_releases``), so
  there is no legitimate reason to; this is what catches a ``session.merge``
  or an attribute write on a loaded row, which need the class in hand;
* calls ``.merge(`` in a module that names it (said separately, so the
  failure names the shape);
* carries raw write SQL against the table: ``INSERT``/``REPLACE INTO``,
  ``UPDATE``, ``DELETE FROM econ_release_value``, in any string -- an
  f-string's literal parts included;
* **names the table by string** (U6e audit L6a): the exact constant
  ``"econ_release_value"`` anywhere, which is what ``sqlalchemy.table(...)``,
  ``Table(...)``, ``metadata.tables[...]`` / ``.tables.get(...)`` and a
  ``TABLE = ...`` later interpolated into SQL all need. Prose that mentions
  the table is never the bare name, so docstrings pass. A migration under
  ``corollary/db/migrations/`` may hand it to a DDL call (``op.create_table``,
  ``op.drop_table``, ...) and nowhere else, so a data migration's
  ``op.bulk_insert(sa.table(...))`` is still caught;
* writes into an **interpolated** table name -- ``f"UPDATE {table} ..."``,
  ``"DELETE FROM {}".format(...)``, ``"INSERT INTO %s"`` -- since such a
  helper writes whatever table it is handed, this one included.

``corollary/db/models.py`` may name it -- it defines it, and lists it in
``__all__`` -- but may not construct it. The checker is exercised on planted
violations first, so a scan that silently matched nothing would fail here.

**And only ``econ_values`` builds an ``Attempt``** (U6e audit L6b). The type
check in ``econ_store.record_attempt`` guarantees an attempt's shape, not that
an ``error``'s text was scrubbed: ``Attempt("error", str(exc))`` passes it. So
any ``Attempt(...)`` outside ``econ_values`` (``attempt_of`` /
``attempt_of_error``) fails here, bar ``econ_store._stored_attempt``, which
rebuilds one from a row already stored.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

import pytest

ROOT: Final = Path(__file__).resolve().parents[2]
PACKAGE: Final = ROOT / "corollary"
#: Where a backfill script would land.
SCRIPTS: Final = ROOT / "scripts"

MODEL: Final = "EconReleaseValue"
TABLE: Final = "econ_release_value"

#: The one writer.
WRITER: Final = Path("corollary/data/econ_store.py")
#: Defines the model; may name it, never construct it.
DEFINER: Final = Path("corollary/db/models.py")

_WRITE_SQL: Final = re.compile(
    rf"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+[\"'`]?{TABLE}\b",
    re.IGNORECASE,
)
#: A write whose table is interpolated, wholly or in part (``econ_release_{x}``):
#: an f-string's ``{}``, ``str.format``'s ``{...}``, or ``%s`` / ``%(name)s``.
_WRITE_INTERPOLATED: Final = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+"
    r"[\"'`]?\w*(?:\{[^}]*\}|%s|%\(\w+\)s)",
    re.IGNORECASE,
)
_STATEMENTS: Final = frozenset({"insert", "update", "delete", "upsert"})
#: Migrations live here: the only modules that may name the table, and only to DDL.
MIGRATIONS: Final = Path("corollary/db/migrations")
#: The DDL a migration may hand the table's name to.
_DDL: Final = frozenset(
    {
        "create_table",
        "drop_table",
        "rename_table",
        "create_index",
        "drop_index",
        "batch_alter_table",
        "add_column",
        "drop_column",
        "alter_column",
        "create_check_constraint",
        "create_foreign_key",
        "create_unique_constraint",
        "create_primary_key",
        "drop_constraint",
    }
)


def _names_model(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == MODEL) or (
        isinstance(node, ast.Attribute) and node.attr == MODEL
    )


def _callee(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _render(node: ast.JoinedStr) -> str:
    """An f-string's literal parts, with each interpolation as ``{}``."""
    return "".join(
        part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "{}"
        for part in node.values
    )


def violations(source: str, path: Path) -> list[str]:
    """Every way ``source`` (the module at ``path``) writes, or could write, the table."""
    tree = ast.parse(source, filename=str(path))
    found: list[str] = []
    is_writer = path == WRITER
    is_definer = path == DEFINER

    # The table's bare name handed to a migration's DDL: the one place a
    # module other than the writer and the definer may spell it.
    ddl_args: set[int] = set()
    if MIGRATIONS in path.parents:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee(node) in _DDL:
                ddl_args.update(id(arg) for arg in node.args)

    named = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == MODEL for a in node.names):
            named = True
            if not (is_writer or is_definer):
                found.append(f"{path}:{node.lineno}: imports {MODEL}")
        elif _names_model(node):
            named = True
        if isinstance(node, ast.Call):
            callee = _callee(node)
            if _names_model(node.func) and not is_writer:
                found.append(f"{path}:{node.lineno}: constructs {MODEL}(...)")
            if callee in _STATEMENTS and any(_names_model(arg) for arg in node.args):
                if not is_writer:
                    found.append(f"{path}:{node.lineno}: {callee}({MODEL})")
            if (
                callee in _STATEMENTS
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "__table__"
                and _names_model(node.func.value.value)
                and not is_writer
            ):
                found.append(f"{path}:{node.lineno}: {MODEL}.__table__.{callee}()")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _WRITE_SQL.search(node.value)
            and not is_writer
        ):
            found.append(f"{path}:{node.lineno}: raw write SQL against {TABLE}")
        if (
            isinstance(node, ast.Constant)
            and node.value == TABLE
            and not (is_writer or is_definer or id(node) in ddl_args)
        ):
            found.append(f"{path}:{node.lineno}: names the table {TABLE!r} by string")
        if isinstance(node, ast.JoinedStr) and not is_writer:
            rendered = _render(node)
            if _WRITE_SQL.search(rendered):
                found.append(f"{path}:{node.lineno}: f-string write SQL against {TABLE}")
            if _WRITE_INTERPOLATED.search(rendered):
                found.append(f"{path}:{node.lineno}: write SQL into an interpolated table")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _WRITE_INTERPOLATED.search(node.value)
            and not is_writer
        ):
            found.append(f"{path}:{node.lineno}: write SQL into an interpolated table")

    if named and not (is_writer or is_definer):
        found.append(f"{path}: names {MODEL}; only {WRITER} may")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee(node) == "merge":
                found.append(f"{path}:{node.lineno}: session.merge in a module naming {MODEL}")
    return found


def _modules() -> list[Path]:
    return sorted([*PACKAGE.rglob("*.py"), *SCRIPTS.rglob("*.py")])


#: The module that builds every ``Attempt`` (``attempt_of``, ``attempt_of_error``).
ATTEMPT_BUILDER: Final = Path("corollary/data/econ_values.py")
#: The one function elsewhere that may: it rebuilds a stored row's attempt.
_READ_BACK: Final = (WRITER, "_stored_attempt")


def attempt_constructions(source: str, path: Path) -> list[str]:
    """Every ``Attempt(...)`` built in ``source`` outside the scrubbing builders (L6b)."""
    if path == ATTEMPT_BUILDER:
        return []
    tree = ast.parse(source, filename=str(path))
    allowed: set[int] = set()
    if path == _READ_BACK[0]:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == _READ_BACK[1]:
                allowed.update(id(inner) for inner in ast.walk(node))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "Attempt" and alias.asname not in (None, "Attempt"):
                    found.append(f"{path}:{node.lineno}: imports Attempt as {alias.asname}")
        if (
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "Attempt")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "Attempt")
            )
            and id(node) not in allowed
        ):
            found.append(
                f"{path}:{node.lineno}: builds an Attempt; use econ_values.attempt_of / "
                "attempt_of_error, which scrub"
            )
    return found


# --------------------------------------------------------------------------
# The checker, on planted violations -- so a scan matching nothing fails
# --------------------------------------------------------------------------

ELSEWHERE: Final = Path("corollary/api/routes/calendar.py")


@pytest.mark.parametrize(
    ("planted", "shape"),
    [
        (
            "from corollary.db.models import EconReleaseValue\n"
            "def f(s):\n    s.add(EconReleaseValue(release_id=1))\n",
            "constructs EconReleaseValue",
        ),
        (
            "from corollary.db import models\n"
            "def f(s):\n    s.add(models.EconReleaseValue(release_id=1))\n",
            "constructs EconReleaseValue",
        ),
        (
            "from corollary.db.models import EconReleaseValue\n"
            "def f(s, row):\n    s.merge(row)\n",
            "session.merge",
        ),
        (
            "from sqlalchemy import update\nfrom corollary.db.models import EconReleaseValue\n"
            "def f(s):\n    s.execute(update(EconReleaseValue).values(actual='1'))\n",
            "update(EconReleaseValue)",
        ),
        (
            "from sqlalchemy.dialects.sqlite import insert\n"
            "from corollary.db.models import EconReleaseValue\n"
            "def f(s):\n    s.execute(insert(EconReleaseValue).values(release_id=1))\n",
            "insert(EconReleaseValue)",
        ),
        (
            "from corollary.db.models import EconReleaseValue\n"
            "def f(s):\n    s.execute(EconReleaseValue.__table__.delete())\n",
            "EconReleaseValue.__table__.delete()",
        ),
        (
            "from sqlalchemy import text\n"
            "def f(s):\n    s.execute(text(\"UPDATE econ_release_value SET actual = '9'\"))\n",
            "raw write SQL",
        ),
        (
            "from sqlalchemy import text\n"
            "def f(s):\n    s.execute(text('insert or replace into econ_release_value VALUES (1)'))\n",
            "raw write SQL",
        ),
        ("SQL = 'DELETE FROM \"econ_release_value\" WHERE 1'\n", "raw write SQL"),
        (
            "from corollary.db.models import EconReleaseValue\n"
            "def f(s):\n    row = s.get(EconReleaseValue, (1, 2))\n    row.actual = '9'\n",
            "names EconReleaseValue",
        ),
        # U6e audit L6a: the table named by string, no model in hand.
        (
            "import sqlalchemy\n"
            "def f(s):\n"
            "    t = sqlalchemy.table('econ_release_value', sqlalchemy.column('actual'))\n"
            "    s.execute(t.update().values(actual='9'))\n",
            "names the table 'econ_release_value' by string",
        ),
        (
            "from sqlalchemy import Table, MetaData\n"
            "T = Table('econ_release_value', MetaData(), autoload_with=None)\n",
            "names the table 'econ_release_value' by string",
        ),
        (
            "def f(s, metadata):\n"
            "    s.execute(metadata.tables['econ_release_value'].delete())\n",
            "names the table 'econ_release_value' by string",
        ),
        (
            "from corollary.db.models import Base\n"
            "def f(s):\n"
            "    s.execute(Base.metadata.tables.get('econ_release_value').insert())\n",
            "names the table 'econ_release_value' by string",
        ),
        # f-string SQL: the literal in the f-string, the name in a constant,
        # and a helper that writes whatever table it is handed.
        (
            "from sqlalchemy import text\n"
            "def f(s, v):\n    s.execute(text(f'UPDATE econ_release_value SET actual = {v}'))\n",
            "f-string write SQL against econ_release_value",
        ),
        (
            "from sqlalchemy import text\n"
            "def f(s, v):\n    s.execute(text(f'DELETE FROM econ_release_{v}'))\n",
            "write SQL into an interpolated table",
        ),
        (
            "from sqlalchemy import text\nTABLE = 'econ_release_value'\n"
            "def f(s, v):\n    s.execute(text(f'UPDATE {TABLE} SET actual = {v}'))\n",
            "names the table 'econ_release_value' by string",
        ),
        (
            "from sqlalchemy import text\n"
            "def f(s, table, v):\n    s.execute(text(f'UPDATE {table} SET actual = {v}'))\n",
            "write SQL into an interpolated table",
        ),
        (
            "def f(s, name):\n    s.exec_driver_sql('DELETE FROM {} WHERE 1'.format(name))\n",
            "write SQL into an interpolated table",
        ),
        (
            "def f(cur, name):\n    cur.execute('INSERT OR REPLACE INTO %s VALUES (1)' % name)\n",
            "write SQL into an interpolated table",
        ),
    ],
)
def test_the_checker_finds_a_planted_writer(planted: str, shape: str) -> None:
    found = violations(planted, ELSEWHERE)
    assert any(shape in line for line in found), found


def test_the_checker_finds_a_writer_in_a_script() -> None:
    planted = (
        "from sqlalchemy import text\n"
        "def backfill(s):\n    s.execute(text(\"INSERT INTO econ_release_value VALUES (1)\"))\n"
    )
    found = violations(planted, Path("scripts/backfill_econ_values.py"))
    assert any("raw write SQL" in line for line in found), found


def test_a_migration_may_name_the_table_to_ddl_and_nowhere_else() -> None:
    migration = Path("corollary/db/migrations/versions/0099_example.py")
    ddl = (
        "import sqlalchemy as sa\nfrom alembic import op\n"
        "def upgrade():\n    op.create_table('econ_release_value', sa.Column('x', sa.Text))\n"
        "    op.create_index('ix_x', 'econ_release_value', ['x'])\n"
        "def downgrade():\n    op.drop_table('econ_release_value')\n"
    )
    assert violations(ddl, migration) == []
    data = (
        "import sqlalchemy as sa\nfrom alembic import op\n"
        "def upgrade():\n"
        "    op.bulk_insert(sa.table('econ_release_value', sa.column('x')), [{'x': '1'}])\n"
    )
    found = violations(data, migration)
    assert any("names the table" in line for line in found), found
    # The allowance is for migrations only: the same DDL elsewhere is named.
    found = violations(ddl, ELSEWHERE)
    assert any("names the table" in line for line in found), found


def test_the_checker_lets_a_read_through_the_store_pass() -> None:
    clean = (
        "from corollary.data.econ_store import read_economic_releases\n"
        "def f(s):\n    return read_economic_releases(s, a, b)\n"
        "SQL = 'SELECT actual FROM econ_release_value'\n"
    )
    assert violations(clean, ELSEWHERE) == []


def test_the_definer_may_name_the_model_but_not_construct_it() -> None:
    assert violations("class EconReleaseValue(Base):\n    pass\n", DEFINER) == []
    found = violations("x = EconReleaseValue(release_id=1)\n", DEFINER)
    assert any("constructs" in line for line in found), found


def test_the_writer_itself_is_allowed() -> None:
    source = (PACKAGE / "data" / "econ_store.py").read_text(encoding="utf-8")
    assert violations(source, WRITER) == []
    # ...and it really is the writer: the scan would have something to allow.
    assert f"{MODEL}(" in source


# --------------------------------------------------------------------------
# The tree
# --------------------------------------------------------------------------


def test_the_scan_reads_the_whole_package_and_the_scripts() -> None:
    modules = {p.relative_to(ROOT).as_posix() for p in _modules()}
    assert WRITER.as_posix() in modules and DEFINER.as_posix() in modules
    assert "corollary/engine/scheduler.py" in modules
    assert "corollary/db/migrations/versions/0017_econ_release_value.py" in modules
    assert "scripts/weekly_audit.py" in modules
    assert len(modules) > 50


def test_no_module_but_econ_store_writes_the_first_print_table() -> None:
    found: list[str] = []
    for module in _modules():
        rel = module.relative_to(ROOT)
        found.extend(violations(module.read_text(encoding="utf-8"), Path(rel.as_posix())))
    assert found == []


# --------------------------------------------------------------------------
# Only econ_values builds an Attempt (U6e audit L6b)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "planted",
    [
        "from corollary.data.econ_values import Attempt\n"
        "def f(exc):\n    return Attempt('error', str(exc))\n",
        "from corollary.data import econ_values\n"
        "def f(exc):\n    return econ_values.Attempt('error', str(exc))\n",
        "from corollary.data.econ_values import Attempt as A\n"
        "def f(exc):\n    return A('error', str(exc))\n",
    ],
    ids=["by-name", "by-attribute", "by-alias"],
)
def test_the_attempt_checker_finds_an_unscrubbed_error(planted: str) -> None:
    assert attempt_constructions(planted, Path("corollary/data/econ_first_prints.py")) != []


def test_the_builder_and_the_stores_read_back_may_build_one() -> None:
    builder = (PACKAGE / "data" / "econ_values.py").read_text(encoding="utf-8")
    assert "Attempt(" in builder and attempt_constructions(builder, ATTEMPT_BUILDER) == []
    store = (PACKAGE / "data" / "econ_store.py").read_text(encoding="utf-8")
    assert attempt_constructions(store, WRITER) == []
    # ...and only in that function: the same call anywhere else in the store is found.
    planted = "def record(exc):\n    return Attempt('error', str(exc))\n"
    assert attempt_constructions(planted, WRITER) != []


def test_no_module_but_econ_values_builds_an_attempt() -> None:
    found: list[str] = []
    for module in _modules():
        rel = Path(module.relative_to(ROOT).as_posix())
        found.extend(attempt_constructions(module.read_text(encoding="utf-8"), rel))
    assert found == []
