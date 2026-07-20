# Component Spec — Retriever

**Phase:** 3 — Component Design  **Rigor:** Deep (ADR 0001)  **Criticality:** make-or-break (read path)

> Convention (Phase-4): names the shape + contract; where a choice is locked it **points to an ADR**
> rather than restating the rationale. Governing ADRs: **0008** (score plumbed, not gated in V1),
> **0005** (embeddings model + re-index-on-change), **0017** (score = normalized similarity) + **0024** (its clamp),
> **0018** (embedding-model consistency guard), **0013** (thin provider interfaces). Requirements:
> **F6** (ask scoped to one class), **F12/N8**
> (`owner_id` isolation seam), **N1** (recall@k, V3 gate).

## Responsibility
Turn *this owner's* question, within *one class*, into the ranked set of source chunks most likely to
support an answer — and hand them, with scores, to the Grounder. It embeds the query, runs the vector
search under the isolation filter, and emits ranked chunks + **normalized similarity scores**. It
makes **no** answer/refuse/relevance judgment — that is the Grounder's (ADR 0008). Retrieval quality
(recall@k, N1) is the make-or-break this box owns; it is **empirical**, settled by the spike, not here.

## Position & dependencies
- **Down — Model provider layer:** calls `Embeddings.embed([question]) → vector` (ADR 0013). The
  query embedding **must** use the identical model + version as the indexed chunks, or similarity is
  meaningless (cross-cutting invariant; ADR 0005 forces a full re-index on any change).
- **Down — Data layer:** pgvector top-k over `chunks` (embedding + `file`/`page_or_slide` metadata),
  under `WHERE owner_id AND class_id`.
- **Up — Grounder:** receives ranked `RetrievedChunk[]` + scores; owns all downstream judgment (ADR 0008).
- **Upstream context — ingestion worker:** the `chunks` this box reads are born there (metadata =
  citation-spine honor-point ①); an un-ingested/empty class is why retrieval returns `[]`.

## Interface / contract
```
retrieve(
    conn:      psycopg.Connection,   # from gct.db.connect() — registers the pgvector adapter
    question:  str,
    owner_id:  OwnerId,
    class_id:  ClassId,          # F6 — every ask is scoped to ONE class
    *,
    embedder:  Embeddings,       # the ACTIVE embedder; also the guard's comparison target
    k:         int = DEFAULT_K,  # interface fixed; VALUE empirical (spike)
) -> [ RetrievedChunk{ chunk_id, text, file, page_or_slide, score } ]

RetrievedChunk.score         : float in [0,1]  # normalized cosine similarity, higher = more
                                               # relevant (ADR 0017, clamped per ADR 0024)
RetrievedChunk.page_or_slide : int             # scalar, never a span (ADR 0019). The column is
                                               # `text`; converted back to int on read, mirroring
                                               # ingest/index.py's int -> str on write.
Returns:     rank order (score desc), length ≤ k, possibly [] (empty/un-ingested class only, in V1)
```
The return shape is exactly the Grounder's `retrieved` input — the two boxes compose directly. `score`
is the single seam currency (ADR 0017); it is **plumbed but not gated** in V1 (ADR 0008).

## Internal approach (pipeline)
1. **Consistency guard** — assert the corpus's stored `embedding_model_id` == the active embedder's
   `model_id`; **fail loud** on mismatch → Grounder **ERROR**, never a refusal (ADR 0018 / 0016). A
   mismatch means garbage similarity that would otherwise be *silent*.
   - **Granularity: the whole class, not the returned rows.** A `SELECT DISTINCT embedding_model_id`
     over the *full* `owner_id AND class_id` scope — the guard is not exempt from the isolation
     filter. Anything other than exactly `{active model_id}` raises, which catches the wrong-model
     case and the **mixed**-model case in one check. Mixed is the real foot-gun: a partial re-index
     leaves one class spanning two vector spaces, and this ERRORs the whole class (roadmap PM-5).
     Folding the stamp into the top-k query instead would only inspect the *k* rows that came back,
     leaving a stale chunk ranked *k+1* invisible — the silent corruption ADR 0018 exists to kill.
   - **Zero rows ⇒ empty/un-ingested class** — nothing stored means nothing to mismatch, so the guard
     passes vacuously and retrieval returns `[]` **here**, before the paid embed call.
   - The comparison's right-hand side is **`embedder.model_id`**, never `config`'s active-model
     constant — sourcing both sides from config compares config to itself and the guard can never
     fire. A mismatch *test* must be built to discriminate that: seed the stamp with the **config**
     value and use an embedder whose `model_id` differs, or the test passes against a broken guard.
2. **Embed query** — `Embeddings.embed([question]) → q_vec` (single vector; ADR 0013), using that
   same active embedder.
