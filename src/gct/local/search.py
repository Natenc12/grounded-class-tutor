"""Bounded lexical retrieval, with no model, network, or embedding dependency.

FTS5 BM25 ranks candidates; the public score is reciprocal rank, not cosine
similarity or a calibrated relevance probability. Grounder makes the support
decision. Scope predicates are applied in SQL before the candidate limit.
"""

from __future__ import annotations

import re
import sqlite3

from gct.contracts import RetrievedChunk

MAX_QUESTION_CHARS = 2_000
MAX_QUERY_TERMS = 32
MAX_RETRIEVED_CHUNKS = 8
MAX_CONTEXT_CHARS = 40_000
_STOP_WORDS = frozenset(
    "a an and are as at be by course do does for from give how in is it of on or "
    "that the these this to what which who why with according materials lecture".split()
)


def query_expression(question: str) -> str:
    """Treat the question as text, never as FTS syntax or a SQL fragment."""
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        raise ValueError("Enter a question of 1 to 2,000 characters.")
    try:
        question.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Enter a readable question.") from exc
    terms = []
    seen = set()
    for term in re.findall(r"[^\W_]+", question.casefold(), flags=re.UNICODE):
        if term in _STOP_WORDS or term in seen:
            continue
        seen.add(term)
        terms.append('"' + term.replace('"', '""') + '"')
        if len(terms) == MAX_QUERY_TERMS:
            break
    return " OR ".join(terms)


def retrieve(
    connection: sqlite3.Connection,
    class_id: str,
    question: str,
    *,
    document_id: str | None = None,
    pages: list[int] | None = None,
    k: int = MAX_RETRIEVED_CHUNKS,
) -> list[RetrievedChunk]:
    """Read a bounded class-scoped candidate set from a validated library.

    The Library boundary validates existence, canonical IDs and physical page scope.
    This helper still checks bounds so a direct caller cannot widen its resource use.
    """
    if type(k) is not int or not 1 <= k <= MAX_RETRIEVED_CHUNKS:
        raise ValueError("Retrieve between one and eight passages.")
    if pages is not None and (
        document_id is None
        or not isinstance(pages, list)
        or not 1 <= len(pages) <= 500
        or any(type(page) is not int or not 1 <= page <= 500 for page in pages)
        or len(set(pages)) != len(pages)
    ):
        raise ValueError("Choose distinct physical pages within one document.")
    expression = query_expression(question)
    if not expression:
        return []
    conditions = ["chunks_fts MATCH ?", "d.class_id = ?"]
    parameters: list = [expression, class_id]
    if document_id is not None:
        conditions.append("d.id = ?")
        parameters.append(document_id)
    if pages is not None:
        conditions.append("c.page_or_slide IN (" + ",".join("?" for _ in pages) + ")")
        parameters.extend(pages)
    parameters.append(k)
    rows = connection.execute(
        "SELECT c.id, c.text, d.filename, c.page_or_slide "
        "FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
        "JOIN documents d ON d.id = c.document_id WHERE "
        + " AND ".join(conditions)
        + " ORDER BY bm25(chunks_fts, 1.0, 0.35), d.id, c.page_or_slide, c.ordinal LIMIT ?",
        parameters,
    ).fetchall()
    selected = []
    context_length = 0
    for rank, row in enumerate(rows, start=1):
        chunk_id, text, filename, page = row
        # Count the exact source labels and separators Grounder will render.
        block = f"[S{len(selected) + 1}] ({filename}, p.{page})\n{text}"
        increment = len(block) + (2 if selected else 0)
        if context_length + increment > MAX_CONTEXT_CHARS:
            continue
        selected.append(RetrievedChunk(chunk_id, text, filename, page, 1.0 / rank))
        context_length += increment
    return selected
