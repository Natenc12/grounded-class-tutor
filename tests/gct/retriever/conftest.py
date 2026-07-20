"""Fixtures for the retriever suite (issue #5).

Two things the retriever tests need that nothing else in the repo provides:

  - `ranking_embedder` — an `Embeddings` stub whose vectors have genuinely DIFFERENT
    DIRECTIONS, so cosine distance is meaningful and rank order is deterministic and
    assertable. The ingest suite's `FakeEmbeddings` cannot do this: it returns
    `[hash(text) % 1000] + [0.0] * 1535`, i.e. positive scalar multiples of one direction,
    and cosine ignores magnitude — every pairwise distance is 0.

  - `seed_chunks` — inserts `chunks` rows directly via SQL. Retriever tests exercise the
    READ path; making them run a real parse/chunk/embed first would couple them to the
    ingest pipeline and to a live embedding provider.

The `db` fixture comes from the root `tests/conftest.py` by conftest inheritance.
"""
from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from typing import Sequence

import pytest

from gct.config import EMBEDDING_DIM

# Fallback angles live in the first quadrant, comfortably away from both axes and from zero:
# every vector is a unit vector, so no zero vector (which would make cosine distance NaN and
# silently poison ORDER BY) and every pairwise cosine similarity stays positive.
_MIN_ANGLE = 0.05
_MAX_ANGLE = math.pi / 2 - 0.05


class RankingEmbeddings:
    """Deterministic `Embeddings`-shaped stub that produces a MEANINGFUL rank order.

    Each text maps to a unit vector `[cos θ, sin θ, 0, 0, …]` in the first quadrant. Cosine
    similarity between two texts is then exactly `cos(θ₁ - θ₂)`, so a test can predict — and
    assert — the ranking by choosing angles: the closer a chunk's angle is to the query's, the
    higher it ranks.

    Use `assign(text, angle)` for tests that need an exact ordering. Unassigned texts fall back
    to a stable angle derived from an md5 of the text (`hash()` is randomized per process and so
    is NOT reproducible across runs — md5 is).

    `dim` defaults to `EMBEDDING_DIM` (1536) so vectors satisfy the `chunks.embedding
    vector(1536)` column.
    """

    def __init__(self, model_id: str = "fake-embed-3", dim: int = EMBEDDING_DIM) -> None:
        self._model_id = model_id
        self._dim = dim
        self._angles: dict[str, float] = {}

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    def assign(self, text: str, angle: float) -> None:
        """Pin `text` to an exact angle in radians, so a test can dictate rank order."""
        self._angles[text] = angle

    def _angle(self, text: str) -> float:
        if text in self._angles:
            return self._angles[text]
        digest = hashlib.md5(text.encode()).digest()
        unit = int.from_bytes(digest[:8], "big") / float(1 << 64)  # [0, 1)
        return _MIN_ANGLE + unit * (_MAX_ANGLE - _MIN_ANGLE)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            angle = self._angle(text)
            vectors.append([math.cos(angle), math.sin(angle)] + [0.0] * (self._dim - 2))
        return vectors


@pytest.fixture
def ranking_embedder() -> RankingEmbeddings:
    return RankingEmbeddings()


@dataclass(frozen=True)
class SeededChunk:
    """What `seed_chunks` wrote, so a test can assert against known provenance."""

    chunk_id: str
    text: str
    file: str
    page_or_slide: int


@pytest.fixture
def seed_chunks(db):
    """Insert `chunks` rows directly and return the `SeededChunk`s, newest call last.

    Signature: `seed_chunks(texts, *, embedder, file="lecture.pdf", owner_id=None,
    class_id=None, model_id=None)`.

    Defaults to the `db` fixture's scope; pass `owner_id`/`class_id` explicitly to seed a
    SECOND owner or class — which is exactly what the F6/F12 isolation test needs. Embeddings
    come from `embedder`, and `embedding_model_id` is stamped from `embedder.model_id` unless
    `model_id` overrides it (the ADR-0018 mismatch and mixed-model tests use that override).

    Creates the required parent rows per call (`chunks` FKs to `files`, which FKs to `classes`).
    `page_or_slide` is assigned 1..n across `texts`, so provenance is predictable. Any extra
    owner this seeds is cleaned up on teardown — the `db` fixture only knows about its own.
    """
    conn, default_owner, default_class = db
    seeded_owners: set[str] = set()

    def _seed(
        texts: Sequence[str],
        *,
        embedder,
        file: str = "lecture.pdf",
        owner_id: str | None = None,
        class_id: str | None = None,
        model_id: str | None = None,
    ) -> list[SeededChunk]:
        owner = owner_id or default_owner
        klass = class_id or default_class
        stamp = model_id or embedder.model_id
        seeded_owners.add(owner)

        conn.execute(
            """
            insert into classes (class_id, owner_id, name) values (%s::uuid, %s, %s)
            on conflict (class_id) do nothing
            """,
            (klass, owner, "seeded class"),
        )
        file_id = str(uuid.uuid4())
        conn.execute(
            """
            insert into files (file_id, owner_id, class_id, filename, status)
            values (%s::uuid, %s, %s::uuid, %s, 'ready')
            """,
            (file_id, owner, klass, file),
        )

        vectors = embedder.embed(list(texts))
        seeded: list[SeededChunk] = []
        for page, (text, vector) in enumerate(zip(texts, vectors), start=1):
            row = conn.execute(
                """
                insert into chunks
                    (file_id, owner_id, class_id, text, file, page_or_slide,
                     embedding, embedding_model_id)
                values (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s)
                returning chunk_id
                """,
                (file_id, owner, klass, text, file, str(page), vector, stamp),
            ).fetchone()
            seeded.append(
                SeededChunk(chunk_id=str(row[0]), text=text, file=file, page_or_slide=page)
            )
        conn.commit()
        return seeded

    yield _seed

    # The db fixture tears down its OWN owner; anything else this seeded is ours to clean.
    for owner in seeded_owners - {default_owner}:
        conn.execute("delete from chunks where owner_id = %s", (owner,))
        conn.execute("delete from files where owner_id = %s", (owner,))
        conn.execute("delete from classes where owner_id = %s", (owner,))
    conn.commit()
