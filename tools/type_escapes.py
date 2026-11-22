"""Count the ways around the type checker in src/gct, and fail if a change adds any (#192).

    uv run python tools/type_escapes.py                 # print the count and every site
    uv run python tools/type_escapes.py --base <ref>    # also fail if the count rose since <ref>

mypy only checks what the code lets it see. `Any`, `cast(...)` and `# type: ignore[code]` each
tell it to look away, so the merge check holds their number in `src/gct` to what the PR's base
had: equal or lower passes, higher fails. Removing one is always allowed.

The counting rule, read from Python's own tokens, so words in strings and docstrings never count:
  - every name `Any` (an import, an annotation, `typing.Any`);
  - every name `cast` followed by `(` (`cast(...)`, `typing.cast(...)`);
  - every `# type: ignore[...]` comment.
A name bound by `Any as A` or `cast as c` in an import counts like the name it stands for. Each
occurrence counts once, so a line with two `Any` counts two.

Refused outright, whatever the count, because each can silence mypy for a line, a function or a
whole file without naming an error: a `# type: ignore` with no `[code]` (at the top of a file it
silences the whole file, and mypy's ignore-without-code does not see that case), any other
`# type:` comment (a type comment can say `Any`), any `# mypy:` comment (inline configuration such
as `ignore-errors`), the name `no_type_check`, and any `.pyi` stub file. Comments are read where
they start with `#`, so `# noqa  # type: ignore` is seen. (A coded ignore at the top of a file is
not refused here because mypy already reports it as an error.)

Not followed: plain assignment (`c = cast`), names imported from another module of the package,
and settings outside src/gct (pyproject.toml's [tool.mypy] is reviewed as a config change).

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
_DIRECTIVE = re.compile(r"#\s*(type|mypy):\s*(\S*)")  # every `# type:` / `# mypy:` in a comment
_CODED_IGNORE = re.compile(r"ignore\[")


def _directives(token: tokenize.TokenInfo) -> list[tuple[str, str]]:
    return _DIRECTIVE.findall(token.string) if token.type == tokenize.COMMENT else []


def escapes(source: str) -> list[tuple[int, str]]:
    """Every counted escape hatch in one file's source, as (line number, what was found)."""
    found: list[tuple[int, str]] = []
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    kinds = {"Any": "Any", "cast": "cast("}  # name -> the escape it is; import aliases join it
    alias_at: set[int] = set()  # where an alias is bound: the original name was counted there
    for i, token in enumerate(tokens):
        for directive, rest in _directives(token):
            if directive == "type" and _CODED_IGNORE.match(rest):
                found.append((token.start[0], "type: ignore[...]"))
        if token.type != tokenize.NAME or token.string not in kinds or i in alias_at:
            continue
        kind = kinds[token.string]
        after = [t.string for t in tokens[i + 1 : i + 3]]
        if len(after) == 2 and after[0] == "as":
            kinds[after[1]] = kind
            alias_at.add(i + 2)
        if kind == "Any" or after[:1] == ["("]:
            found.append((token.start[0], kind))
    return found


def refused(source: str) -> list[tuple[int, str]]:
    """What is never allowed in one file's source, whatever the count, as (line number, what)."""
    found: list[tuple[int, str]] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.NAME and token.string == "no_type_check":
            found.append((token.start[0], "no_type_check"))
        for directive, rest in _directives(token):
            if directive == "mypy":
                found.append((token.start[0], "a # mypy: comment"))
            elif not rest.startswith("ignore"):
                found.append((token.start[0], "a # type: comment"))
            elif not _CODED_IGNORE.match(rest):
                found.append((token.start[0], "a # type: ignore with no [code]"))
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

    banned = [
        f"{name}:{line}: {what}" for name, src in files.items() for line, what in refused(src)
    ]
    banned += [
        f"{p.relative_to(root).as_posix()}: a .pyi stub" for p in (root / PACKAGE).rglob("*.pyi")
    ]
    if banned:
        print("FAILED: never allowed in src/gct, whatever the count:", *banned, sep="\n  ")
        return 1
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
