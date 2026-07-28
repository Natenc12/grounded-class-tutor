# 0015. Grounder [S#] citation contract — labeled-context format, validation ladder, failure handling

- **Date:** 2026-07-04
- **Status:** accepted

## Context
ADR 0014 locked the Grounder's partial-support mechanic on the insight **citation = support
assertion**, unifying citation-spine honor-points ② (render chunks into labels) and ③ (resolve
labels back to citations) onto one `[S#]` token vocabulary. It left three things open for the
component spec: the exact labeled-context format (②), the resolution/validation behavior on the
spine's named failure modes (③), and what the Grounder *does* when validation fails. This ADR
settles the full `[S#]` token lifecycle, in and out.

Constraints: ADR 0013 (thin, text-parsed generation — no structured-output provider features);
0014's **parse-fails-safe** invariant; N3 penalizes over-refusal (≤0.10 false-refusal) as much as
under-refusal; N7 says generation cost is trivial at dogfood scale; N11 makes citation rendering
the product's trust surface.

## Decision
**② Labeled-context format.** The context-builder renders each retrieved chunk as a labeled block:
```
[S1] (lecture-3.pdf, p.4)
<chunk text …>
```
Labels are **per-request ordinals** (`S1…Sn`, retrieval-rank order), **not** DB ids — short for the
model, and the `S#→chunk_row` map stays server-side (we never expose or trust a model-supplied id).
Each block carries exactly the spine metadata born at parse (file + page/slide), enough to render
an N11 citation on resolution.

**③ Validation ladder** — the honest V1-structural / V3-semantic line, per case:
- **Valid label** (`S#` in range) → resolve to `{file, page/slide, chunk_id}`. Happy path.
- **Dangling label** (`[S5]`, only S1–S4 handed in) → **caught in V1**, structurally (out of range).
- **No-citation claim** → V1 catches only the **coarse** case: non-empty prose with **zero** `[S#]`
  labels. Per-claim "this sentence lacks a cite" needs claim segmentation → **not feasible in
  text-parse V1**; it is V3 judge-based faithfulness eval.
- **Wrong-chunk cite** (valid `[S2]`, but S2 doesn't support the claim) → **invisible in V1** — the
  accepted 0014 limitation; V3/N4.

So V1 cheaply enforces label **validity** + the coarse zero-citation guard; per-claim presence and
semantic correctness belong to the V3 harness.

**Failure handling — validate → retry-once → fail-safe-flag:**
1. Parse + validate labels and the coverage marker.
2. On any structural failure (dangling label, zero-citation non-empty prose, missing/unparseable
   coverage marker), **regenerate once** — a single bounded retry absorbs transient formatting
   slips without meaningful latency/complexity (generation is near-free at dogfood scale, N7). This
   is the read-path cousin of the worker's retry invariant.
3. If it still fails, **fail safe as integrity-flag-and-show**: present the prose carrying a
   **visible integrity flag** ("some references couldn't be verified against your materials") —
   never as a clean grounded answer — and record the event as spike telemetry. Chosen over
   degrade-to-refusal for **transparency**: don't pretend the answer is fully grounded, but don't
   throw away possibly-useful prose either.

**Cross-cutting UI constraint (load-bearing):** the client must **visually distinguish a verified-
grounded answer from an integrity-flagged one.** Trust is preserved only if the two are not
confusable — this is a hard requirement on the answer surface, extending N11.

## Alternatives considered
- **Surgically excise the offending claim** from prose — rejected: semantic surgery, brittle in V1,
  and a stripped-but-still-present claim is the "ungrounded claim shown as grounded" we're avoiding.
- **Hard-refuse the whole answer on first validation failure** — rejected: too brittle, drives
  false-refusal (N3). Retry-once is the softer, cheaper first response.
- **No retry (flag immediately)** — rejected: wastes a near-free chance to fix a transient slip.
- **Degrade-to-refusal on final failure** — rejected: safer/cleaner but discards useful partial
  prose and is *less* transparent than an explicit integrity flag.
- **Model-supplied chunk ids instead of ordinals** — rejected: longer, leaks DB structure, and
  invites the model to fabricate an id we'd have to trust. Server-side ordinal map is safer.

## Consequences
- Completes the Grounder's citation/partial-support behavior with ADR 0014 (mechanic) — together
  they spec honor-points ②/③ end to end.
- **Answer now has four render states:** fully-grounded (coverage=complete), partial (coverage=gaps),
  refusal (degenerate — empty prose), and **integrity-flagged** (validation failed after retry).
  The integrity-flagged state is **new** relative to ADR 0012's five P0 surfaces — treat it as a
  *state-variant of the existing answer surface*, not a sixth screen; reconcile with 0012 and note
  the N11 extension (verified-vs-flagged must be visually distinct). **Open — tracked in #49**, to be
  settled before the Slice 4 client lands.
- Adds a small deterministic validation + retry loop to the Grounder — spec it as part of the
  component design; emit validation-failure counts as spike telemetry.
- Still open for the Grounder spec: the remaining runtime failure modes — **empty retrieval** (no
  chunks handed in → degenerate refusal path) and **provider error/timeout** bubbling from the thin
  generation interface (0013).
```
