"""The library file refuses contradictory data, whoever writes it (#226, closing #191).

On `main`, Postgres let a file be `ready` while carrying a failure reason, and let a chunk name a
different class from its file. Three fixes each closed one way in, and a direct call could still
write the bad row. Here the shape of the schema makes those rows impossible to write:

- a document has no status, because it exists only when it is complete;
- a passage has no class of its own; it gets its class through its document;
- a passage points at a page its document has, and a page points at its document;
- a document's page count and its page rows cannot disagree.

Each test below tries the bad write with plain SQL on a connection that has foreign keys on, the
way every writer to the file must.
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
    return add_document(class_id, ["Rivers carry water.", "", "Aquifers store groundwater."])


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_xinfo({table})")]


def _refused(connection: sqlite3.Connection, *statements: tuple[str, tuple]) -> None:
    connection.execute("BEGIN")
    with pytest.raises(sqlite3.IntegrityError):
        for sql, parameters in statements:
            connection.execute(sql, parameters)
        connection.execute("COMMIT")
    connection.execute("ROLLBACK")


def test_a_document_has_no_status_column(raw_connection):
    """A document row exists only once the whole document is saved (#227 writes it in one
    transaction), so there is no status to store and no "ready but failed" row to write. A
    status, state or failure-reason column here would bring back the contradiction #191 counted.
    If you are adding a column, it must not be one of those; then add it to this list."""
    assert _columns(raw_connection, "documents") == [
        "id",
        "class_id",
        "filename",
        "sha256",
        "page_count",
        "original",
    ]


def test_a_passage_has_no_class_column(raw_connection):
    """A passage reaches its class only through its document, so it cannot name a different class
    from its document's. A class column on passages would be a second copy that can disagree.
    If you are adding a column, it must not name a class; then add it to this list."""
    assert _columns(raw_connection, "passages") == [
        "id",
        "document_id",
        "page_or_slide",
        "ordinal",
        "text",
    ]


def test_a_passage_on_a_page_its_document_does_not_have_is_refused(raw_connection, document_id):
    _refused(
        raw_connection,
        (
            "INSERT INTO passages (document_id, page_or_slide, ordinal, text) "
            "VALUES (?, 4, 0, 'From nowhere.')",
            (document_id,),
        ),
    )


def test_a_passage_with_no_document_is_refused(raw_connection, document_id):
    for missing in (None, "00000000-0000-0000-0000-000000000000"):
        _refused(
            raw_connection,
            (
                "INSERT INTO passages (document_id, page_or_slide, ordinal, text) "
                "VALUES (?, 1, 1, 'Orphan text.')",
                (missing,),
            ),
        )


def test_a_page_with_no_document_is_refused(raw_connection, document_id):
    for missing in (None, "00000000-0000-0000-0000-000000000000"):
        _refused(
            raw_connection,
            (
                "INSERT INTO pages (document_id, page_count, page_or_slide, text) "
                "VALUES (?, 3, 1, 'Orphan page.')",
                (missing,),
            ),
        )


def test_a_page_past_the_documents_page_count_is_refused(raw_connection, document_id):
    for page_count in (3, 4):
        _refused(
            raw_connection,
            (
                "INSERT INTO pages (document_id, page_count, page_or_slide, text) "
                "VALUES (?, ?, 4, 'Page four of three.')",
                (document_id, page_count),
            ),
        )


def test_a_document_missing_some_of_its_pages_is_refused(raw_connection, class_id):
    """A ten-page document must have ten page rows. The document points at its last page, and
    each page after the first points at the page before it, so no page can be missing."""
    document = (
        "INSERT INTO documents (id, class_id, filename, sha256, page_count, original) "
        "VALUES ('d', ?, 'notes.pdf', ?, 3, x'25504446')",
        (class_id, "a" * 64),
    )

    def page(number: int) -> tuple[str, tuple]:
        return (
            "INSERT INTO pages (document_id, page_count, page_or_slide, text) "
            "VALUES ('d', 3, ?, '')",
            (number,),
        )

    _refused(raw_connection, document)
    _refused(raw_connection, document, page(1), page(2))
    _refused(raw_connection, document, page(1), page(3))
    _refused(raw_connection, document, page(2), page(3))


def test_removing_one_page_of_a_document_is_refused(raw_connection, document_id):
    for number in (1, 2, 3):
        _refused(
            raw_connection,
            (
                "DELETE FROM pages WHERE document_id = ? AND page_or_slide = ?",
                (document_id, number),
            ),
        )


def test_a_document_in_no_class_is_refused(raw_connection):
    for missing in (None, "no-such-class"):
        _refused(
            raw_connection,
            (
                "INSERT INTO documents (id, class_id, filename, sha256, page_count, original) "
                "VALUES ('d', ?, 'notes.pdf', ?, 1, x'25504446')",
                (missing, "a" * 64),
            ),
            (
                "INSERT INTO pages (document_id, page_count, page_or_slide, text) "
                "VALUES ('d', 1, 1, '')",
                (),
            ),
        )


def test_the_same_file_twice_in_one_class_is_refused(raw_connection, add_document, class_id):
    add_document(class_id, ["Same bytes."])

    with pytest.raises(sqlite3.IntegrityError):
        add_document(class_id, ["Same bytes."])
    raw_connection.execute("ROLLBACK")


def test_the_same_file_in_two_classes_is_two_documents(library, add_document, class_id, count):
    other = library.create_class("Geology").id

    add_document(class_id, ["Same bytes."])
    add_document(other, ["Same bytes."])

    assert count("documents") == 2
    assert library.check() == []


def test_values_of_the_wrong_type_are_refused(raw_connection, document_id):
    _refused(
        raw_connection,
        (
            "INSERT INTO passages (document_id, page_or_slide, ordinal, text) "
            "VALUES (?, 1, 'second', 'Typed wrong.')",
            (document_id,),
        ),
    )
