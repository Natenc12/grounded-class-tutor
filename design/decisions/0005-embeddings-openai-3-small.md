# 0005. Embeddings model: OpenAI text-embedding-3-small (default, spike-confirmed)

- **Date:** 2026-07-04
- **Status:** accepted (default; final production model confirmed by the retrieval spike)

## Context
Anthropic ships **no native embeddings** (constraint C2), so a dedicated embeddings model is
required. Retrieval quality is the make-or-break, and it starts at the embedding model. We need a
default to build the skeleton on **without prematurely locking the final choice** — which is
empirical and belongs to the make-or-break spike.

## Decision
Default to **OpenAI `text-embedding-3-small`** for the walking skeleton, kept behind the embeddings
provider interface (ADR 0004). **Confirm the final production model empirically in the retrieval
spike**, baking off `3-small` against `voyage-3` / `voyage-3-large` on the *real* corpus. Upgrade
only if the spike shows retrieval quality needs it.

## Alternatives considered
(Prices per 1M tokens, mid-2026.)
- **OpenAI `text-embedding-3-large` ($0.13)** — better, but 6.5× the price of `3-small` for a modest
  bump. Not the default; on the table for the spike.
- **Voyage `voyage-3-large` (~$0.18)** — **best-in-class** retrieval (beats OpenAI-3-large ~9.7% on
  MTEB), Anthropic-recommended, supports int8/binary to shrink vectors. The top **upgrade
  candidate**; the spike decides.
- **Cohere `embed-v3` (~$0.10) / Google (~$0.006)** — viable; not chosen as default. Ecosystem +
  familiarity favor OpenAI to start.
- **Local (BGE / E5 / nomic-embed, free)** — rejected as default: weaker retrieval, and cost is not
  the constraint (ADR 0004).

**Why `3-small` as the default:** cheapest credible option ($0.02/1M; batch $0.01), strong
retrieval-for-cost, 1536 dims, ubiquitous ecosystem. At dogfood scale the price gap to premium
models is immaterial, so the default is chosen for **simplicity/ubiquity** and the **spike settles
quality**.

## Consequences
- Embedding dimension (**1536**) sets the pgvector column width. **Changing the model later forces a
  full re-embed + re-index** — embeddings across models are not interchangeable — so a model switch
  is a deliberate, budgeted migration, not a casual swap (this is why it lives behind an interface).
- The make-or-break spike **must include a retrieval-quality bake-off** as an explicit step, on real
  materials, measured (recall@k / precision@k).
