# 0024. Retriever score clamped at zero — amending ADR 0017's range claim

- **Date:** 2026-07-20
- **Status:** accepted — amends **ADR 0017** (its `[0,1]` range claim; the seam rationale stands)

## Context
ADR 0017 fixed the Retriever's seam currency as normalized cosine similarity and wrote the
conversion as `similarity = 1 - cosine_distance`, promising a range of `[0, 1]`. Building the
Retriever (#5) surfaced that **those two claims cannot both hold.**

pgvector's `<=>` returns cosine distance in **`[0, 2]`**, not `[0, 1]`. Measured live against
`grounded_class_tutor` during prep:

| vectors | `<=>` distance | `1 - d` |
|---|---|---|
| identical direction (`[3,0,0]`, `[7,0,0]`) | `0` | `1.0` |
| orthogonal (`[1,0,0]`, `[0,1,0]`) | `1` | `0.0` |
| **opposite** (`[1,0,0]`, `[-1,0,0]`) | **`2`** | **`-1.0`** ⚠ |
| zero vector involved | `NaN` | `NaN` ⚠ |

So ADR 0017's literal formula yields `[-1, 1]`. This matters precisely because of *why* 0017 exists:
nothing in V1 reads the score, so a wrong range hides until the V3 relevance filter (`score >= τ`)
turns on — and until then every downstream reader (the Grounder, #6) binds to a `[0,1]` contract the
code would not actually honor.

Only that one bounded factual claim inside 0017 is wrong. Its **reasoning survives intact** — the
seam should speak *relevance*, not *distance*, and should own its units rather than pushing an
inversion onto a future consumer. Hence an amendment, not a supersession: editing 0017 in place
would erase the evidence that we got it wrong, which is the opposite of what ADRs are for.
(Amendment style mirrors ADR 0002's status line.)

## Decision
**`score = max(0.0, 1.0 - distance)`** — clamped at zero, converted at the Retriever boundary.
This keeps *both* of ADR 0017's promises: its formula and its stated `[0, 1]` range.

Clamping is monotonic, so 0017's "ordering is unaffected" property still holds.

## Alternatives considered
- **Emit un-clamped `1 - d`, accept `[-1, 1]`** — rejected: silently breaks the contract every
  downstream reader binds to. The seam's whole job is to be trustworthy before anyone reads it.
- **Rescale `(2 - d) / 2`** — rejected: preserves more information, but compresses all real scores
  toward `0.5` and diverges from 0017's literal formula, so a τ calibrated later would not mean what
  0017 describes.
- **`assert distance <= 1.0` instead of clamping** — the live candidate at prep time, on the
  reasoning that a surprise should surface rather than be floored away. **Rejected during the
  build**, on a stronger argument than the one available at prep: a negative cosine is a *legitimate*
  (if rare) input, not a bug, so an assert converts a valid student question into a crash on the read
  path. For a product whose contract is "answer or honestly refuse," an outage is the worst of the
  three outcomes. The clamp discards nothing V3 would want to distinguish either — "semantically
  opposite" and "not relevant" are the same verdict at a **relevance** seam.

## Consequences
- Retriever contract is unchanged and now true: `score : float in [0,1]`, higher = more relevant.
- ADR 0017's status line points here; its formula should be read as amended by this ADR.
- `components/retriever.md` step 4 states the clamped formula.
- **The clamp is unit-tested but was never exercised end-to-end** (`test_to_score_normalizes_and_clamps`
  covers `2.0 -> 0.0` directly). No integration test produced a distance above `1.0`: the ranking
  fixture pins vectors to the first quadrant, so every real distance stayed well under it. Whether
  `text-embedding-3-small` ever yields an anti-correlated chunk over real course material is
  **unknown** — worth a look when the spike (ADR 0022) puts real distance distributions in front of
  us, since it also bears on where τ can sit.
- **`NaN` is clamped to `0.0` — by argument order, not by an explicit guard.** A zero vector on
  either side makes `<=>` return `NaN`. Every comparison against `NaN` is false, so Python's `max`
  keeps the first argument it saw: `max(0.0, 1.0 - nan)` returns `0.0`, while the swapped
  `max(1.0 - nan, 0.0)` would return `nan` and let it flow into the Grounder. The shipped code uses
  the safe order deliberately — `retrieve.py` marks the argument order as load-bearing, and
  `test_to_score_clamps_nan_to_zero` pins it (including proving the swapped rewrite fails). No
  `isnan` guard exists, and none is needed while that order holds. Real text cannot produce a zero
  vector through the ingest path; only a hand-built fixture can. (The original bullet blamed the
  ingest suite's `FakeEmbeddings` here — also wrong: that fixture never emits a zero vector. Its
  sha256-anchored vectors all share one direction, which breaks ranking tests by making every
  distance `0`, not `NaN` — its own docstring says so.)

  *Corrected 2026-07-26:* this bullet originally claimed `max(0.0, 1.0 - nan)` returns `nan` and
  "passes the clamp untouched" — the opposite of Python's actual behavior and of the shipped,
  test-pinned code. Left as a recorded correction rather than silently rewritten, per this ADR's
  own amendment ethos: a reader who "fixed" the argument order citing the old text would have
  introduced the exact bug it described.
