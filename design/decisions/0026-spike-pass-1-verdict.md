# 0026. Spike Pass 1 verdict — the core Slices 2–4 are built on

- **Date:** 2026-08-03
- **Status:** accepted

## Context
ADR 0022 §Decision 2 reads *"Build Slices 2–4 on the **validated** core."* Nothing discharged that
word. `design/roadmap.md` → *Spike Pass 1* states an Exit — *"the smoke suite passes by eyeball"* —
which describes an observation, not a record of one, so "validated" was a claim no document made and
Slice 2 would have started on it as an assumption.

Both Pass 1 probes are closed and their evidence is in `eval/FINDINGS.md` under two dated sections
(the generation probe, #60; the chunking probe's eight full-suite runs across three windows, #59).
That file records **observations, never decisions** by its own header rule — *"a finding that ripens
into a choice goes to an ADR or an issue, and gets a pointer here."* The verdict is such a choice,
so it lives here and the findings file points at it.

Pass 1's charter bounds what this ADR may say: **validation, not optimization** (ADR 0022 §1) — the
probes answer *works / red*, never *which is best*. The comparison protocol belongs to Pass 2 (#45).

## Decision

### 1. The core is validated — at a named configuration, not in general
The configuration validated is the one Slice 2 inherits:

- chunk window `CHUNK_SIZE_WORDS = 250` / `CHUNK_OVERLAP_WORDS = 40` (`src/gct/ingest/chunk.py`);
- the Grounder system prompt as it now stands, including the affirmative partial-support rule added
  by #60 (`src/gct/grounder/answer.py`);
- `k = 5`; `text-embedding-3-small` / `gpt-4o-mini`; the five-file dogfood religion corpus.

The evidence is the two full-suite runs of **exactly that configuration** (2026-08-01), recorded in
`eval/FINDINGS.md`: grounded 8/8, correct-refusal 4/4, hallucination 0/4, integrity-flag 0,
retrieval 8/8, zero errors, both times. In-corpus questions came back cited; out-of-corpus ones came
back refused.

**Stated with its denominator, because the denominator is small.** That is **n = 2** suites on a
bench whose noise floor is **one whole question** — 12.5 points on the 8-question in-corpus suite
(measured, #45). The claim being made is *"it grounds and refuses on real course materials at this
configuration"*, not any rate.

### 2. What this verdict does not claim
- **Not a quality bar.** ADR 0023 is explicit that the smoke suite is a spike-ranking instrument and
  **not a release gate**; V1 is demoable-not-measured (ADR 0004), and the ship bar is V3's N1–N4.
  `HANDOFF.md`'s "green = buildable, not proven faithful" caveat is **not** retired by this ADR.
- **Not a ranking.** No chunk window and no prompt wording is claimed to beat another. Every adjacent
  gap in the chunking probe's `grounded_pass_rate` column is exactly one question wide — inside the
  noise floor — which is why its per-question sections, not its rates, carry its result.
- **Not a margin.** At the shipped window `q007`'s expected page ranks **first**; what ranked second
  there was never recorded, so its margin is **unmeasured**. The chunks that displace it at 500/80
  score 0.562 / 0.555 — which is what the expected chunk itself scores at 250/40 — so the dilution
  mechanism is not peculiar to the widest window tested. "Ranks first" is the claim; "has headroom"
  is not.

### 3. The red Pass 1 caught is a bound on the chunking axis, not a Slice 2 blocker — Slice 2 is released
At a 500/80 window, `q007`'s expected page (`Livingston Cosmogony.pdf` p.4) falls out of the top-5
entirely, reproduced 3/3, on a **single-source** row — which `_require_expected_files` already names
as a guaranteed permanent miss. The mechanism is measured: a wider window dilutes the passage's
topical signal until neighbouring-page chunks outrank it.

That window is not the one that ships, and the failure is not reachable at the one that does. It
therefore **bounds the axis Pass 2 tunes** rather than blocking the write path Slice 2 builds. This
is the cheap design signal Pass 1 exists to produce, caught before the skeleton was thickened.

## Alternatives considered
- **Declare the core validated unconditionally** — rejected: the two probes never crossed. All eight
  chunking runs predate #60's prompt rule, and the generation probe held the window fixed at the
  default. No evidence covers any configuration other than the one named in §1, so an unqualified
  claim would assert more than was measured.
- **Block Slice 2 on the `q007` red** — rejected on two counts: the failure is not reachable at the
  shipped configuration, and Pass 1's own charter makes a red a *design signal on the axis Pass 2
  owns*, not a build gate. Blocking would also smuggle in a ranking claim ("the shipped window is the
  wrong one") that Pass 1 is barred from making.
- **Record the verdict in `eval/FINDINGS.md` alone** — rejected: that file's header forbids decisions,
  and the design corpus would then carry ADR 0022's instruction to build on a *validated* core with no
  record anywhere that anything discharged it.
- **Amend ADR 0022 rather than write a new ADR** — rejected: none of 0022's claims are revised. The
  two-pass sequencing, the scope split, and the Pass 2 slate all stand exactly as written. This
  discharges a step; it does not revise a claim, so the amendment convention in
  `decisions/0000-template.md` does not apply and would misdescribe the relationship.

## Consequences
- **ADR 0022 step 1 is discharged and step 2 is unblocked.** 0022 carries a dated pointer here.
- **`q005` is retired as this phase's target, with a counterfactual rather than a lucky run** —
  GROUNDED 5/5 under the new prompt rule and REFUSAL 3/3 with that rule removed, scoped to the
  default window. The five-time reproduction that made it Pass 1's clean target is closed.
- **A negative result is carried forward deliberately: `partial_rate` is 0.0% across 120 in-corpus
  asks.** The lever most expected to produce the first PARTIAL — a tuned prompt — did not; it
  converted a flat refusal straight to a full GROUNDED. The bucket ADR 0023 spends most of its length
  designing and protecting remains entirely unexercised by observation, now including under windows
  that visibly changed both retrieval and grounding outcomes. **Pass 2 must not assume the PARTIAL
  path works merely because it is specified.** ADR 0023 §5's guardrails are unaffected — an empty
  bucket is not a licence to collapse it.
- **The prompt lever and the chunk window are not independent, and Pass 2 must account for it.**
  `q005` moves on the window with its own chunk and its retrieval hit *unchanged* — the target chunk
  is byte-identical and scores 0.565 at every window, while its share of the assembled top-5 context
  goes 7.3% → 4.7% → 3.2%. A bake-off varying one lever at the other's default is measuring an
  interaction neither probe owns. Carried to #45.
- **A limit that must be stated beside any reading of the chunking sweep:** 23 of the 52 parsed units
  are deck slides, the longest is 133 words, and every one is a single chunk at every window tested —
  so deck chunk text was byte-identical across the whole sweep, while six of the eight in-corpus rows
  are deck-anchored. The window reached those rows only indirectly.
- **No provisional default changed.** `chunk.py` is untouched; the roadmap's Slice 1 *Provisional
  defaults* bullet names the chunking **strategy** ("fixed-size + overlap"), not a size, and the same
  file already declares prompt wording empirical — so nothing in it is stale and no ADR describes
  something the code no longer does.
- **`eval/FINDINGS.md` keeps the evidence and gains a pointer here.** This ADR is the single writer
  for the verdict; the findings file remains the single writer for the observations.
