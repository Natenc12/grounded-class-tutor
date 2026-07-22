"""Pipeline - the pure ingest pipeline: parse -> chunk -> embed -> prepare rows -> index,
composed as ONE unit that is free of any job/queue/lease machinery (the PM-4 seam; Slice 2
wraps this whole unit in the worker shell without rewriting it - issue #4, CLAUDE.md invariants).

"Pure" here means *no queue/status/lease coupling*, NOT *no DB*: the index transaction is part of
this pipeline (`index_file`, in `index.py`). What this module does NOT do: no retry loop, no
backoff, no `jobs` table - `ParseError` (terminal) and `TransientEmbeddingError` (transient)
propagate untouched; classifying is upstream's job, *handling* is Slice 2's (ADR 0011 / 0020).

`compose` is the in-memory half (no DB); `ingest_file` is the top-level entry Slice 1 calls
directly. Every chunk row carries `owner_id`/`class_id` (F6/F12 isolation) and
`embedding_model_id`, stamped from the embedder that produced the vectors (ADR 0018 - the value
the Retriever asserts against).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psycopg

from gct.ingest.chunk import chunk_units
from gct.ingest.index import index_file
from gct.ingest.parse import parse_file
from gct.providers.base import Embeddings

# NB: pipeline importing index_file at runtime is fine - index.py imports PreparedChunk only under
# TYPE_CHECKING, so the edge is one-directional (no circular import).


@dataclass(frozen=True)
class PreparedChunk:
    """One chunk fully prepared for the index write - text + provenance + embedding + scope + stamp.

    The in-memory row set assembled BEFORE the index transaction opens (ADR 0020: "full row set in
    hand" before `BEGIN`). `page_or_slide` stays a scalar `int` here (never-span, ADR 0019) and is
    converted to text at the SQL boundary (the `chunks.page_or_slide` column is `text`). `file_id`
    is a file-level scalar passed to `index_file` separately (not stamped per chunk); `chunk_id` is
    DB-generated. This shape is a public contract the Retriever (#5) reads back - do not drift it.
    """

    text: str
    file: str
    page_or_slide: int
    embedding: list[float]  # dim == EMBEDDING_DIM
    owner_id: str
    class_id: str
    embedding_model_id: str


def compose(
    path: str | Path,
    owner_id: str,
    class_id: str,
    *,
    embedder: Embeddings,
) -> list[PreparedChunk]:
    """Parse -> chunk -> embed -> build the full `PreparedChunk` set, in order. Pure: no DB.

    Runs `parse_file` -> `chunk_units`, embeds every chunk's text via `embedder.embed` (the adapter
    owns sub-batching), and zips the vectors back onto the chunks - asserting one vector per chunk
    so a length/alignment mismatch fails loud rather than mis-citing. Stamps `embedding_model_id`
    from `embedder.model_id` (ADR 0018) and `owner_id`/`class_id` (F6/F12) onto every row.

    `ParseError` (terminal) and `TransientEmbeddingError` (transient) propagate untouched - handling
    is Slice 2's (ADR 0020). An empty parse cannot occur: `parse_file` raises
    `ParseError("empty", ...)` rather than returning `[]`.
    """
    chunks = chunk_units(parse_file(path))
    vectors = embedder.embed([c.text for c in chunks])
    # Alignment guard: one vector per chunk, in order. A mismatch means a chunk would carry the
    # wrong embedding and later be retrieved for text it doesn't match - a silent mis-citation, the
    # exact trust failure this product exists to prevent. Raise (not assert): this guard must hold
    # even under `python -O`, which strips assert statements.
    if len(vectors) != len(chunks):
        raise ValueError(
            f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks"
        )
    return [
        PreparedChunk(
            text=chunk.text,
            file=chunk.file,
            page_or_slide=chunk.page_or_slide,
            embedding=vector,
            owner_id=owner_id,
            class_id=class_id,
            embedding_model_id=embedder.model_id,
        )
        # strict=True is REDUNDANT here on purpose: the guard above already raised on any length
        # mismatch, so this can never fire. It's kept because a bare `zip` truncates silently, and
        # leaving one in the pipeline invites someone to relocate or drop the guard without
        # noticing that the zip was relying on it. Satisfies B905 without a noqa.
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]


def ingest_file(
    path: str | Path,
    owner_id: str,
    class_id: str,
    *,
    embedder: Embeddings,
    conn: psycopg.Connection,
) -> str:
    """Ingest one file end to end and return its generated `file_id`. Slice 1 calls this directly.

    Generates `file_id` (`uuid4`) Python-side so the full chunk row set is complete before the index
    transaction opens (ADR 0020), runs `compose`, then hands the rows to `index_file` for the
    all-or-nothing atomic write (which also creates the minimal `files` row -> `ready`). `conn`
    must be a connection with the pgvector adapter registered - use `gct.db.connect()`.
    """
    file_id = str(uuid4())
    chunks = compose(path, owner_id, class_id, embedder=embedder)
    index_file(
        conn,
        file_id=file_id,
        filename=Path(path).name,
        owner_id=owner_id,
        class_id=class_id,
        chunks=chunks,
    )
    return file_id
