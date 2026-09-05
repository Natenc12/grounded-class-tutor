"""Retriever - turn THIS owner's question, within ONE class, into the ranked set of source
chunks most likely to support an answer, with normalized similarity scores (issue #5;
design/components/retriever.md). The first half of the read path, where the write path's
`chunks` become an answer's evidence.

Kept whole on purpose: the five pipeline steps below all churn one file, so splitting them
would only create collisions (issue #5, "one module, single owner").

What this box does NOT do - all of it deliberate, none of it an oversight:
  - **No relevance gate, no threshold.** The score is PLUMBED, NOT GATED in V1 (ADR 0008); the
    V3 filter turns on at this seam as `score >= tau` without reshaping anything.
  - **No re-ranking** (F11/V4), **no answer/refuse/relevance judgment** of any kind. The
    Retriever reports; the Grounder decides (ADR 0008, retriever.md Sec.Invariants).
  - **No retry.** Embedding-provider errors propagate untouched; the ask-level retry budget
    lives in the Grounder (retriever.md Sec.Failure-modes).

THE V1 NO-FLOOR CONSEQUENCE (stated so it is never mistaken for a bug): with no threshold, a
non-empty class returns k chunks for EVERY question. So `retrieve(...) == []` means the class is
empty/un-ingested - NEVER "nothing matched well enough". In V1 the entire refusal guarantee
rests on the Grounder's judgment (retriever.md Sec."The V1 no-floor invariant").

Both DB queries here carry the full `owner_id AND class_id` scope - the consistency guard is not
exempt from the isolation filter (F6/F12).
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from gct.ids import canonical_uuid
from gct.providers.base import Embeddings

# Provisional top-k. The interface fixes the param; the VALUE is empirical - tuned against
# eval/questions.jsonl in Spike Pass 2 (design/roadmap.md, retriever.md Sec.Open/deferred).
# Lives here, not in config.py, matching CHUNK_SIZE_WORDS in ingest/chunk.py: config.py is the
# embedding-invariant anchor, not a bag of tuning knobs.
DEFAULT_K = 5


class EmbeddingModelMismatchError(RuntimeError):
    """The corpus's stored `embedding_model_id` != the active embedder's `model_id` (ADR 0018).

    Raised, never swallowed and never auto-reconciled: ADR 0018 explicitly rejects re-embedding
    the query in the stored model, because that hides a real misconfiguration and can silently
    run two vector spaces at once. A mismatch means every similarity is garbage while looking
    structurally perfect - k rows, ranked, scored - which is exactly the silent failure this
    guard exists to convert into a loud one. Surfaces as a Grounder ERROR, never a refusal
    (ADR 0016).
    """


@dataclass(frozen=True)
class RetrievedChunk:
    """One retrieved chunk: its text, its provenance, and how well it matched.

    PUBLIC CONTRACT - this is exactly the Grounder's `retrieved` input shape, so the two boxes
    compose directly (retriever.md Sec.Interface); issue #6 binds to it. Do not drift the field
    names or types without updating that issue.

    `page_or_slide` is a scalar int, never a range (never-span, ADR 0019) - the honesty
    guarantee behind a citation. The `chunks.page_or_slide` column is `text`, so it converts
    back to int at the SQL boundary, mirroring `ingest/index.py`'s int -> str on the way in.
    """

    chunk_id: str
    text: str
    file: str
    page_or_slide: int
    score: float  # normalized similarity in [0,1], higher-better (ADR 0017, clamped per ADR 0024)


def _assert_embedding_consistency(
    conn: psycopg.Connection,
    *,
    owner_id: str,
    class_id: str,
    embedder: Embeddings,
) -> bool:
    """Guard the ADR-0018 invariant for this class. Returns False when the class is empty.

    An empty class returns False so the caller returns `[]` WITHOUT paying for a query embedding
    (retriever.md Sec.Failure-modes); a stamp that doesn't match raises.

    A whole-class DISTINCT is the point - folding the stamp into the top-k query instead would
    only inspect the k rows that came back, leaving a stale chunk ranked k+1 invisible. It also
    catches the MIXED-model case, which is the real foot-gun: swapping the embedder without
    re-indexing every file leaves a class with mixed stamps (roadmap PM-5).

    CRITICAL: the comparison's right-hand side is `embedder.model_id` - the model that ACTUALLY
    produced the stored vectors - NOT `config.ACTIVE_EMBEDDING_MODEL_ID`. Sourcing both sides
    from config would compare config to itself, and the guard could never fire (CLAUDE.md;
    ADR 0018). The mismatch test is what proves this guard is real.
    """
    # Whole-class DISTINCT, carrying the FULL owner+class scope: the guard is not exempt from
    # the isolation filter (F6/F12). class_id is a uuid column, owner_id is text - cast at the
    # SQL boundary, matching ingest/index.py's idiom.
    #
    # `class_id` ARRIVES ALREADY CANONICAL and this is the reason the parse lives in `retrieve`
    # rather than here: this helper is private and `retrieve` is its only caller, so one parse at
    # the public entry covers both of this module's `::uuid` binds, and a second parse here would
    # be a second writer for the same decision. A future second caller of this helper inherits
    # that precondition - canonicalise before calling, or move the parse down.
    rows = conn.execute(
        """
        select distinct embedding_model_id
        from chunks
        where owner_id = %(owner_id)s and class_id = %(class_id)s::uuid
        """,
        {"owner_id": owner_id, "class_id": class_id},
    ).fetchall()

    stored = {row[0] for row in rows}

    # Zero rows - and ONLY zero rows - means an empty/un-ingested class. Deliberately keyed on
    # the empty result set rather than any exception, so a real DB error still propagates.
    if not stored:
        return False

    # RHS is the ACTIVE EMBEDDER's model_id, never config: config-vs-config could never fire.
    # One equality catches both the wrong-model case and the mixed-model case (roadmap PM-5).
    if stored != {embedder.model_id}:
        raise EmbeddingModelMismatchError(
            f"class {class_id} has embedding_model_id(s) {sorted(stored)!r}, "
            f"but the active embedder is {embedder.model_id!r} (ADR 0018). "
            "Re-index the class with the active embedder; never auto-reconcile."
        )

    return True


def _to_score(distance: float) -> float:
    """Cosine distance -> normalized similarity in [0,1], higher-better (ADR 0017, clamped per
    ADR 0024).

    The seam and the range are ADR 0017's; only the clamp is ADR 0024's, which amends 0017's
    range claim and leaves its reasoning intact. pgvector's `<=>` ranges [0,2], so a bare
    `1 - distance` would yield [-1,1] and break the range downstream binds to. Clamping is
    monotonic, so ordering is unaffected.

    ARGUMENT ORDER IS LOAD-BEARING - do not rewrite this as `max(1.0 - distance, 0.0)`, which
    reads more naturally and is NOT equivalent. A zero vector on either side makes pgvector's
    distance NaN, and every comparison against NaN is False, so `max` simply keeps whichever
    argument it saw first: `max(0.0, 1.0 - nan)` is 0.0, while the swapped `max(1.0 - nan, 0.0)`
    is nan. Only this order keeps the [0,1] contract - a nan score would flow straight into the
    Grounder. Real text cannot produce a zero vector; a test fixture can. Pinned by
    `test_to_score_clamps_nan_to_zero`.
    """
    return max(0.0, 1.0 - distance)


def retrieve(
    conn: psycopg.Connection,
    question: str,
    owner_id: str,
    class_id: str,
    *,
    embedder: Embeddings,
    k: int = DEFAULT_K,
) -> list[RetrievedChunk]:
    """Retrieve the top-k chunks for `question`, scoped to one owner and one class.

    PUBLIC CONTRACT (retriever.md Sec.Interface) - issue #6 binds to this signature. `conn`
    first and `embedder` keyword-only mirrors `ingest.pipeline.ingest_file`, so the read path
    reads like the write path.

    Guard (ADR 0018) BEFORE the paid embed call, then a scoped pgvector top-k. A
    `TransientEmbeddingError` from the query embed propagates untouched - no retry here.

    Returns in rank order (score desc), length <= k. `[]` ONLY for an empty/un-ingested class
    (see module docstring). A corpus smaller than `k` returns all of it - short, not an error.

    `k < 1` raises `ValueError`. That guard exists to protect the `[]` contract, not for tidiness:
    `k=0` would make `LIMIT 0` return no rows from a perfectly healthy class, and the Grounder
    short-circuits `[] -> canned REFUSAL` (ADR 0016, grounder.md), so the student would be told
    their class is empty when it is not. `k=-1` would otherwise leak a raw psycopg
    `InvalidRowCountInLimitClause` out of a public function.

    `conn` MUST come from `gct.db.connect()`: the query vector is a `list[float]` and only
    adapts to `vector(1536)` when the pgvector type is registered on the connection.

    `class_id` is canonicalised before any statement and a non-uuid is refused with a `ValueError`
    naming the remedy (#126) - see step 0b. `owner_id` gets no such treatment and needs none: it
    is a `text` column bound with no cast, so no spelling of it can fail inside Postgres.
    """
    # 0. `k < 1` is a caller bug, and a silent one: `LIMIT 0` returns [] from a healthy class,
    #    which the Grounder cannot tell apart from an empty class and turns into a REFUSAL.
    #    Fail loud here rather than let a bad k impersonate the empty-class signal.
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k!r}")

    # 0b. Canonicalise `class_id` BEFORE ANY STATEMENT (#126). Both queries below bind it into a
    #     `%(class_id)s::uuid` cast, and `uuid.UUID` accepts spellings that cast refuses
    #     (`urn:uuid:<id>` is the demonstrated one), so a caller-supplied spelling handed over raw
    #     fails inside Postgres rather than here.
    #
    #     THE HARM IS SPECIFIC TO THIS FUNCTION AND WORSE THAN A REJECTED READ. `retrieve` is a
    #     reader with no transaction of its own, so on a non-autocommit connection the failed cast
    #     aborts the implicit transaction psycopg opened (ADR 0025) and EVERY LATER STATEMENT on
    #     that connection fails with `InFailedSqlTransaction` - the caller loses the connection,
    #     not just the query. `enqueue` was spared exactly this because it wraps its writes in
    #     `conn.transaction()`, which rolls back cleanly; nothing here does.
    #
    #     LENIENT, not strict, and the criterion is where the id comes from rather than reader vs
    #     writer - `enqueue`'s docstring is the writer of that argument. Every `class_id` reaching
    #     `retrieve` was typed by a person somewhere upstream (a script, the eval runner, an HTTP
    #     query parameter), so converting an accepted spelling is right and refusing it would only
    #     move the same work into every caller. That is also what makes this function AGREE with
    #     `class_exists`, which has always accepted these spellings: before this, one could say the
    #     class exists while the other aborted the connection asking about it.
    #
    #     REJECTED: doing the same for `owner_id`, which sits right beside it in every parameter
    #     dict here. It is a `text` column bound with NO cast (migrations/0001_init.sql), so it has
    #     no spelling Postgres can reject and cannot produce this failure at all. A `None` owner
    #     binds as NULL and matches nothing, which is a silent empty result rather than a poisoned
    #     connection - a different complaint, and not one this function is the right place to fix.
    canonical_class_id = canonical_uuid(
        class_id,
        fn="retrieve",
        param="class_id",
        remedy=(
            "Pass the id `create_class` returned for the class being searched - an id that is "
            "not a uuid cannot name any class, so there is nothing to retrieve from."
        ),
    )

    # 1. Guard FIRST (ADR 0018) - before the paid embed call, so an empty class costs nothing
    #    and a mismatched class fails before it can produce garbage similarity. A mismatch
    #    raises out of here; only the empty-class case returns False.
    if not _assert_embedding_consistency(
        conn, owner_id=owner_id, class_id=canonical_class_id, embedder=embedder
    ):
        return []

    # 2. Embed the query with the SAME active embedder. Deliberately NOT wrapped: a
    #    TransientEmbeddingError propagates untouched, because the retry budget belongs to the
    #    Grounder, not here (ADR 0008, retriever.md Sec.Failure-modes).
    q_vec = embedder.embed([question])[0]

    # 3. Scoped top-k. Two load-bearing details in this SQL:
    #    - `<=>` (cosine) matches the HNSW `vector_cosine_ops` index, so `<->` or `<#>` would
    #      silently bypass it AND change what the score means (ADR 0017).
    #    - `::vector` is REQUIRED. A bare `list[float]` adapts as `double precision[]` and a
    #      `<=>` expression has no column to infer the target type from, so it fails with
    #      `UndefinedFunction: operator does not exist: vector <=> double precision[]`.
    #      (`index.py`'s INSERT needs no cast only because the COLUMN supplies the type.)
    #      The cast is preferred over `pgvector.Vector(q_vec)`, which works identically but adds
    #      an import - a settled choice, not an oversight.
    rows = conn.execute(
        """
        select chunk_id, text, file, page_or_slide,
               (embedding <=> %(q_vec)s::vector) as distance
        from chunks
        where owner_id = %(owner_id)s and class_id = %(class_id)s::uuid
        order by distance asc
        limit %(k)s
        """,
        {"q_vec": q_vec, "owner_id": owner_id, "class_id": canonical_class_id, "k": k},
    ).fetchall()

    # 4/5. Distance -> normalized similarity, page_or_slide text -> int (mirroring index.py's
    #      int -> str on write). ORDER BY distance ASC is already score-desc, since _to_score is
    #      monotonically decreasing in distance - no re-sort, and NO gate (ADR 0008).
    return [
        RetrievedChunk(
            chunk_id=str(chunk_id),
            text=text,
            file=file,
            page_or_slide=int(page_or_slide),
            score=_to_score(distance),
        )
        for chunk_id, text, file, page_or_slide, distance in rows
    ]
