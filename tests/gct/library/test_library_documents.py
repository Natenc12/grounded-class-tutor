"""Saving, listing, reading and deleting documents, and resolving a citation (#227).

A document arrives as its class, filename, original bytes, real page count and the parsed pages
that have text. The library stores a row for every page (blank ones included, so page numbers in
citations match the file), cuts passages with the existing chunker, and gives each passage a
stable id. The id is derived from the class, the file's content hash, the passage's position and
its text, so re-saving a file gives the same ids, and the same file in two classes never shares
one. A citation resolves only inside the class that asked.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gct.ingest.chunk import CHUNK_SIZE_WORDS
from gct.ingest.parse import ParsedUnit
from gct.library import (
    Document,
    DocumentContentError,
    Library,
    Page,
    SavedDocument,
    UnknownClassError,
    UnknownDocumentError,
    UnknownPassageError,
)

ORIGINAL = b"%PDF-1.7 hydrology lecture"


def _units(pages: dict[int, str]) -> list[ParsedUnit]:
    return [ParsedUnit(text=text, file="ignored.pdf", page_or_slide=n) for n, text in pages.items()]


def _save(library: Library, class_id: str, original: bytes = ORIGINAL, **changes) -> Document:
    return _save_result(library, class_id, original, **changes).document


def _save_result(
    library: Library, class_id: str, original: bytes = ORIGINAL, **changes
) -> SavedDocument:
    arguments = {
        "filename": "lecture-01.pdf",
        "original": original,
        "page_count": 3,
        "units": _units({1: "Rivers carry water.", 3: "Aquifers store groundwater."}),
    }
    arguments.update(changes)
    return library.save_document(class_id, **arguments)


def _passage_ids(connection: sqlite3.Connection, document_id: str) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT stable_id FROM passages WHERE document_id = ? ORDER BY page_or_slide, ordinal",
            (document_id,),
        )
    ]


def _rows(connection: sqlite3.Connection) -> dict[str, int]:
    tables = ("documents", "pages", "passages", "passages_fts_docsize")
    return {t: connection.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def _search(connection: sqlite3.Connection, word: str) -> list[int]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT rowid FROM passages_fts WHERE passages_fts MATCH ?", (word,)
        )
    ]


@pytest.fixture
def hydrology(library: Library) -> str:
    return library.create_class("Hydrology").id


@pytest.fixture
def geology(library: Library) -> str:
    return library.create_class("Geology").id


def test_a_saved_document_is_there_after_reopening(library: Library, hydrology, library_path):
    saved = _save(library, hydrology)
    library.close()

    with Library(library_path) as reopened:
        assert reopened.list_documents(hydrology) == [saved]
        assert saved.page_count == 3
        assert reopened.document_pages(hydrology, saved.id) == [
            Page(1, "Rivers carry water."),
            Page(2, ""),
            Page(3, "Aquifers store groundwater."),
        ]
        [first, second] = _passage_ids(reopened._connection, saved.id)
        assert reopened.passage(hydrology, first).text == "Rivers carry water."
        cited = reopened.passage(hydrology, second)
        assert (cited.document_id, cited.filename, cited.page_or_slide) == (
            saved.id,
            "lecture-01.pdf",
            3,
        )
        assert _search(reopened._connection, "groundwater") != []
        assert reopened.check() == []


def test_a_long_page_becomes_several_passages_on_that_page(library: Library, hydrology):
    long_page = " ".join(f"word{i}" for i in range(CHUNK_SIZE_WORDS * 2))
    saved = _save(library, hydrology, page_count=2, units=_units({2: long_page}))

    rows = library._connection.execute(
        "SELECT page_or_slide, ordinal FROM passages WHERE document_id = ? ORDER BY ordinal",
        (saved.id,),
    ).fetchall()
    assert len(rows) >= 2
    assert rows == [(2, n) for n in range(len(rows))]
    assert library.check() == []


def test_documents_are_listed_by_filename_and_only_for_their_class(
    library: Library, hydrology, geology
):
    b = _save(library, hydrology, b"b", filename="b-notes.pdf")
    a = _save(library, hydrology, b"a", filename="A-slides.pptx")
    _save(library, geology, b"c", filename="c.pdf")

    assert library.list_documents(hydrology) == [a, b]
    with pytest.raises(UnknownClassError):
        library.list_documents("no-such-class")


def test_the_same_file_twice_in_one_class_returns_the_first_and_adds_nothing(
    library: Library, hydrology
):
    first = _save_result(library, hydrology)
    before = _rows(library._connection)

    again = _save_result(library, hydrology, filename="renamed-copy.pdf")

    assert first.already_in_class is False
    assert again == SavedDocument(first.document, already_in_class=True)
    assert _rows(library._connection) == before
    assert library.list_documents(hydrology) == [first.document]


def test_the_same_file_saved_from_two_windows_is_one_document(
    library: Library, hydrology, library_path: Path
):
    with Library(library_path) as other_window:
        first = _save(other_window, hydrology)
    assert _save(library, hydrology) == first
    assert len(library.list_documents(hydrology)) == 1


def test_passage_ids_are_the_same_when_the_file_is_deleted_and_saved_again(
    library: Library, hydrology
):
    first = _save(library, hydrology)
    ids = _passage_ids(library._connection, first.id)
    library.delete_document(hydrology, first.id)

    second = _save(library, hydrology)

    assert second.id != first.id
    assert _passage_ids(library._connection, second.id) == ids
    assert library.passage(hydrology, ids[0]).document_id == second.id


def test_the_same_file_in_two_classes_never_shares_a_passage_id(
    library: Library, hydrology, geology
):
    in_hydrology = _save(library, hydrology)
    in_geology = _save(library, geology)

    hydrology_ids = _passage_ids(library._connection, in_hydrology.id)
    geology_ids = _passage_ids(library._connection, in_geology.id)
    assert set(hydrology_ids).isdisjoint(geology_ids)
    assert library.passage(geology, geology_ids[0]).document_id == in_geology.id


def test_a_passage_id_from_one_class_cannot_be_looked_up_through_another(
    library: Library, hydrology, geology
):
    saved = _save(library, hydrology)
    [passage_id, _] = _passage_ids(library._connection, saved.id)

    with pytest.raises(UnknownPassageError):
        library.passage(geology, passage_id)
    with pytest.raises(UnknownDocumentError):
        library.document_pages(geology, saved.id)
    with pytest.raises(UnknownDocumentError):
        library.delete_document(geology, saved.id)
    assert library.passage(hydrology, passage_id).text == "Rivers carry water."


def test_deleting_a_document_removes_it_from_search_and_citations(library: Library, hydrology):
    kept = _save(library, hydrology, b"kept", units=_units({1: "Wetlands filter runoff."}))
    deleted = _save(library, hydrology)
    deleted_ids = _passage_ids(library._connection, deleted.id)

    library.delete_document(hydrology, deleted.id)

    assert library.list_documents(hydrology) == [kept]
    assert _search(library._connection, "groundwater") == []
    assert _search(library._connection, "wetlands") != []
    for passage_id in deleted_ids:
        with pytest.raises(UnknownPassageError):
            library.passage(hydrology, passage_id)
    with pytest.raises(UnknownDocumentError):
        library.document_pages(hydrology, deleted.id)
    assert library._connection.execute(
        "SELECT count(*) FROM pages WHERE document_id = ?", (deleted.id,)
    ).fetchone() == (0,)
    assert library.check() == []


def test_deleting_an_unknown_document_is_refused(library: Library, hydrology):
    _save(library, hydrology)
    before = _rows(library._connection)

    with pytest.raises(UnknownDocumentError):
        library.delete_document(hydrology, "no-such-document")

    assert _rows(library._connection) == before


def test_saving_into_an_unknown_class_is_refused_and_writes_nothing(library: Library):
    with pytest.raises(UnknownClassError):
        _save(library, "no-such-class")

    assert set(_rows(library._connection).values()) == {0}
    assert not library._connection.in_transaction


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"page_count": 0}, "at least one page"),
        ({"page_count": 2}, "does not fit a document of 2 pages"),
        ({"units": _units({1: "a"}) + _units({1: "b"})}, "appears twice"),
        ({"units": _units({1: "  ", 2: "\n"})}, "no text to search"),
        ({"units": _units({1: "bad \ud800 text"})}, "cannot be saved"),
        ({"filename": "  "}, "needs a filename"),
        ({"original": b""}, "original file's bytes"),
    ],
)
def test_contents_that_do_not_fit_are_refused_and_write_nothing(
    library: Library, hydrology, changes, message
):
    with pytest.raises(DocumentContentError, match=message):
        _save(library, hydrology, **changes)

    assert set(_rows(library._connection).values()) == {0}


def test_a_three_hundred_page_document_with_blank_pages_saves_and_deletes(
    library: Library, hydrology
):
    pages = {n: f"page {n} " + "sediment transport " * 120 for n in range(1, 301) if n % 25}
    saved = _save(library, hydrology, b"%PDF big" * 10_000, page_count=300, units=_units(pages))

    stored = library.document_pages(hydrology, saved.id)
    assert [page.page_or_slide for page in stored] == list(range(1, 301))
    assert stored[24] == Page(25, "")
    assert library.check() == []

    library.delete_document(hydrology, saved.id)
    assert set(_rows(library._connection).values()) == {0}
    assert library.check() == []


@pytest.mark.parametrize("deleted_at_statement", [1, 2, 3])
def test_a_document_deleted_by_another_window_while_its_pages_are_read_is_never_empty(
    library: Library, hydrology, library_path: Path, deleted_at_statement
):
    """Each read sees one moment: the whole document, or the "not in this class" refusal, never
    an empty page list from a check made before the delete and a fetch made after it."""
    saved = _save(library, hydrology)
    _delete_from_another_window_at(library, library_path, deleted_at_statement, saved.id)

    try:
        assert len(library.document_pages(hydrology, saved.id)) == 3
    except UnknownDocumentError:
        pass


@pytest.mark.parametrize("deleted_at_statement", [1, 2, 3])
def test_a_class_deleted_by_another_window_while_it_is_listed_is_never_empty(
    library: Library, hydrology, library_path: Path, deleted_at_statement
):
    saved = _save(library, hydrology)
    _delete_from_another_window_at(library, library_path, deleted_at_statement, hydrology)

    try:
        assert library.list_documents(hydrology) == [saved]
    except UnknownClassError:
        pass


def _delete_from_another_window_at(
    library: Library, library_path: Path, statement: int, row_id: str
) -> None:
    other_window = sqlite3.connect(library_path, isolation_level=None)
    other_window.execute("PRAGMA foreign_keys = ON")
    started = 0

    def delete_when_reached(_: str) -> None:
        nonlocal started
        started += 1
        if started == statement:
            other_window.execute("DELETE FROM documents WHERE id = ?", (row_id,))
            other_window.execute("DELETE FROM classes WHERE id = ?", (row_id,))
            other_window.close()

    library._connection.set_trace_callback(delete_when_reached)
