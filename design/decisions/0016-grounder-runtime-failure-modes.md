# 0016. Grounder runtime failure modes — empty-retrieval short-circuit, provider-error ≠ refusal

- **Date:** 2026-07-04
- **Status:** accepted

## Context
ADRs 0014/0015 specced the Grounder's citation/partial-support behavior and its *validation*
failure handling (retry-once → integrity-flag). Two **runtime** failure modes remained: an **empty
retrieval** (no chunks handed in) and a **provider error/timeout** bubbling from the thin generation
interface (0013), which sits on the synchronous `/ask` read path.

## Decision
**Empty retrieval → short-circuit to a deterministic refusal.** Zero chunks means an empty supported
set, which is the degenerate case of the 0014 mechanic. Emit a canned refusal with **no generation
call** — zero cost, zero hallucination risk. In V1 (τ open, top-k always returns k rows if they
exist) this ≈ "asking against an empty/un-ingested class"; it must refuse cleanly, not improvise.

**Provider error/timeout → a distinct ERROR state, never a refusal.** A provider outage is *not* a
grounding judgment. Mapping it to "your materials don't cover this" would be a **dishonest
conflation of an infra failure with a corpus judgment.** Persistent provider failure returns a
transport-level ERROR ("couldn't generate an answer right now — try again"), distinct from refusal
and from integrity-flagged.

**One shared retry budget.** The single re-attempt from ADR 0015 is shared: it fires on *either* a
validation failure *or* a transient provider error — **at most 2 generation attempts per ask.**
Bounds latency (N5) and keeps the retry logic flat (no multiplicative retries).

## Alternatives considered
- **Call the model on empty context anyway** (let it refuse) — rejected: wasteful and adds a
  hallucination surface for a case we can answer deterministically.
- **Map provider errors to the refusal path** — rejected: dishonest; corrupts the trust promise and
  the N3 refusal metric with non-grounding events.
- **Separate retry budgets for validation vs. provider errors** — rejected: multiplicative attempts
  blow the latency budget for no real robustness gain at dogfood scale.

## Consequences
- Confirms the Grounder answer surface has **five render states**: GROUNDED, PARTIAL, REFUSAL,
  INTEGRITY_FLAGGED (0015), and **ERROR** (this ADR). ERROR is transport-level, not a grounding
  outcome — the client renders it distinctly from a refusal.
- Empty-retrieval short-circuit is a safe V1 preview of the cost benefit ADR 0008 assigned to the
  V3 pre-generation filter — but keyed on the trivial zero-chunks case, not a calibrated threshold.
- Completes the Grounder's failure-mode set; the component spec (`components/grounder.md`) can be
  assembled. Closes the Grounder-runtime open item from 0015.
```
