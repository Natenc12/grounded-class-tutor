# 0022. Spike sequencing — two-pass: validate on the tracer, tune on the dogfood corpus

- **Date:** 2026-07-04
- **Status:** accepted

## Context
Phase 5 established that no spike blocks the build; the tracer bullet (Slice 1) is the *bench* the
spikes run on (ADR 0021 / roadmap Step 3). That left one thing unsequenced: **when** do the spikes run
relative to Slices 2–4 (async worker, API, client)? The fork:
- **Tune-early** (spikes right after Slice 1, before building 2–4) protects the differentiator but
  tunes against a **single-file inline pipeline** — a corpus too tiny for chunking/k/embedder results
  to generalize (overfitting risk).
- **Build-then-tune** (whole skeleton first) gives a realistic corpus but risks the **"V1 crude,
  evaluate later" drift** the `state.md` explicitly warns about — sinking effort into 2–4 before
  learning the grounding is weak.

Each pure option sacrifices one guard. The decision splits the difference by splitting the *purpose*
of running the spikes.

## Decision — two passes, distinguished by purpose
1. **Pass 1 — validation (immediately after Slice 1).** On the tracer + seed smoke suite
   (`eval/questions.jsonl`, ADR 0021), focused on **chunking and generation only**. Goal is
   **validation, not optimization**: confirm the differentiator *actually works* — grounds and refuses
   correctly — before investing in Slices 2–4. A red result here is a design signal, caught cheap.
2. **Build Slices 2–4** on the *validated* core (async write path, API, client).
3. **Pass 2 — tuning (after dogfooding on a larger corpus).** Full bake-off slate — chunking, top-k,
   **embeddings** (3-small vs voyage-3), **generation** (GPT vs Claude) — now that the corpus is
   **representative enough** to justify tuning decisions. Scores against the grown `eval/questions.jsonl`.

## Alternatives considered
- **Tune-early only** — rejected: chunking/k/embedder tuned on one inline file overfit a corpus that
  isn't representative; decisions wouldn't survive contact with the real corpus.
- **Build-then-tune only** — rejected: no check that grounding *works* until after 2–4 are built;
  invites the exact "crude now, evaluate later" drift the score seam and this roadmap guard against.
- **Single combined pass at the end** — rejected: collapses validation into tuning, so a fundamental
  grounding failure is discovered only after the whole skeleton exists.

## Consequences
- **Roadmap:** Pass 1 and Pass 2 are explicit steps in the V1 build sequence, not footnotes to the
  spike table. Pass 1 sits between Slice 1 and Slice 2; Pass 2 follows Slice 4 + dogfooding.
- **Pass-1 scope is deliberately narrow** (chunking + generation) — the two biggest N1/N2 levers that
  need the *least* corpus to reveal a working-or-not signal. Parsing tooling, top-k, and the embeddings
  bake-off defer to Pass 2, where corpus size makes them meaningful.
- **Embeddings bake-off in Pass 2 implies a re-index** of the dogfood corpus if voyage-3 wins — cheap
  at dogfood scale and already accepted (C4 / ADR 0005). Slice 0 still sets a provisional `dim=1536`
  (3-small); Pass 2 may overturn it.
- **Reinforces ADR 0021:** the eval artifact is what makes *both* passes rankable; Pass 1 uses the
  seed smoke suite, Pass 2 the grown corpus — same file, more questions.
- No contract reshaped — this sequences *when* provisional defaults get decided, not *what* they are.
