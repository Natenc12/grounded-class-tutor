"""`POST /ask` over HTTP (issue #108): what each of the five states renders as, and what the
error envelope is allowed to say.

WHICH TESTS DRIVE THE REAL GROUNDER, AND WHICH DO NOT. Almost every state here is produced by the
SHIPPED pipeline - `retrieve` against real rows in Postgres, then the real `answer()` - with only
the two providers substituted, so what is under test is `retrieve -> answer -> ask -> the route`
end to end. The substituted generator is scripted rather than stubbed: it returns the exact text a
model would have returned for that state (a cited answer, a coverage-only decline, a dangling
label, a raised provider error), and the real parser/validator/decider then produce the state. So
the state -> status map is exercised through the machinery that assigns the states, not against a
hand-built result.

TWO tests cannot do that honestly and say so at their own site: the unknown-`error.kind` fallback
(no code in the repo emits an unknown kind - that is the point of the test) and the
escaped-exception path (`ask()` propagates everything it does not convert, and manufacturing a real
psycopg failure would test psycopg). Those two monkeypatch `gct.api.routers.ask.ask` directly.

The providers are swapped by assigning `api.app.state.generator` / `.embedder` - the same place
`create_app`'s lifespan puts them and the place `deps.get_generator`/`get_embedder` read per
request - so a test can change what the model says without building a second app. NOTHING here
constructs a real client and nothing takes a `live_*` fixture: the suite is offline and free.

Chunks are seeded through `db_other` (autocommit), so the handler's own per-request connection
sees committed rows exactly as it would in production; `db`'s owner-scoped teardown collects them.
No test here asserts that the ROUTE wrote anything, because it writes nothing - `/ask` is the read
path. What is read back on `db_other` is the seed, before the request that depends on it.
"""

from __future__ import annotations

import uuid

import pytest

from gct.api.routers import ask as ask_router
from gct.ask import ERROR_KIND_EMBEDDING_MISMATCH, AskResult
from gct.config import EMBEDDING_DIM
from gct.grounder.answer import (
    ERROR_KIND_PROVIDER_TERMINAL,
    ERROR_KIND_PROVIDER_TRANSIENT,
    Coverage,
    GrounderError,
    GrounderResult,
    GrounderState,
    Integrity,
)
from gct.providers.base import TransientEmbeddingError, TransientGenerationError
from gct.retriever.retrieve import DEFAULT_K

# The embedder stub's `model_id`. It has to match the stamp on the seeded chunks or the ADR-0018
# guard fires - which is a state this file tests deliberately, and must not reach by accident.
STUB_MODEL_ID = "stub-embed"

RESPONSE_KEYS = {"state", "answer_prose", "citations", "coverage", "integrity"}


def _query_vector() -> list[float]:
    """The vector every question embeds to here: the axis the seeded chunks fan out from."""
    return [1.0] + [0.0] * (EMBEDDING_DIM - 1)


def _chunk_vector(rank: int) -> list[float]:
    """Chunk `rank`'s vector - tilted a little further off `_query_vector` for each rank.

    Distinct cosine distances, strictly increasing in `rank`, so `order by distance asc` is a
    DETERMINED order rather than whatever the index happened to return. That matters here: `[S1]`
    in a scripted reply has to resolve to a known chunk for the citation assertions to mean
    anything. A zero vector would make pgvector's distance NaN and the order arbitrary.
    """
    return [1.0, 0.1 * rank] + [0.0] * (EMBEDDING_DIM - 2)


class ScriptedGeneration:
    """A `Generation` that replays a script and records what it was asked.

    Each entry is either text to return or an exception to raise; the last entry repeats, so a
    one-entry script answers every attempt the Grounder's retry budget allows (ADR 0016).
    """

    model_id = "scripted-gen"

    def __init__(self, *script: str | Exception) -> None:
        self.script = list(script)
        self.calls: list[list[dict]] = []

    def generate(self, messages):
        self.calls.append(list(messages))
        entry = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(entry, Exception):
            raise entry
        return entry

    @property
    def prompts(self) -> list[str]:
        """The user message of each attempt - SOURCES block plus QUESTION."""
        return [call[1]["content"] for call in self.calls]


