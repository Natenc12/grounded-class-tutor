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
