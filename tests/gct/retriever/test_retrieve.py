"""Unit tests for the score-conversion stage (issue #5, ADR 0017 + Decision 1).

Pure function under test - no DB, no network. `_to_score` maps pgvector's `<=>` cosine distance
(real range [0,2], not the ADR's assumed [0,1]) to a normalized [0,1] similarity, clamped at zero.
"""
from __future__ import annotations

from gct.retriever.retrieve import _to_score


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
