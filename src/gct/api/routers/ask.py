"""`POST /ask` - one question, one class, a cited answer or an honest decline (issue #108).

The routing is four lines; the DECISIONS are the ticket. `gct.ask.ask` returns an `AskResult`
wrapping the Grounder's five states (ADR 0014/0015/0016), and nothing below this module has ever
had to say what those states are over HTTP. This module says it, and holds no grounding logic of
its own (ADR 0009): it validates, calls one library callable, and renders.

THE STATE -> STATUS MAP, WHICH IS THIS ROUTE'S TO MAKE. All four GROUNDING states are 200 -
`GROUNDED`, `PARTIAL`, `REFUSAL`, `INTEGRITY_FLAGGED`. A refusal is the product working: the
corpus was searched and it did not cover the question, which is a true answer about the student's
materials and the trust promise the whole system is built on. A 404 would say the request was
wrong when the request was fine. This is the same rule `GET /files/{file_id}` already follows for
a `failed` row, and it is the HTTP face of ADR 0016's "error != refusal".

`ERROR` is the one state that is NOT a 2xx - see `_ERROR_STATUS` below for the split, and
`ask_question`'s docstring for why the envelope rather than a 200 body.

THE BROAD CATCH THIS TICKET ASKS FOR IS ALREADY INSTALLED, ONE LAYER OUT. `ask()` converts exactly
two exception types and propagates everything else on purpose (its docstring argues the case at
length), so this handler really will see raw psycopg errors and terminal provider errors. It does
NOT wrap itself in `try/except Exception`, because `gct.api.errors._unhandled` - registered on the
app by `create_app` - already renders any escaped exception as a 500 `internal` envelope with the
exception text going to the server log and NOT to the client. A local catch could only re-raise
into that same rendering, which would make this module a second writer for it, or invent a message
of its own, which is how `str(exc)` on a psycopg error reaches a browser carrying a DSN. The
boundary is real and it is the app's, not this route's; what this route owes it is to not swallow
anything on the way. Pinned by `test_an_escaped_exception_is_the_shared_500_envelope`.

THE CONNECTION CONTRACT, same as `files.py`: this handler reads (`class_exists`) before it calls
`ask()`, and psycopg opens its implicit transaction on any first statement. `ask()` itself has no
`require_idle` - it only reads - so the trap here is milder than the upload route's, but the
autocommit connection `gct.api.deps.get_conn` yields is still the wiring this shape assumes
(ADR 0025, guarded per ADR 0027).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from gct.api.deps import Conn, Embedder, Generator, OwnerId
from gct.api.errors import ApiError
from gct.ask import ERROR_KIND_EMBEDDING_MISMATCH, ask
from gct.classes import class_exists
from gct.grounder.answer import (
    ERROR_KIND_PROVIDER_TERMINAL,
    ERROR_KIND_PROVIDER_TRANSIENT,
    Citation,
    Coverage,
    GrounderState,
    Integrity,
)

router = APIRouter(prefix="/ask", tags=["ask"])

# A question is a sentence or two. The bound is not about storage - nothing here is stored - it is
# about what gets sent to a model: the question rides in every generation call, so an unbounded
# one is an unauthenticated caller choosing how much a single request costs, and a long enough one
# crowds the retrieved sources out of the context window and degrades exactly the grounding this
# product sells. 2000 characters is roughly a full page of prose - far past any real question and
# far short of a document. Deliberately NOT `gct.config.MAX_INGEST_WORDS`: that is the ceiling on
# a FILE being ingested (ADR 0029), a different quantity on a different path, and borrowing it
# would tie two limits together that have no reason to move together.
MAX_QUESTION_CHARS = 2000

# The HTTP status each `error.kind` renders as. Both kinds a Grounder ERROR can carry, plus the
# one `ask()` mints at the retrieval seam - three constants, two modules, one map.
#   `provider_transient` -> 503: the ONLY one of the three where trying again can work. The
#     Grounder already spent its retry budget before emitting it (ADR 0016), so this means "the
#     provider is still failing", which is what 503 says and what a client should back off on.
#   `embedding_mismatch` -> 500: the corpus was indexed with one embedder and queried with
#     another (ADR 0018). Retrying is futile and the request was well-formed; the class needs
#     re-indexing by an operator. That is a server fault, not a client one.
#   `provider_terminal` -> 500: a bad key or a malformed request to the provider. Same shape -
#     nothing the client sent is wrong, and nothing it can send will change the outcome.
# A kind in NONE of these still ships, via the `.get(...)` default below, so an unknown kind
# cannot become a `KeyError` inside the handler - which `errors._unhandled` would render as a bare
# 500 `internal`, losing the kind the library did tell us. The DEFAULT is the safety net, not the
# guard: `test_every_error_kind_has_a_status` derives the kind set by introspecting both producing
# modules and fails the moment one is added without a status here, which is what stops this map
# going quietly stale behind its own fallback.
_ERROR_STATUS = {
    ERROR_KIND_PROVIDER_TRANSIENT: 503,
    ERROR_KIND_EMBEDDING_MISMATCH: 500,
    ERROR_KIND_PROVIDER_TERMINAL: 500,
}
_UNKNOWN_ERROR_STATUS = 500

# WHAT THE CLIENT READS INSTEAD OF `error.message`, for the kinds whose message is not ours to
# forward. Everything not listed here forwards `GrounderError.message` verbatim, which is the
# designed shape: `schemas.ErrorBody` documents the `kind`/`message` pair as the `GrounderError`
# vocabulary, and for `embedding_mismatch` and `provider_transient` that message is prose THIS
# REPO wrote (`retrieve.EmbeddingModelMismatchError`, `ask._retrieval_error`, `answer._error`),
# naming the remedy where there is one.
#
# `provider_terminal` is the exception, and it is not a style call. Its message is built as
# `f"{type(err).__name__}: {err}"` over a RAW provider exception (`grounder/answer.py`, the broad
# `except` around `generate()`) - by construction the one kind whose text nobody here has read.
# OpenAI's own 401 body quotes back the key prefix it rejected. Forwarding it would do precisely
# what `errors._unhandled` refuses to do, in its own words: ship "psycopg's DSN-bearing messages
# and provider errors to the browser". The library's sentence is not lost: it is WRITTEN TO THE
# SERVER LOG at the substitution site below, which is where a provider's own error text belongs
# and the only place the sentences here promise the operator will find it. Withholding it from
# the client and never recording it anywhere would make that promise false.
_ERROR_MESSAGE_OVERRIDE = {
    ERROR_KIND_PROVIDER_TERMINAL: "We could not generate an answer: the service behind it "
    "refused the request outright. That is a configuration problem on this server, not "
    "something about your question or your materials, and retrying will not change it. "
    "Whoever runs this instance can find the details in the server log.",
}

# The render for an ERROR whose kind this module has never heard of. Its `kind` IS forwarded - a
# machine-readable token the library chose, and hiding it would leave the client with nothing -
# but its message is not, for the same reason `provider_terminal`'s is not: nobody has read the
# text of a message that does not exist yet, so it cannot be vouched for as client-facing.
# Reaching this at all means `test_every_error_kind_has_a_status` should have gone red first.
# Named for this module and configured by nothing here, which is what a library does: whether the
# record is formatted, and where it lands, belongs to the process running the app.
#
# WHAT THAT ACTUALLY MEANS UNDER STOCK UVICORN, because the obvious guess is wrong and was
# checked: uvicorn configures its OWN loggers and leaves the root logger with no handlers, so
# this record does NOT ride uvicorn's handler the way `errors._unhandled`'s traceback does. It
# reaches stderr through `logging.lastResort`, Python's fallback - unformatted, with no level or
# timestamp beside uvicorn's own lines. It escapes the process, which is what the client-facing
# sentence promises, and it escapes ONLY because it is a WARNING: `lastResort` starts at WARNING,
# so the same call at INFO would write nowhere and say nothing about it. Do not lower the level
# here without giving the app a handler first.
logger = logging.getLogger(__name__)

_UNKNOWN_ERROR_MESSAGE = (
    "We could not generate an answer, and this version of the API has no advice for the reason "
    "it was given. Nothing is wrong with your materials. The details are in the server log."
)


class AskRequest(BaseModel):
    """One question, scoped to one class. Single-shot: no conversation id, no history (ADR 0009).

    NO `k`. `DEFAULT_K` is a tuned retrieval default with exactly one home
    (`gct.retriever.retrieve`, tuned in Spike Pass 2), and letting a caller vary it would expose a
    tuning knob with no product meaning, make the answer to one question depend on an
    unauthenticated client's choice, and freeze the top-k into this contract so a later retrieval
    change becomes a breaking one. `ask()`'s `k=` stays available to the eval runner and the
    scripts, which is where varying it is the point.

    `class_id` is a bare `str` with no pydantic constraint on purpose. The route parses it with
    `uuid.UUID` and renders every unusable spelling as one 400 `bad_class_id`; a `min_length` or a
    pattern here would send SOME of those through the 422 `validation` envelope instead, so a
    client would have to handle two different bodies for one mistake.
    """

    class_id: str
    question: str = Field(max_length=MAX_QUESTION_CHARS)

    @field_validator("question")
    @classmethod
    def _reject_blank(cls, question: str) -> str:
        """A blank or whitespace-only question is refused, not trimmed and not answered.

        Refused because there is nothing to ground: V1 has no relevance floor, so an empty
        question still retrieves k chunks from a non-empty class (`retrieve`'s no-floor
        consequence) and the model is then asked to answer it. The student would get a confident,
        cited answer to a question they did not ask, which is the failure this product exists to
        not have.

        Returned VERBATIM rather than stripped, the same choice `create_class` makes about a
        name: this codebase refuses at boundaries instead of quietly converting, so what the model
        is asked is exactly what the student typed. Both directions are pinned -
        `test_a_blank_question_is_the_shared_422_envelope` and
        `test_a_question_reaches_the_model_verbatim`.
        """
        if not question.strip():
            raise ValueError("question must not be blank - send the question the student typed")
        return question


class CitationOut(BaseModel):
    """One resolved `[S#]` - citation-spine honor-point ③ (`grounder.answer.Citation`).

    Field-for-field the library's `Citation`, and that is the point: `label` is the per-request
    ordinal the model was handed, `file` + `page_or_slide` are the provenance the answer surface
    shows the student. `chunk_id` is carried so a client can ask for the exact passage later
    without re-running retrieval; it is a server-side id filled in from OUR map, never from
    anything the model said (ADR 0015), so it discloses nothing the caller did not already own.
    """

    label: str
    file: str
    page_or_slide: int
    chunk_id: str


class CoverageOut(BaseModel):
    """The always-emitted coverage statement (ADR 0014): `complete`, or the list of gaps."""

    complete: bool
    gaps: list[str]


class IntegrityOut(BaseModel):
    """Whether the answer passed V1-structural validation (ADR 0015).

    `ok=False` with populated `reasons` is the INTEGRITY_FLAGGED state's payload, and the client
    MUST render it visibly differently from a clean answer - a hard requirement on the answer
    surface, without which the state is worthless. Carried here so Slice 4's UI has the reasons.
    """

    ok: bool
    reasons: list[str]


class AskResponse(BaseModel):
    """The 200 body: what the Grounder decided, and everything a client needs to render it.

    `state` is the whole output contract (ADR 0014-0016). `GrounderState` subclasses `str`, so it
    serialises to `"GROUNDED"` with no custom encoder.

    NO `error` FIELD. `GrounderError` is populated only for `state=ERROR` (its own docstring), and
    ERROR is never a 2xx here - it leaves through `_ERROR_STATUS` as the shared error envelope. An
    `error: null` on every success body would be a field that can only ever be null, and a client
    that learned to check it would be checking the wrong place for the one case it fires.

    NO `retrieved` FIELD, and this one is a decision rather than an omission. `AskResult.retrieved`
    exists for the EVAL RUNNER: recall@k is scored over the whole retrieved set, because a run that
    retrieved the right page and failed to cite it must be distinguishable from one that never
    retrieved it (ADR 0023; `AskResult`'s own docstring). The HTTP client is Slice 4's answer
    surface (ADR 0012), which renders CITATIONS - the chunks the model actually used. Shipping the
    retrieved set would put the full text of every retrieved chunk on the wire for a client with no
    use for it, and would make the top-k an observable part of this contract, so retuning `k` in
    Spike Pass 2 would become an API change. The eval runner calls `ask()` directly and keeps
    everything it needs.

    NO `class_id`/`question` ECHO, for the reason `FileStatusResponse` carries no `file_id`: the
    caller supplied both, so echoing them adds nothing it does not already hold.
    """

    state: GrounderState
    answer_prose: str | None
    citations: list[CitationOut]
    coverage: CoverageOut
    integrity: IntegrityOut


def _render_citations(citations: list[Citation]) -> list[CitationOut]:
    return [
        CitationOut(label=c.label, file=c.file, page_or_slide=c.page_or_slide, chunk_id=c.chunk_id)
        for c in citations
    ]


def _render_coverage(coverage: Coverage) -> CoverageOut:
    return CoverageOut(complete=coverage.complete, gaps=list(coverage.gaps))


def _render_integrity(integrity: Integrity) -> IntegrityOut:
    return IntegrityOut(ok=integrity.ok, reasons=list(integrity.reasons))


@router.post("", status_code=200)
def ask_question(
    conn: Conn,
    owner: OwnerId,
    embedder: Embedder,
    generator: Generator,
    body: AskRequest,
) -> AskResponse:
    """Answer `question` from this owner's class corpus, cited - or decline honestly. 200.

    THE WHOLE READ PATH OVER HTTP, and this route's half of the slice's exit criterion. One
    library call does the work (`gct.ask.ask`, issue #8); the providers are the app-wide
    singletons (`gct.api.deps`), never built per request - a provider client is a connection pool
    and a config read, and `ask()`'s collaborators are keyword-only precisely so they are injected.

    THE OWNERSHIP CHECK RUNS FIRST, AND IT IS NOT A FORMALITY. An unknown or foreign `class_id` is
    a 404 - never a REFUSAL - because letting it fall through to `ask()` retrieves nothing from a
    class that does not exist, and `answer()` renders zero chunks as the canned refusal "no course
    material was retrieved for this question" (ADR 0016). The student would be told their
    materials do not cover the question, about a corpus that was never there: a false statement on
    the exact surface this product's trust rests on. `class_exists` returns ONE `False` for "does
    not exist" and "belongs to someone else" by construction, so rendering it as 404 cannot leak
    cross-owner existence (F12) - the same 404, byte for byte, as `/files` returns.

    `class_id` IS PARSED ONCE, AT THE TOP, AND THE CANONICAL SPELLING GOES DOWNSTREAM. Same fix as
    `upload_file`, for the same live defect one layer down: `retrieve` binds `class_id` RAW into
    `%(class_id)s::uuid` (both of its queries), and `uuid.UUID` accepts spellings that cast
    rejects - `urn:uuid:<id>` is the demonstrated one (issue #121, which reports the identical
    shape in `enqueue`). Handing the caller's spelling to both calls would pass `class_exists`,
    which canonicalises before it binds, and then abort inside `retrieve` with a raw
    `InvalidTextRepresentation` - a 500 for a request that was entirely valid. Parsing here means
    this route cannot repeat that shape whether or not #121 is ever fixed.

    A non-uuid `class_id` is 400 in the route's own words. `class_exists` does raise `ValueError`
    for it, but its message explains a connection-abort concern a client cannot act on.

    ERROR IS THE ONE STATE THAT LEAVES AS AN EXCEPTION, and that does not contradict ADR 0016's
    "failure states are RETURNED, not raised". `ask()` returns it, as the ADR requires; this line
    is the first place that has ever had to choose an HTTP rendering for it, which is a decision
    the ADR does not make and this route owns. 503/500 is chosen over a 200 body because
    `errors.py` states that every non-2xx the adapter produces is the shared envelope, and a
    client should not have to parse a success body to discover the request did not succeed. The
    `kind` is the library's own token, adopted verbatim rather than re-minted, exactly as
    `upload_file` adopts `StagingError.reason`; so is the message, except where
    `_ERROR_MESSAGE_OVERRIDE` says otherwise.

    `result.error` is non-None whenever `state is ERROR` by contract (`GrounderError`: "populated
    ONLY for state=ERROR"). A None there raises `AttributeError` and lands on the 500 `internal`
    envelope - loud, and the right direction for a broken contract, which is why there is no
    defensive branch inventing a second answer for it.
    """
    try:
        class_uuid = str(uuid.UUID(body.class_id))
    except ValueError as exc:
        raise ApiError(
            400,
            "bad_class_id",
            "class_id must be a uuid. Use the id returned when the class was created.",
        ) from exc
    if not class_exists(conn, class_id=class_uuid, owner_id=owner):
        raise ApiError(
            404,
            "class_not_found",
            "No such class. Check the class id, or create the class before asking about it.",
        )

    # `class_uuid`, never `body.class_id`: `retrieve` binds it straight into a `::uuid` cast.
    result = ask(
        conn, body.question, owner, class_uuid, embedder=embedder, generator=generator
    ).result

    if result.state is GrounderState.ERROR:
        kind = result.error.kind
        message = (
            _ERROR_MESSAGE_OVERRIDE.get(kind, result.error.message)
            if kind in _ERROR_STATUS
            else _UNKNOWN_ERROR_MESSAGE
        )
        if message is not result.error.message:
            # SUBSTITUTED, so the library's own sentence is going nowhere else. `ApiError` is a
            # HANDLED exception - `errors._api_error` renders it and returns - so nothing
            # re-raises afterwards the way an uncaught exception does, and without this line the
            # two sentences above would tell an operator to read a log nothing was written to.
            # The identity test, not `!=`, is what keeps this exact: the two forwarding kinds
            # pass `result.error.message` through unchanged and log nothing, and a substitution
            # that happened to be equal to it would still be a substitution.
            logger.warning(
                "POST /ask returned %s for a %s error; the message withheld from the client "
                "was: %s",
                _ERROR_STATUS.get(kind, _UNKNOWN_ERROR_STATUS),
                kind,
                result.error.message,
            )
        # `detail` stays null: the envelope documents it as optional STRUCTURE, and putting the
        # human sentence there would hand the first client to parse it prose where the schema
        # promised shape (`files.py` makes the same call, for the same reason).
        raise ApiError(_ERROR_STATUS.get(kind, _UNKNOWN_ERROR_STATUS), kind, message)

    return AskResponse(
        state=result.state,
        answer_prose=result.answer_prose,
        citations=_render_citations(result.citations),
        coverage=_render_coverage(result.coverage),
        integrity=_render_integrity(result.integrity),
    )
