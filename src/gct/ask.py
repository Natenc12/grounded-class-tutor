"""Ask - the read path's entry point: one question, scoped to one owner and one class, taken
from the corpus to a cited answer or an honest refusal (issue #8). Retriever -> Grounder, and
nothing else.

This module is deliberately THIN, and thinness is the contract, not a stage of development. The
two boxes it composes each ship a settled public contract - `retrieve()` returns ranked
`RetrievedChunk[]` with scores plumbed-not-gated (ADR 0008), `answer()` returns one of five
states and never raises for a model failure (ADR 0014/0015/0016). Any judgment added HERE would
be a third place that decides what the student is told, and the two below would immediately have
somewhere to disagree with it. So on the happy path this function retrieves, grounds, and
returns; there is no re-filtering of `retrieved`, no relevance gate, no state rewriting.

What this module DOES own, because nothing below it can: the seam where a RETRIEVAL-side failure
becomes a Grounder-shaped result. `retriever.md` Sec."Failure modes" says a model mismatch and an
embedding-provider error must each "fail loud -> Grounder ERROR", and the Retriever cannot do
that itself - it raises, by design, because swallowing either one would turn a broken corpus into
an empty result set that the Grounder cannot tell apart from an empty class (which it renders as
a REFUSAL: "your materials don't cover this"). `ask()` is the first caller standing on both sides
of that seam, so the conversion lands here. See `ask()`'s docstring for the exact boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from gct.grounder.answer import (
    ERROR_KIND_PROVIDER_TRANSIENT,
    Coverage,
    GrounderError,
    GrounderResult,
    GrounderState,
    Integrity,
    answer,
)
from gct.providers.base import Embeddings, Generation, TransientEmbeddingError
from gct.retriever.retrieve import (
    DEFAULT_K,
    EmbeddingModelMismatchError,
    RetrievedChunk,
    retrieve,
)

# `error.kind` for the ADR-0018 mismatch. It lives HERE, not beside the grounder's two
# `provider_*` constants, because the Grounder never produces it: the condition is detected in
# the Retriever and classified at this seam, so the constant belongs to the box that emits it.
# Clients still switch on `state`; `kind` exists so telemetry can tell a misconfigured corpus
# (re-index needed) from a flaky provider (retry might help).
ERROR_KIND_EMBEDDING_MISMATCH = "embedding_mismatch"


@dataclass(frozen=True)
class AskResult:
    """One ask's outcome: the Grounder's verdict, plus the evidence it was handed.

    PUBLIC CONTRACT - the API adapter and the eval runner both consume this.

    `result` is a `GrounderResult`, NESTED AND UNTOUCHED. Not flattened, not re-wrapped, not
    "enriched": the Grounder's shipped contract (grounder.md Sec.Interface) stays byte-identical
    inside this wrapper, so a caller that already knows how to render a `GrounderResult` keeps
    working, and a change to the five-state contract has exactly one owner.

    `retrieved` is the EXACT top-k list handed to the Grounder, in rank order - the same object,
    not a copy or a filtered view. It exists because the eval runner scores `expected_sources`
    against the whole retrieved set (recall@k, N1), and `GrounderResult` structurally cannot
    carry that: its `citations` name only the chunks the model actually cited, so a run that
    retrieved the right page and failed to cite it is indistinguishable, from the result alone,
    from one that never retrieved it. Those are opposite defects - a grounding failure versus a
    retrieval failure - and telling them apart is the whole point of measuring both.

    `[]` when retrieval returned nothing (empty/un-ingested class, retriever.md) AND on the two
    ERROR paths below, where retrieval never produced a set at all.

    `retrieval_ran` is what tells those two `[]` cases apart, and it exists because `retrieved`
    alone CANNOT. An empty class retrieves nothing and that is a MEASURED MISS - we looked and the
    corpus did not have it. A retrieval-side ERROR never looked at all, and scoring it as a miss
    reports a recall failure that never happened (ADR 0023 Sec.1: the retrieval signal is defined
    over questions where retrieval actually ran). The eval runner needs a tri-state - hit / miss /
    not-applicable - and this flag is the third state's only honest source.

    Set at the SITE THAT KNOWS, not inferred downstream. `state is ERROR and not retrieved` would
    derive the same fact today, but only by coincidence of two other modules' contracts. A flag
    written on the error path cannot become wrong; that inference silently can.
    """

    result: GrounderResult
    retrieved: list[RetrievedChunk]
    retrieval_ran: bool = True


def _retrieval_error(kind: str, message: str) -> GrounderResult:
    """A retrieval-side failure, shaped as the Grounder's transport-level ERROR (ADR 0016).

    Field-for-field the same shape as `answer.py`'s private `_error()`, for the reasons documented
    there. A local twin rather than an import of another module's underscore-private helper: that
    would make its internals our dependency, and this is a handful of literal fields. If the ERROR
    shape changes, both sites change together - which the ERROR tests on either side will force.
    """
    return GrounderResult(
        state=GrounderState.ERROR,
        answer_prose=None,
        citations=[],
        coverage=Coverage(complete=False, gaps=[]),
        integrity=Integrity(ok=True, reasons=[]),
        error=GrounderError(kind=kind, message=message),
    )


def ask(
    conn: psycopg.Connection,
    question: str,
    owner_id: str,
    class_id: str,
    *,
    embedder: Embeddings,
    generator: Generation,
    k: int = DEFAULT_K,
) -> AskResult:
    """Answer `question` from this owner's class corpus, cited - or decline honestly.

    PUBLIC CONTRACT (issue #8). Parameter style mirrors `retrieve()`: the positional four are the
    ASK, while `embedder` and `generator` are INJECTED COLLABORATORS - keyword-only so they can't
    be passed by accident, and so the tests stay offline. `k` defaults to the Retriever's
    `DEFAULT_K`, imported rather than re-declared so the tuned value has one home.

    The happy path is three lines and no decisions:
        retrieve(...) -> answer(question, retrieved, owner_id, generator=generator) -> AskResult

    In particular the EMPTY-RETRIEVAL case is NOT special-cased here. `retrieve()` returning `[]`
    flows into `answer()`, which short-circuits it to the canned REFUSAL with no generation call
    (ADR 0016). Intercepting it here would duplicate that decision in a second place, and the
    copy would be the one that drifted.

    THE ERROR BOUNDARY - what comes back as ERROR, what escapes as an exception, and why:

    Exactly TWO exception types are converted, both from the retrieval step, both to
    `state=ERROR` with `retrieved=[]`:
      - `EmbeddingModelMismatchError` -> kind `embedding_mismatch`. This is retriever.md's
        "fail loud -> Grounder ERROR" sentence, finally executed: the Retriever raises because
        ADR 0018 forbids auto-reconciling, and this is the first caller that can turn that raise
        into the ERROR the spec promises. ERROR, never REFUSAL - a misconfigured corpus is an
        infra fact, and rendering it as "your materials don't cover this" would be a lie about
        the student's materials (ADR 0016).
      - `TransientEmbeddingError` -> kind `provider_transient`, message naming the QUERY EMBED so
        telemetry can tell it from the generation-side transient that carries the same kind.
        NO RETRY here, deliberately: the one shared retry budget is generation-side and belongs
        to the Grounder (ADR 0016), and V1 grants the query embed no budget of its own. Adding
        one would be a second budget in a second box - the latency multiplication ADR 0016
        rejected (N5) - and it belongs in the spec before it belongs in the code.

    EVERYTHING ELSE PROPAGATES, and the narrowness is the design:
      - A TERMINAL provider error (bad key, malformed request) is unclassifiable at this seam.
        `providers/base.py` wraps only the retryable class and leaves terminal errors as the
        provider's own exception types, precisely so nothing above has to know them; enumerating
        them here would re-import the coupling ADR 0013's interface exists to prevent. (The
        Grounder can catch broadly around `generate()` because there it is ONE line, with
        nothing else inside the `try` that could throw.)
      - A broad `except` here would swallow psycopg failures, and our own bugs, into a fake
        provider ERROR: the `ValueError` from `k < 1` would come back as a tidy "couldn't
        generate an answer right now", the exact trap `answer.py`'s step-3 comment warns about.
        A caller bug must look like a caller bug.

    `answer()`'s own failures do NOT reach this boundary at all - it returns them as states,
    never raises (grounder.md; "failure states are RETURNED, not raised"), so a provider outage
    on the generation side arrives as an ordinary `GrounderResult` with `state=ERROR` and rides
    the normal return.

    `conn` MUST come from `gct.db.connect()` - the query vector only adapts to `vector(1536)` on
    a connection with the pgvector type registered.
    """
    try:
        retrieved = retrieve(conn, question, owner_id, class_id, embedder=embedder, k=k)
    except EmbeddingModelMismatchError as err:
        # ADR 0018: the corpus and the active embedder disagree, so every similarity would be
        # garbage while looking structurally perfect. Loud, and shaped as ERROR - not a refusal.
        return AskResult(
            result=_retrieval_error(ERROR_KIND_EMBEDDING_MISMATCH, str(err)),
            retrieved=[],
            retrieval_ran=False,
        )
    except TransientEmbeddingError as err:
        # The query embed failed retryably. We do not retry it (ADR 0016 - the budget is
        # generation-side); the message names the step so this is distinguishable in telemetry
        # from the Grounder's own `provider_transient`.
        return AskResult(
            result=_retrieval_error(
                ERROR_KIND_PROVIDER_TRANSIENT, f"query embedding failed: {err}"
            ),
            retrieved=[],
            retrieval_ran=False,
        )

    # `retrieved` is passed on AND returned - the same list, so what the eval runner scores is
    # exactly what the model was shown. `[]` is not intercepted: `answer()` owns that decision.
    return AskResult(
        result=answer(question, retrieved, owner_id, generator=generator),
        retrieved=retrieved,
    )
