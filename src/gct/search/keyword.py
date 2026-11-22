from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from gct.contracts import RetrievedChunk
from gct.library import Library, LibraryError, UnknownClassError, UnknownDocumentError

DEFAULT_K = 5

_WORD = re.compile(r"[^\W_]+")

_STOP_WORDS = frozenset(
    """
    a about am an and any are as at be been being but by can could did do does doing for from
    had has have he her here hers him his how i if in into is it its me my of on or our ours
    she should so than that the their theirs them then there these they this those to us was
    we were what when where which who whom whose why will with would you your yours
    """.split()
)

_MATCHES = """
    SELECT passages.stable_id, passages.text, documents.filename, passages.page_or_slide,
        bm25(passages_fts)
    FROM passages_fts
    JOIN passages ON passages.id = passages_fts.rowid
    JOIN documents ON documents.id = passages.document_id
    WHERE passages_fts MATCH :expression
        AND documents.class_id = :class_id
        AND (:document_id IS NULL OR documents.id = :document_id)
    ORDER BY bm25(passages_fts), documents.filename, documents.sha256,
        passages.page_or_slide, passages.ordinal
    LIMIT :k
"""


def search(
    library: Library,
    class_id: str,
    question: str,
    *,
    k: int = DEFAULT_K,
    document_id: str | None = None,
) -> list[RetrievedChunk]:
    if type(k) is not int or k < 1:
        raise ValueError("Ask for at least one passage.")
    expression = _expression(question)
    with _snapshot(library) as connection:
        if not connection.execute("SELECT 1 FROM classes WHERE id = ?", (class_id,)).fetchone():
            raise UnknownClassError("That class is not in this library; refresh the list.")
        if (
            document_id is not None
            and not connection.execute(
                "SELECT 1 FROM documents WHERE id = ? AND class_id = ?", (document_id, class_id)
            ).fetchone()
        ):
            raise UnknownDocumentError("That document is not in this class; refresh the list.")
        if not expression:
            return []
        rows = connection.execute(
            _MATCHES,
            {"expression": expression, "class_id": class_id, "document_id": document_id, "k": k},
        ).fetchall()
    return [
        RetrievedChunk(
            chunk_id=passage_id, text=text, file=filename, page_or_slide=page, score=-bm25
        )
        for passage_id, text, filename, page, bm25 in rows
    ]


def _expression(question: str) -> str:
    words: dict[str, str] = {}
    for word in _WORD.findall(question):
        if word.lower() not in _STOP_WORDS:
            words.setdefault(word.lower(), word)
    return " OR ".join(f'"{word}"' for word in words.values())


@contextmanager
def _snapshot(library: Library) -> Iterator[sqlite3.Connection]:
    failure = "The class could not be searched; try again, and run a check if it keeps happening."
    try:
        connection = sqlite3.connect(
            library.path.absolute().as_uri() + "?mode=rw",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
    except sqlite3.Error as exc:
        raise LibraryError(failure) from exc
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        yield connection
    except sqlite3.Error as exc:
        raise LibraryError(failure) from exc
    finally:
        connection.close()
