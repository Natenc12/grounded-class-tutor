"""Unit tests for the pure compose stage (issue #4, ADR 0018 stamp / 0019 never-span).

`compose` is the no-DB half of the pipeline: parse -> chunk -> embed -> build `PreparedChunk`s.
Tested with a real generated PDF (`pdf_factory`) and the deterministic `fake_embedder` stub, so no
network / no Postgres. The DB-backed `index_file`/`ingest_file` tests live elsewhere
(test_index.py).
"""

from __future__ import annotations

import pytest

from gct.ingest.chunk import chunk_units
from gct.ingest.parse import ParseError, parse_file
from gct.ingest.pipeline import PreparedChunk, compose, ingest_file

OWNER_ID = "test-owner"
CLASS_ID = "test-class"


def test_compose_stamps_provenance_model_and_embeds_all(pdf_factory, fake_embedder):
    """One `PreparedChunk` per `TextChunk`, in order, with provenance + scope + stamp + embedding.

    Two short pages -> one chunk each (text under `CHUNK_SIZE_WORDS`). Compares compose's output
    directly against the real `chunk_units(parse_file(...))` so the alignment (order + provenance)
    is proven against the actual chunker, not a hand-guessed count.
    """
    path = pdf_factory("lecture.pdf", ["alpha beta gamma", "delta epsilon"])
    expected_chunks = chunk_units(parse_file(path))

    prepared = compose(path, OWNER_ID, CLASS_ID, embedder=fake_embedder)

    assert len(prepared) == len(expected_chunks)
    for p, c in zip(prepared, expected_chunks, strict=True):
        assert isinstance(p, PreparedChunk)
        # Provenance carried through unchanged (citation spine, honor-point ①).
        assert p.text == c.text
        assert p.file == c.file
        assert p.page_or_slide == c.page_or_slide
        # Scope stamped on every row (F6/F12 isolation).
        assert p.owner_id == OWNER_ID
        assert p.class_id == CLASS_ID
        # Model id sourced from the embedder that produced the vectors (ADR 0018), not hardcoded.
        assert p.embedding_model_id == fake_embedder.model_id
        # Right dimension, and the vector is exactly the one the embedder returns for THIS text
        # (proves per-chunk order alignment, not just count).
        assert len(p.embedding) == fake_embedder.dim
        assert p.embedding == fake_embedder.embed([p.text])[0]


def test_compose_propagates_parse_error_on_empty_file(pdf_factory, fake_embedder):
    """A zero-text file raises `ParseError` from `parse_file`, and compose lets it fly untouched
    (terminal-failure *handling* is Slice 2's job, ADR 0020)."""
    path = pdf_factory("blank.pdf", [None])  # genuinely blank page, no text drawn

    with pytest.raises(ParseError):
        compose(path, OWNER_ID, CLASS_ID, embedder=fake_embedder)


class _ExplodingEmbedder:
    """An `Embeddings`-shaped stub whose `embed` always raises - the failure injection for
    `test_ingest_file_embed_failure_leaves_db_untouched` below (issue #42)."""

    model_id = "exploding-embedder"
    dim = 1

    def embed(self, texts):
        raise RuntimeError("embedder exploded")


def test_ingest_file_embed_failure_leaves_db_untouched(pdf_factory, db):
    """ADR 0020's property ("an embed failure leaves the DB untouched") holds only by STATEMENT
    ORDER inside `ingest_file`: `compose` (embed included) is pure and fully precedes the only
    transaction (`index_file`, in `index.py`) - nothing currently goes red if that ordering ever
    breaks. It's exactly the shape a Slice-2 worker will be tempted to break, e.g. pre-creating the
    `files` row as `processing` before `compose` runs. An embedder that raises must leave zero
    `files` rows AND zero `chunks` rows for this owner - if a future worker pre-writes a row before
    `compose`, this goes red."""
    conn, owner_id, class_id = db
    path = pdf_factory("lecture.pdf", ["alpha beta gamma"])

    with pytest.raises(RuntimeError):
        ingest_file(path, owner_id, class_id, embedder=_ExplodingEmbedder(), conn=conn)

    files_count = conn.execute(
        "select count(*) from files where owner_id = %s", (owner_id,)
    ).fetchone()[0]
    chunks_count = conn.execute(
        "select count(*) from chunks where owner_id = %s", (owner_id,)
    ).fetchone()[0]
    assert files_count == 0
    assert chunks_count == 0


def test_ingest_file_end_to_end(pdf_factory, fake_embedder, db):
    """The top-level Slice-1 entry: a real file goes through parse→chunk→embed→index and lands as a
    queryable, `ready` file. Returns the minted `file_id`; chunk rows are present under it."""
    conn, owner_id, class_id = db
    path = pdf_factory("lecture.pdf", ["alpha beta gamma", "delta epsilon"])

    file_id = ingest_file(path, owner_id, class_id, embedder=fake_embedder, conn=conn)

    assert isinstance(file_id, str)
    status = conn.execute(
        "select status from files where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    assert status == "ready"

    n_chunks = conn.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    # One chunk per short page (text under CHUNK_SIZE_WORDS) — matches the real chunker.
    assert n_chunks == len(chunk_units(parse_file(path)))


# --- Fixture reliability (issue #23) ----------------------------------------------------------


def test_fake_embedder_is_stable_across_processes(fake_embedder):
    """`FakeEmbeddings` maps a text to the SAME vector in every process, forever.

    It previously seeded from `hash(text) % 1000`. `hash()` is randomized per process, and the
    modulo collapsed the space to 1000 buckets — so two distinct chunk texts collided on roughly
    0.1% of pairs, and when they did, the alignment assertion in
    `test_compose_stamps_provenance_model_and_embeds_all` passed VACUOUSLY. That flake is worst
    exactly while the suite is being leaned on daily, which is why #23 replaced it with sha256.

    Assert against a hard-coded expected value (not just self-consistency within one run): a future
    "improvement" that reintroduces per-process randomization must fail here, loudly.
    """
    # sha256(b"hello world")[:6] as a big-endian int — computed once, hardcoded here so a future
    # regression to a randomized/process-local hash fails loudly instead of just staying
    # "consistent".
    vector = fake_embedder.embed(["hello world"])[0]
    assert vector[0] == 203741030093645.0
    assert vector[1:] == [0.0] * (fake_embedder.dim - 1)