class FixedEmbeddings:
    """An `Embeddings` that returns `_query_vector()`, or raises what it was given."""

    dim = EMBEDDING_DIM

    def __init__(self, *, model_id: str = STUB_MODEL_ID, raises: Exception | None = None) -> None:
        self.model_id = model_id
        self._raises = raises

    def embed(self, texts):
        if self._raises is not None:
            raise self._raises
        return [_query_vector() for _ in texts]


def _seed(db_other, api, *, texts: list[str], model_id: str = STUB_MODEL_ID) -> list[str]:
    """Publish one `files` row and one chunk per text, in rank order; return their chunk_ids.

    Committed through `db_other` (autocommit) rather than the handler's connection - which does
    not exist yet - so the rows are genuinely published before the request runs. `db`'s teardown
    deletes by owner, and every row here carries `api.owner_id`.
    """
    (file_id,) = db_other.execute(
        "insert into files (owner_id, class_id, filename, status) "
        "values (%s, %s::uuid, %s, 'ready') returning file_id",
        (api.owner_id, api.class_id, "lecture-3.pdf"),
    ).fetchone()
    chunk_ids = []
    for rank, text in enumerate(texts):
        (chunk_id,) = db_other.execute(
            "insert into chunks (file_id, owner_id, class_id, text, file, page_or_slide, "
            "embedding, embedding_model_id) "
            "values (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s) returning chunk_id",
            (
                str(file_id),
                api.owner_id,
                api.class_id,
                text,
                "lecture-3.pdf",
                str(rank + 1),
                _chunk_vector(rank),
                model_id,
            ),
        ).fetchone()
        chunk_ids.append(str(chunk_id))
    return chunk_ids


def _providers(api, generator=None, embedder=None) -> ScriptedGeneration:
    """Swap the app's provider singletons for this test's. Returns the generator, for its log."""
    generator = generator if generator is not None else ScriptedGeneration("")
    api.app.state.generator = generator
    api.app.state.embedder = embedder if embedder is not None else FixedEmbeddings()
    return generator


def _ask(api, class_id: str | None = None, question: str = "What is the first way?", **extra):
    body = {"class_id": api.class_id if class_id is None else class_id, "question": question}
    body.update(extra)
    return api.client.post("/ask", json=body)


# --- the four grounding states, all 200 -------------------------------------------------------


def test_a_grounded_answer_is_200_with_citations_resolved_to_the_seeded_chunks(api, db_other):
    """The whole read path over HTTP: real retrieval, real parse/validate/resolve, real decide.

    The citation's `chunk_id` is asserted against the id `db_other` published, which is what makes
    this a test of the citation spine rather than of pydantic: `[S1]` in the model's reply is
    resolved through the SERVER-SIDE map (ADR 0015), so the id can only be right if the ordinal
    the route handed the model matched the chunk the retriever ranked first.
    """
    chunk_ids = _seed(db_other, api, texts=["Motion requires a mover.", "Contingency."])
    _providers(
        api, ScriptedGeneration("Everything moved is moved by another [S1].\nCOVERAGE: complete")
    )

    response = _ask(api)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == RESPONSE_KEYS, "the response gained or lost a field"
    assert body["state"] == "GROUNDED"
    assert body["answer_prose"] == "Everything moved is moved by another [S1]."
    assert body["citations"] == [
        {
            "label": "S1",
            "file": "lecture-3.pdf",
            "page_or_slide": 1,
            "chunk_id": chunk_ids[0],
        }
    ]
    assert body["coverage"] == {"complete": True, "gaps": []}
    assert body["integrity"] == {"ok": True, "reasons": []}


