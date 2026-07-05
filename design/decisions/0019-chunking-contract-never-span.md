# 0019. Chunking contract — provenance-carrying, never-span page/slide; strategy is a spike

- **Date:** 2026-07-04
- **Status:** accepted

## Context
The ingestion worker's chunking step is repeatedly flagged as "the biggest lever on N1/N2" —
which correctly makes the chunking **strategy** (boundary method, target size, overlap) an
*empirical* question tuned against the eval set, i.e. a spike, not something to specify by
speculation. But the rest of the system does not bind to the algorithm; it binds to the **shape
and guarantees** of a chunk. The Retriever reads `chunks(embedding + file/page_or_slide)`; the
Grounder renders `(file, page_or_slide)` into `[S#]` labels (honor-point ②) and resolves them
back to scalar-`page_or_slide` `Citation`s (③). Those two boxes are **closed**. So the worker
needs a chunk **contract** locked now, even while the strategy stays open.

A second question hides inside the contract and is *not* empirical: when a chunk's text would
span a page/slide boundary, what does its scalar `page_or_slide` become? That is a citation-honesty
decision (the product's differentiator), and its answer determines whether `page_or_slide` can stay
scalar or must widen to a range — which would reopen the two closed specs.

## Decision
Split the chunking concern along a **spec / spike line**, and lock the cross-boundary rule.

**SPEC (the contract downstream binds to — locked here):**
- Every chunk **carries** `(file, page_or_slide)` provenance — honor-point ① is non-negotiable;
  no chunk exists without knowing where it came from (lossless provenance through chunking).
- Every chunk is **independently embeddable and citable** — self-contained enough to stand on its
  own as a retrieved unit and as a citation.
- A chunk maps to **exactly one** `page_or_slide` (scalar), enforced by the cross-boundary rule below.
- `embedding_model_id` is **stamped at index time** (the ADR 0018 hook the Retriever asserts against).

**SPIKE (empirical — tuned against the eval set, owned by the chunking spike, ADR 0021 design-level):**
- *How* boundaries are drawn (fixed-token / semantic / structural / by-heading).
- Target **size** and **overlap** amount.
- Tokenizer / splitter tooling choice.

**Cross-boundary rule — (a) NEVER span page/slide boundaries in V1.** Chunk boundaries respect
page/slide boundaries; a chunk is always wholly within one page/slide, so `page_or_slide` stays a
clean scalar and the citation points *exactly* at the source of its text.

## Alternatives considered
- **(b) Span allowed, cite the start** (`page_or_slide` = where the chunk began) — rejected: a
  citation could point to p.4 for a claim whose support actually sits on p.5. Cheap, but it spends
  the product's core trust promise (honest provenance) to save a chunk boundary.
- **(c) Span allowed, carry a range** (`page_or_slide` = `p.4–5`) — rejected for V1: honest, but it
  **ripples** — `page_or_slide` stops being scalar, forcing changes to the Grounder's label render
  (②) and the `Citation` shape, both in *closed* specs. Reopening two make-or-break boxes to buy a
  retrieval optimization we have no data justifies is exactly the speculation this ADR avoids.
- **Leave the whole concern to the spike** — rejected: the Retriever and Grounder need a chunk
  contract to be true specs; "figure out chunks later" would leave a hole where a binding contract
  belongs. Strategy is empirical; the *contract* is not.

## Consequences
- `page_or_slide` **stays scalar** across the whole system — zero ripple into the closed Grounder /
  Retriever / Citation specs. Citation provenance is exactly honest by construction.
- **Cost:** a concept straddling a page/slide break lands in two adjacent chunks. This is an **N1**
  concern owned by the **chunking spike**, and is largely absorbed by retrieval returning both
  adjacent chunks. If evaluation later shows meaningful degradation, revisit ranges (option c)
  **with data**, not speculation — a deliberate, deferred reopening, not an accident.
- The chunking spike is now scoped: it optimizes strategy/size/overlap/tooling **within** a fixed
  contract (provenance-carrying, self-contained, single-page, model-stamped) — it cannot silently
  change the shape the rest of the system reads.
- Feeds `components/ingestion-worker.md` (Phase-3 spec) as the locked chunk contract; completes the
  citation spine at honor-point ①.
