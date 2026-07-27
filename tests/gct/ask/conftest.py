"""Fixtures for the ask suite (issue #8).

`ask()` is the first box that spans BOTH halves of the read path, so its suite needs one stub
from each side plus a real corpus underneath. pytest exposes a conftest only to its own directory
and below, so the sibling suites' stubs are not importable here - these are deliberate local
twins, shaped to what this suite has to discriminate:

  - `RankingEmbeddings` / `ranking_embedder` - vectors with genuinely different DIRECTIONS, so
    cosine distance is meaningful and rank order is assertable. The ingest suite's
    `FakeEmbeddings` cannot serve: its vectors are positive scalar multiples of ONE direction and
    cosine ignores magnitude, so every pairwise distance is 0 and "rank order" would be whatever
    Postgres felt like returning - a happy-path test asserting rank would pass vacuously.
    (`BrokenEmbeddings`, the failing-embedder stub, lives in `test_ask.py` instead: these
    directories have no `__init__.py`, so a test module cannot import a plain class from its own
    conftest - only fixtures cross that line. The retriever suite splits the same way.)
  - `ScriptedGeneration` / `scripted` - a `Generation` stub that replays queued replies and
    RAISES queued exceptions. It records every call, which is what makes "the generator was NEVER
    called" an assertable fact rather than an assumption - and three of this suite's six tests
    rest on exactly that.
  - `seed_class` - seeds a corpus through the REAL WRITE PATH (`ingest.index_file`), not raw
    INSERTs. That matters here specifically: `ask()`'s happy path is the end-to-end claim that
    what the write path stored is what the read path retrieves and cites, and a fixture that
    hand-wrote the rows would be testing the fixture's idea of the schema instead of the
    pipeline's. It goes through `PreparedChunk` -> `index_file`, i.e. the same atomic transaction
    and the same `embedding_model_id` stamp the real ingest produces (ADR 0018/0020).

The `db` fixture comes from the root `tests/conftest.py` by conftest inheritance; taking it is
what earns a test the `db` marker (tests/conftest.py derives the mark from the fixture).
"""
from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Sequence

import pytest

from gct.config import EMBEDDING_DIM
from gct.ingest.index import index_file
from gct.ingest.pipeline import PreparedChunk
from gct.providers.base import Message

# Fallback angles sit in the first quadrant, away from both axes and from zero: every vector is a
# unit vector, so no zero vector (which makes pgvector's `<=>` return NaN and silently poisons
# ORDER BY) and every pairwise cosine similarity stays positive.
_MIN_ANGLE = 0.05
_MAX_ANGLE = math.pi / 2 - 0.05


class RankingEmbeddings:
    """Deterministic `Embeddings` stub whose vectors produce a PREDICTABLE rank order.

    Each text maps to a unit vector `[cos θ, sin θ, 0, …]`, so cosine similarity between two
    texts is exactly `cos(θ₁ - θ₂)`: pin the angles with `assign` and the ranking is dictated,
    not hoped for. Unassigned texts fall back to a stable md5-derived angle (`hash()` is
    randomized per process and would not reproduce across runs).

    `dim` defaults to `EMBEDDING_DIM` so vectors fit the `chunks.embedding vector(1536)` column;
    `model_id` is settable because the ADR-0018 mismatch test turns on it.
    """

    def __init__(self, model_id: str = "fake-embed-3", dim: int = EMBEDDING_DIM) -> None:
        self._model_id = model_id
        self._dim = dim
        self._angles: dict[str, float] = {}
        self.calls = 0

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
        self.calls += 1
        return [
            [math.cos(self._angle(text)), math.sin(self._angle(text))] + [0.0] * (self._dim - 2)
            for text in texts
        ]


class ScriptedGeneration:
    """Deterministic `Generation` stub: replays `script` in order, one item per `generate` call.

    A `str` item is returned as the model's reply; an `Exception` INSTANCE is raised instead.

    Running off the end of the script fails the test rather than returning a silent default, and
    it fails via `pytest.fail` on purpose: `answer()` deliberately catches broad `Exception`
    around `generate()`, so an `AssertionError` raised here would be swallowed and re-reported as
    a tidy `state=ERROR` - a budget regression coming back disguised as a provider outage.
    `pytest.fail` raises pytest's `Failed`, which derives from `BaseException` and travels
    straight through that except clause.
    """

    def __init__(self, *script: str | Exception, model_id: str = "fake-gen-1") -> None:
        self._script = list(script)
        self._model_id = model_id
        self.calls: list[list[Message]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def generate(self, messages: Sequence[Message]) -> str:
        self.calls.append(list(messages))
        if not self._script:
            pytest.fail(
                f"generate() called {len(self.calls)} time(s) but only "
                f"{len(self.calls) - 1} reply/replies were scripted - the caller exceeded "
                "its generation budget"
            )
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def ranking_embedder() -> RankingEmbeddings:
    return RankingEmbeddings()


@pytest.fixture
def scripted():
    """Factory: `scripted("reply", SomeError("boom"), ...)` -> a `ScriptedGeneration`."""

    def _make(*script: str | Exception, model_id: str = "fake-gen-1") -> ScriptedGeneration:
        return ScriptedGeneration(*script, model_id=model_id)

    return _make


@pytest.fixture
def seed_class(db):
    """Ingest `texts` into the `db` fixture's class through the real write path.

    Signature: `seed_class(texts, *, embedder, file="lecture.pdf", model_id=None)`; returns the
    `PreparedChunk`s written, `page_or_slide` running 1..n so provenance is predictable.

    `model_id` overrides the `embedding_model_id` stamp WITHOUT changing which embedder produced
    the vectors - that override exists solely for the ADR-0018 mismatch test, and it is the only
    thing here that departs from what `ingest_file` would write.

    Stops one step short of `ingest_file` (which would need a generated PDF and the real parser)
    and calls `index_file` directly: the same atomic transaction, the same `files` row flipped to
    `ready`, the same rows - while letting the test pin each chunk's text, and therefore its
    angle, and therefore the rank order it asserts. Teardown is the `db` fixture's, which deletes
    by `owner_id` (chunks -> files -> classes).
    """
    conn, owner_id, class_id = db

    def _seed(
        texts: Sequence[str],
        *,
        embedder,
        file: str = "lecture.pdf",
        model_id: str | None = None,
    ) -> list[PreparedChunk]:
        vectors = embedder.embed(list(texts))
        chunks = [
            PreparedChunk(
                text=text,
                file=file,
                page_or_slide=page,
                embedding=vector,
                owner_id=owner_id,
                class_id=class_id,
                embedding_model_id=model_id or embedder.model_id,
            )
            for page, (text, vector) in enumerate(zip(texts, vectors, strict=True), start=1)
        ]
        index_file(
            conn,
            file_id=str(uuid.uuid4()),
            filename=file,
            owner_id=owner_id,
            class_id=class_id,
            chunks=chunks,
        )
        return chunks

    return _seed
