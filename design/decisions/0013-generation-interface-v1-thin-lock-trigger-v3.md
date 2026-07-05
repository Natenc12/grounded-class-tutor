# 0013. Generation interface: thin text-parse in V1; structured-output + provider-lock deferred to a pre-registered V3 trigger

- **Date:** 2026-07-04
- **Status:** accepted
- **Amends:** ADR 0004 (names the ceiling cost the abstraction carries, and a concrete revisit trigger)

## Context
Closing the last Phase-2 open item — the provider-interface shape — surfaced a coupling between two
questions: (1) *text-parse vs. structured-output* for citations/refusal, and (2) *is provider-agnostic
generation glorified — should we just lock GPT?* They are one question, because structured-output is
exactly the feature that fights portability: OpenAI strict-schema and Anthropic tool-use differ in
shape, so demanding structured output through a provider-agnostic seam leaks provider specifics into it.

ADR 0004 justified the model abstraction primarily as *"what lets the make-or-break spike bake off
models,"* and called it "cheap insurance." Two refinements to that:
- The generation bake-off on the **real corpus has not run.** Locking GPT now would decide the
  differentiator's model with **zero data on the actual materials** — contradicting the empirical
  make-or-break spine of the whole project.
- 0004 under-named a real cost: a thin/portable generation interface **caps you at
  lowest-common-denominator features** — you forgo GPT's strict structured outputs precisely where
  they'd most help the citation spine. Agnosticism has a ceiling. The lock instinct is not wrong; it
  is **early**.

The maturity ladder dissolves the V1 tension: V1 grounding is explicitly *crude and unmeasured*
(0004), so V1 does not need structured output at all.

## Decision
**Embeddings — stay provider-agnostic, unconditionally.** This is where the bake-off is load-bearing
(3-small vs. voyage-3 drives retrieval recall = make-or-break) and the interface
(`embed(texts) → vectors`) is thin and lossless. No structured-output tension exists here. The
embedding provider also exposes `model_id` / `dim` so the index=query model-consistency invariant is
*detectable*, not implicit.

**V1 generation — thin, text in/out.** `generate(messages) → text`; the Grounder owns prompt
construction and parses inline `[S1]`-style citation labels ("text-parse"). Crude is ladder-sanctioned.
This keeps the interface trivially portable, which keeps *both* the generation and embeddings bake-offs
in spikes cheap. The structured-vs-portable fight simply does not arise in V1.

**Structured-output + a possible generation provider-lock — deferred to V3, on a pre-registered
trigger.** V3 is when citation-correctness / faithfulness / refusal become *measured gates*, i.e. the
exact moment structured output earns its keep and a vendor lock could be justified by data.

### The V3 lock trigger (pre-registered)
**Precondition (all required):** the V3 eval harness + labeled corpus exist at a size where margins
are meaningful; every margin is reported against the eval set's confidence interval (noise floor); the
numeric thresholds below are **pre-registered before the bake-off is run**, not chosen after seeing
results.

Then, evaluating GPT vs. Claude on the **quality axes** (faithfulness, refusal accuracy, citation
correctness):
- **Decisive-winner lock** — one provider beats the other by a pre-registered *material margin* that
  **also exceeds the eval CI** → lock the winner and adopt its provider-specific structured outputs.
- **Tie-breaker lock** — the quality gap is below the material threshold on *all* quality axes →
  optionality buys nothing measurable, so the abstraction's LCD ceiling is pure deadweight → lock on
  the **secondary axes** (cost, latency, structured-output ergonomics). Cost/latency decide **only
  inside a quality tie.**
- **Otherwise, keep the abstraction** — differences are material but direction is unstable (e.g. GPT
  wins faithfulness, Claude wins refusal) = genuine optionality, worth its carrying cost.

**Placeholder pre-registration targets (finalize against the real eval set):** material quality margin
≈ **≥10%** relative on any quality axis (beyond CI); tie-breaker cost delta ≈ **≥30%**. These are
starting numbers, not the decision.

**Priority rule:** quality is the gate; cost/latency are tiebreakers only. Cost never overrides a
quality difference — a cheaper but worse-grounding provider does not win.

## Alternatives considered
- **Structured-output in V1** — rejected: builds robustness machinery before anything is measured
  (crude V1 is sanctioned), stiffens streaming, and leaks provider specifics into the seam — raising
  the abstraction's cost with no V1 payoff.
- **Lock GPT now** — rejected: decides the differentiator's model with no corpus data, contradicting
  the empirical make-or-break spine; forecloses the generation bake-off that de-risks grounding. The
  lock is a decision to *earn from V3 data*, held open at ~zero cost by the thin interface.
- **Keep agnostic forever, no revisit** — rejected: leaves the LCD ceiling unnamed and unpaid — the
  exact gap ADR 0004 missed. The pre-registered V3 trigger forces the revisit instead of letting it
  quietly never happen.

## Consequences
- V1 provider layer: `Embeddings.embed(texts)→vectors` (+ `model_id`/`dim`), `Generation.generate
  (messages)→text`; Grounder owns prompt + citation parsing. Confirms the thin shape in `architecture.md`.
- Adds a concrete V3 gate deliverable dependency: the lock decision *requires* the eval harness +
  corpus (already V3 P0) and pre-registered thresholds. Note this at Phase 5 (Roadmap) so it is not
  deferred silently — ties to the `state.md` watch on the differentiator.
- Amends ADR 0004: the abstraction's ceiling cost is now named, and its revisit is now triggered, not
  open-ended.
