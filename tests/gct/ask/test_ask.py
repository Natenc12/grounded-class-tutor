"""Tests for `ask()` - the read path composed end to end (issue #8).

Everything here is OFFLINE and free: a real local Postgres (the `db` fixture) plus a fake
embedder and a scripted generator. That is the whole point of the injection discipline - `ask()`
takes `embedder=` and `generator=` keyword-only, so the suite reaches every branch without a
paid call, and none of these tests carries the `live` mark.

Organised by the claim being defended:

  - `TestHappyPath` - the composition itself: write path -> retrieve -> ground -> cited answer,
    and that `retrieved` really is the evidence the model was shown.
  - `TestEmptyClass` - `[]` flows THROUGH to the Grounder's canned refusal; `ask()` does not
    intercept it, and nothing is paid for.
  - `TestRetrievalErrors` - the seam this module exists to own: the two retrieval-side failures
    that become `state=ERROR` (retriever.md Sec."Failure modes"), never a refusal.
  - `TestPropagation` - the other half of that boundary, and the more important half: what must
    NOT be converted. A caller bug has to look like a caller bug.
  - `TestNesting` - the Grounder's contract survives the wrapper untouched.

Every test that takes `db` is automatically `db`-marked (tests/conftest.py derives the mark from
the fixture). A SKIP here locally means Postgres is down - it is not a pass.
"""
from __future__ import annotations

from collections.abc import Sequence

import pytest

from gct.ask import ERROR_KIND_EMBEDDING_MISMATCH, AskResult, ask
from gct.config import ACTIVE_EMBEDDING_MODEL_ID
from gct.grounder.answer import (
    ERROR_KIND_PROVIDER_TRANSIENT,
    Citation,
    GrounderResult,
    GrounderState,
)
from gct.providers.base import TransientEmbeddingError

QUESTION = "what does the argument from design claim?"

# A well-formed reply against a 2-source context: two cited claims + a complete coverage marker.
# Written to match what the Grounder's prompt asks for, so the happy path lands on GROUNDED
# rather than on INTEGRITY_FLAGGED for a formatting slip.
GOOD_REPLY = (
    "The argument reasons from apparent order to a designer [S1]. "
    "Its analogy is to a watch found on a heath [S2].\n"
    "COVERAGE: complete"
)


class BrokenEmbeddings:
    """Raises a chosen exception on `embed`, while MIRRORING another embedder's identity.

    Mirroring `model_id` is load-bearing, not a convenience: the ADR-0018 guard runs BEFORE the
    query embed, so a stub with its own model_id would be stopped by the guard and the test would
    prove the mismatch path instead of the embed path it named. Seeding is done with the healthy
    embedder, so this only ever sees the QUERY call - which is what the transient test needs.

    The exception is injected so one stub covers both sides of `ask()`'s error boundary: a
    `TransientEmbeddingError` (converted to `state=ERROR`) and a `RuntimeError` (must escape).

    Lives here rather than in conftest.py because these test directories have no `__init__.py`,
    so `from .conftest import ...` would not resolve; the retriever suite keeps its equivalent
    stubs in its test module for the same reason.
    """

    def __init__(self, like, error: Exception) -> None:
        self._model_id = like.model_id
        self._dim = like.dim
        self._error = error
        self.calls = 0

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        raise self._error


