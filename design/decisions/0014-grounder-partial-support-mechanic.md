# 0014. Grounder partial-support mechanic — citation = support, always-emit coverage

- **Date:** 2026-07-04
- **Status:** accepted

## Context
ADR 0008 set the *policy* — partial-support from V1: answer what the context supports, flag what
it doesn't, never fill the gap with outside knowledge; wholesale refusal is the degenerate case.
It deliberately left the *mechanic* open: **how** the Grounder expresses the supported/unsupported
boundary in a way we can (a) parse, (b) render, and (c) eventually measure (N2/N3 for partial
responses). This is the first Phase-3 (Component Design) decision for the Grounder — the project's
make-or-break, high-uncertainty component, so it earned adversarial depth.

Constraints in play: ADR 0013 keeps the V1 generation interface thin and text-parsed
(`generate(messages)→text`, no structured-output provider features); the citation spine already
requires parsing `[S#]` labels out of free text (honor-point ③); N3 penalizes **over**-refusal
(false-refusal ≤ 0.10) as much as under-refusal.

## Decision
**Citation = support assertion.** There is no separate "support" channel. The `[S#]` label does
triple duty at once: grounds a claim to a source, asserts the claim is supported by the corpus, and
resolves to a rendered citation. The support vocabulary and the citation-label vocabulary are the
same tokens the context-builder handed in (honor-point ②).

Partial-support falls out of **one rule**: *assert a claim only if you can attach a valid `[S#]`
label to it; anything you cannot label goes into an explicit coverage statement instead of being
asserted.* A Grounder answer is therefore two structural parts:
- **Answer prose** — every substantive claim carries an inline `[S#]` into the handed context.
- **Coverage statement — always emitted** as a controlled trailing marker we parse ourselves
  (`complete` vs. an enumerated gap list). This is a *text convention we impose and parse*, not a
  provider structured-output feature — fully compatible with ADR 0013.

**Wholesale refusal is the degenerate case** (ADR 0008): empty supported set → empty prose →
coverage statement swallows the whole question → renders as refusal. One mechanism, not two.

**The V1-structural / V3-semantic line** (gives "grounding ships crude in V1" teeth for this box):
- **V1 enforces, deterministically:** every claim carries a label; the label is **valid** (`S#`
  exists in the handed context); the coverage marker is present and parseable; the prompt forbids
  outside knowledge. A **single generation call** (reaffirms ADR 0008; the two-pass per-chunk
  relevance filter is the V3 turn-on).
- **V3 measures, empirically:** does the cited chunk actually *entail* the claim (N2/N4)? Is the
  boundary drawn *correctly* (N3)? Plus the calibrated score-filter.
- The Grounder in V1 **does not verify claim↔chunk entailment** — it enforces label presence +
  validity and defers semantic correctness to the V3 eval harness.

**Invariant — parse fails safe:** if the coverage marker can't be found/parsed, fail toward
"uncertain/flag," **never** toward "fully grounded." Silently treating an unparseable answer as
complete-coverage is the one unacceptable failure — it claims grounding we can't confirm.

## Alternatives considered
- **Separate per-claim support tags** (e.g. model tags each claim supported/unsupported alongside
  citations) — rejected: redundant with the citation, and heavier/brittler to parse in text-parse
  V1. The citation already *is* the support signal.
- **Conditional coverage statement** (emit only when there's a gap) — rejected: cleaner UX but
  absence-of-section is ambiguous (full coverage vs. model forgot), which corrupts the parse. The
  over-hedge/false-refusal risk of always-emitting is a **prompt-tuning** problem for the spike, not
  a structural one — the right place to pay it.
- **Two-pass (judge support, then generate) in V1** — rejected for V1: that is the V3 defense-in-
  depth filter arriving early; ADR 0008 keeps V1 a single model-judged call with the score plumbed
  but not gating.

## Consequences
- Unifies Grounder sub-problems: context-builder (②), partial-support (this), and citation
  resolution (③) share one token vocabulary — `[S#]`.
- **Accepted limitation:** citation-*presence* is only a weak proxy for support. A model can assert
  "X [S2]" where S2 doesn't support X; V1 cannot catch this (it's semantic — N2/N4, measured at V3).
  The mechanic guarantees *structure*, not *truth*. State this plainly in the component spec so a
  green V1 is never over-read as faithful.
- Feeds the V3 eval harness: N2/N3 metric definitions must target this shape — per-claim
  faithfulness over `[S#]`-labeled claims + boundary-correctness over the coverage marker (extends
  ADR 0008's consequence; flag at Phase 5).
- Still open in the Grounder spec (next Phase-3 work): the context-builder's exact labeled format
  (②); citation resolution + validation behavior on invalid/`[S5]`-nonexistent and no-citation
  claims (③); failure modes (malformed output, empty retrieval, provider error/timeout from the
  thin generation interface).
```
