"""Tests for the retriever's pipeline stages (issue #5).

- `TestToScore` - pure, no DB, no network. `_to_score` maps pgvector's `<=>` cosine distance
  (real range [0,2], not the ADR's assumed [0,1]) to a normalized [0,1] similarity, clamped at
  zero (ADR 0017 + Decision 1).
- `TestAssertEmbeddingConsistency` - DB-backed. The ADR-0018 guard, including the mixed-model
  case (roadmap PM-5) and the empty-class short-circuit.
- `TestRetrieve` - DB-backed, end-to-end through the whole pipeline: ranking + provenance,
  the F6/F12 isolation filter, and the failure table (empty class, short corpus, provider
  error).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence

import pytest

from gct.config import ACTIVE_EMBEDDING_MODEL_ID
from gct.providers.base import TransientEmbeddingError
from gct.retriever.retrieve import (
    EmbeddingModelMismatchError,
    RetrievedChunk,
    _assert_embedding_consistency,
    _to_score,
    retrieve,
)


class TestToScore:
    """score = max(0.0, 1.0 - distance) - normalized, clamped, order-preserving."""

    def test_to_score_normalizes_and_clamps(self):
        """0.0 -> 1.0 (identical direction), 1.0 -> 0.0 (orthogonal), and the clamp: 2.0 (opposite
        direction) -> 0.0, not the un-clamped -1.0 that would break the [0,1] contract. Also checks
        monotonicity: a smaller distance never yields a lower score than a larger one."""
        assert _to_score(0.0) == 1.0
        assert _to_score(1.0) == 0.0
        assert _to_score(2.0) == 0.0  # clamped - bare `1 - distance` would give -1.0

        d1, d2 = 0.3, 0.9
        assert d1 < d2
        assert _to_score(d1) >= _to_score(d2)

    def test_to_score_clamps_nan_to_zero(self):
        """A NaN distance yields 0.0, not NaN - and that depends on `max`'s ARGUMENT ORDER.

        A zero vector on either side makes pgvector's `<=>` return NaN. Every comparison against
        NaN is False, so `max` keeps whichever argument it saw first: `max(0.0, 1.0 - nan)` is
        0.0, but the more natural-reading `max(1.0 - nan, 0.0)` is nan. This test exists so that
        rewrite fails loudly instead of leaking nan into `RetrievedChunk.score`, which ADR 0017
        contracts as [0,1] and the Grounder consumes.
        """
        nan = float("nan")
        assert _to_score(nan) == 0.0
        assert not math.isnan(_to_score(nan))
        # The rewrite this guards against, shown to be genuinely different:
        assert math.isnan(max(1.0 - nan, 0.0))


class TestAssertEmbeddingConsistency:
    """The ADR-0018 guard: stored `chunks.embedding_model_id` vs the ACTIVE embedder.

    DB-backed: taking the `db` fixture earns the `db` marker automatically (tests/conftest.py).
    CI runs these against its service container; a skip here locally means the DB is down,
    not a pass.
    """

    def test_guard_passes_on_matching_model(self, db, ranking_embedder, seed_chunks):
        """Stored stamp == the active embedder's model_id -> True (proceed to the search)."""
        conn, owner_id, class_id = db
        seed_chunks(["alpha", "beta"], embedder=ranking_embedder)

        assert (
            _assert_embedding_consistency(
                conn, owner_id=owner_id, class_id=class_id, embedder=ranking_embedder
            )
            is True
        )

    def test_guard_raises_on_mismatch(self, db, ranking_embedder, seed_chunks):
        """A class stamped by a DIFFERENT model fails loud - never a silent garbage ranking.

        This is the test that proves the guard is real, and the stamp is chosen to make it
        discriminating: the seeded stamp is `config.ACTIVE_EMBEDDING_MODEL_ID` itself, which is
        NOT the active embedder here. A guard that compared the stored set against config would
        see a match and pass - the "guard that can never fire" bug (Risk #1). Only a guard whose
        right-hand side is `embedder.model_id` raises.
        """
        conn, owner_id, class_id = db
        assert ranking_embedder.model_id != ACTIVE_EMBEDDING_MODEL_ID
        seed_chunks(["alpha"], embedder=ranking_embedder, model_id=ACTIVE_EMBEDDING_MODEL_ID)

        with pytest.raises(EmbeddingModelMismatchError):
            _assert_embedding_consistency(
                conn, owner_id=owner_id, class_id=class_id, embedder=ranking_embedder
            )

    def test_guard_raises_on_mixed_models(self, db, ranking_embedder, seed_chunks):
        """A partially re-indexed class - two stamps in ONE class - ERRORs the whole class.

        Roadmap PM-5's foot-gun. Note HALF the corpus carries the correct stamp, so a guard that
        inspected only the k returned rows could miss it; the whole-class DISTINCT cannot
        (Decision 2).
        """
        conn, owner_id, class_id = db
        seed_chunks(["fresh"], embedder=ranking_embedder)
        seed_chunks(["stale"], embedder=ranking_embedder, model_id="text-embedding-ada-002")

        with pytest.raises(EmbeddingModelMismatchError):
            _assert_embedding_consistency(
                conn, owner_id=owner_id, class_id=class_id, embedder=ranking_embedder
            )

    def test_guard_reports_empty_class(self, db, ranking_embedder):
        """No chunks -> False (not an exception, not True), so the caller returns [] without
        paying for a query embedding. Nothing stored means nothing to mismatch."""
        conn, owner_id, class_id = db

        assert (
            _assert_embedding_consistency(
                conn, owner_id=owner_id, class_id=class_id, embedder=ranking_embedder
            )
            is False
        )

    def test_guard_is_scoped_to_this_owner_and_class(self, db, ranking_embedder, seed_chunks):
        """A mismatched stamp in ANOTHER owner's class must not fire this class's guard (F6/F12).

        The guard's DISTINCT carries the full scope; a guard missing the filter would see the
        other tenant's `wrong-model` stamp and raise. (The full isolation test is `retrieve`'s.)
        """
        conn, owner_id, class_id = db
        seed_chunks(["mine"], embedder=ranking_embedder)
        seed_chunks(
            ["theirs"],
            embedder=ranking_embedder,
            owner_id=f"other-owner-{uuid.uuid4()}",
            class_id=str(uuid.uuid4()),
            model_id="wrong-model",
        )

        assert (
            _assert_embedding_consistency(
                conn, owner_id=owner_id, class_id=class_id, embedder=ranking_embedder
            )
            is True
        )