class TestHappyPath:
    """Write path -> `retrieve` -> `answer` -> a cited `AskResult`, over a real corpus."""

    def test_ask_returns_grounded_answer_with_resolved_citations(
        self, db, ranking_embedder, seed_class, scripted
    ):
        """The end-to-end claim: what ingest stored is what the ask retrieves, cites, and returns.

        Angles are pinned so the expected top-2 is DICTATED, not hoped for - and the corpus is
        seeded out of rank order, so a passing assertion cannot be insertion order in disguise.
        `k=2` with three chunks present makes the cap observable as well.
        """
        conn, owner_id, class_id = db
        ranking_embedder.assign(QUESTION, 0.10)
        ranking_embedder.assign("far afield", 1.30)
        ranking_embedder.assign("nearest match", 0.12)
        ranking_embedder.assign("middle match", 0.60)
        seeded = seed_class(
            ["far afield", "nearest match", "middle match"], embedder=ranking_embedder
        )
        pages_by_text = {chunk.text: chunk.page_or_slide for chunk in seeded}

        result = ask(
            conn,
            QUESTION,
            owner_id,
            class_id,
            embedder=ranking_embedder,
            generator=scripted(GOOD_REPLY),
            k=2,
        )

        assert result.result.state is GrounderState.GROUNDED
        assert result.result.coverage.complete is True
        assert result.result.integrity.ok is True

        # `retrieved` is the top-k in rank order, capped by k - the eval runner scores
        # expected_sources against exactly this.
        assert [chunk.text for chunk in result.retrieved] == ["nearest match", "middle match"]
        scores = [chunk.score for chunk in result.retrieved]
        assert scores == sorted(scores, reverse=True)

        # Citations resolve through the SERVER-SIDE map, so every cited chunk_id must be one we
        # actually handed the model. This is the assertion that proves `retrieved` is the SAME
        # list the Grounder consumed and not a re-query: a second retrieval would produce
        # different chunk_ids only if the sets diverged, and this catches exactly that.
        retrieved_ids = {chunk.chunk_id for chunk in result.retrieved}
        assert [c.label for c in result.result.citations] == ["S1", "S2"]
        assert all(citation.chunk_id in retrieved_ids for citation in result.result.citations)

        # Provenance survived the whole round trip - the citation spine's honor-point ③ resolving
        # back to what the write path stamped at ①.
        for citation, chunk in zip(result.result.citations, result.retrieved, strict=True):
            assert citation.file == chunk.file == "lecture.pdf"
            assert citation.page_or_slide == chunk.page_or_slide
            assert citation.page_or_slide == pages_by_text[chunk.text]

    def test_ask_passes_the_retrieved_chunks_to_the_generator(
        self, db, ranking_embedder, seed_class, scripted
    ):
        """The prompt the model saw contains the retrieved text - `retrieved` is not a side copy.

        Without this, `AskResult.retrieved` could be a plausible-looking list that had nothing to
        do with the answer, and every other assertion here would still pass.
        """
        conn, owner_id, class_id = db
        seed_class(["a distinctive sentence about teleology"], embedder=ranking_embedder)
        generator = scripted("It is about teleology [S1].\nCOVERAGE: complete")

        result = ask(
            conn, QUESTION, owner_id, class_id, embedder=ranking_embedder, generator=generator
        )

        assert generator.call_count == 1
        prompt = "\n".join(message["content"] for message in generator.calls[0])
        assert len(result.retrieved) == 1
        assert result.retrieved[0].text in prompt


class TestEmptyClass:
    """An empty/un-ingested class flows THROUGH to the Grounder's canned refusal (ADR 0016)."""

    def test_empty_class_refuses_without_generating(self, db, ranking_embedder, scripted):
        """REFUSAL, `retrieved == []`, and the model was NEVER called.

        `call_count == 0` is the load-bearing assertion, not the state: a model handed no sources
        would probably refuse anyway, so asserting only the state would stay green while every
        empty class quietly paid for a call. It also pins the DIVISION OF LABOUR - the refusal
        comes from `answer()`'s empty-retrieval short-circuit, not from a copy of that decision
        inside `ask()`, which is why `ask()` deliberately does not special-case `[]`.
        """
        conn, owner_id, class_id = db
        generator = scripted()  # nothing scripted: any call at all fails the test

        result = ask(
            conn, QUESTION, owner_id, class_id, embedder=ranking_embedder, generator=generator
        )

        assert result.retrieved == []
        assert result.result.state is GrounderState.REFUSAL
        assert result.result.error is None
        assert generator.call_count == 0
        # The guard short-circuits before the paid embed, so an empty class costs nothing at all.
        assert ranking_embedder.calls == 0