def test_a_partial_answer_is_200_and_carries_the_gaps_the_model_declared(api, db_other):
    """PARTIAL is GROUNDED's other branch in `_decide` - citations present, coverage incomplete -
    and both branches are pinned so neither is passing by never firing."""
    _seed(db_other, api, texts=["Motion requires a mover."])
    _providers(
        api,
        ScriptedGeneration(
            "The first way argues from motion [S1].\nCOVERAGE: gaps: the fourth way; the fifth way"
        ),
    )

    response = _ask(api)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "PARTIAL"
    assert body["coverage"] == {"complete": False, "gaps": ["the fourth way", "the fifth way"]}
    assert len(body["citations"]) == 1


def test_a_refusal_is_200_and_not_a_client_error(api, db_other):
    """THE DECISION THIS TICKET EXISTS FOR. The corpus was searched and did not cover the
    question: that is a true answer about the student's materials, so it is a success. A 4xx would
    say the request was wrong when the request was fine (ADR 0016; the same rule `GET /files`
    follows for a `failed` row).

    Produced by the real `_decide`: a reply with no `[S#]` and only a coverage line validates
    cleanly - empty prose is the degenerate refusal, not a zero-citation failure - and resolves to
    no citations, which is the REFUSAL row.
    """
    _seed(db_other, api, texts=["Motion requires a mover."])
    _providers(api, ScriptedGeneration("COVERAGE: gaps: nothing here discusses the fifth way"))

    response = _ask(api)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "REFUSAL"
    assert body["answer_prose"] is None
    assert body["citations"] == []
    assert body["coverage"] == {
        "complete": False,
        "gaps": ["nothing here discusses the fifth way"],
    }


def test_an_empty_class_refuses_without_ever_calling_the_model(api):
    """A class with no chunks: `retrieve` returns `[]` and `answer()` short-circuits to the canned
    refusal with NO generation call (ADR 0016). Asserted on the generator's own call log, because
    "we did not pay for this" is the half a status code cannot show.

    The class EXISTS here - it is `db`'s seeded row - which is what separates this from the 404
    below: an empty class is a real corpus with nothing in it, and a missing class is not a corpus.
    """
    generator = _providers(api)

    response = _ask(api)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "REFUSAL"
    assert body["coverage"]["gaps"] == ["no course material was retrieved for this question"]
    assert generator.calls == [], "an empty class paid for a generation call"


def test_an_integrity_flagged_answer_is_200_and_ships_its_reasons(api, db_other):
    """Validation fails on both attempts, so the prose is shown FLAGGED rather than clean
    (ADR 0015 integrity-flag-and-show). 200, because there is an answer to render - the client is
    required to render it differently, which is why `integrity.reasons` is on the wire.

    `[S9]` is a dangling label against a one-chunk corpus, which is the real label-validity rung.
    The call count pins that the shared retry budget was actually spent (ADR 0016), so this is the
    post-retry row of the decision table and not a first-attempt accident.
    """
    _seed(db_other, api, texts=["Motion requires a mover."])
    generator = _providers(
        api, ScriptedGeneration("The fifth way is teleological [S9].\nCOVERAGE: complete")
    )

    response = _ask(api)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "INTEGRITY_FLAGGED"
    assert body["answer_prose"] == "The fifth way is teleological [S9]."
    assert body["integrity"]["ok"] is False
    assert body["integrity"]["reasons"], "flagged with no reason to show the student"
    assert body["citations"] == [], "a dangling label resolved to a citation"
    assert len(generator.calls) == 2, "the shared retry budget was not spent"


# --- ERROR: the envelope, and the kind -> status split ----------------------------------------


