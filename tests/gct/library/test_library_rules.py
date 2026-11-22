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


def _refused(connection: sqlite3.Connection, rule: str, *statements: tuple[str, tuple]) -> None:
    connection.execute("BEGIN")
    with pytest.raises(sqlite3.IntegrityError, match=rule):
        for sql, parameters in statements:
            connection.execute(sql, parameters)
        connection.execute("COMMIT")
    connection.execute("ROLLBACK")


def _passage(
    document_id: object, page: object, ordinal: object = 1, stable_id: object = "new-passage"
) -> tuple[str, tuple]:
    return (
        "INSERT INTO passages (stable_id, document_id, page_or_slide, ordinal, text) "
        "VALUES (?, ?, ?, ?, 'Some text.')",
        (stable_id, document_id, page, ordinal),
    )


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
        "stable_id",
        "document_id",
        "page_or_slide",
        "ordinal",
        "text",
    ]


def test_a_passage_on_a_page_its_document_does_not_have_is_refused(raw_connection, document_id):
    _refused(raw_connection, "FOREIGN KEY", _passage(document_id, 4))


def test_a_passage_with_no_document_is_refused(raw_connection, document_id):
    _refused(raw_connection, "NOT NULL", _passage(None, 1))
    _refused(raw_connection, "FOREIGN KEY", _passage("00000000-0000-0000-0000-000000000000", 1))


def test_a_page_with_no_document_is_refused(raw_connection, document_id):
    for missing, rule in ((None, "NOT NULL"), ("00000000-0000-0000-0000-000000000000", "FOREIGN")):
        _refused(
            raw_connection,
            rule,
            (
                "INSERT INTO pages (document_id, page_count, page_or_slide, text) "
                "VALUES (?, 3, 1, 'Orphan page.')",
                (missing,),
            ),
        )


def test_a_page_past_the_documents_page_count_is_refused(raw_connection, document_id):
    for page_count, rule in ((3, "CHECK"), (4, "FOREIGN KEY")):
        _refused(
            raw_connection,
            rule,
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

    _refused(raw_connection, "FOREIGN KEY", document)
    _refused(raw_connection, "FOREIGN KEY", document, page(1), page(2))
    _refused(raw_connection, "FOREIGN KEY", document, page(1), page(3))
    _refused(raw_connection, "FOREIGN KEY", document, page(2), page(3))


def test_removing_one_page_of_a_document_is_refused(raw_connection, document_id):
    for number in (1, 2, 3):
        _refused(
            raw_connection,
            "FOREIGN KEY",
            (
                "DELETE FROM pages WHERE document_id = ? AND page_or_slide = ?",
                (document_id, number),
            ),
        )


def test_a_document_in_no_class_is_refused(raw_connection):
    for missing, rule in ((None, "NOT NULL"), ("no-such-class", "FOREIGN KEY")):
        _refused(
            raw_connection,
            rule,
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

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        add_document(class_id, ["Same bytes."])
    raw_connection.execute("ROLLBACK")


def test_the_same_file_in_two_classes_is_two_documents(library, add_document, class_id, count):
    other = library.create_class("Geology").id

    add_document(class_id, ["Same bytes."])
    add_document(other, ["Same bytes."])

    assert (count("documents"), count("passages")) == (2, 2)
    assert library.check() == []


def test_values_of_the_wrong_type_are_refused(raw_connection, document_id):
    _refused(raw_connection, "cannot store TEXT", _passage(document_id, 1, ordinal="second"))


def test_a_passage_needs_a_stable_id(raw_connection, document_id):
    """#227 fills `stable_id` with an id derived from the content, which citations and
    recordings (#198) rely on. A passage without one could not be cited."""
    _refused(raw_connection, "NOT NULL", _passage(document_id, 1, stable_id=None))
    _refused(raw_connection, "CHECK", _passage(document_id, 1, stable_id=" "))


def test_a_stable_id_names_one_passage_in_the_whole_library(
    library, raw_connection, add_document, class_id
):
    """The same id twice is refused even across classes, so a citation can never resolve to a
    passage in another class. The same file in two classes therefore needs two ids: #227 derives
    them so they differ (the stand-in in conftest includes the class)."""
    other = add_document(library.create_class("Geology").id, ["Rivers carry water."])
    taken = raw_connection.execute("SELECT stable_id FROM passages LIMIT 1").fetchone()[0]

    _refused(raw_connection, "UNIQUE", _passage(other, 1, stable_id=taken))


def test_a_stable_id_lookup_within_a_class_uses_an_index(raw_connection, document_id):
    plan = " ".join(
        row[3]
        for row in raw_connection.execute(
            "EXPLAIN QUERY PLAN SELECT p.text FROM passages p "
            "JOIN documents d ON d.id = p.document_id WHERE p.stable_id = ? AND d.class_id = ?",
            ("x", "y"),
        )
    )
    assert "SCAN" not in plan, plan
