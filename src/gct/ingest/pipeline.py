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

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import psycopg

from gct.config import MAX_INGEST_WORDS
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS, chunk_units
from gct.ingest.index import index_file
from gct.ingest.parse import ParseError, parse_file
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
    # Named `chunk_size`/`chunk_overlap` here but `size`/`overlap` in chunk.py: that module is
    # already "chunk", so the short names are unambiguous there, whereas a bare `size` in a
    # signature that also takes a `path` does not say what it sizes. The asymmetry is deliberate.
    chunk_size: int = CHUNK_SIZE_WORDS,
    chunk_overlap: int = CHUNK_OVERLAP_WORDS,
    max_words: int = MAX_INGEST_WORDS,
) -> list[PreparedChunk]:
    """Parse -> chunk -> embed -> build the full `PreparedChunk` set, in order. Pure: no DB.

    Sub-batching is the embedding adapter's job, not this one's. `embedding_model_id` is stamped
    from `embedder.model_id` - the model that ACTUALLY produced the vectors, never from config
    (ADR 0018); `owner_id`/`class_id` go on every row (F6/F12).

    `chunk_size`/`chunk_overlap` default to the module constants and are forwarded to
    `chunk_units`, which validates them; they are provisional spike parameters (ADR 0019).

    `max_words` is the input ceiling (ADR 0029), defaulting to `config.MAX_INGEST_WORDS`; past it
    this raises `ParseError("too_long", ...)` before embedding anything. See the guard below.

    `ParseError` (terminal) and `TransientEmbeddingError` (transient) propagate untouched - handling
    is Slice 2's (ADR 0020). An empty parse cannot occur: `parse_file` raises
    `ParseError("empty", ...)` rather than returning `[]`.
    """
    units = parse_file(path)
    # The input ceiling (ADR 0029), and its POSITION is the whole point: parsing and chunking are
    # free, `embed` below is the only call that costs money, and it is handed every chunk at once.
    # A ceiling checked after that call bounds nothing at all. Counted over `units` rather than
    # over `chunks` because chunks OVERLAP - overlapped words are billed once but would be counted
    # twice, so a chunk-based count tracks the (provisional, ADR 0019) window rather than the
    # input, and loosens on its own the next time the window moves.
    #
    # `.split()` mirrors `_chunk_one`'s own split exactly, so this measures the literal word stream
    # the chunker is about to consume - not an estimate of it.
    #
    # Raised as `ParseError` even though nothing here parsed: `reason` is what carries meaning, and
    # `worker.py`'s `except ParseError` writes `exc.reason` straight into `files.failed_reason`
    # untranslated (ADR 0020's terminal class). A new exception type would need that path widened
    # to reach the same outcome; this needs no worker change at all.
    total_words = sum(len(unit.text.split()) for unit in units)
    if total_words > max_words:
        raise ParseError(
            "too_long",
            f"{Path(path).name} has {total_words} words, over the {max_words}-word ingest ceiling",
        )
    chunks = chunk_units(units, size=chunk_size, overlap=chunk_overlap)
    vectors = embedder.embed([c.text for c in chunks])
    # Alignment guard: one vector per chunk, in order. A mismatch means a chunk would carry the
    # wrong embedding and later be retrieved for text it doesn't match - a silent mis-citation, the
    # exact trust failure this product exists to prevent. Raise (not assert): this guard must hold
    # even under `python -O`, which strips assert statements.
    if len(vectors) != len(chunks):
        raise ValueError(f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks")
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
    file_id: str | None = None,
    embedder: Embeddings,
    conn: psycopg.Connection,
    chunk_size: int = CHUNK_SIZE_WORDS,
    chunk_overlap: int = CHUNK_OVERLAP_WORDS,
    max_words: int = MAX_INGEST_WORDS,
    publish_guard: Callable[[psycopg.Connection], bool] | None = None,
) -> str:
    """Ingest one file end to end and return its `file_id`. Slice 1 calls this directly.

    `file_id` is minted here (`uuid4`) only when the caller supplies none. Either way it is fixed
    Python-side before `compose` runs, so the full chunk row set is complete before the index
    transaction opens (ADR 0020); the rows then go to `index_file` for the all-or-nothing atomic
    write (which also creates the minimal `files` row -> `ready`). The return is the id actually
    used - supply one and the same id comes back, never a new one.

    Slice 2's worker is the caller that supplies one. `enqueue` created that worker's `files` row as
    `queued` before the job was claimed (ADR 0011), so minting a fresh id here would strand the row
    the student is watching and index the chunks under a second one. With the id supplied,
    `index_file`'s upsert lands on its UPDATE branch instead and one file stays one row.

    `publish_guard` is FORWARDED UNTOUCHED to `index_file`, which is the only thing this function
    does with it (#92, ADR 0030). It belongs here rather than being the worker's own step because
    the guard must run inside the index transaction to mean anything, and that transaction opens
    below this frame - a caller cannot wrap it from outside. The slow parse/chunk/embed work above
    is exactly the window in which a caller's entitlement to publish expires, so the check has to
    be re-asked after that work, not before it.

    `conn` must satisfy TWO requirements, not one:
      - the pgvector adapter is registered - use `gct.db.connect()`;
      - it is NOT already inside a transaction (ADR 0025). Otherwise `index_file`'s transaction
        degrades to a savepoint and this function returns having committed nothing - the chunk set
        is invisible to every other connection and dies with the caller's outer transaction. See
        `index_file`'s docstring for the full precondition and how to satisfy it.

    `chunk_size`/`chunk_overlap` are forwarded to `compose` and default to the module constants.
    They are provisional spike parameters (ADR 0019) - a later caller wrapping this seam (Slice
    2's worker) should not treat their names or existence as settled.

    `max_words` is likewise forwarded: the input ceiling lives in `compose`, one step before the
    only paid call, so a file over it raises `ParseError("too_long", ...)` here with no embedding
    bought and no DB touched (ADR 0029). It is a PRECONDITION, not queue machinery - the PM-4 seam
    holds and Slice 2's worker wraps this unchanged (ADR 0020).

    Raises `ValueError` on a malformed `file_id`, before `compose` runs. Only the FORMAT is
    checked: nothing here or in `index_file` verifies the id names a row this caller may write.
    That scope question is #24, parked on #71 - see the note on the guard below.
    """
    if file_id is None:
        file_id = str(uuid4())
    else:
        # Validated BEFORE `compose`, which parses and then EMBEDS - a paid call. Postgres would
        # reject a malformed id anyway, but only at `index_file`'s `::uuid` cast, i.e. after the
        # embedding run is already bought. This is the cheapest check in the pipeline and it was
        # the last to run; the fix is ordering, not detection. It also gives Slice 2 a clean
        # terminal failure (a bad id never becomes good on retry, ADR 0011) instead of a psycopg
        # error raised from inside the index transaction.
        #
        # STRICT on purpose: Python's `UUID()` parses spellings Postgres rejects (urn:uuid:...,
        # stray hyphens), which would pass a lax `UUID(file_id)` guard and still die at the
        # `::uuid` cast - after the embed was bought. The worker's id arrives from the DB already
        # canonical, so any other spelling (or a `uuid.UUID` instance) here is an upstream bug;
        # refuse it loudly rather than convert it quietly.
        #
        # Only the FORMAT is checkable here. Whether the id names a row this caller may write is a
        # question for the database, and asking it would mean issuing a statement on `conn` before
        # `index_file` opens its transaction - which is precisely the ADR 0025 precondition this
        # function's own docstring forbids breaking. No layer checks it today: a wrong id is
        # silently written into (#24 records the evidence). Where that check belongs - the
        # worker's claim step or the publish step - is #24's question, parked on #71; do not
        # decide it here.
        try:
            canonical = str(UUID(file_id)) == file_id.lower()
        except (ValueError, AttributeError, TypeError):
            canonical = False
        if not canonical:
            raise ValueError(
                f"file_id must be a canonical uuid string (pass str(id)), got {file_id!r}"
            )

    chunks = compose(
        path,
        owner_id,
        class_id,
        embedder=embedder,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        max_words=max_words,
    )
    index_file(
        conn,
        file_id=file_id,
        filename=Path(path).name,
        owner_id=owner_id,
        class_id=class_id,
        chunks=chunks,
        publish_guard=publish_guard,
    )
    return file_id
