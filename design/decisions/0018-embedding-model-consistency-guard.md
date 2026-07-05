# 0018. Embedding-model consistency guard — store `model_id`, assert at retrieval, fail loud

- **Date:** 2026-07-04
- **Status:** accepted

## Context
Similarity search is only meaningful if query- and index-time embeddings come from the **identical
model + version** — a standing cross-cutting invariant (architecture.md; ADR 0005, which makes any
embeddings change force a full re-index, and flags the `3-small` vs `voyage-3` bake-off that *will*
exercise a swap). A mismatch is uniquely nasty: it throws **no error**. Retrieval keeps returning k
chunks, ranked, scored — all structurally valid, all semantically garbage. The system looks healthy
and answers get quietly worse, which is the hardest failure to notice and the most expensive to
debug (it presents as "retrieval feels off," not as a crash).

Left as config discipline, the invariant is real but unenforced — one forgotten re-index after a
model swap corrupts every answer until someone suspects the embeddings.

## Decision
**Make the invariant enforced, cheaply — the "(b)-lite" guard:**
- **Persist the embedding `model_id` (model + version) on indexed chunks** (or a corpus/class-level
  index-manifest row) at ingestion time — the version of the model that produced the stored vectors.
- **The Retriever asserts** the corpus's stored `model_id` == the active embedder's `model_id`
  before/at query time, and **fails loud** on mismatch rather than returning degraded results.
- A mismatch is an operational/config error (surfaces as a Retriever transport-style failure →
  Grounder **ERROR**, never a refusal; ADR 0016) — it is *not* an empty-retrieval or refusal outcome.

`model_id` is data we want regardless: it's the exact signal a re-index needs to key on (ADR 0005 /
C4), and it makes "what indexed this corpus?" answerable.

## Alternatives considered
- **(a) Trust provider config, no guard** — rejected: zero code, but leaves the single silent,
  high-downside failure in the read path unguarded; the whole point of the invariant is that you
  *can't* see the violation, so "be disciplined" is exactly the wrong tool. Debugging degraded
  retrieval after the fact costs far more than the guard.
- **Auto-reconcile (re-embed the query in the stored model)** — rejected: hides a real
  misconfiguration and can silently run two model versions; C4's stance is that a model change
  means a deliberate full re-index, not per-query papering-over. Fail loud, don't auto-heal.
- **Full checksum of embedding config** — rejected as over-built for V1: `model_id` (model+version)
  is the field that actually changes the vector space; a broader fingerprint is a later hardening.

## Consequences
- **Schema:** `chunks` (or an index-manifest) carries `embedding_model_id`; the ingestion worker
  writes it at index time; the provider layer exposes the active embedder's `model_id` (already in
  the `Embeddings` interface per ADR 0013). The Retriever reads both and compares.
- **Retriever spec (`components/retriever.md`):** the guard becomes a pipeline step + invariant, and
  "embedding-model mismatch" moves from a silent failure mode to a caught one (→ ERROR).
- Cheap insurance against the ADR-0005 re-index-on-swap footgun that the planned embeddings bake-off
  will actually trigger.
- Scope is V1-minimal: `model_id` equality, not a full config fingerprint (deferred).