class TestRetrievalErrors:
    """Retrieval-side failures become `state=ERROR` here - retriever.md's "fail loud -> ERROR"."""

    def test_embedding_model_mismatch_becomes_error(
        self, db, ranking_embedder, seed_class, scripted
    ):
        """A corpus stamped by a different model -> ERROR, kind `embedding_mismatch`, no call.

        The stamp is chosen to make this DISCRIMINATING, per retriever.md's warning: the seeded
        stamp is `config.ACTIVE_EMBEDDING_MODEL_ID` itself, which is NOT this embedder's
        `model_id`. A guard comparing the stored set against config would see a match and pass
        the ask straight through to a garbage ranking; only a guard whose right-hand side is
        `embedder.model_id` raises, and only then does this ERROR exist to convert.

        ERROR and not REFUSAL is the trust invariant (ADR 0016): a misconfigured corpus is an
        infra fact, and "your materials don't cover this" would be a lie about the materials.
        """
        conn, owner_id, class_id = db
        assert ranking_embedder.model_id != ACTIVE_EMBEDDING_MODEL_ID
        seed_class(
            ["some real content"], embedder=ranking_embedder, model_id=ACTIVE_EMBEDDING_MODEL_ID
        )
        generator = scripted()

        result = ask(
            conn, QUESTION, owner_id, class_id, embedder=ranking_embedder, generator=generator
        )

        assert result.result.state is GrounderState.ERROR
        assert result.result.error is not None
        assert result.result.error.kind == ERROR_KIND_EMBEDDING_MISMATCH
        assert result.retrieved == []
        assert generator.call_count == 0
        # Same field discipline as the Grounder's own ERROR: no prose, no citations, and NO
        # coverage claim - we never got an answer to judge (ADR 0016).
        assert result.result.answer_prose is None
        assert result.result.citations == []
        assert result.result.coverage.complete is False
        assert result.result.coverage.gaps == []

    def test_transient_query_embedding_failure_becomes_error(
        self, db, ranking_embedder, seed_class, scripted
    ):
        """A `TransientEmbeddingError` on the QUERY embed -> ERROR, kind `provider_transient`.

        Seeding uses the healthy embedder, so the failure lands on the query call and nowhere
        else; `BrokenEmbeddings` mirrors its `model_id` so the ADR-0018 guard PASSES and this
        test cannot accidentally re-prove the mismatch path above.

        No retry, by design: the one shared retry budget is generation-side and belongs to the
        Grounder (ADR 0016). `calls == 1` is what pins that - a retry loop sneaking in here would
        show up as 2.
        """
        conn, owner_id, class_id = db
        seed_class(["some real content"], embedder=ranking_embedder)
        exploding = BrokenEmbeddings(ranking_embedder, TransientEmbeddingError("rate limited"))
        generator = scripted()

        result = ask(conn, QUESTION, owner_id, class_id, embedder=exploding, generator=generator)

        assert result.result.state is GrounderState.ERROR
        assert result.result.error is not None
        assert result.result.error.kind == ERROR_KIND_PROVIDER_TRANSIENT
        # The message names the step, so telemetry can tell this from the Grounder's own
        # `provider_transient`, which carries the identical kind.
        assert "query embedding" in result.result.error.message
        assert "rate limited" in result.result.error.message
        assert result.retrieved == []
        assert generator.call_count == 0
        assert exploding.calls == 1  # tried exactly once - no budget of its own


class TestPropagation:
    """The other half of the boundary: what `ask()` must NOT convert into a provider ERROR."""

    def test_non_provider_embedding_error_propagates(
        self, db, ranking_embedder, seed_class, scripted
    ):
        """A `RuntimeError` from the embedder escapes UNWRAPPED - it is not a provider verdict.

        `ask()` catches exactly two types. A broad `except` would render every unclassified
        failure - a terminal provider error, a psycopg fault, one of our own bugs - as a tidy
        "couldn't generate an answer right now", which is both a lie and unfindable. Terminal
        provider errors in particular are deliberately left unwrapped by `providers/base.py`, so
        this seam could not classify them without importing the provider ADR 0013 exists to keep
        it away from.
        """
        conn, owner_id, class_id = db
        seed_class(["some real content"], embedder=ranking_embedder)
        broken = BrokenEmbeddings(ranking_embedder, RuntimeError("bad api key"))
        generator = scripted()

        with pytest.raises(RuntimeError, match="bad api key"):
            ask(conn, QUESTION, owner_id, class_id, embedder=broken, generator=generator)

        assert generator.call_count == 0

    def test_bad_k_raises_rather_than_erroring(self, db, ranking_embedder, seed_class, scripted):
        """`k < 1` is a CALLER BUG and must look like one - `ValueError`, not `state=ERROR`.

        This is the trap the narrow catch exists to avoid, made concrete: swallowing it would
        hand back an ERROR result blaming the provider for a mistake in the call, over a class
        that is perfectly healthy (the retriever's own reason for raising - `LIMIT 0` would
        return `[]` from a healthy class and the Grounder would refuse).
        """
        conn, owner_id, class_id = db
        seed_class(["some real content"], embedder=ranking_embedder)
        generator = scripted()

        for bad_k in (0, -1):
            with pytest.raises(ValueError):
                ask(
                    conn,
                    QUESTION,
                    owner_id,
                    class_id,
                    embedder=ranking_embedder,
                    generator=generator,
                    k=bad_k,
                )

        assert generator.call_count == 0


class TestNesting:
    """`AskResult` WRAPS the Grounder's contract; it does not reshape or reinterpret it."""

    def test_result_is_an_unmodified_grounder_result(
        self, db, ranking_embedder, seed_class, scripted
    ):
        """`result` is a real `GrounderResult`, field types intact - not a flattened lookalike.

        The wrapper exists only to carry `retrieved` alongside it (which `GrounderResult`
        structurally cannot: its `citations` name only the CITED chunks, so it could never tell a
        retrieval miss from a citation miss). Anything beyond that would give the five-state
        contract a second owner.
        """
        conn, owner_id, class_id = db
        seed_class(["teleology and design"], embedder=ranking_embedder)

        result = ask(
            conn,
            QUESTION,
            owner_id,
            class_id,
            embedder=ranking_embedder,
            generator=scripted("Design is claimed [S1].\nCOVERAGE: complete"),
        )

        assert isinstance(result, AskResult)
        assert isinstance(result.result, GrounderResult)
        assert isinstance(result.result.state, GrounderState)
        assert all(isinstance(citation, Citation) for citation in result.result.citations)
        assert isinstance(result.result.coverage.gaps, list)
        assert isinstance(result.result.integrity.reasons, list)
        assert result.result.error is None