class CountingEmbeddings:
    """Wraps an embedder and counts `embed` calls, to prove a call did NOT happen."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    @property
    def dim(self) -> int:
        return self._inner.dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return self._inner.embed(texts)


class ExplodingEmbeddings:
    """Raises `TransientEmbeddingError` on `embed`, as the real adapter does on a rate limit.

    `model_id` mirrors the seeded stamp so the ADR-0018 guard PASSES and the failure lands on
    the embed step - otherwise the test would prove the wrong thing.
    """

    def __init__(self, model_id: str, dim: int) -> None:
        self._model_id = model_id
        self._dim = dim

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise TransientEmbeddingError("rate limited")


class TestRetrieve:
    """The whole pipeline, DB-backed: guard -> embed -> scoped top-k -> scores -> ranked return.

    Angles are chosen well away from zero: a zero vector makes pgvector's `<=>` return NaN,
    which orders unpredictably AND survives `max(0.0, 1.0 - nan)` as nan (Risk #2).
    """

    def test_retrieve_returns_ranked_scored_chunks(self, db, ranking_embedder, seed_chunks):
        """Score-descending, `len <= k`, and provenance carried through intact.

        `ranking_embedder.assign` pins each text to an exact angle, so the expected order is
        dictated rather than hoped for: the closer a chunk's angle is to the question's, the
        smaller its cosine distance and the higher its score.
        """
        conn, owner_id, class_id = db
        question = "what is the argument?"
        ranking_embedder.assign(question, 0.10)
        # Deliberately seeded out of rank order, so a passing test cannot be insertion order.
        ranking_embedder.assign("far", 1.30)
        ranking_embedder.assign("nearest", 0.12)
        ranking_embedder.assign("middle", 0.60)
        seeded = seed_chunks(["far", "nearest", "middle"], embedder=ranking_embedder)
        by_text = {chunk.text: chunk for chunk in seeded}

        results = retrieve(conn, question, owner_id, class_id, embedder=ranking_embedder, k=2)

        assert len(results) == 2  # k caps it, though 3 chunks exist
        assert all(isinstance(chunk, RetrievedChunk) for chunk in results)
        assert [chunk.text for chunk in results] == ["nearest", "middle"]

        scores = [chunk.score for chunk in results]
        assert scores == sorted(scores, reverse=True)
        assert all(0.0 <= score <= 1.0 for score in scores)

        # Provenance survives the round trip - the citation spine depends on it.
        for chunk in results:
            expected = by_text[chunk.text]
            assert chunk.chunk_id == expected.chunk_id
            assert chunk.file == expected.file
            assert chunk.page_or_slide == expected.page_or_slide
            # The column is `text`; Decision 4 converts back to int on read.
            assert isinstance(chunk.page_or_slide, int)

    def test_retrieve_filters_by_owner_and_class(self, db, ranking_embedder, seed_chunks):
        """THE F6/F12 TEST. Neither another CLASS nor another OWNER leaks into these results.

        Built to genuinely fail if the WHERE clause were dropped: both foreign chunks are pinned
        NEARER the question than the in-scope chunk, so an unscoped search would rank them
        first - the assertion breaks on content, not merely on length.
        """
        conn, owner_id, class_id = db
        question = "what is the argument?"
        ranking_embedder.assign(question, 0.10)
        ranking_embedder.assign("mine", 0.60)  # farthest, yet must be the ONLY result
        ranking_embedder.assign("other-class", 0.11)
        ranking_embedder.assign("other-owner", 0.12)

        seed_chunks(["mine"], embedder=ranking_embedder)
        # Same owner, DIFFERENT class - catches a query scoped on owner_id alone.
        seed_chunks(["other-class"], embedder=ranking_embedder, class_id=str(uuid.uuid4()))
        # Different owner, DIFFERENT class - catches a query scoped on class_id alone.
        seed_chunks(
            ["other-owner"],
            embedder=ranking_embedder,
            owner_id=f"other-owner-{uuid.uuid4()}",
            class_id=str(uuid.uuid4()),
        )

        results = retrieve(conn, question, owner_id, class_id, embedder=ranking_embedder, k=5)

        assert [chunk.text for chunk in results] == ["mine"]

    def test_retrieve_empty_class_returns_empty(self, db, ranking_embedder):
        """An empty/un-ingested class returns [] - and does NOT pay for a query embedding.

        The short-circuit is the point: the guard reports the empty class before step 2, so a
        class with nothing in it never costs an API call (Decision 2's consequence).
        """
        conn, owner_id, class_id = db
        counting = CountingEmbeddings(ranking_embedder)

        results = retrieve(conn, "anything at all", owner_id, class_id, embedder=counting)

        assert results == []
        assert counting.calls == 0

    def test_retrieve_corpus_smaller_than_k(self, db, ranking_embedder, seed_chunks):
        """Fewer chunks than k returns all of them. Short is NOT an error (retriever.md)."""
        conn, owner_id, class_id = db
        question = "what is the argument?"
        ranking_embedder.assign(question, 0.10)
        seed_chunks(["only-one", "only-two"], embedder=ranking_embedder)

        results = retrieve(conn, question, owner_id, class_id, embedder=ranking_embedder, k=5)

        assert len(results) == 2
        assert {chunk.text for chunk in results} == {"only-one", "only-two"}

    def test_retrieve_rejects_nonpositive_k(self, db, ranking_embedder, seed_chunks):
        """`k < 1` raises rather than returning [] - because [] means "empty class" downstream.

        `k=0` used to give `LIMIT 0` -> [] from a perfectly healthy class, and the Grounder
        short-circuits [] to a canned REFUSAL (ADR 0016), so the student would be told their
        class is empty when it is not. `k=-1` used to leak a raw psycopg
        `InvalidRowCountInLimitClause`. Both are now one loud ValueError.
        """
        conn, owner_id, class_id = db
        seed_chunks(["alpha", "beta"], embedder=ranking_embedder)

        for bad_k in (0, -1):
            with pytest.raises(ValueError):
                retrieve(conn, "a question", owner_id, class_id, embedder=ranking_embedder, k=bad_k)

    def test_retrieve_propagates_embedding_error(self, db, ranking_embedder, seed_chunks):
        """A `TransientEmbeddingError` escapes `retrieve` uncaught - no retry, no swallow.

        ADR 0008: the Retriever does not own a retry budget; the Grounder does. Catching it here
        would turn a provider outage into an empty result set, which the Grounder cannot tell
        apart from an empty class.
        """
        conn, owner_id, class_id = db
        seed_chunks(["alpha"], embedder=ranking_embedder)
        exploding = ExplodingEmbeddings(ranking_embedder.model_id, ranking_embedder.dim)

        with pytest.raises(TransientEmbeddingError):
            retrieve(conn, "a question", owner_id, class_id, embedder=exploding)
