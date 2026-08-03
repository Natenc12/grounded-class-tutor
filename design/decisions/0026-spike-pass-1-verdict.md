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
so it lives here.

**No measurement is reproduced here except where the figure is itself the decision's content** —
otherwise this ADR points at the dated FINDINGS section that owns it. That is not fastidiousness for
its own sake: a score in the q007 mechanism table was already mis-transcribed once from an adjacent
subsection, and corrected in place a day later. A second copy in a second file is how that becomes
permanent.

Pass 1's charter bounds what this ADR may say: **validation, not optimization** (ADR 0022 §1) — the
probes answer *works / red*, never *which is best*. The comparison protocol belongs to Pass 2 (#45).

## Decision

### 1. The core is validated — at a named configuration, not in general
The configuration validated is the one Slice 2 inherits:

- chunk window `CHUNK_SIZE_WORDS = 250` / `CHUNK_OVERLAP_WORDS = 40` (`src/gct/ingest/chunk.py`);
- the Grounder system prompt as it now stands, including the affirmative partial-support rule added
  by #60 (`src/gct/grounder/answer.py`);
- `k = 5`; `text-embedding-3-small` / `gpt-4o-mini`; the five-file dogfood religion corpus.

The evidence is the two full-suite runs of **exactly that configuration** (2026-08-01, recorded under
*Full suite, twice* in FINDINGS' generation-probe section): in-corpus questions came back cited,
out-of-corpus ones came back refused, and every trust-critical column was clean both times — no
hallucination, no integrity flag, no error, full retrieval. The rate vector itself lives there, not
here.

**Stated with its denominator, because the denominator is small.** That is **n = 2** suites on a
bench whose noise floor is **one whole question** — measured by #45, and the reason no rate in this
ADR is treated as a score.

### 2. What this verdict does not claim
- **Not a quality bar.** ADR 0023 is explicit that the smoke suite is a spike-ranking instrument and
  **not a release gate**; V1 is demoable-not-measured (ADR 0004), and the ship bar is V3's N1–N4.
  `HANDOFF.md`'s caveat — *"Green = buildable"*, and never read a demoable V1 as *proven faithful* —
  is **not** retired by this ADR. Validated-at-a-configuration is a narrower claim than trustworthy.
- **Not a ranking.** No chunk window and no prompt wording is claimed to beat another. Every
  *adjacent* gap in the chunking probe's grounded column is one question wide — the noise floor above
  — which is why that probe's per-question sections, not its rates, carry its result. (Its widest and
  narrowest rows differ by more than one question; that is not licence to rank them, because the
  protocol for reading a difference at all is Pass 2's, #45.)
  **One deliberate exception, scoped:** `q005`'s prompt counterfactual below *is* a comparative claim
  about two wordings. What licenses it is what FINDINGS says makes it causal rather than coincidental
  — both arms run back to back in one session, against a question that had reproduced five times at
  that window without flipping. FINDINGS scopes that tally to the default window on purpose, and so
  does this ADR: it is a claim about one question at one window, not about wordings in general, and
  §Consequences records that q005 moves on the window too.
- **Not a margin.** At the shipped window `q007`'s expected page ranks **first**; what ranked second
  there was never recorded, so its margin is **unmeasured**. What the sweep does show is that the
  page's own score falls at every widening step, and that the scores in play across windows are the
  same size as the gap being claimed. "Ranks first" is supported; "has headroom" is not.

### 3. The red Pass 1 caught is a bound on the chunking axis, not a Slice 2 blocker — Slice 2 is released
At a 500/80 window, `q007`'s expected page (`Livingston Cosmogony.pdf` p.4) falls out of the top-5
entirely, reproduced 3/3. The mechanism is measured: a wider window dilutes the passage's topical
signal until neighbouring-page chunks outrank it. The row is **single-source**, which matters for a
specific reason — `scoring.retrieval_hit`'s any-match rule means a multi-source row can hide a
chunking-induced miss behind a surviving leg, and this row cannot. (It is *not* the case that
`_require_expected_files` names this miss: that guard is about a page carrying **no indexed chunk**,
which raises `SetupError` before scoring. p.4 was indexed at every window and was scored. FINDINGS
and epic #58 both make this misattribution; FINDINGS now carries a dated correction.)

That window is not the one that ships, and the failure was **not observed** at the one that does. It
therefore **bounds the axis Pass 2 tunes** rather than blocking the write path Slice 2 builds. This
is the cheap design signal Pass 1 exists to produce, caught before the skeleton was thickened.

## Alternatives considered
- **Declare the core validated unconditionally** — rejected: the two probes never crossed. All eight
  chunking runs predate #60's prompt rule, and the generation probe held the window fixed at the
  default. So **no evidence covers any configuration other than §1's under the prompt that ships** —
  the sweep measured two further windows, but every one of those runs used the prior prompt. An
  unqualified claim would assert more than was measured.
- **Block Slice 2 on the `q007` red** — rejected on two counts: the failure was not observed at the
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
  GROUNDED 5/5 under the new prompt rule and REFUSAL 3/3 with that rule removed, back to back, at the
  default window. This is the scoped comparative claim §2 licenses. The five-time reproduction that
  made q005 Pass 1's clean target is closed.
- **A negative result is carried forward deliberately: PARTIAL has never fired**, across every
  in-corpus ask tallied to date — FINDINGS carries the two running counts that make up that total,
  one per probe section, and they are the writers of it. The lever most expected to
  produce the first PARTIAL — a tuned prompt — did not; it converted a flat refusal straight to a full
  GROUNDED. The bucket ADR 0023 spends most of its length designing and protecting remains entirely
  unexercised by observation, now including under windows that visibly changed both retrieval and
  grounding outcomes. **Pass 2 must not assume the PARTIAL path works merely because it is
  specified.** ADR 0023 §5's guardrails are unaffected — an empty bucket is not a licence to collapse
  it.
- **The prompt lever and the chunk window are not independent, and Pass 2 must account for it.**
  `q005` moves on the window with its own chunk and its retrieval hit *unchanged* — the target chunk
  is byte-identical across windows and retrieves at an identical score; what changes is its share of
  the assembled top-5 context (the three measurements are in FINDINGS' chunking-probe section). A
  bake-off varying one lever at the other's default is measuring an interaction neither probe owns.
  Carried to #45.
- **A limit that must be stated beside any reading of the chunking sweep:** **every** deck slide in
  the corpus is a single chunk at **every** window tested, so deck chunk text was byte-identical
  across the whole sweep — while most in-corpus rows are deck-anchored. The window reached those rows
  only indirectly, and an all-green sweep would have proven less than it appeared to. The counts are
  in FINDINGS.
- **No provisional default changed.** `chunk.py` is untouched; the roadmap's Slice 1 *Provisional
  defaults* bullet names the chunking **strategy** ("fixed-size + overlap"), not a size, and the same
  file already declares prompt wording empirical — so nothing in it is stale and no ADR describes
  something the code no longer does.
- **`eval/FINDINGS.md` keeps the evidence and gains a pointer here.** This ADR is the single writer
  for the verdict; that file is the single writer for the measurements.
