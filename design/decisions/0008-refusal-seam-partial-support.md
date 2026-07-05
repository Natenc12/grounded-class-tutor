# 0008. Refusal seam — partial-support policy, laddered V1→V3

- **Date:** 2026-07-04
- **Status:** accepted

## Context
F8 — refuse to invent beyond the corpus — is the product's core trust promise, and N3 sets its
numeric bar (≥0.90 correct-refusal, ≤0.10 false-refusal). Phase-2 architecture split the RAG core
into a **Retriever** (query → ranked chunks + similarity scores) and a **Grounder** (chunks →
answer/cite/refuse), which puts the refusal decision at a nameable seam. Two questions had to be
settled: *where* the refusal decision lives and laddered across V1/V3, and *what refusal behaves like*.

A raw retrieval-score threshold τ is an uncalibrated, corpus-dependent signal that can't be picked
honestly without the V3 eval set. Separately, real course corpora are often *partially* complete —
an all-or-nothing refusal is both worse UX and arguably wrong when the materials cover part of a
question.

## Decision
**Behavior — partial-support is the policy (from V1):** the Grounder answers what the retrieved
context supports, **explicitly flags what it doesn't cover, and never fills the gap with outside
knowledge.** Wholesale refusal is the degenerate case (empty supported set → flag everything →
decline) — one policy, not two.

**Placement / laddering — separate the seam from the behavior:**
- **Architecture (from V1):** the Retriever always returns **similarity scores** and the Grounder
  receives them — the seam exists day one, so V3 adds a filter rather than reshaping anything. The
  answer/flag/refuse decision lives in the **Grounder** (model-judged), not a pre-generation gate.
- **V1 behavior:** model-judged partial-support, **crude and unmeasured**. The score is plumbed but
  **not gating** (τ effectively open). This is the honest reading of "grounding ships crude in V1."
- **V3 behavior:** calibrate the score into a **per-chunk relevance filter** (which chunks are good
  enough to include), keeping the answer/flag/refuse call in the Grounder — a defense-in-depth pair
  that also short-circuits obvious out-of-corpus queries before generation (cost).

**Accepted cost:** partial-support *raises* the empirical bar. N2/N3 stop being binary — they must
be defined for **partial responses** (per-claim faithfulness + supported/unsupported
boundary-correctness, measured within a single answer). The eval set gets more important and more
complex; that is the deliberate price of the better product.

## Alternatives considered
- **All-or-nothing refusal** — rejected: safer trust and easier to measure, but worse UX and wrong
  for partially-covered questions; the better product is worth the harder measurement.
- **Pre-generation wholesale score-gate** — rejected as the primary mechanism: a hard pre-gen bail
  *kills* partial answers (it declines before the Grounder runs) and rests on an uncalibrated τ.
  Demoted to a V3 per-chunk relevance filter feeding the Grounder.
- **Both mechanisms from V1 ("belt and suspenders now")** — rejected: τ can't be calibrated before
  the V3 eval set exists, so a V1 gate would be guesswork. Plumb the seam in V1, calibrate at V3.

## Consequences
- Refines F8/0002's "refuse when not in corpus" into an explicit partial-support policy.
- Grounder contract must **not** assume answers are all-or-nothing; the Retriever contract must
  always emit similarity scores even though V1 doesn't gate on them.
- Adds work to the V3 eval harness: N2/N3 metric definitions for partial responses. Flag at Phase 5
  (Roadmap) so the V3 gate accounts for it.
- Partial-support "boundary-drawing" reliability is an **empirical** make-or-break — settled by the
  eval set + spike, not on paper.