3. **Vector search (scoped)** — pgvector top-k:
   ```
   SELECT chunk_id, text, file, page_or_slide,
          (embedding <=> :q_vec::vector) AS distance
   FROM chunks
   WHERE owner_id = :owner_id AND class_id = :class_id::uuid
   ORDER BY distance ASC
   LIMIT :k
   ```
   Two casts are load-bearing, not decoration. **`::vector`** — a bare `list[float]` parameter adapts
   as `double precision[]`, and unlike an INSERT there is no column here to infer the target type
   from, so omitting it fails outright with `UndefinedFunction: operator does not exist: vector <=>
   double precision[]`. **`::uuid`** — `chunks.class_id` is `uuid` while `owner_id` is `text`, the
   same boundary-cast idiom `ingest/index.py` uses on the write side. Separately, **`<=>` itself is a
   contract**, not a preference: the ANN index is built `vector_cosine_ops`, so `<->` (L2) or `<#>`
   (inner product) would still return ranked rows while silently bypassing the index *and* changing
   what `score` means relative to ADR 0017.
   The `WHERE owner_id AND class_id` seam ships in V1 as app-level filtering; V3 turns on RLS
   *underneath* it as enforcement, not a rewrite (F13/N8, ADR 0008-style laddering).
4. **Convert scores at the boundary** — `score = max(0, 1 - distance)` → normalized similarity in
   `[0,1]`, higher-better (ADR 0017, **clamp per ADR 0024**: pgvector's cosine distance runs `[0,2]`,
   so the un-clamped formula would go negative). Ordering is unchanged (monotonic).
5. **Return** ranked `RetrievedChunk[]` (≤ k). **No re-rank** (that is F11/V4), **no threshold gate**
   (ADR 0008).

## Failure modes
| mode | V1 behavior |
|---|---|
| **Empty / un-ingested class** (no chunks match scope) | return `[]` → Grounder short-circuits to REFUSAL (ADR 0016). In V1 this is the *only* cause of `[]`. |
| **Corpus < k chunks** | return all of them (`len < k`) — not an error; the Grounder handles a short set. |
| **Embedding-provider error / timeout** | propagate as a transport error → surfaces as Grounder **ERROR** (never a refusal; ADR 0016). Retrieval itself does not retry — the ask-level retry budget lives in the Grounder. |
| **Embedding-model mismatch** (query model ≠ index model) | **caught** by the consistency guard (step 1) → fail loud → Grounder ERROR (ADR 0018). Was the one silent, high-downside mode; now guarded. |
| **Low-relevance results present but returned anyway** | **by design in V1** — no floor; the refusal burden is wholly the Grounder's (see invariant below). |

## Invariants
- **Score is normalized similarity** — `[0,1]`, higher = more relevant, converted at this boundary
  (ADR 0017, clamped at zero per ADR 0024). Rank order and score are consistent by construction.
- **Isolation filter from V1** — every retrieval query carries `owner_id AND class_id`; absent scope
  never widens the search. V3 RLS is enforcement over this same seam (F13/N8).
- **Embedding-model consistency (enforced)** — query- and index-time embeddings are the identical
  model+version (ADR 0005); the Retriever **asserts** this against the stored `embedding_model_id`
  and fails loud, never mixing vector spaces (ADR 0018).
- **No judgment in this box** — no re-rank, no relevance gate, no refuse decision in V1. The Retriever
  reports; the Grounder decides (ADR 0008).

## The V1 no-floor invariant (what this box does *not* do in V1)
Consequence of ADR 0008, stated so it isn't an accident: with **no threshold gate**, the Retriever
returns *k* chunks for **every** question over a non-empty corpus. Therefore in V1:
- `retrieved == []` ⟺ **empty/un-ingested class** — never "query matched nothing relevant";
- the **entire** refusal guarantee (F8, the differentiator) rests on the **Grounder's** prompt-level
  judgment alone. The Retriever contributes *nothing* to refusal in V1.

This is the deliberate reading of "grounding ships crude in V1" (ADR 0008 / 0004 ladder). The V3
relevance filter (`score >= τ`, ADR 0017) is what adds a retrieval-side floor — defense-in-depth,
turned on at this seam, calibrated against the eval set. **Watch:** don't let this quietly become the
place the differentiator is deferred — the score seam existing in V1 is the guardrail against that.

## Open / deferred (out of this spec)
- **`k` value** — empirical (spike); the interface fixes the param, the number is tuned to N1
  (recall@k ≥ 0.85). V1 ships a default constant.
- **Embeddings model bake-off** — `3-small` default (ADR 0005) vs. `voyage-3` — spike (ADR 0021).
- **Chunking strategy** — owned by the ingestion worker, but it is the **biggest lever on N1** that
  this box's recall depends on. Spike deliverable.
- **Eval hooks** — emit retrieved scores + rank as spike telemetry so recall@k / precision@k are
  measurable on the real corpus (flag at Phase 5 — Roadmap).