def test_a_transient_generation_failure_is_503_carrying_the_librarys_own_sentence(api, db_other):
    """`provider_transient` is the one kind where trying again can work, and 503 is the status
    that says so. The message is `answer()`'s, forwarded verbatim: it is prose this repo wrote,
    and `ErrorBody` documents the kind/message pair as exactly the `GrounderError` vocabulary.

    This is the OTHER side of `_ERROR_MESSAGE_OVERRIDE` from the terminal test below - one kind
    forwards, one substitutes, and pinning only the substitution would pass against a route that
    substituted for everything.
    """
    _seed(db_other, api, texts=["Motion requires a mover."])
    generator = _providers(api, ScriptedGeneration(TransientGenerationError("429 rate limited")))

    response = _ask(api)

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["kind"] == ERROR_KIND_PROVIDER_TRANSIENT
    assert "generation failed after" in error["message"]
    assert error["detail"] is None
    assert len(generator.calls) == 2, "503 was returned without spending the retry budget"


def test_a_transient_query_embedding_failure_is_also_503(api, db_other):
    """The retrieval half of the same kind - `ask()`'s own conversion at the Retriever seam, not
    the Grounder's. Same kind, so the same status: a client backs off either way.

    The message names the STEP (`query embedding failed`), which is the distinction `ask()` put
    there for telemetry; it survives to the wire because this kind forwards.
    """
    _seed(db_other, api, texts=["Motion requires a mover."])
    generator = _providers(api, embedder=FixedEmbeddings(raises=TransientEmbeddingError("timeout")))

    response = _ask(api)

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["kind"] == ERROR_KIND_PROVIDER_TRANSIENT
    assert "query embedding failed" in error["message"]
    assert generator.calls == [], "the query embed failed and a generation was still paid for"


def test_a_terminal_provider_failure_is_500_and_never_echoes_the_providers_words(api, db_other):
    """`provider_terminal` is 500 - the request was fine, the server's configuration is not, and
    retrying changes nothing.

    THE MESSAGE IS THE ROUTE'S, NOT THE LIBRARY'S, AND THAT IS THE POINT OF THIS TEST. The
    Grounder builds this kind's message as `f"{type(err).__name__}: {err}"` over a RAW provider
    exception, so its text is whatever the vendor wrote - OpenAI's 401 body quotes back the key
    prefix it rejected. Forwarding it would ship provider error text to a browser, which is the
    exact thing `errors._unhandled` refuses to do. The marker below stands in for that text: it
    must not appear anywhere in the response.
    """
    _seed(db_other, api, texts=["Motion requires a mover."])
    secret = "sk-proj-THIS-MUST-NOT-REACH-A-CLIENT"
    _providers(api, ScriptedGeneration(RuntimeError(f"Incorrect API key provided: {secret}")))

    response = _ask(api)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["kind"] == ERROR_KIND_PROVIDER_TERMINAL
    assert error["message"] == ask_router._ERROR_MESSAGE_OVERRIDE[ERROR_KIND_PROVIDER_TERMINAL]
    assert secret not in response.text, "the provider's own error text reached the client"
    assert "RuntimeError" not in response.text


def test_an_embedding_mismatch_is_500_and_names_the_remedy(api, db_other):
    """ADR 0018, end to end: the corpus is stamped with one model and the active embedder is
    another, so `retrieve` raises, `ask()` converts it to ERROR `embedding_mismatch`, and this
    route renders 500 - a misconfigured corpus is an operator fact, never a retry and never a
    refusal about the student's materials.

    The message forwards: `EmbeddingModelMismatchError`'s sentence is ours and it names what to do
    ("re-index the class"), which is what `ErrorBody.message` documents itself to be.
    """
    _seed(db_other, api, texts=["Motion requires a mover."], model_id="text-embedding-3-large")
    generator = _providers(api)

    response = _ask(api)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["kind"] == ERROR_KIND_EMBEDDING_MISMATCH
    assert "Re-index" in error["message"]
    assert generator.calls == [], "a mismatched corpus still paid for a generation call"


