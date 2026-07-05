# 0023. Eval scoring — two-signal, with the 5 Grounder states scored against `expectation`

- **Date:** 2026-07-04
- **Status:** **accepted** (ratified by Nate, 2026-07-04, PM-1 — supersedes the provisional collapse first drafted this session)

## Context
Pre-mortem finding **PM-1**. The Grounder emits five states — `GROUNDED | PARTIAL | REFUSAL |
INTEGRITY_FLAGGED | ERROR` (grounder spec §8) — but `eval/questions.jsonl.expectation` is binary:
`answer | refuse` (ADR 0021 §3). The collapse between them was never defined. This is load-bearing:
the eval artifact's *primary purpose is spike ranking* (ADR 0021 — "gates the entire spike slate…
a bake-off only produces a decision if its outputs can be ranked"); its secondary purpose is a crude
product-validation read at Slice 1 exit. It is **not** a release gate — V1 is "demoable, not measured";
the ship gate is V3's N1–N4.

**The insight that shaped the rule (from ratification discussion):** PARTIAL is *not* a verified-correct
signal (V1 checks structure, not entailment — the V1-structural/V3-semantic line). It is the model's
**self-reported** partial-support outcome (ADR 0014): assert what's citable, declare the rest as gaps.
Its *root cause is ambiguous* — "corpus genuinely partial" vs. "retrieval missed a chunk that exists" —
and the Grounder cannot tell them apart. Therefore **PARTIAL must not silently collapse into PASS**:
for a spike-ranking instrument that would throw away signal and could mask the exact chunking/k failure
the suite exists to expose.

## Decision

### 1. Two independent signals per in-corpus question (don't overload one collapse)
The eval file's `expected_sources` already separates the two concerns, so score them separately:
- **Retrieval signal** — was an `expected_sources` chunk in the retrieved top-k? (recall@k proxy, N1) →
  the lever chunking / k / embedder spikes tune. **Retrieval misses surface here regardless of the final
  state**, so they cannot hide behind PARTIAL.
- **Grounding signal** — the Grounder `state` → the lever generation / prompt spikes tune (N2/N3).

### 2. Grounding-state scoring — PARTIAL is a tracked bucket, not a pass
Denominator `N_scored` = **all non-ERROR questions of that expectation type** (this is what makes the
anti-gaming property hold — see §3).

| state | in-corpus (`answer`) | out-of-corpus (`refuse`) |
|---|---|---|
| `GROUNDED` | **PASS** (pass-rate numerator) | **FAIL** — hallucinated |
| `PARTIAL` | **tracked bucket** — in `N_scored`, **not** in the pass numerator | **FAIL** — asserted when it should refuse |
| `REFUSAL` | **FAIL** — false-refusal | **PASS** |
| `INTEGRITY_FLAGGED` | **FAIL** — structural defect | **FAIL** |
| `ERROR` | **excluded** from `N_scored`, **logged** (health) | **excluded**, logged |

### 3. Metrics — primary is clean; the rest ride alongside over the same denominator
- **Primary (rank on):** `grounded_pass_rate` = GROUNDED / `N_scored` (in-corpus);
  `correct_refusal_rate` = REFUSAL / `N_scored` (out-of-corpus).
- **Secondary (same `N_scored`):** `partial_rate`, `integrity_flag_rate`, `false_refusal_rate`.
- **Health (separate):** `error_rate` — ERROR is excluded from ranking (infra, not a corpus verdict,
  ADR 0016) but logged, so a FAIL→ERROR escape hatch is visible; many ERRORs is itself a red flag.

**Why PARTIAL sits in the denominator, not excluded.** If PARTIAL were dropped from `N_scored`, trading
a FAIL→PARTIAL would *shrink* the denominator and spuriously *raise* the pass rate — rewarding the trade.
Keeping PARTIAL in `N_scored` makes that trade leave `grounded_pass_rate` **flat** while `partial_rate`
moves: the shift is observable, not buried. (E.g. `70/20/10` vs. `70/0/30` GROUNDED/PARTIAL/FAIL both
read 70% primary; the secondary rates are what distinguish them — the whole point.)

### 4. Ranking is distribution-based, not a single scalar (deliberate)
No partial-credit weighting (PARTIAL is not "0.5 of a pass"). Two spikes tied on `grounded_pass_rate`
are broken by inspecting the partial/fail split. This buys **observability over false precision** — the
chosen tradeoff for a spike-ranking instrument, not a release gate.

### 5. Two guardrails this rule must not lose (record explicitly — anti-erosion)
Both sound obvious *now*; they are written down so a later reader can't "simplify" the intent away.
- **PARTIAL is neither success nor failure for ranking — it is an observed intermediate state.** It is
  deliberately *not* collapsed into PASS or FAIL. Any future edit that folds PARTIAL into either one
  destroys the exact signal this decision exists to preserve (§2–§3). If PARTIAL looks like an awkward
  third thing, that awkwardness is the point — leave it a tracked bucket.
- **This rule is intentionally V1-specific.** It exists to make the *smoke suite* useful for **spike
  comparison**, not to approximate the final evaluation framework. V3 may compute richer, different
  metrics (faithfulness N2, citation correctness N4, formal recall@k N1, refusal calibration N3). Do not
  retrofit this collapse to "match" V3, and do not treat a good `grounded_pass_rate` as a V3-grade
  quality verdict — it is a crude bench for ranking bake-offs, nothing more.

## Forward-shape (V1 eyeball → V3 harness, no reshape — ADR 0021 principle)
V1: a human applies this table by eye over the ~12-question smoke suite and records the rate vector.
V3: the harness computes the identical table + rates automatically over the **same** file. The rule
lives in the core as a pure `state → outcome` map + a retrieval-hit check against `expected_sources`,
so V3 is the *same* definitions executed. It seeds the N-bars: `grounded_pass_rate`/`correct_refusal_rate`
are the crude V1 proxies for N2/N3; the retrieval signal is the crude proxy for N1 recall@k.

## Alternatives considered
- **PARTIAL = answered = PASS (single collapse; the provisional first draft)** — rejected on ratification:
  for a spike-ranking instrument it throws away the PARTIAL signal and lets an in-corpus PARTIAL read as a
  clean success, blunting exactly the chunking/generation signal Pass 1 needs.
- **PARTIAL excluded from the denominator** — rejected: makes FAIL→PARTIAL *raise* the pass rate (§3).
- **PARTIAL = 0.5 partial-credit in a single scalar** — rejected: collapses the A-vs-B distribution into
  one number and pretends a PARTIAL is worth exactly half; distribution-based comparison is more honest.
- **Strict: only GROUNDED passes, PARTIAL = FAIL** — rejected as the V1 default: that's a V3 faithfulness
  bar, over-punishing honest partials; available as a stricter *suite* later if wanted.

## Consequences
- **PM-1 resolved (ratified):** the smoke suite is scoreable, Spike Pass 1 has a real exit, and the eval
  artifact delivers the ranking it was created for — with retrieval and grounding signals unconflated.
- **Grounder ↔ eval-runner contract:** the `state → outcome` map + the `expected_sources` retrieval check
  are a small shared contract, living in the core so V1 eyeball and V3 harness read one definition.
- **Eval telemetry confirmed:** the Retriever must emit retrieved chunk ids + scores/rank so the retrieval
  signal is computable against `expected_sources` (already flagged in retriever.md eval hooks).
- No closed contract reopened — this adds scoring definitions over existing outputs (ADR 0021 §obj).
