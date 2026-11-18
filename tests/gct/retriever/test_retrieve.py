"""Tests for the retriever's pipeline stages (issue #5).

- `TestToScore` - pure, no DB, no network. `_to_score` maps pgvector's `<=>` cosine distance
  (real range [0,2]) to a normalized [0,1] similarity, clamped at zero (ADR 0017, clamped per
  ADR 0024 - which amended 0017's range claim precisely because [0,2] breaks a bare `1 - d`).
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

import psycopg
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
        contracts as [0,1] (clamped per ADR 0024) and the Grounder consumes.
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

    # --- The id boundary (#126) -------------------------------------------------------------
    #
    # `retrieve` bound the caller's `class_id` spelling raw into BOTH of this module's
    # `%(class_id)s::uuid` casts - the whole-class consistency DISTINCT and the top-k. The two
    # tests below are the two directions of the same guard, and neither is optional: an accept-only
    # pin is satisfied by a function that accepts everything, and a refuse-only pin by one that
    # refuses everything.

    # Python's `uuid.UUID` parses four spellings of one id; Postgres's `::uuid` cast parses three.
    # Pinning the split is what keeps the accept test from being trivially green - without it, a
    # passing `urn` case is indistinguishable from a Postgres that quietly started accepting it.
    _POSTGRES_ACCEPTS_THE_CAST = {"plain": True, "braced": True, "hex32": True, "urn": False}

    @pytest.mark.parametrize("spelling", ["plain", "braced", "hex32", "urn"])
    def test_every_uuid_spelling_python_accepts_reaches_the_same_chunks(
        self, db, db_other, ranking_embedder, seed_chunks, spelling
    ):
        """Every spelling of one class_id retrieves exactly what the canonical spelling does.

        BOTH SQL SITES ARE EXERCISED BY THIS, which is why it is one test and not two:
        `_assert_embedding_consistency` runs first and must return True (its own cast), and only
        then does the top-k run (the second cast). Fix one site and leave the other raw and the
        `urn` case still goes red - from whichever cast was left.

        `urn` is the only case sensitive to the fix; Postgres normalises the braced and bare-hex
        spellings itself. The other three pin the OTHER decision - that `retrieve` is LENIENT.
        Swap in `ingest_file`-style strict rejection of any non-canonical spelling and braced,
        hex32 and urn go red together. Two different guards, one parametrization.
        """
        conn, owner_id, class_id = db
        canonical = uuid.UUID(class_id)
        written = {
            "plain": str(canonical),
            "braced": f"{{{canonical}}}",
            "hex32": canonical.hex,
            "urn": canonical.urn,
        }[spelling]
        assert uuid.UUID(written) == canonical, "the spelling under test names a different class"

        # Measured on `db_other`, which is autocommit - a refused cast is its own transaction
        # there and cannot poison anything this test does later.
        try:
            db_other.execute("select %s::uuid", (written,)).fetchone()
            postgres_accepts = True
        except psycopg.errors.InvalidTextRepresentation:
            postgres_accepts = False
        assert postgres_accepts is self._POSTGRES_ACCEPTS_THE_CAST[spelling], (
            f"Postgres's own handling of the {spelling} spelling changed; the premise is stale"
        )

        question = "what is the argument?"
        ranking_embedder.assign(question, 0.10)
        seed_chunks(["alpha chunk", "beta chunk"], embedder=ranking_embedder)

        baseline = retrieve(conn, question, owner_id, str(canonical), embedder=ranking_embedder)
        got = retrieve(conn, question, owner_id, written, embedder=ranking_embedder)

        assert [c.text for c in baseline] == ["alpha chunk", "beta chunk"] or [
            c.text for c in baseline
        ] == ["beta chunk", "alpha chunk"], "the canonical baseline itself retrieved nothing usable"
        assert got == baseline, f"the {spelling} spelling retrieved a different result"

    @pytest.mark.parametrize(
        "bad",
        [
            "intro-to-religion",  # ValueError from the parser
            None,  # TypeError: no hex/bytes/fields/int argument was given
            12345,  # AttributeError: int has no `.replace`
            uuid.UUID("6f1e1b4a-0f0e-4a3e-9a7d-2f0a1b2c3d4e"),  # AttributeError, and the LIKELY one
        ],
        ids=["malformed", "none", "int", "already_parsed"],
    )
    def test_retrieve_refuses_a_non_uuid_class_id_and_leaves_the_connection_usable(
        self, db, ranking_embedder, seed_chunks, bad
    ):
        """The refusal half - and the CONNECTION assertion is the point of this one.

        `retrieve` is a reader with no transaction of its own. Before this, the raw bind meant a
        spelling Postgres refused aborted the implicit transaction psycopg had already opened
        (ADR 0025), so every later statement on that connection failed with
        `InFailedSqlTransaction`: the caller lost the connection, not just the query. That is the
        harm that distinguishes this site from the library's other id boundaries - `enqueue` wraps
        its writes in `conn.transaction()` and psycopg rolls that back cleanly.

        So the transaction is OPENED ON PURPOSE before the call, and the assertions after the
        raise are the measurement: still INTRANS rather than INERROR, a plain statement still
        answers, and a real `retrieve` on the same connection still returns the seeded chunks.
        The last one is what a bare `select 1` cannot show - that the caller's actual work still
        runs.

        Parametrized over all four ways `uuid.UUID` reports a bad argument, because two of them
        (`TypeError`, `AttributeError`) are precisely what escaped the guards this ticket widened
        elsewhere. `already_parsed` is the case that will actually happen: a caller holding the
        `uuid.UUID` psycopg handed it back passes that instead of `str(...)`.
        """
        conn, owner_id, class_id = db
        question = "what is the argument?"
        ranking_embedder.assign(question, 0.10)
        seed_chunks(["alpha chunk", "beta chunk"], embedder=ranking_embedder)

        conn.execute("select 1").fetchone()
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS, (
            "this test's premise is a connection with an OPEN implicit transaction"
        )

        with pytest.raises(ValueError, match=r"retrieve\(\) requires a uuid class_id") as caught:
            retrieve(conn, question, owner_id, bad, embedder=ranking_embedder)

        assert "create_class" in str(caught.value), "the refusal does not name what to pass instead"
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS, (
            "the refusal aborted the caller's transaction - the exact harm this guard prevents"
        )
        assert conn.execute("select 1").fetchone() == (1,)
        still_works = retrieve(conn, question, owner_id, class_id, embedder=ranking_embedder)
        assert {c.text for c in still_works} == {"alpha chunk", "beta chunk"}, (
            "the refusal cost the caller its connection"
        )
        conn.rollback()