def test_an_unknown_error_kind_falls_back_to_500_with_a_route_owned_message(api, monkeypatch):
    """The safety net under `_ERROR_STATUS`. NO code in the repo emits an unknown kind - that is
    what `test_every_error_kind_has_a_status` enforces - so this state cannot be produced honestly
    and `ask` is substituted here rather than driven. What is under test is the handler's
    `.get(...)` default: an unrecognised kind must be a 500 carrying that kind, not a `KeyError`
    that `errors._unhandled` would flatten into a bare `internal` and lose the token with.

    The kind IS forwarded (a client gets the library's token) while the message is NOT: nobody has
    read the text of a message that does not exist yet.
    """
    unknown = GrounderResult(
        state=GrounderState.ERROR,
        answer_prose=None,
        citations=[],
        coverage=Coverage(complete=False, gaps=[]),
        integrity=Integrity(ok=True, reasons=[]),
        error=GrounderError(kind="quota_exhausted", message="internal detail: dbname=secret"),
    )
    monkeypatch.setattr(
        ask_router,
        "ask",
        lambda *a, **kw: AskResult(result=unknown, retrieved=[], retrieval_ran=False),
    )

    response = _ask(api)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["kind"] == "quota_exhausted"
    assert error["message"] == ask_router._UNKNOWN_ERROR_MESSAGE
    assert "dbname=secret" not in response.text


def test_every_error_kind_has_a_status():
    """THE GUARD, not the fallback. `_ERROR_STATUS`'s `.get(...)` default keeps an unknown kind
    from becoming a 500 `internal`, but a default is exactly what lets a map go stale in silence:
    a kind added to the library later would render as a generic server fault forever and nothing
    would go red.

    So the kind set is DERIVED - every `ERROR_KIND_*` the two producing modules export - rather
    than listed here, which is the same technique `test_every_staging_reason_has_a_status` uses
    against `STAGING_REASONS`. Add a constant to either module without a status here and this
    fails. `gct.ask` re-exports the Grounder's transient kind, so the union covers all three
    whichever module happens to move.

    Not `db`-marked: it touches no database and must run everywhere the router imports.
    """
    import gct.ask
    import gct.grounder.answer

    declared = {
        value
        for module in (gct.ask, gct.grounder.answer)
        for name, value in vars(module).items()
        if name.startswith("ERROR_KIND_")
    }

    assert declared, "the introspection found no kinds, so this guard proves nothing"
    assert set(ask_router._ERROR_STATUS) == declared, (
        "a kind the library can emit has no status here, or a status names a kind it cannot"
    )
    assert set(ask_router._ERROR_STATUS.values()) <= {500, 503}
    assert set(ask_router._ERROR_MESSAGE_OVERRIDE) <= set(ask_router._ERROR_STATUS), (
        "an override names a kind the status map does not"
    )


# --- scope: 404, and the one spelling of it ---------------------------------------------------


def test_an_unknown_class_is_404_and_never_a_refusal(api):
    """A class that does not exist must NOT fall through to `ask()`. It would retrieve nothing,
    and `answer()` renders zero chunks as "no course material was retrieved for this question" -
    a statement about a corpus that was never there. 404 says what is actually true.

    `test_an_empty_class_refuses_without_ever_calling_the_model` is this assertion's other half:
    a real class with nothing in it IS a 200 refusal, so the 404 here is the ownership check
    firing rather than the route refusing everything.
    """
    _providers(api)

    response = _ask(api, class_id=str(uuid.uuid4()))

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "kind": "class_not_found",
            "message": "No such class. Check the class id, or create the class before asking "
            "about it.",
            "detail": None,
        }
    }


def test_another_owners_class_is_404_with_the_identical_body(api, foreign_class):
    """F12: someone else's class and no class at all are one response, byte for byte. A different
    status or a different sentence would confirm to an unauthenticated caller that the id is real.
    `class_exists` returns one `False` for both, so the route cannot tell them apart even in
    principle - this asserts nothing downstream reintroduced the difference."""
    _providers(api)
    _, other_class_id = foreign_class

    theirs = _ask(api, class_id=other_class_id)
    nobodys = _ask(api, class_id=str(uuid.uuid4()))

    assert theirs.status_code == nobodys.status_code == 404
    assert theirs.content == nobodys.content


