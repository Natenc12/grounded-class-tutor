"""Fixtures for the library tests.

The schema tests put documents in with plain SQL through `raw_connection`, which switches foreign
keys on the way every writer to the file must, rather than through `Library.save_document`. The
rules under test live in the file's schema, so they hold for any writer, not only for the
`Library` methods.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from gct.library import Library


@pytest.fixture
def library_path(tmp_path: Path) -> Path:
    return tmp_path / "course.sqlite3"


@pytest.fixture
def library(library_path: Path) -> Iterator[Library]:
    with Library(library_path) as opened:
        yield opened


@pytest.fixture
def raw_connection(library: Library, library_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(library_path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    yield connection
    connection.close()


@pytest.fixture
def add_document(raw_connection: sqlite3.Connection):
    def add(class_id: str, pages: list[str], *, filename: str = "lecture-01.pdf") -> str:
        return _add_document(raw_connection, class_id, pages, filename=filename)

    return add


def _add_document(
    connection: sqlite3.Connection,
    class_id: str,
    pages: list[str],
    *,
    filename: str = "lecture-01.pdf",
) -> str:
    """Insert one complete document (every page, one passage per page with text) in a transaction.

    This is the shape `Library.save_document` writes: the document row, a row for each of its
    pages including pages without text, and passages only on pages that have text. The passage
    `stable_id` here is a stand-in for the derived id `save_document` gives.
    """
    document_id = str(uuid.uuid4())
    original = ("%PDF-1.7 " + filename + " ".join(pages)).encode()
    sha256 = hashlib.sha256(original).hexdigest()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO documents (id, class_id, filename, sha256, page_count, original) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (document_id, class_id, filename, sha256, len(pages), original),
    )
    for number, text in enumerate(pages, start=1):
        connection.execute(
            "INSERT INTO pages (document_id, page_count, page_or_slide, text) VALUES (?, ?, ?, ?)",
            (document_id, len(pages), number, text),
        )
        if text.strip():
            connection.execute(
                "INSERT INTO passages "
                "(stable_id, document_id, page_or_slide, ordinal, text, search_text) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (f"{class_id}:{sha256}:{number}:0", document_id, number, text, text),
            )
    connection.execute("COMMIT")
    return document_id


@pytest.fixture
def count(raw_connection: sqlite3.Connection):
    def rows(table: str) -> int:
        return raw_connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    return rows
