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
    score: float  # normalized similarity in [0,1], higher = more relevant (ADR 0017)


def _assert_embedding_consistency(
    conn: psycopg.Connection,
    *,
    owner_id: str,
    class_id: str,
    embedder: Embeddings,
) -> bool:
    """Guard the ADR-0018 invariant for this class. Returns False when the class is empty.

    Runs `SELECT DISTINCT embedding_model_id FROM chunks WHERE owner_id AND class_id` and
    compares the result against the ACTIVE EMBEDDER:
      - empty set   -> the class has no chunks -> return False, so the caller returns []
                       WITHOUT paying for a query embedding. `[]` for an empty/un-ingested class
                       is the documented behavior (retriever.md Sec.Failure-modes).
      - exactly {embedder.model_id} -> return True.
      - anything else -> raise `EmbeddingModelMismatchError`. This one check catches BOTH the
                       wrong-model case and the MIXED-model case, which is the real foot-gun:
                       swapping the embedder without re-indexing every file leaves a class with
                       mixed stamps, and the guard then ERRORs the whole class (roadmap PM-5).

    A whole-class DISTINCT is the point - folding the stamp into the top-k query instead would
    only inspect the k rows that came back, leaving a stale chunk ranked k+1 invisible.

    CRITICAL: the comparison's right-hand side is `embedder.model_id` - the model that ACTUALLY
    produced the stored vectors - NOT `config.ACTIVE_EMBEDDING_MODEL_ID`. Sourcing both sides
    from config would compare config to itself, and the guard could never fire (CLAUDE.md;
    ADR 0018). The mismatch test is what proves this guard is real.
    """
    # Whole-class DISTINCT, carrying the FULL owner+class scope: the guard is not exempt from
    # the isolation filter (F6/F12). class_id is a uuid column, owner_id is text - cast at the
    # SQL boundary, matching ingest/index.py's idiom.
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
    """Cosine distance -> normalized similarity in [0,1], higher = more relevant (ADR 0017).

    `score = max(0.0, 1.0 - distance)`.

    The clamp is load-bearing and NOT what ADR 0017 literally writes. pgvector's `<=>` returns
    cosine distance in [0, 2] (verified: opposite vectors give exactly 2), so the ADR's bare
    `1 - cosine_distance` yields [-1, 1] - contradicting the [0,1] range the same ADR promises
    downstream. Flooring at 0 keeps both the formula and the contract: a negative cosine means
    "semantically opposite", which for a RELEVANCE seam is indistinguishable from "not
    relevant", so nothing V3's `score >= tau` would ever want to tell apart is lost.

    Clamping is monotonic, so ADR 0017's "ordering is unaffected" property still holds.

    ARGUMENT ORDER IS LOAD-BEARING - do not rewrite this as `max(1.0 - distance, 0.0)`, which
    reads more naturally and is NOT equivalent. A zero vector on either side makes pgvector's
    distance NaN, and every comparison against NaN is False, so `max` simply keeps whichever
    argument it saw first: `max(0.0, 1.0 - nan)` is 0.0, while the swapped `max(1.0 - nan, 0.0)`
    is nan. Only this order keeps the [0,1] contract - a nan score would flow straight into the
    Grounder. Real text cannot produce a zero vector; a test fixture can. Pinned by
    `test_to_score_clamps_nan_to_zero`.
    """
    # Clamp at 0: pgvector's `<=>` cosine distance ranges [0,2] (not [0,1]), so a bare
    # `1 - distance` can go negative and break the [0,1] contract ADR 0017 promises (Decision 1).
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

    Pipeline, in order:
      1. `_assert_embedding_consistency` (ADR 0018). False -> return [] immediately, before any
         embedding call. Raises on mismatch.
      2. Embed the query via the SAME active embedder: `embedder.embed([question])[0]`
         (ADR 0013). `TransientEmbeddingError` propagates untouched - no retry here.
      3. Scoped pgvector top-k:
             SELECT chunk_id, text, file, page_or_slide,
                    (embedding <=> %(q_vec)s::vector) AS distance
             FROM chunks WHERE owner_id = %(owner_id)s AND class_id = %(class_id)s::uuid
             ORDER BY distance ASC LIMIT %(k)s
         The `<=>` operator is load-bearing, not cosmetic: the HNSW index is built
         `vector_cosine_ops`, so `<->` or `<#>` would silently bypass it AND change what the
         score means relative to ADR 0017.
         The `::vector` cast is REQUIRED, not decorative - verified during prep. A bare
         `list[float]` parameter adapts as `double precision[]`, and there is no column in a
         `<=>` expression to infer the target type from, so it fails with:
             UndefinedFunction: operator does not exist: vector <=> double precision[]
         (`index.py`'s INSERT gets away without a cast only because the COLUMN supplies the
         type.) Casting at the SQL boundary matches the `%(class_id)s::uuid` idiom already in
         `index.py`; `pgvector.Vector(q_vec)` works identically but adds an import.
      4. Convert each distance via `_to_score`, and `page_or_slide` text -> int.
      5. Return in rank order (score desc), length <= k.

    Returns `[]` ONLY for an empty/un-ingested class (see module docstring). A corpus smaller
    than `k` returns all of it - short, not an error.

    `k < 1` raises `ValueError`. That guard exists to protect the `[]` contract, not for tidiness:
    `k=0` would make `LIMIT 0` return no rows from a perfectly healthy class, and the Grounder
    short-circuits `[] -> canned REFUSAL` (ADR 0016, grounder.md), so the student would be told
    their class is empty when it is not. `k=-1` would otherwise leak a raw psycopg
    `InvalidRowCountInLimitClause` out of a public function.

    `conn` MUST come from `gct.db.connect()`: the query vector is a `list[float]` and only
    adapts to `vector(1536)` when the pgvector type is registered on the connection.
    """
    # 0. `k < 1` is a caller bug, and a silent one: `LIMIT 0` returns [] from a healthy class,
    #    which the Grounder cannot tell apart from an empty class and turns into a REFUSAL.
    #    Fail loud here rather than let a bad k impersonate the empty-class signal.
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k!r}")

    # 1. Guard FIRST (ADR 0018) - before the paid embed call, so an empty class costs nothing
    #    and a mismatched class fails before it can produce garbage similarity. A mismatch
    #    raises out of here; only the empty-class case returns False.
    if not _assert_embedding_consistency(
        conn, owner_id=owner_id, class_id=class_id, embedder=embedder
    ):
        return []

    # 2. Embed the query with the SAME active embedder. Deliberately NOT wrapped: a
    #    TransientEmbeddingError propagates untouched, because the retry budget belongs to the
    #    Grounder, not here (ADR 0008, retriever.md Sec.Failure-modes).
    q_vec = embedder.embed([question])[0]

    # 3. Scoped top-k. `<=>` (cosine) matches the HNSW `vector_cosine_ops` index and ADR 0017's
    #    score definition; `::vector` is required because a bare list adapts as
    #    `double precision[]` and there is no column here to infer the type from.
    rows = conn.execute(
        """
        select chunk_id, text, file, page_or_slide,
               (embedding <=> %(q_vec)s::vector) as distance
        from chunks
        where owner_id = %(owner_id)s and class_id = %(class_id)s::uuid
        order by distance asc
        limit %(k)s
        """,
        {"q_vec": q_vec, "owner_id": owner_id, "class_id": class_id, "k": k},
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
