"""Keyword search matches text whatever Unicode form the page or the question uses (#248).

A page's passage keeps exactly what the parser extracted, so citations show it unchanged; only the
text search matches against is put into one form (NFKC).
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest

from gct.ingest.parse import ParsedUnit
from gct.library import Library, OutdatedLibraryError
from gct.library.schema import APPLICATION_ID, STATEMENTS
from gct.search import search


@pytest.fixture
def library_path(tmp_path: Path) -> Path:
    return tmp_path / "course.sqlite3"


@pytest.fixture
def library(library_path: Path) -> Iterator[Library]:
    with Library(library_path) as opened:
        yield opened


@pytest.fixture
def class_id(library: Library) -> str:
    return library.create_class("Philosophy").id


def _save(library: Library, class_id: str, text: str) -> None:
    unit = ParsedUnit(text=text, file="handout.pdf", page_or_slide=1)
    library.save_document(class_id, "handout.pdf", f"%PDF {text}".encode(), 1, [unit])


def _texts(library: Library, class_id: str, question: str) -> list[str]:
    return [chunk.text for chunk in search(library, class_id, question)]


@pytest.mark.parametrize(
    ("page", "question"),
    [
        ("Any modi\ufb01cation of the licence must be speci\ufb01c.", "modification"),
        ("Any modi\ufb01cation of the licence must be speci\ufb01c.", "What is specific?"),
        ("The \ufb02ow of the river is a\ufb00ected by the dam.", "flow"),
        ("The \uff4c\uff41\uff4b\uff45 is fed by \uff29\uff43\uff45 melt.", "lake"),
        ("The \uff4c\uff41\uff4b\uff45 is fed by \uff29\uff43\uff45 melt.", "ice"),
        (unicodedata.normalize("NFD", "The ātman is the self."), "ātman"),
        (unicodedata.normalize("NFD", "Η ψυχή είναι αθάνατη."), "ψυχή"),
        (unicodedata.normalize("NFD", "한국어 수업 자료"), "한국어"),
        ("The ātman is the self.", unicodedata.normalize("NFD", "What is ātman?")),
    ],
    ids=[
        "fi ligature",
        "fi ligature in a question",
        "fl and ff ligatures",
        "fullwidth letters",
        "fullwidth capital",
        "decomposed accent on the page",
        "decomposed Greek on the page",
        "decomposed Korean on the page",
        "decomposed question, composed page",
    ],
)
def test_a_plain_question_finds_a_page_in_another_unicode_form(
    library: Library, class_id: str, page: str, question: str
):
    _save(library, class_id, page)

    assert _texts(library, class_id, question) == [page]


def test_a_passage_is_shown_and_cited_exactly_as_extracted(library: Library, class_id: str):
    page = "The area is x\u00b2 + \u00bd y, a modi\ufb01cation of the \ufb01rst rule."
    _save(library, class_id, page)

    [chunk] = search(library, class_id, "modification")

    assert chunk.text == page
    assert library.passage(class_id, chunk.chunk_id).text == page


def test_a_library_made_before_unicode_forms_is_refused_not_upgraded(library_path: Path):
    old_statements = [
        statement.replace("        search_text TEXT NOT NULL,\n", "")
        .replace("search_text", "text")
        .replace("\n            VALUES", " VALUES")
        for statement in STATEMENTS
    ]
    connection = sqlite3.connect(library_path, isolation_level=None)
    for statement in old_statements:
        connection.execute(statement)
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute("PRAGMA user_version = 1")
    connection.close()
    before = library_path.read_bytes()

    with pytest.raises(OutdatedLibraryError, match="import your course files again"):
        Library(library_path)

    assert library_path.read_bytes() == before
