"""Count the ways around the type checker in src/gct, and fail if a change adds any (#192).

    uv run python tools/type_escapes.py                 # print the count and every site
    uv run python tools/type_escapes.py --base <ref>    # also fail if the count rose since <ref>

mypy only checks what the code lets it see. `Any`, `cast(...)` and `# type: ignore` each tell it
to look away, so the merge check holds their number in `src/gct` to what the PR's base had:
equal or lower passes, higher fails. Removing one is always allowed.

The counting rule, read from Python's own tokens, so words in strings and docstrings never count:
  - every name `Any` (an import, an annotation, `typing.Any`);
  - every name `cast` followed by `(` (`cast(...)`, `typing.cast(...)`);
  - every comment containing `type: ignore` (the space after the colon is optional, as in mypy).
Each occurrence counts once, so a line with two `Any` counts two. One blind spot, by design: an
import under another name (`from typing import Any as A`) counts at the import only.

The working tree is counted; the base is read from git (`git show <ref>:<file>`), so nothing is
checked out. In CI the base is the first parent of the merge commit a pull request is tested as.
Locally, compare with where your branch started: `--base "$(git merge-base origin/main HEAD)"`.
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tokenize
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "src/gct"
_IGNORE = re.compile(r"type:\s*ignore")


def escapes(source: str) -> list[tuple[int, str]]:
    """Every escape hatch in one file's source, as (line number, what was found)."""
    found: list[tuple[int, str]] = []
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    for token, following in zip(tokens, tokens[1:] + [None], strict=True):
        if token.type == tokenize.NAME and token.string == "Any":
            found.append((token.start[0], "Any"))
        elif token.type == tokenize.NAME and token.string == "cast":
            if following is not None and following.string == "(":
                found.append((token.start[0], "cast("))
        elif token.type == tokenize.COMMENT and _IGNORE.search(token.string):
            found.append((token.start[0], "type: ignore"))
    return found


def working_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((root / PACKAGE).rglob("*.py"))
    }


def at_ref(root: Path, ref: str) -> dict[str, str]:
    names = _git(root, "ls-tree", "-r", "--name-only", ref, "--", PACKAGE).splitlines()
    return {name: _git(root, "show", f"{ref}:{name}") for name in names if name.endswith(".py")}


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8"
    )
    if done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def per_file(files: dict[str, str]) -> Counter[str]:
    return Counter({name: len(escapes(source)) for name, source in files.items()})


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", metavar="REF", help="fail if the count is higher than at REF")
    args = parser.parse_args(argv)

    files = working_tree(root)
    for name, source in files.items():
        for line, what in escapes(source):
            print(f"{name}:{line}: {what}")
    head = per_file(files)
    print(f"{head.total()} type escape hatches in {PACKAGE}")
    if args.base is None:
        return 0

    base = per_file(at_ref(root, args.base))
    print(f"{base.total()} at {args.base}")
    for name in sorted(head.keys() | base.keys()):
        if head[name] != base[name]:
            print(f"  {name}: {base[name]} -> {head[name]}")
    if head.total() > base.total():
        print(
            f"FAILED: this change adds {head.total() - base.total()}. Fix the hint the checker "
            "disagrees with instead of telling it to look away."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
