from __future__ import annotations

import ast
import io
import json
import re
import tokenize
from pathlib import Path

import gct

SRC = Path(gct.__file__).parent
BASELINE = Path(__file__).with_name("comment_baseline.json")
REGENERATE = "uv run python tests/gct/test_comment_ratchet.py"
PRAGMA = re.compile(r"#\s*(noqa|type:|pragma|fmt:)")
DOCSTRING_OWNERS = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def prose_lines(text: str) -> int:
    lines = {
        tok.start[0]
        for tok in tokenize.generate_tokens(io.StringIO(text).readline)
        if tok.type == tokenize.COMMENT and not PRAGMA.match(tok.string)
    }
    for node in ast.walk(ast.parse(text)):
        first = node.body[0] if isinstance(node, DOCSTRING_OWNERS) and node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str) and first.end_lineno is not None:
                lines.update(range(first.lineno, first.end_lineno + 1))
    return len(lines)


def measure(src: Path = SRC) -> dict[str, int]:
    counts = {p.relative_to(src).as_posix(): prose_lines(p.read_text()) for p in src.rglob("*.py")}
    return {name: n for name, n in sorted(counts.items()) if n}


def problems(allowed: dict[str, int], now: dict[str, int]) -> list[str]:
    grown = [
        f"{f}: {n}, allowed {allowed.get(f, 0)}" for f, n in now.items() if n > allowed.get(f, 0)
    ]
    if grown:
        return [
            "Comment and docstring lines in src/gct may only go down (#186). A file missing from "
            "tests/gct/comment_baseline.json is allowed 0. State the rule as a test or a type "
            "instead:\n  " + "\n  ".join(grown)
        ]
    if now != allowed:
        return [
            "Comment or docstring lines were removed from src/gct (or a file was deleted), so "
            f"tests/gct/comment_baseline.json is out of date. Lower it by running `{REGENERATE}`. "
            "Every PR that retires code or removes comments needs this once; the command only "
            "ever lowers counts."
        ]
    return []


def lowered(old: dict[str, int], now: dict[str, int]) -> dict[str, int]:
    return {f: min(n, old[f]) for f, n in now.items() if f in old}


def test_comments_in_product_code_only_shrink() -> None:
    found = problems(json.loads(BASELINE.read_text()), measure())
    assert not found, found[0]


def copied_src(tmp_path: Path) -> Path:
    dest = tmp_path / "gct"
    for p in SRC.rglob("*.py"):
        target = dest / p.relative_to(SRC)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(p.read_text())
    return dest


def test_a_new_comment_in_a_baselined_file_fails_with_count_and_limit(tmp_path: Path) -> None:
    src, allowed = copied_src(tmp_path), json.loads(BASELINE.read_text())
    n = allowed["grounder/answer.py"]
    with (src / "grounder/answer.py").open("a") as f:
        f.write("# a new comment\n")
    found = problems(allowed, measure(src))
    assert found and f"grounder/answer.py: {n + 1}, allowed {n}" in found[0]


def test_a_new_comment_in_an_unlisted_file_is_allowed_zero(tmp_path: Path) -> None:
    src = copied_src(tmp_path)
    path = sorted((src / "library").glob("*.py"))[0]
    path.write_text(path.read_text() + "# a new comment\n")
    found = problems(json.loads(BASELINE.read_text()), measure(src))
    assert found and f"library/{path.name}: 1, allowed 0" in found[0]


def test_a_removed_comment_asks_for_the_regenerate_command(tmp_path: Path) -> None:
    allowed = json.loads(BASELINE.read_text())
    now = {**allowed, "grounder/answer.py": allowed["grounder/answer.py"] - 1}
    found = problems(allowed, now)
    assert found and REGENERATE in found[0]
    assert lowered(allowed, now) == now


def test_regenerating_never_raises_a_count_or_adds_a_file() -> None:
    old = {"a.py": 5, "b.py": 3, "gone.py": 2}
    assert lowered(old, {"a.py": 9, "b.py": 1, "new.py": 4}) == {"a.py": 5, "b.py": 1}


def test_only_comments_and_docstrings_count() -> None:
    text = (
        '"""Module doc\nover two lines."""\n'
        "import os  # noqa: F401\n"
        "PROMPT = '''a long\nmulti-line\nstring'''\n"
        "def f() -> str:\n"
        "    'one-line docstring'\n"
        "    return 'x'  # trailing comment\n"
    )
    assert prose_lines(text) == 4


if __name__ == "__main__":
    now = measure()
    old = json.loads(BASELINE.read_text()) if BASELINE.exists() else now
    BASELINE.write_text(json.dumps(lowered(old, now), indent=1) + "\n")