def test_a_non_uuid_class_id_is_400_in_the_routes_own_words(api):
    """`class_exists` would raise `ValueError` here, but its message explains a connection-abort
    concern a client cannot act on. The route parses first and substitutes its own sentence; the
    library's must not reach the wire."""
    _providers(api)

    response = _ask(api, class_id="not-a-uuid")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["kind"] == "bad_class_id"
    assert (
        error["message"]
        == "class_id must be a uuid. Use the id returned when the class was created."
    )
    assert "connection" not in error["message"], "the library's internal message reached a client"


@pytest.mark.parametrize("spelling", ["plain", "braced", "hex32", "urn"])
def test_every_uuid_spelling_python_accepts_reaches_the_same_class(api, db_other, spelling):
    """The route parses `class_id` ONCE and hands the canonical form downstream.

    `uuid.UUID` accepts four spellings; Postgres's `::uuid` cast accepts three. `class_exists`
    normalises before it binds, so all four pass the ownership check - and `retrieve` binds
    `class_id` RAW into that cast (both of its queries), so `urn:uuid:...` would reach it
    unchanged and abort with `InvalidTextRepresentation`: a 500 for a request that named a real
    class the caller owns. That is issue #121's shape, reported against `enqueue`; this pins that
    `/ask` cannot repeat it whether or not #121 lands.
    """
    _seed(db_other, api, texts=["Motion requires a mover."])
    _providers(api, ScriptedGeneration("Argued from motion [S1].\nCOVERAGE: complete"))
    canonical = uuid.UUID(api.class_id)
    written = {
        "plain": str(canonical),
        "braced": f"{{{canonical}}}",
        "hex32": canonical.hex,
        "urn": canonical.urn,
    }[spelling]
    assert uuid.UUID(written) == canonical, "the spelling under test names a different class"

    response = _ask(api, class_id=written)

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "GROUNDED"


# --- request validation -----------------------------------------------------------------------


@pytest.mark.parametrize("question", ["", "   ", "\n\t "])
def test_a_blank_question_is_the_shared_422_envelope(api, question):
    """Nothing to ground. V1 has no relevance floor, so a blank question still retrieves k chunks
    from a non-empty class and the model would answer it - the student would get a confident,
    cited answer to a question they never asked.

    422 rather than 400 because the refusal is pydantic's, and `errors._validation` already owns
    that status and puts the field list in `detail`. Minting a 400 here would give one mistake two
    bodies."""
    _providers(api)

    response = _ask(api, question=question)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["kind"] == "validation"
    assert isinstance(error["detail"], list)
    assert any("question" in str(entry) for entry in error["detail"])


def test_a_missing_question_is_the_shared_422_envelope(api):
    _providers(api)

    response = api.client.post("/ask", json={"class_id": api.class_id})

    assert response.status_code == 422
    assert response.json()["error"]["kind"] == "validation"


def test_the_question_bound_refuses_one_character_over_and_admits_the_bound_itself(api, db_other):
    """Both sides of the cap, so `too long` is not passing because the route refuses everything.
    The bound is a module constant, read here rather than typed, so moving it moves this test."""
    _seed(db_other, api, texts=["Motion requires a mover."])
    _providers(api, ScriptedGeneration("Argued from motion [S1].\nCOVERAGE: complete"))

    over = _ask(api, question="x" * (ask_router.MAX_QUESTION_CHARS + 1))
    at = _ask(api, question="x" * ask_router.MAX_QUESTION_CHARS)

    assert over.status_code == 422
    assert over.json()["error"]["kind"] == "validation"
    assert at.status_code == 200, at.text


