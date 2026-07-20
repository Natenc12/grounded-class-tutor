"""Tests for the retriever's pipeline stages (issue #5).

  - `TestToScore` - pure, no DB, no network. `_to_score` maps pgvector's `<=>` cosine distance
    (real range [0,2], not the ADR's assumed [0,1]) to a normalized [0,1] similarity, clamped at
    zero (ADR 0017 + Decision 1).
  - `TestAssertEmbeddingConsistency` - DB-backed. The ADR-0018 guard, including the mixed-model
    case (roadmap PM-5) and the empty-class short-circuit.
"""
from __future__ import annotations

import uuid

import pytest

from gct.config import ACTIVE_EMBEDDING_MODEL_ID
from gct.retriever.retrieve import (
    EmbeddingModelMismatchError,
    _assert_embedding_consistency,
    _to_score,
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


class TestAssertEmbeddingConsistency:
    """The ADR-0018 guard: stored `chunks.embedding_model_id` vs the ACTIVE embedder.

    DB-backed (needs the `db` fixture and real Postgres). Per CLAUDE.md these are NOT marked
    `live`, so they self-skip in CI - a skip here locally means the DB is down, not a pass.
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
