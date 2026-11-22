"""Opening a library file: a new file gets the schema and its stamp; anything else that is not
this version's library is refused with a clear error and left exactly as it was (#226).

There are no upgrades before the first release, so a file is never reset or rewritten to make it
fit. The realistic failures are a file that is not a library (a PDF renamed `.sqlite3`, another
app's database), one made by a newer tutor, and one whose tables are not the expected ones.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gct.library import (
    Library,
    LibraryError,
    NewerLibraryError,
    NotALibraryError,
    OutdatedLibraryError,
)
from gct.library.schema import APPLICATION_ID, VERSION


def _stamp(path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(path)
    try:
        return (
            connection.execute("PRAGMA application_id").fetchone()[0],
            connection.execute("PRAGMA user_version").fetchone()[0],
        )
    finally:
        connection.close()


def _refuses_untouched(path: Path, error: type[LibraryError]) -> LibraryError:
    before = path.read_bytes()
    siblings = sorted(p.name for p in path.parent.iterdir())
    with pytest.raises(error) as raised:
        Library(path)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == siblings
    return raised.value


def test_a_new_file_gets_the_schema_and_the_version_stamp(library_path: Path):
    with Library(library_path) as library:
        assert library.list_classes() == []
        assert library.check() == []

    assert _stamp(library_path) == (APPLICATION_ID, VERSION)


def test_reopening_keeps_the_classes(library_path: Path):
    with Library(library_path) as library:
        hydrology = library.create_class("Hydrology")
        geology = library.create_class("Geology")

    with Library(library_path) as library:
        assert library.list_classes() == [geology, hydrology]
        assert library.check() == []


def test_an_empty_existing_file_becomes_a_library(library_path: Path):
    library_path.touch()

    with Library(library_path) as library:
        assert library.list_classes() == []

    assert _stamp(library_path) == (APPLICATION_ID, VERSION)


def test_a_pdf_renamed_as_a_library_is_refused_and_left_untouched(library_path: Path):
    library_path.write_bytes(
        b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n" + bytes(range(256)) * 40
    )

    error = _refuses_untouched(library_path, NotALibraryError)

    assert "not a class library" in str(error)


def test_another_apps_database_is_refused_and_left_untouched(library_path: Path):
    connection = sqlite3.connect(library_path)
    connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    connection.execute("INSERT INTO notes (body) VALUES ('keep me')")
    connection.commit()
    connection.close()

    _refuses_untouched(library_path, NotALibraryError)


def test_a_library_from_a_newer_version_is_refused_and_left_untouched(library_path: Path):
    with Library(library_path) as library:
        library.create_class("Hydrology")
    connection = sqlite3.connect(library_path)
    connection.execute(f"PRAGMA user_version = {VERSION + 1}")
    connection.close()

    error = _refuses_untouched(library_path, NewerLibraryError)

    assert "newer version" in str(error)


@pytest.mark.parametrize(
    "change",
    [
        "DROP TABLE passages_fts",
        "DROP TRIGGER passages_fts_delete",
        "ALTER TABLE documents ADD COLUMN status TEXT",
        "CREATE TABLE extra (id INTEGER PRIMARY KEY)",
    ],
)
def test_a_library_without_the_expected_tables_is_refused_and_left_untouched(
    library_path: Path, change: str
):
    Library(library_path).close()
    connection = sqlite3.connect(library_path)
    connection.execute(change)
    connection.commit()
    connection.close()

    _refuses_untouched(library_path, OutdatedLibraryError)


def test_a_library_stamped_with_an_older_version_is_refused(library_path: Path):
    Library(library_path).close()
    connection = sqlite3.connect(library_path)
    connection.execute("PRAGMA user_version = 0")
    connection.close()

    _refuses_untouched(library_path, OutdatedLibraryError)


def test_a_missing_folder_is_a_library_error(tmp_path: Path):
    with pytest.raises(LibraryError):
        Library(tmp_path / "no-such-folder" / "course.sqlite3")


def _rewrite_stored_schema(path: Path, where: str) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        "UPDATE sqlite_schema SET sql = replace(replace(sql, char(10), ' '), '    ', '  ') "
        f"WHERE sql IS NOT NULL AND {where}"
    )
    connection.execute("PRAGMA writable_schema = OFF")
    connection.close()


def test_a_library_whose_stored_schema_text_is_reformatted_still_opens(
    library_path: Path,
):
    """Opening compares what the app relies on (tables, columns, types, keys, triggers), not the
    stored text, so reformatting it, or a SQLite update that words the keyword index's own
    hidden tables differently, does not make a healthy library look damaged."""
    with Library(library_path) as library:
        hydrology = library.create_class("Hydrology")
    _rewrite_stored_schema(library_path, "1")

    with Library(library_path) as library:
        assert library.list_classes() == [hydrology]
        assert library.check() == []


def test_a_library_whose_keyword_index_tables_are_worded_differently_still_opens(
    library_path: Path,
):
    Library(library_path).close()
    connection = sqlite3.connect(library_path, isolation_level=None)
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        "UPDATE sqlite_schema SET sql = replace(sql, 'CREATE TABLE', 'create table') "
        "WHERE name LIKE 'passages_fts_%' AND type = 'table'"
    )
    connection.execute("PRAGMA writable_schema = OFF")
    connection.close()

    Library(library_path).close()


def test_a_library_missing_a_column_is_refused_and_left_untouched(library_path: Path):
    Library(library_path).close()
    connection = sqlite3.connect(library_path)
    connection.execute("ALTER TABLE pages DROP COLUMN text")
    connection.commit()
    connection.close()

    _refuses_untouched(library_path, OutdatedLibraryError)


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        ("pages", "BETWEEN 1 AND page_count", "BETWEEN 1 AND page_count + 1"),
        ("passages_fts_delete", "old.id, old.text", "old.id, ''"),
        ("pages", "nullif(page_or_slide - 1, 0)", "nullif(page_or_slide - 2, 0)"),
    ],
    ids=["loosened check rule", "changed trigger body", "changed generated column"],
)
def test_a_library_with_an_outdated_rule_is_refused_and_left_untouched(
    library_path: Path, name: str, old: str, new: str
):
    """Before the first release the schema changes in place, so a library made by an earlier
    build can differ only in a rule's wording. It must be refused, not opened with old rules."""
    Library(library_path).close()
    connection = sqlite3.connect(library_path, isolation_level=None)
    assert (
        old
        in connection.execute("SELECT sql FROM sqlite_schema WHERE name = ?", (name,)).fetchone()[0]
    )
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        "UPDATE sqlite_schema SET sql = replace(sql, ?, ?) WHERE name = ?", (old, new, name)
    )
    connection.execute("PRAGMA writable_schema = OFF")
    connection.close()

    error = _refuses_untouched(library_path, OutdatedLibraryError)

    assert "earlier build" in str(error)
    assert "create a new library and import your course files again" in str(error)
