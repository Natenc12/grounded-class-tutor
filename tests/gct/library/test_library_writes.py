"""Every write goes through one transaction helper, and any failure, including one at COMMIT,
leaves the connection rolled back and ready for the next write (#226 review).

A write that fails must not stay visible on the connection that tried it: the app would say the
save failed, then show it, then lose it on restart. The page-count rule is checked only at
COMMIT, which is exactly where a document save with a missing page fails (#227), and a lock held
by a second window makes COMMIT itself fail.

The failure messages say what the person can do about it: another window has the library open,
the file is read-only, the disk is full, or something else.
"""

from __future__ import annotations

import ast
import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

import gct.library
from gct.library import Library, LibraryError

LOCKED = "another window is using the library"
READ_ONLY = "the library file is read-only"
DISK_FULL = "the disk is full"
OTHER = "something went wrong"


@pytest.fixture
def impatient(library_path: Path) -> Iterator[Library]:
    with Library(library_path, timeout=0.05) as opened:
        yield opened


@pytest.fixture
def other_window(library_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(library_path, timeout=0.05, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    yield connection
    if connection.in_transaction:
        connection.execute("ROLLBACK")
    connection.close()


@pytest.fixture
def read_only(library_path: Path) -> Iterator[Path]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can write to a read-only file")
    with Library(library_path) as library:
        library.create_class("Hydrology")
    library_path.chmod(stat.S_IRUSR)
    yield library_path
    library_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _names(library: Library) -> list[str]:
    return [c.name for c in library.list_classes()]


def test_a_rule_that_fails_at_commit_leaves_nothing_and_the_next_write_works(library: Library):
    hydrology = library.create_class("Hydrology")

    with pytest.raises(LibraryError, match=OTHER):
        with library._transaction():
            library._connection.execute(
                "INSERT INTO documents (id, class_id, filename, sha256, page_count, original) "
                "VALUES ('bad', ?, 'notes.pdf', ?, 3, x'25504446')",
                (hydrology.id, "a" * 64),
            )
            for number in (1, 2):
                library._connection.execute(
                    "INSERT INTO pages (document_id, page_count, page_or_slide, text) "
                    "VALUES ('bad', 3, ?, '')",
                    (number,),
                )

    assert not library._connection.in_transaction
    assert library._connection.execute("SELECT count(*) FROM documents").fetchone() == (0,)
    assert library._connection.execute("SELECT count(*) FROM pages").fetchone() == (0,)
    library.create_class("Geology")
    assert _names(library) == ["Geology", "Hydrology"]


def test_a_commit_blocked_by_another_window_leaves_nothing_and_both_can_write(
    impatient: Library, other_window: sqlite3.Connection, library_path: Path
):
    impatient.create_class("A")
    other_window.execute("BEGIN")
    other_window.execute("SELECT count(*) FROM classes").fetchone()

    with pytest.raises(LibraryError, match=LOCKED):
        impatient.create_class("C")

    assert not impatient._connection.in_transaction
    assert _names(impatient) == ["A"]
    other_window.execute("COMMIT")
    other_window.execute("INSERT INTO classes (id, name) VALUES ('b', 'B')")
    impatient.create_class("D")
    with Library(library_path) as reopened:
        assert _names(reopened) == ["A", "B", "D"]


def test_a_write_while_another_window_is_writing_says_so(
    impatient: Library, other_window: sqlite3.Connection
):
    other_window.execute("BEGIN IMMEDIATE")

    with pytest.raises(LibraryError, match=LOCKED):
        impatient.create_class("Hydrology")

    other_window.execute("ROLLBACK")
    impatient.create_class("Hydrology")


def test_a_write_to_a_read_only_library_says_so(read_only: Path):
    with Library(read_only) as library:
        with pytest.raises(LibraryError, match=READ_ONLY):
            library.create_class("Geology")
        assert _names(library) == ["Hydrology"]


def test_a_write_on_a_full_disk_says_so(library: Library):
    pages = library._connection.execute("PRAGMA page_count").fetchone()[0]
    library._connection.execute(f"PRAGMA max_page_count = {pages}")

    with pytest.raises(LibraryError, match=DISK_FULL):
        library.create_class("x" * 100_000)

    assert not library._connection.in_transaction
    assert _names(library) == []
    library.create_class("Hydrology")


def test_check_on_a_read_only_library_says_it_could_not_run(read_only: Path):
    with Library(read_only) as library:
        with pytest.raises(LibraryError, match=f"could not run: {READ_ONLY}"):
            library.check()


def test_check_while_another_window_is_writing_says_it_could_not_run(
    impatient: Library, other_window: sqlite3.Connection
):
    other_window.execute("BEGIN IMMEDIATE")
    other_window.execute("INSERT INTO classes (id, name) VALUES ('b', 'B')")

    with pytest.raises(LibraryError, match=f"could not run: {LOCKED}"):
        impatient.check()

    other_window.execute("COMMIT")
    assert impatient.check() == []


def test_transactions_are_started_and_ended_in_one_place_only():
    """Only `Library._transaction` may begin, commit or roll back, so every write gets the same
    rollback-on-any-failure handling. Write new library code inside `with self._transaction():`
    instead of issuing these statements or calling `commit()` or `rollback()` yourself."""
    package = Path(gct.library.__file__).parent
    found = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if function.name == "_transaction":
                continue
            for node in ast.walk(function):
                text = node.value if isinstance(node, ast.Constant) else None
                if isinstance(text, str) and any(
                    word in text.upper().split() for word in ("BEGIN", "COMMIT", "ROLLBACK")
                ):
                    found.append(f"{path.name}:{node.lineno} {text!r}")
                if isinstance(node, ast.Attribute) and node.attr in ("commit", "rollback"):
                    found.append(f"{path.name}:{node.lineno} .{node.attr}()")
    assert found == []