class TestRetrievalRan:
    """`retrieval_ran` — the flag that tells "we looked and missed" from "we never looked".

    The eval runner scores a retrieval MISS and a retrieval that NEVER RAN differently, and it
    cannot: `retrieved` is `[]` in both cases. An empty class genuinely retrieved nothing (a
    measured miss); a retrieval-side ERROR never produced a set at all. Scoring the second as the
    first reports a recall failure that did not happen, and drags infra outages into the metric
    the chunking/k/embedder bake-offs rank on (ADR 0023 §1).
    """

    def test_healthy_retrieval_reports_it_ran(self, db, ranking_embedder, seed_class, scripted):
        """The default. Anything that reached the Grounder ran retrieval, by definition."""
        conn, owner_id, class_id = db
        # Two chunks, because GOOD_REPLY cites [S1] AND [S2] — a reply naming a label the context
        # does not have is an integrity failure, and the retry would eat the scripted budget.
        seed_class(
            [
                "the argument from design reasons from apparent order to a designer",
                "its analogy is to a watch found upon a heath",
            ],
            embedder=ranking_embedder,
        )

        result = ask(conn, QUESTION, owner_id, class_id,
                     embedder=ranking_embedder, generator=scripted(GOOD_REPLY))

        assert result.result.state is GrounderState.GROUNDED
        assert result.retrieval_ran is True

    def test_empty_class_ran_retrieval_and_is_a_measured_miss(
        self, db, ranking_embedder, scripted
    ):
        """THE DISCRIMINATING CASE: `retrieved == []` but retrieval absolutely did run.

        This is why the flag cannot be replaced by `not result.retrieved`. An un-ingested class
        returns an empty top-k, and that IS the measurement — the corpus does not cover it. If
        this reported `retrieval_ran=False` the runner would drop a real miss out of the recall
        denominator and inflate the hit rate toward 1.0 by discarding exactly the rows that
        should pull it down.
        """
        conn, owner_id, class_id = db  # nothing seeded
        generator = scripted()

        result = ask(conn, QUESTION, owner_id, class_id,
                     embedder=ranking_embedder, generator=generator)

        assert result.retrieved == []
        assert result.retrieval_ran is True          # ran, and missed
        assert result.result.state is GrounderState.REFUSAL
        assert generator.call_count == 0             # canned refusal, nothing paid for

    def test_embedding_mismatch_reports_retrieval_never_ran(
        self, db, ranking_embedder, seed_class, scripted
    ):
        conn, owner_id, class_id = db
        # Same discriminating stamp as the mismatch test above: the corpus is labelled with the
        # ACTIVE model id, which is not this embedder's own.
        seed_class(["some real content"], embedder=ranking_embedder,
                   model_id=ACTIVE_EMBEDDING_MODEL_ID)

        result = ask(conn, QUESTION, owner_id, class_id,
                     embedder=ranking_embedder, generator=scripted())

        assert result.result.state is GrounderState.ERROR
        assert result.retrieved == []
        assert result.retrieval_ran is False

    def test_transient_query_embed_reports_retrieval_never_ran(
        self, db, ranking_embedder, seed_class, scripted
    ):
        conn, owner_id, class_id = db
        seed_class(["some real content"], embedder=ranking_embedder)
        exploding = BrokenEmbeddings(
            ranking_embedder, TransientEmbeddingError("429 slow down")
        )

        result = ask(conn, QUESTION, owner_id, class_id,
                     embedder=exploding, generator=scripted())

        assert result.result.state is GrounderState.ERROR
        assert result.retrieved == []
        assert result.retrieval_ran is False


class TestErrorShapeTwin:
    """`ask._retrieval_error` is a deliberate local twin of `answer._error` (ask.py docstring).

    That duplication is justified there as "if the ERROR shape ever changes, both sites change
    together — which the ERROR tests on either side will force." As written that was NOT true:
    each suite asserted its own module's fields, so one side could drift with only its own test
    updated. This is the test that actually forces it.
    """

    def test_both_modules_build_field_identical_error_results(self):
        from gct.ask import _retrieval_error
        from gct.grounder.answer import _error

        ours = _retrieval_error("some_kind", "some message")
        theirs = _error("some_kind", "some message")

        assert ours == theirs, (
            "ask._retrieval_error and answer._error have drifted. ADR 0016's ERROR shape has one "
            "meaning; keep the twins field-identical or collapse them."
        )
