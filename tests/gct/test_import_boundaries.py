"""The grounder and the eval code must not import from `gct.retriever` (#219).

The pgvector retriever is deleted in stage 2 of #216. Anything the answer and eval code share with
it lives in `gct.contracts`, so that deletion never has to edit them. This walks the import
statements in both packages and fails on any that reach `gct.retriever`. There are no exceptions.

The practice exam and the library's ask run with no Postgres (#230), so importing either in a fresh
interpreter must not load anything stage 2 deletes.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import gct

SRC = Path(gct.__file__).parent
GUARDED = ("grounder", "eval")
RETIRED = (
    "gct.api",
    "gct.ask",
    "gct.classes",
    "gct.db",
    "gct.ingest.index",
    "gct.jobs",
    "gct.retriever",
    "pgvector",
    "psycopg",
)


def _module_of(path: Path) -> list[str]:
    parts = ["gct", *path.relative_to(SRC).with_suffix("").parts]
    return parts[:-1] if parts[-1] == "__init__" else parts


def _resolve(node: ast.ImportFrom, path: Path) -> str:
    if node.level == 0:
        return node.module or ""
    package = _module_of(path)
    if path.name != "__init__.py":
        package = package[:-1]
    base = package[: len(package) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])])


def _retriever_imports(path: Path) -> list[str]:
    rel = path.relative_to(SRC).as_posix()
    found = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "gct.retriever" or alias.name.startswith("gct.retriever."):
                    found.append(f"{rel}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = _resolve(node, path)
            for alias in node.names:
                target = f"{module}.{alias.name}"
                if not (target == "gct.retriever" or target.startswith("gct.retriever.")):
                    continue
                found.append(f"{rel}:{node.lineno} from {module} import {alias.name}")
    return found


def test_grounder_and_eval_do_not_import_the_retriever():
    files = sorted(p for package in GUARDED for p in (SRC / package).rglob("*.py"))
    assert files, f"no source files found under {SRC} for {GUARDED}"
    found = [hit for path in files for hit in _retriever_imports(path)]
    assert not found, (
        "gct.grounder and gct.eval must not import from gct.retriever; "
        "move shared types to gct.contracts instead:\n  " + "\n  ".join(found)
    )


@pytest.mark.parametrize("module", ["gct.eval.runner", "gct.library.ask"])
def test_the_practice_exam_and_the_library_ask_load_nothing_stage_2_deletes(module):
    loaded = subprocess.run(
        [sys.executable, "-c", f"import sys, {module}; print('\\n'.join(sys.modules))"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert module in loaded
    assert [name for name in loaded if name.startswith(RETIRED)] == []
