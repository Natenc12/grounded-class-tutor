# 0021. Eval set as a version-controlled artifact — one file, manual smoke test → V3 harness

- **Date:** 2026-07-04
- **Status:** accepted

## Context
Phase 5 (roadmap + spike planning) surfaced two coupled facts:

1. **The differentiator ships untested until V3.** The grounding loop (retrieval, citation, refusal)
   ships **crude and unmeasured** in V1 by design (ADR 0004 ladder); the formal numeric bars
   (N1 recall@k, N2 faithfulness, N3 refusal calibration, N4 citation correctness) and the harness
   that measures them are **V3 P0**. Risk (raised in the 2026-07-04 diagnose, carried in `state.md`):
   V1 *demos* fine while the reason-to-exist goes untested against a still-unbuilt V3 harness.
2. **The spike slate cannot conclude without a benchmark.** Step 3 of Phase 5 established that **no
   spike blocks the build** — the tracer bullet (Slice 1) is instead the *bench* the spikes run on
   (chunking strategy, top-k, embeddings bake-off 3-small vs voyage-3, generation bake-off GPT vs
   Claude, parsing tooling). But a bake-off only produces a *decision* if its outputs can be **ranked**.
   Without a consistent question set, every spike is vibes, not evidence.

Both point at the same missing asset: a **shared, reusable set of known-answer + known-out-of-corpus
questions**. Held in a human's head it dies with the session and can't be diffed, reused, or grown.
The decision is to make it a **first-class artifact**.

## Decision

### 1. A version-controlled `eval/` artifact, from V1
Create `eval/questions.jsonl` at the project root, version-controlled, travelling into the build repo.
It is **infrastructure, not a test script** — every future experiment scores against it.

### 2. Format — JSONL, one question per record
Chosen over CSV: questions carry **nested and multi-valued** fields (`expected_sources`, `suites`,
`tags`) and V3 will add *more* structure, not less. JSONL keeps that structured, stays hand-editable,
and gives **clean git diffs** — adding a question is a one-line, reviewable addition.

### 3. Schema — forward-shaped so V1 and V3 read the *same* file
```jsonc
{
  "id": "q001",
  "question": "What are the three conditions for a valid contract?",
  "class": "law-101",
  "expectation": "answer",          // "answer" (in-corpus) | "refuse" (out-of-corpus) → N3
  "expected_sources": [             // in-corpus only → feeds N1 recall@k + N4 citation
    { "file": "week3-contracts.pdf", "page_or_slide": 12 }
  ],
  "answer_notes": "offer, acceptance, consideration",  // known-answer sketch → N2 faithfulness eyeball
  "suites": ["smoke"],              // set-membership for subset selection (see §4)
  "tags": ["contracts", "core"],
  "added": "2026-07-04"
}
```
The `expectation` / `expected_sources` / `answer_notes` fields map 1:1 onto the N1–N4 bars, so the
scoring method changes across the ladder but the **fields do not**.

### 4. `suites` is an array (set-membership), not a scalar tier
A question can belong to several named sets at once — a `smoke` question is *also* part of the full
regression set. A scalar (`tier`/`priority`) forces a schema reshape the first time that happens,
contradicting the no-reshape principle this ADR rests on; an array never does. Subset selection is
"does `suites` contain X." At V1 every seed question is simply `["smoke"]`. (Also avoids the words
`tier`/`priority`, both already loaded in this project — rigor tiers; P0/P1.)

### 5. The maturity ladder applies to the eval set itself
- **V1** — a human runs the loop over the smoke suite, eyeballing output against `expectation` +
  `expected_sources`. Crude scoring; confirms the loop *demonstrably* grounds and refuses.
- **V3** — the formal harness reads the **identical file** and *computes* N1–N4 automatically.

The file **grows in question count and suite coverage; its shape never changes.** Same artifact starts
as a manual smoke test and becomes the formal evaluation harness — the strongest property of this
decision.

### 6. Priority split — P0 file, P1 execution
- **The artifact + a seed smoke suite (~12 questions)** is **V1 P0.** It's cheap and it **gates the
  entire spike slate** — without it the spikes can't conclude. This is infrastructure, not testing.
- **Running the manual pass, scoring, and expanding coverage** stays **V1 P1** — enough to give the
  differentiator one real test in V1 without dragging the full V3 harness forward.

## Alternatives considered
- **Questions in the dogfooder's head / ad-hoc each session** — rejected: not reusable, not diffable,
  not comparable across spikes; the differentiator's only V1 check would be irreproducible.
- **CSV** — rejected: forces nested/multi-valued fields (`expected_sources`, `suites`) into delimited
  cells; fights V3's added structure. JSONL is the reshape-proof shape.
- **Scalar `tier`/`priority` field** — rejected: breaks set-membership (a question in two suites) and
  forces a later reshape, against this ADR's own no-reshape principle. See §4.
- **Defer any eval artifact to V3 (build the harness, then the questions)** — rejected: leaves the V1
  spikes with nothing to rank *and* the differentiator untested in V1. The questions are the durable
  asset; the harness is just automated scoring layered on later.
- **Full V3 numeric bars in V1** — rejected: over-scopes V1 (ADR 0004 says V1 is crude/unmeasured).
  The P0/P1 split (§6) takes the cheap, load-bearing part (the file) and leaves the ceremony to V3.

## Consequences
- **New requirement F15** (`requirements.md`): the `eval/questions.jsonl` artifact + seed smoke suite,
  V1 P0; manual run + coverage growth, V1 P1. Forward-shaped so the V3 harness (N1–N4) reads it.
- **Unblocks the spike slate:** every bake-off (chunking, k, embedder, generator) scores against one
  consistent benchmark, making spike outcomes *comparable across the life of the project*.
- **Roadmap coupling (Phase 5):** `eval/questions.jsonl` is authored alongside **Slice 1** (the tracer
  bullet) — the tracer needs a bench, and the bench needs the tracer to run against. They land together.
- **V3 continuity:** the harness is *automated scoring over the same file*, not a new artifact. N1–N4
  read `expectation` / `expected_sources` / `answer_notes` directly.
- **Scoring function is ADR 0023 (PM-1).** This ADR defines the *file*; the **two-signal scoring** that
  makes runs rankable — retrieval (`expected_sources` in top-k) separated from grounding (the Grounder
  state), with PARTIAL a tracked bucket rather than a pass — is **ADR 0023**. Same-file principle holds:
  V1 eyeballs that rule, V3 computes it.
- Nothing here reshapes a closed contract — it adds a peer caller's input file, consistent with the
  callable-core architecture (the eval harness is already a named peer caller in `architecture.md`).
