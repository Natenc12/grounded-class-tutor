"""The one `live` (paid) test — a minimal probe whose real deliverable is the MARKER MACHINERY.

Epic #9 flagged that #8 creates the suite's first paid test, and that the `live` mark must be
DERIVED the moment that happens, the way `db` is — because hand-declared it fails in two
directions at once (pyproject.toml's own registration text). This file is that first carrier:
it takes `live_openai_embedder`, and the `live_` prefix is what applies the mark
(tests/conftest.py `pytest_collection_modifyitems`). There is no `@pytest.mark.live` anywhere in
this file, on purpose — if the derivation breaks, `pytest -m live --collect-only` goes to zero
carriers and exit code 5, which is the loudest available signal that the fence around CI's
`-m "not live"` gate has a hole in it.

Kept to ONE round-trip on the cheapest endpoint, deliberately: the full end-to-end proof over
the smoke suite lives in `scripts/ask_smoke.py` (run by hand, per run, with its cost in plain
sight), not in a test that every `pytest` habit would silently re-bill.
"""
from __future__ import annotations

from gct.config import ACTIVE_EMBEDDING_MODEL_ID, EMBEDDING_DIM


class TestLiveEmbeddingProbe:
    def test_embedding_round_trip_matches_the_config_anchor(self, live_openai_embedder):
        """One real embed call: right count, right dim, right model — the ADR 0018 anchor live.

        The assertions mirror `scripts/smoke_slice0.py`'s embedding check; what is NEW here is
        that they run inside the marked, derivable test suite, so `pytest -m live -q` is a real
        command with a real carrier instead of exit-code-5 trivia.
        """
        [vector] = live_openai_embedder.embed(["cosmogony is an account of world-origins"])

        assert len(vector) == live_openai_embedder.dim == EMBEDDING_DIM
        assert live_openai_embedder.model_id == ACTIVE_EMBEDDING_MODEL_ID
