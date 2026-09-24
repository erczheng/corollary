"""The news recorders read the process environment and never load ``.env``.

Rule 6: the key reaches a recorder through the launcher (``uv run --env-file
<path> python ...``), the same way it reaches ``corollary/``. A recorder that
calls ``load_dotenv`` itself reads the real file on its own authority, which
is the one thing the ``Read(./.env)`` denial exists to stop.

Checked over the **syntax tree**, not the text: the docstrings say
``load_dotenv`` in order to explain its absence, and a substring match would
either fail on that prose or be loosened until it caught nothing.
"""

import ast
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def dotenv_uses(path: Path) -> list[str]:
    """Every import of ``dotenv`` and every call to ``load_dotenv``/``dotenv_values``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [
                f"line {node.lineno}: import {alias.name}"
                for alias in node.names
                if alias.name.split(".")[0] == "dotenv"
            ]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "dotenv":
                found.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name in ("load_dotenv", "dotenv_values"):
                found.append(f"line {node.lineno}: {name}(...)")
    return found


@pytest.mark.parametrize("recorder", ["record_finnhub.py", "record_massive.py"])
def test_a_news_recorder_never_loads_the_env_file(recorder: str) -> None:
    path = FIXTURES / recorder
    assert path.exists(), f"{path} moved; this guard would pass over nothing"
    assert dotenv_uses(path) == [], (
        f"{recorder} loads .env itself. Every mode must read the process "
        "environment only; the launcher supplies it with --env-file."
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "record_alpaca.py still calls load_dotenv on its default path. It is "
        "outside the Phase 3 step 4 news unit, so it is recorded here rather "
        "than changed; strict, so the day it is fixed this turns red and the "
        "marker comes off."
    ),
)
def test_the_alpaca_recorder_never_loads_the_env_file() -> None:
    assert dotenv_uses(FIXTURES / "record_alpaca.py") == []


def test_the_detector_sees_every_form_it_claims_to() -> None:
    """A guard that has never failed proves nothing: prove it fails."""
    import tempfile

    source = (
        '"""mentions load_dotenv in prose, which must not count."""\n'
        "import dotenv\n"
        "from dotenv import load_dotenv\n"
        "load_dotenv('.env')\n"
        "dotenv.dotenv_values('.env')\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "record_x.py"
        path.write_text(source, encoding="utf-8")
        assert dotenv_uses(path) == [
            "line 2: import dotenv",
            "line 3: from dotenv import ...",
            "line 4: load_dotenv(...)",
            "line 5: dotenv_values(...)",
        ]
