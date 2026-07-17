"""Unit tests for the pure compose stage (issue #4, ADR 0018 stamp / 0019 never-span).

`compose` is the no-DB half of the pipeline: parse -> chunk -> embed -> build `PreparedChunk`s.
Tested with a real generated PDF (`pdf_factory`) and the deterministic `fake_embedder` stub, so no
network / no Postgres. The DB-backed `index_file`/`ingest_file` tests live elsewhere (test_index.py).
"""
from __future__ import annotations

import pytest

from gct.ingest.chunk import chunk_units
from gct.ingest.parse import ParseError, parse_file
from gct.ingest.pipeline import PreparedChunk, compose

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
    for p, c in zip(prepared, expected_chunks):
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
