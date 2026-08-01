# Eval findings — observed phenomena, kept so V3 doesn't re-derive them

A living record of things we **measured or noticed** while running the bench — retrieval quirks,
corpus properties, model behaviors. The eval artifact (`questions.jsonl`, ADR 0021) is the ground
truth we score against; this file is what the scoring *taught us*. V1 is structural, not
optimizing (ADR 0004 ladder) — these entries exist precisely because we are NOT acting on most of
them yet, and unrecorded observations will have to be re-derived at V3/spike time from scratch.

**Discipline:** entries are dated and carry their evidence (a question id, a probe, a run). This
file records *observations*, never decisions — a finding that ripens into a choice goes to an ADR
or an issue, and gets a pointer here. Stale entries are corrected in place with a dated note, not
silently rewritten.

---

## 2026-07-27 — first live run of the smoke suite (PR #44, Slice 1 exit)

### Corpus mass skew: the two OCR PDFs are 81% of the searchable chunks
The five dogfood files parse to 122 chunks: Livingston 62 + Long 37 (**99 combined**) vs. 7+8+8
for the three PPTX decks. Dense PDF prose therefore crowds sparse slide text in vector space:
**q003** ("How does the course define cosmogony?") retrieved an all-Livingston top-5 — three
chunks from p.2 alone — with the curated `expected_sources` slide (Lecture 18 s.2) absent from
the top-k entirely. The question still passed (GROUNDED via Livingston p.2, which genuinely
covers the definition); only the retrieval side-signal reported the curated page missing.
**Matters later:** the chunking spike (Pass 1/2) must rank candidates on a corpus where PDF
chunk-mass dominates; recall@k against deck-anchored ground truth is structurally biased until
chunk granularity is tuned or expected_sources reflects PDF coverage.
**Curation candidate (open, Nate's call):** add `Livingston Cosmogony.pdf` p.2 as a second
expected source on q003 — under the any-match hit rule that turns the misleading `hit=no` into an
honest `hit=yes`.
> **2026-07-27 — RESOLVED, applied.** Nate approved the curation; `eval/questions.jsonl` q003 now
> carries both sources. Verified before editing rather than on the strength of this entry: the
> parsed text of `Livingston Cosmogony.pdf` p.2 contains the definition nearly verbatim against
> q003's `answer_notes` ("an account of the emergence or creation of world order … intimately tied
> to basic concerns about the natural and social order, the status of the gods and humankind, and
> human action"). Measured effect on the next live run: q003 `hit=no → hit=yes`, and
> `retrieval_hit_rate` 7/8 → **8/8**. The page-numbering question this entry left implicit is also
> settled — `expected_sources` uses the **PDF page index**, matching what `parse_file` stamps
> (Livingston index-page 4 carries the Nut/Geb/Shu passage q007 expects, while its *printed* label
> is "200"). So the original `hit=no` was ground-truth incompleteness, not a numbering artifact.

### Broad survey/title slides act as attractors — especially for out-of-corpus questions
First noticed by Nate while prototyping (title slides surfacing for everything, and for refusal
questions in particular); confirmed by probe on **q009** (Norse/Ymir, out-of-corpus): the top of
the ranking is the "Various types of cosmogony" listing slides (Lecture 19 s.2, Lecture 18 s.4).
Broad overview text sits near *everything* in embedding space. Benign in V1 **by design**: there
is no relevance floor (ADR 0008), top-k always fills, and the Grounder carried the whole refusal
burden (4/4 honest refusals). **Matters later:** attractor slides are exactly what a V3
`score ≥ τ` floor must NOT admit; also a chunking-spike consideration (survey slides may deserve
different treatment than content slides).

### The score gap is real and visible: out-of-corpus ≈0.51 vs in-corpus 0.59–0.66
Same probes: q009's top-5 clusters at 0.507–0.517 while in-corpus questions retrieve at
0.59–0.66 (q003 top hit 0.656). First empirical hint of **where τ might sit** for the V3
relevance filter (ADR 0017's normalized-similarity seam). One corpus, one embedder, twelve
questions — a hint, not a calibration.

### q005 is a consistent false refusal — a generation-side lever, not retrieval
"According to Lecture 20, what does 'Creation Science' claim?" — retrieval **hits** the right
slide (hit=yes) but the model refuses, reporting gaps ("specific claims of 'Creation Science' are
not detailed"). Reproduced twice in one day. The slide's parsed text may carry less of the claim
than the deck visually suggests, or the prompt's assertion bar is set too cautious for
definitional-by-attribution content. **Matters later:** this is the exact shape Spike Pass 1's
generation/prompt lever exists to tune; it is the only in-corpus miss in an otherwise 7/8 run.

### The coverage-marker contract survived first contact: 12/12
Epic #9 flagged the marker syntax as provisional pending real model output. First live run:
every reply carried exactly one parseable `COVERAGE:` line — zero integrity flags, zero retries
observed. The provisional prompt/regex pair needs no widening yet. **Matters later:** if a future
model swap (generation bake-off) starts flagging, the rule stands — regex and prompt move in the
same commit.

### OCR corpus reality: NUL bytes exist in scanned PDFs (fixed in code, recorded as corpus fact)
`Livingston Cosmogony.pdf` p.11 carried a literal `0x00` in its extracted text — Postgres rejects
NUL in `text` columns, which killed indexing *after* the embedding spend. Now stripped at the
parse chokepoint (PR #44). **Matters later:** scanned/OCR PDFs carry more than mojibake (the
manifest's known caveat) — they carry structurally hostile bytes; the Spike-Pass-2 parsing
bake-off (#13) should test candidate parsers against exactly this class of input.

### Non-idempotent write path × scratch scripts = silent corpus inflation
Repeated interactive runs of a prototype ingest script accumulated **9 duplicate ingests** of
Lecture 18 (each a full 7-chunk copy — `ingest_file` mints a fresh `file_id` per call, so
`index_file`'s delete-by-file_id idempotency never fires across runs). Effect measured before
cleanup: a refusal question's top-5 contained the same slide three times. The smoke runner now
detects and WARNs on this state; the duplicates were removed (corpus back to 122 chunks).
**Matters later:** this is live ammunition for Slice 2's idempotent write path (and #24's
re-index semantics) — the failure is not hypothetical.

---

## 2026-07-27 — two verification runs after the PR #44 review fixes

Same corpus (122 chunks, already converged — both runs re-ingested nothing), same suite, same
`k=5`, same models, back to back. Run against the post-review code: autocommit at wiring,
`retrieval_ran` plumbed, the q003 curation applied.

### The primary ranking metric moved 12.5 points between two identical runs
`grounded_pass_rate` read **75.0% (6/8)** on run 1 and **87.5% (7/8)** on run 2, with **no input
changed between them** — same questions, same corpus, same k, same model. The mover was **q006**
("How does Lecture 20 contrast the scientific and existential perspectives on truth?"): REFUSAL on
run 1, GROUNDED on run 2 citing Lecture 20 s.6 + s.7 — the s.6 the eval file names. Retrieval was
identical (`hit=yes` both times), so this is purely generation-side non-determinism crossing the
assert/decline boundary.

**Why this is the most important entry in this file.** ADR 0023 §4 says ranking is
distribution-based, and §5 says the bench exists to RANK bake-off candidates. This is the first
measurement of its **noise floor**, and the noise floor is one whole question: on an 8-question
in-corpus suite, one flip is 12.5 points. **A spike that beats another by less than ~12.5 points on
a single run of this suite has not been shown to beat it at all.** That is a property of n=8, not of
the model — the fix is more questions or repeated runs, both of which cost money, which is exactly
the trade Spike Pass 1 has to make deliberately rather than discover mid-bake-off.
**Matters later:** before the first bake-off, decide the protocol — best-of-N, mean-of-N, or a
larger suite. Comparing two spikes on one run each is currently not sound.

### q005 is now a four-time reproduction — the one genuinely consistent in-corpus miss
REFUSAL on the PR's original run and on both runs here (gaps: "specific claims made by 'Creation
Science'", "how it argues its validity"), always with `hit=yes` — retrieval keeps handing the model
Lecture 20 s.2 and the model keeps declining to state what the slide attributes. Unlike q006 this
does not flip. **Matters later:** this is the clean target for Spike Pass 1's generation/prompt
lever — a definitional-by-attribution question where the assertion bar reads as too cautious.

### A REFUSAL can state no gaps at all
Run 1's q006 refusal printed `gaps: (none stated)` — a decline that names nothing missing. Not an
integrity failure (the coverage marker parsed; `integrity_flag_rate` was 0% across both runs), and
the Grounder reports the model's statement unrewritten by design. But a refusal with an empty gap
list tells the student nothing about *what* their materials lack, which is the one useful thing a
refusal carries. **Matters later:** worth a prompt-side look in the same pass as q005 — and note
`_detail`'s "(none stated)" rendering existed for exactly this shape and had never fired before.

### The metric vector, both runs
| metric | run 1 | run 2 |
|---|---|---|
| grounded_pass_rate | 75.0% (6/8) | **87.5% (7/8)** |
| false_refusal_rate | 25.0% (2/8) | 12.5% (1/8) |
| partial_rate | 0.0% | 0.0% |
| integrity_flag_rate | 0.0% | 0.0% |
| correct_refusal_rate | **100%** (4/4) | **100%** (4/4) |
| hallucination_rate | 0.0% (0/4) | 0.0% (0/4) |
| retrieval_hit_rate | **100%** (8/8) | **100%** (8/8) |
| error_count | 0 | 0 |

Stable across both: **zero hallucinations, 4/4 honest refusals, zero integrity flags, zero errors,
and 8/8 retrieval** — the trust-critical column did not move. All the variance sits in the
assert/decline decision on in-corpus questions. The exit gate passed both times.

---

## 2026-07-27 — fourth live run, plus the first attribution/entailment spot-check

Run 4 of the gate (post second-review fixes): `grounded_pass_rate` **87.5% (7/8)**, `partial_rate`
0%, `false_refusal_rate` 12.5% (q005 again), `correct_refusal_rate` 100% (4/4),
`hallucination_rate` 0%, `integrity_flag_rate` 0%, `retrieval_hit_rate` **100% (8/8)**,
`error_count` 0. Gate PASSED. Nothing new in the vector; q006 landed GROUNDED again (it is the
±12.5-point mover, see above), q005 is now a **five-time** reproduction.

### `integrity.ok=True` means structural honesty, NOT completeness of attribution — observed
*(Nate's observation from his own run; reproduced under probe and recorded here with evidence.)*

The **mechanism** is already settled doctrine and is not new: ADR 0015 says V1 catches only the
**coarse** zero-citation case (non-empty prose with *zero* labels), because per-claim presence needs
claim segmentation; `grounder.md` §Failure-modes and `answer.py::_validate` both say the same. What
was never recorded is that it **actually fires on this corpus**, and how much of an answer it can
let through.

Measured on **q008** ("According to Charles H. Long, what is one of the oldest and most widespread
religious symbols…"), 2026-07-27, sentence-split probe over the returned prose:

```
q008  state=GROUNDED  integrity.ok=True  coverage.complete=True  gaps=[]
  [UNCITED] One of the oldest and most widespread religious symbols, according to Charles H.
  [UNCITED] Long, is the symbol of Mother Earth.
  [UNCITED] This symbolism arises because of the homology between creation and woman; …
  [CITED  ] The earth itself is likened to a woman, … [S1][S3]
  sentences=4  uncited=3  labels_used=['[S1]', '[S3]']
```

**Three of four sentences carried no label, and the answer still scored GROUNDED.** The coarse guard
is satisfied by the *one* labelled sentence, so nothing fires. Note what this means against ADR 0014's
central rule — "assert a claim only if you can attach a valid `[S#]`; anything you cannot label goes
into the coverage statement instead": those three sentences escape **both** halves. They are neither
labelled nor declared as gaps, and `coverage` still reads `complete`. So on this corpus **GROUNDED
means "at least one claim is attributed and the marker parsed", not "every claim is attributed."**

**Matters later — and this is the cheaper half of the V3 faithfulness problem.** Two different holes
are usually spoken of together; they are not the same and do not cost the same to close:

| hole | what is wrong | what detecting it needs |
|---|---|---|
| per-claim citation **presence** (this entry) | a claim carries no label at all | claim segmentation only — **no judge** |
| claim↔chunk **entailment** (N2/N4, ADR 0014's accepted limitation) | the cited chunk doesn't support the claim | a judge / semantic eval — V3 |

ADR 0015 ruled per-claim presence "not feasible in text-parse V1", and as an **enforcement** rung
that still stands (a sentence splitter deciding what gets INTEGRITY_FLAGGED would misfire on lists,
headings, and transitional prose). But the probe above shows a crude splitter is already good enough
to **measure** it. An `uncited_sentence_rate` alongside the ADR 0023 vector would be spike telemetry,
not a validation rung — it would have surfaced this without a judge and without changing any state.
Not proposed as a decision here; flagged as available cheaply if Spike Pass 1 wants it.

### First actual entailment spot-check: the cited answers are faithful (3 of 3 checked)
Nothing in any prior pass had ever compared an answer's prose against the text of the chunks it
cited — V1 guarantees structure, not truth (ADR 0014), so the question was simply left open. Checked
by hand for q001, q006, q008 by printing each citation's stored chunk text beside the prose:

- **q001** — the five-type taxonomy is near-verbatim from S1/S2 (Lecture 19 s.2 + Lecture 18 s.4).
- **q006** — the objective/subjective contrast quotes Lecture 20 s.6 directly; the "meaning over
  factual correctness" gloss is supported by s.7's discussion prompt.
- **q008** — supported, *including the three uncited sentences above*: S1's text contains "one of the
  oldest and most widespread of religious symbols is the symbol of Mother Earth… It is the woman who
  bears children; it is she who experiences the mysteries of birth, growth, and change."

**This is a spot-check of three answers on one run, not a measurement** — it says the mechanism is
not obviously broken, and it is the first evidence in either direction. Note the interaction worth
carrying: q008's *uncited* claims were nonetheless *entailed*, so the two holes above are genuinely
independent — an answer can fail attribution while passing faithfulness, which is exactly why a
single "is it grounded?" number could never separate them.

### PARTIAL's root-cause ambiguity is disambiguated by the OTHER signal — and PARTIAL has never fired
*(Nate's observation: a PARTIAL means either the corpus genuinely lacks the rest, or retrieval missed
a chunk that exists — bad chunking, k too small, weak embedder.)*

The ambiguity itself **is already recorded**, verbatim, in ADR 0023's Context: "*its root cause is
ambiguous — 'corpus genuinely partial' vs. 'retrieval missed a chunk that exists' — and the Grounder
cannot tell them apart*." That is the whole reason §2 refuses to collapse PARTIAL into PASS.

What ADR 0023 does **not** say, and what is worth having written down as a reading procedure: **the
two-signal split is precisely the instrument that separates those two causes.** The Grounder alone
cannot, but the pair can —

| state | `hit` | reads as |
|---|---|---|
| PARTIAL | **yes** | the expected chunk WAS in the top-k and the model still declared a gap → **generation/prompt** lever |
| PARTIAL | **no** | the expected chunk never reached the model → **chunking / k / embedder** lever |

with one caveat that ties to the any-match rule (`scoring.retrieval_hit`): on a **multi-source** row
(q001, and q003 since its curation) `hit=yes` only means *at least one* expected source arrived, so
`PARTIAL + hit=yes` there can still be an honest corpus gap on the *other* leg. The discriminator is
clean on single-source rows — 6 of the 8 in-corpus questions today.

**And the table has never been exercised: `partial_rate` is 0.0% on all four live runs.** The bucket
ADR 0023 spends most of its length protecting has not fired once on this corpus. Every in-corpus
question so far resolves all-or-nothing — GROUNDED, or a flat REFUSAL (q005). **Matters later:** a
spike-ranking instrument whose most carefully-designed signal is empty is ranking on a narrower basis
than its design assumes, and the same q005-shaped question that produces a flat refusal today is the
one most likely to produce a PARTIAL under a tuned prompt. Worth watching in Spike Pass 1 rather than
acting on now.

---

## 2026-08-01 — Spike Pass 1, the chunking probe (#59): eight runs across three windows

First runs on the parameterized chunker. Each window ran under its **own owner**, so each got its own
corpus — `_converge_corpus` skips any file already `ready` for an owner, matching on filename alone,
so a second window under one owner would have silently scored the first window's chunks.

| window | owner | chunks | runs | `grounded_pass_rate` | `retrieval_hit_rate` | q005 | q007 |
|---|---|---|---|---|---|---|---|
| 150/25 | `nate-spike-chunk-150-25` | 184 | 3 | 8/8, 8/8, 8/8 | 8/8 ×3 | GROUNDED ×3 | hit=yes ×3 |
| 250/40 *(default)* | `nate-spike-chunk-250-40` | 122 | 2 | 7/8, 7/8 | 8/8 ×2 | REFUSAL ×2 | hit=yes ×2 |
| 500/80 | `nate-spike-chunk-500-80` | 80 | 3 | 6/8, 5/8, 6/8 | **7/8 ×3** | REFUSAL ×3 | **hit=no ×3** |

`partial_rate`, `integrity_flag_rate`, `hallucination_rate` and `error_count` were **0 in every run**;
`correct_refusal_rate` was **4/4 in every run**. The gate passed on all eight.

**Not a ranking, and the table must not be read as one** (ADR 0022 §1; the comparison protocol is
Pass 2's, #45). The one 5/8 reading at 500/80 had a third in-corpus question refuse that was not
captured in that run's transcript — one question is exactly the noise floor #45 measured, and it is
recorded here as noise rather than reconstructed.

### RED — at 500/80 the expected page for q007 falls out of the top-5 entirely
Reproduced **3/3**, and `Livingston Cosmogony.pdf` p.4 is a **single-source** row, which
`_require_expected_files` already names as "a guaranteed permanent miss". This is the cheap design
signal Pass 1 exists to catch, on exactly the axis ADR 0023 §1 assigns to chunking.

Mechanism, from a direct top-5 probe of the same question against all three corpora:

| window | p.4's chunk | its score | rank | what displaced it |
|---|---|---|---|---|
| 150/25 | 150w | **0.614** | 1 | — |
| 250/40 | 250w | 0.565 | 1 | — |
| 500/80 | folded into a 500w chunk | absent | — | p.9 chunks at 0.562 / 0.555 |

A wider window dilutes a specific passage's topical signal until a neighbouring page outranks it. The
result reproduces exactly, unlike the generation-side wobble below, because retrieval over a fixed
index is deterministic — worth remembering when deciding how many runs a future reading needs.

### q005 is a chunking-side lever too — which qualifies, but does not contradict, the entry above
The 2026-07-27 entries record q005 as "a generation-side lever, not retrieval," on the evidence that
retrieval always hit. That remains true, and the probe makes it sharper: the target chunk (Lecture 20
s.2, **45 words**) is one chunk at *every* window, is byte-identical across all three, and retrieves
at the **identical score 0.565** in each — `hit=yes` throughout. Nothing about the target changed.

What changed is what shares the top-5 with it, and therefore how much of the assembled context it is:

| window | top-5 total | q005's target as a share | outcome |
|---|---|---|---|
| 150/25 | ~615 words | 7.3% | GROUNDED ×3 |
| 250/40 | ~966 words | 4.7% | REFUSAL ×2 (plus five prior reproductions) |
| 500/80 | ~1416 words | 3.2% | REFUSAL ×3 |

So a question can be moved by the chunk window **without its own chunk or its retrieval hit changing
at all**. Offered as a hypothesis consistent with three windows, not a law: n=3, one corpus.

**Matters later, and it matters to #60:** the prompt lever and the chunk window are *not independent*
levers on q005. A wording change evaluated at the default window and a window change evaluated at the
default wording are measuring an interaction neither one owns.

### What this sweep could not vary — state this beside any reading of it
23 of the 52 parsed units are deck slides, the longest is **133 words**, and every one of them is a
single chunk at every window tested. Deck chunk *text* was therefore byte-identical across the whole
sweep, and six of the eight in-corpus rows are deck-anchored. The window's reach into those rows is
entirely indirect — through what else lands in the top-5, which is precisely how q005 moved. An
all-green sweep on this corpus would have proven less than it appeared to.

One further blind spot, structural: `retrieval_hit`'s any-match rule means the multi-source rows
(q001, and q003 since its curation) cannot report a chunking-induced miss as long as one leg survives.
The signal is clean on the six single-source rows.

### PARTIAL still has never fired
`partial_rate` was 0.0% in all eight runs, across three different corpora. The bucket ADR 0023 spends
most of its length protecting remains empty, now including under windows that visibly changed both
retrieval and grounding outcomes.
