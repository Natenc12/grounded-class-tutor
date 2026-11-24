"""`check()` finds damage that opening the library does not look for (#226).

Opening only reads the stamp and the list of tables, so it stays fast. `check()` is the explicit,
slower look: SQLite's own integrity check, rows whose parent is missing (possible only from a
writer that left foreign keys off), and whether the keyword-search index still matches the
passages it was built from.
"""

from __future__ import annotations

import sqlite3

import pytest

from gct.library import Library


@pytest.fixture
def class_id(library: Library) -> str:
    return library.create_class("Hydrology").id


@pytest.fixture
def document_id(add_document, class_id: str) -> str:
    return add_document(class_id, ["Rivers carry water.", "Aquifers store groundwater."])


def test_a_healthy_library_passes(library: Library, document_id):
    assert library.check() == []


def test_a_passage_removed_from_the_search_index_is_reported(
    library: Library, raw_connection, document_id
):
    rowid, text = raw_connection.execute("SELECT id, search_text FROM passages LIMIT 1").fetchone()
    raw_connection.execute(
        "INSERT INTO passages_fts (passages_fts, rowid, search_text) VALUES ('delete', ?, ?)",
        (rowid, text),
    )

    assert library.check() == ["The keyword-search index does not match the passages."]


def test_a_search_entry_with_no_passage_is_reported(library: Library, raw_connection, document_id):
    raw_connection.execute(
        "INSERT INTO passages_fts (rowid, search_text) VALUES (9999, 'text that is in no passage')"
    )

    assert library.check() == ["The keyword-search index does not match the passages."]


def test_a_passage_edited_behind_the_index_is_reported(
    library: Library, raw_connection, document_id
):
    raw_connection.execute("DROP TRIGGER passages_fts_update")
    raw_connection.execute(
        "UPDATE passages SET text = 'Something else entirely.', "
        "search_text = 'Something else entirely.'"
    )

    assert "The keyword-search index does not match the passages." in library.check()


def test_an_orphan_written_with_foreign_keys_off_is_reported(library: Library, library_path):
    careless = sqlite3.connect(library_path, isolation_level=None)
    careless.execute(
        "INSERT INTO documents (id, class_id, filename, sha256, page_count, original) "
        "VALUES ('d', 'no-such-class', 'notes.pdf', ?, 1, x'25504446')",
        ("a" * 64,),
    )
    careless.execute(
        "INSERT INTO pages (document_id, page_count, page_or_slide, text) VALUES ('d', 1, 1, '')"
    )
    careless.close()

    assert library.check() == ["A row in documents points at a classes row that does not exist."]
