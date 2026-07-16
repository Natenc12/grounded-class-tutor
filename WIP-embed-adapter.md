# WIP — #3 embed-adapter (TEMPORARY — delete before merge)

Scratch tracker for [issue #3](https://github.com/Natenc12/grounded-class-tutor/issues/3).
**Last step of this PR: delete this file.**

## Goal (one line)
Make `OpenAIEmbeddings.embed` sub-batch under *both* provider caps and classify transient
errors — all **below** the unchanged `embed(texts)→vectors` interface (ADR 0013).

## The bug we're actually fixing
`embed()` already sub-batches — but only on the **2048-input count cap**, ignoring the
**~300k-token cap**. The token cap is the binding one (crossover ≈ 146 tokens/input); real
chunks run several hundred tokens, so a "legal" 2048-count batch routinely blows past 300k
tokens → the exact hard failure PM-1 says this adapter exists to prevent.

## Decisions (settled)
- **Token counting → char heuristic, not tiktoken.** Contract is "stay under the cap," not
  "maximize fill." Conservative estimate + margin honors it with zero deps. Swap to tiktoken
  later only if N6 throughput tuning demands tighter packing.
- **Retry → classify-only; worker owns backoff.** Adapter raises a typed
  `TransientEmbeddingError` (transient mirror of `parse.py`'s `ParseError`) and never retries.
  Slice-2 worker owns the ADR 0011 backoff budget. Matches parse.py precedent
  ("the caller decides retry policy, this module never does"); keeps the PM-4 seam pure.

## Task checklist
- [x] **Token-aware packer** in `src/gct/providers/openai_provider.py`
  - [x] `_EMBED_TOKEN_BUDGET` = 300k cap with conservative margin (~250k)
  - [x] per-text est: `len(text) // CHARS_PER_TOKEN + 1` (divisor 3 = overestimate for dense text)
  - [x] greedy pack in order (`_sub_batches`); flush when next would breach count **or** token cap
  - [x] edges: `embed([])→[]`; lone over-budget text ships as singleton (comment why)
  - [x] fix stale `_EMBED_INPUT_CAP` comment (cites PM-2 → correctness floor is PM-1)
- [x] **`TransientEmbeddingError`** in `src/gct/providers/base.py`
  - [x] docstring: adapter classifies, never retries; worker owns ADR 0011 budget
  - [x] `embed()` catches `RateLimitError`, `APITimeoutError`, `InternalServerError`,
        `APIConnectionError` → `TransientEmbeddingError`; terminal errors propagate
- [x] **Tests** — new `tests/gct/providers/test_openai_embeddings.py` (fake client via `client=`)
  - [x] order preserved + length matches across multiple sub-batches
  - [x] splits on token budget even under 2048 count (⭐ the regression test)
  - [x] splits on the count cap
  - [x] every recorded sub-batch call is under **both** caps
  - [x] `embed([])→[]`
  - [x] transient error → `TransientEmbeddingError`; non-transient propagates
- [x] Run: `uv run ruff check` + `uv run pytest` — 31 passed
- [ ] **Delete this file**, then open PR (Blocks #4)

## Out of scope (do NOT do here)
Worker/backoff loop (Slice 2) · batch-size throughput knob (N6, deferred) · tiktoken ·
any change to the `embed` signature.

## Provenance
Source: design/roadmap.md → Slice 1 "embed" · ingestion-worker.md §4 (PM-1 sub-batching note)
ADRs: 0013 (thin interface) · 0018 (model-id stamp) · 0005 (embeddings model) · 0011 (retry budget)
