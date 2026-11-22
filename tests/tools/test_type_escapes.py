"""The guard on `Any`, `cast(` and `# type: ignore` in src/gct, `tools/type_escapes.py` (#192).

Loaded from its file like `tools/drive.py`. The base comparison runs against a throwaway git
repository, so nothing here depends on this checkout's history.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "type_escapes.py"
_spec = importlib.util.spec_from_file_location("type_escapes_under_test", SCRIPT)
assert _spec is not None and _spec.loader is not None
type_escapes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(type_escapes)

TYPED = "def f(x: int) -> int:\n    return x\n"
ONE_ANY = "from typing import Any\n"


def test_each_escape_counts_once_per_occurrence_by_its_tokens():
    source = (
        "import typing\n"
        "from typing import Any, cast\n"  # 2: Any, and not `cast` (no bracket follows)
        "def f(x: Any) -> Any:\n"  # 2: two on one line
        "    y = typing.cast(int, x)\n"  # 1
        "    z = cast(int, x)  # type: ignore[redundant-cast]\n"  # 2: cast( and the ignore
        "    return y + z  #type:ignore[operator]\n"  # 1: mypy reads this spelling too
    )

    assert type_escapes.escapes(source) == [
        (2, "Any"),
        (3, "Any"),
        (3, "Any"),
        (4, "cast("),
        (5, "cast("),
        (5, "type: ignore[...]"),
        (6, "type: ignore[...]"),
    ]


def test_a_name_imported_as_an_alias_counts_like_the_original():
    source = (
        "from typing import Any as Anything, cast as to\n"  # 1: the Any; an uncalled cast is not
        "def f(x: Anything) -> int:\n"  # 1
        "    return to(int, x)\n"  # 1
    )

    assert type_escapes.escapes(source) == [(1, "Any"), (2, "Any"), (3, "cast(")]


def test_words_in_strings_and_docstrings_are_not_escapes():
    source = (
        '"""Any caller may cast( a value; # type: ignore is banned."""\n'
        "MESSAGE = 'Any cast( here # type: ignore'\n"
        "def anything(cast_to: int) -> int:  # Anything goes\n"
        "    return cast_to\n"
    )

    assert type_escapes.escapes(source) == []
    assert type_escapes.refused(source) == []


@pytest.mark.parametrize(
    ("source", "what"),
    [
        ("# type: ignore\nX: int = 'a'\n", "a # type: ignore with no [code]"),  # the whole file
        ("X: int = 'a'  # type: ignore\n", "a # type: ignore with no [code]"),
        ("X: int = 'a'  # noqa: E501  #type:ignore\n", "a # type: ignore with no [code]"),
        ("x = f()  # type: Any\n", "a # type: comment"),
        ("# mypy: ignore-errors\n", "a # mypy: comment"),
        ("# mypy: disable-error-code=assignment\n", "a # mypy: comment"),
        ("from typing import no_type_check\n", "no_type_check"),
        ("@typing.no_type_check\ndef f() -> int:\n    return 'b'\n", "no_type_check"),
    ],
)
def test_ways_to_silence_mypy_without_naming_an_error_are_refused(source, what):
    assert what in [found for _, found in type_escapes.refused(source)]


def test_a_coded_ignore_is_counted_not_refused():
    source = "X: int = 'a'  # type: ignore[assignment]\n"

    assert type_escapes.refused(source) == []
    assert type_escapes.escapes(source) == [(1, "type: ignore[...]")]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A repository whose committed base has one `Any` in src/gct, tagged `base`."""
    (tmp_path / "src" / "gct").mkdir(parents=True)
    (tmp_path / "src" / "gct" / "a.py").write_text(ONE_ANY + TYPED)
    (tmp_path / "src" / "gct" / "notes.txt").write_text("Any cast( # type: ignore\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "tag", "base")
    return tmp_path


def run(repo: Path, *argv: str) -> int:
    return type_escapes.main(list(argv), root=repo)


def test_no_change_since_the_base_passes(repo, capsys):
    assert run(repo, "--base", "base") == 0
    assert "1 type escape hatches in src/gct\n1 at base\n" in capsys.readouterr().out


def test_adding_one_anywhere_in_the_package_fails_and_names_the_file(repo, capsys):
    (repo / "src" / "gct" / "sub").mkdir()
    (repo / "src" / "gct" / "sub" / "b.py").write_text("x = cast(int, 1)\n")

    assert run(repo, "--base", "base") == 1
    out = capsys.readouterr().out
    assert "src/gct/sub/b.py:1: cast(" in out
    assert "  src/gct/sub/b.py: 0 -> 1" in out
    assert "FAILED: this change adds 1." in out


def test_moving_one_between_files_passes(repo):
    (repo / "src" / "gct" / "a.py").write_text(TYPED)
    (repo / "src" / "gct" / "b.py").write_text(ONE_ANY)

    assert run(repo, "--base", "base") == 0


def test_removing_one_passes(repo, capsys):
    (repo / "src" / "gct" / "a.py").write_text(TYPED)

    assert run(repo, "--base", "base") == 0
    assert "  src/gct/a.py: 1 -> 0" in capsys.readouterr().out


def test_without_a_base_it_only_reports(repo, capsys):
    (repo / "src" / "gct" / "a.py").write_text(ONE_ANY * 3)

    assert run(repo) == 0
    assert capsys.readouterr().out.endswith("3 type escape hatches in src/gct\n")


def test_a_base_git_cannot_find_stops_the_check_instead_of_passing(repo):
    with pytest.raises(SystemExit, match="git ls-tree .* failed"):
        run(repo, "--base", "no-such-ref")


def test_a_refused_escape_fails_even_when_the_count_goes_down(repo, capsys):
    (repo / "src" / "gct" / "a.py").write_text(TYPED)  # one Any fewer
    (repo / "src" / "gct" / "zz.py").write_text("# type: ignore\nX: int = 'a'\n")

    assert run(repo, "--base", "base") == 1
    out = capsys.readouterr().out
    assert "src/gct/zz.py:1: a # type: ignore with no [code]" in out
    assert run(repo) == 1  # refused with or without a base


def test_a_stub_file_in_the_package_is_refused(repo, capsys):
    (repo / "src" / "gct" / "a.pyi").write_text("def f(x): ...\n")

    assert run(repo, "--base", "base") == 1
    assert "src/gct/a.pyi: a .pyi stub" in capsys.readouterr().out