def test_a_question_reaches_the_model_verbatim(api, db_other):
    """Refuse, do not convert. A blank question is rejected outright (above) and an accepted one
    is passed through UNTRIMMED - the same choice `create_class` makes about a class name. What
    the model is asked is exactly what the student typed, so this asserts on the prompt the
    generator actually received rather than on the response."""
    _seed(db_other, api, texts=["Motion requires a mover."])
    generator = _providers(api, ScriptedGeneration("Argued from motion [S1].\nCOVERAGE: complete"))
    question = "  What is the first way?  "

    response = _ask(api, question=question)

    assert response.status_code == 200, response.text
    assert generator.prompts[0].endswith(f"QUESTION\n{question}")


# --- what the response deliberately does NOT carry ---------------------------------------------


def test_the_top_k_is_not_client_settable_and_the_retrieved_set_stays_off_the_wire(api, db_other):
    """Two omissions, one request, because they are the same decision seen twice.

    `k` is not a request field: an unrecognised `k` in the body is ignored by pydantic, and the
    prompt still carries exactly `DEFAULT_K` sources - so the answer to one question cannot depend
    on an unauthenticated caller's tuning. `S{DEFAULT_K + 1}` is absent from the prompt AND
    `S{DEFAULT_K}` is present, so this fails whether the route widened the top-k or narrowed it.

    `retrieved` is not a response field: the retrieved set exists for the eval runner's recall@k
    (ADR 0023) and the HTTP client renders citations. Asserting the exact key set is what stops it
    reappearing later as a convenience.
    """
    _seed(db_other, api, texts=[f"chunk {i}" for i in range(DEFAULT_K + 2)])
    generator = _providers(api, ScriptedGeneration("Argued from motion [S1].\nCOVERAGE: complete"))

    response = _ask(api, k=99)

    assert response.status_code == 200, response.text
    assert set(response.json()) == RESPONSE_KEYS
    prompt = generator.prompts[0]
    assert f"[S{DEFAULT_K}]" in prompt, "fewer sources than DEFAULT_K reached the model"
    assert f"[S{DEFAULT_K + 1}]" not in prompt, "a client-supplied k widened the top-k"


def test_an_escaped_exception_is_the_shared_500_envelope(api, monkeypatch):
    """D7's other half: `ask()` converts two exception types and PROPAGATES everything else on
    purpose, so this route really does see raw psycopg errors and terminal provider errors.

    It has no `try/except Exception` of its own, and this is the test that says the boundary still
    exists: `errors._unhandled`, installed app-wide by `create_app`, renders any escaped exception
    as a 500 `internal` with the exception text going to the LOG and not the client. A local catch
    could only duplicate that rendering or invent a message - which is how a psycopg DSN reaches a
    browser. The marker below is that DSN's stand-in and must not appear in the response.

    `ask` is substituted rather than driven: manufacturing a real psycopg failure inside the
    handler would be testing psycopg, and the property under test is that whatever escapes lands
    on the app-wide handler intact.
    """
    _providers(api)
    secret = "dbname=grounded_class_tutor password=hunter2"

    def _boom(*args, **kwargs):
        raise RuntimeError(f"connection failed: {secret}")

    monkeypatch.setattr(ask_router, "ask", _boom)

    response = _ask(api)

    assert response.status_code == 500
    assert response.json() == {
        "error": {"kind": "internal", "message": "internal error", "detail": None}
    }
    assert secret not in response.text, "an escaped exception's text reached the client"


def test_the_route_does_not_catch_broadly_itself(api):
    """The claim above, asserted against the source rather than only its effect: the handler must
    contain no `except Exception`. Behaviour alone cannot tell "the app handler rendered it" from
    "the route caught it and re-raised an identical 500", and the two differ the day either one
    changes - a route-local catch would be a second writer for a rendering `errors.py` owns."""
    import inspect

    source = inspect.getsource(ask_router.ask_question)
    assert "except Exception" not in source
    assert "except BaseException" not in source
