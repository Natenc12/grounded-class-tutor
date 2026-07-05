# 0017. Retriever score seam — normalized cosine similarity, converted at the boundary

- **Date:** 2026-07-04
- **Status:** accepted

## Context
ADR 0008 placed the relevance filter at the **Grounder** seam and decided the Retriever always emits
a per-chunk similarity **score**, plumbed but **not gated** in V1; the V3 relevance filter "turns on
here." For that turn-on to be a *switch* and not a *reshape*, the score's **direction and units** must
be pinned now — this is the seam's contract, and it is load-bearing precisely because nothing in V1
reads it, so a wrong choice hides until V3.

pgvector's cosine operator `<=>` returns cosine **distance** (`0` = identical, higher = *less*
similar) — the raw thing the query naturally produces. Emitting that raw value up the seam would make
the `score` field mean "worse," force every future threshold to read backwards (`relevant = score <
τ`), and put a distance in a field the Grounder spec (`components/grounder.md`) already documents as
`score` in a relevance sense. The V3 filter would then own an inversion the seam should have owned.

## Decision
**The Retriever emits `score` as normalized cosine similarity in `[0, 1]`, higher = more relevant,**
converted **at the Retriever boundary** (`similarity = 1 - cosine_distance`). The seam speaks
*relevance*, not *distance*:
- Vector search orders by pgvector distance internally (its natural, index-friendly form); the
  Retriever converts to similarity before returning. Ordering is unaffected — monotonic.
- The V3 relevance filter is then a clean `score >= threshold` with a threshold that reads the
  obvious direction. Calibrating the **value** stays empirical (spike / eval set); only the
  **currency** is fixed here.
- `score` is the single seam currency. Rank order and score are consistent by construction (both
  derive from the same distance).

## Alternatives considered
- **Emit raw cosine distance, let the V3 filter convert** — rejected: pushes an inversion into the
  future consumer, makes `score` mean "worse," and mismatches the Grounder's existing `score` field
  semantics. The seam, not the filter, should own its units.
- **Emit raw distance but rename the field `distance`** — rejected: honest, but every downstream
  reader (Grounder, V3 filter, spike telemetry, any eval readout) then carries a "lower is better"
  special case forever; normalizing once at the boundary is cheaper and reads right everywhere.
- **Normalize to a z-score / percentile against the batch** — rejected for V1: corpus-relative
  scores aren't comparable across queries and can't anchor a fixed V3 threshold; absolute cosine
  similarity is the calibratable quantity.

## Consequences
- Retriever contract: `Chunk.score : float in [0,1]`, similarity, higher-better — the fixed seam
  currency handed to the Grounder.
- The V3 relevance filter (ADR 0008) turns on as `score >= τ`, a switch not a reshape — τ's value
  remains an empirical spike deliverable.
- Assumes cosine distance (the pgvector operator choice); if a future index uses L2/inner-product,
  the boundary conversion changes but the seam contract (`[0,1]`, higher-better) does **not**. The
  conversion is the Retriever's private business.
- Feeds `components/retriever.md` (Phase-3 spec) as a locked invariant.
