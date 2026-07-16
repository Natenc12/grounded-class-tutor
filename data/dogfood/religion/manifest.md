# Dogfood corpus — `religion-rph202`

The real course materials the Slice-1 eval smoke suite (`eval/questions.jsonl`) is scored against.
**The files themselves are gitignored** (copyright; ADR 0021 / C5 — real materials the dogfooder
owns). This manifest is the committed, reproducible-by-description record of what the bench points at:
drop these exact files into `data/dogfood/religion/` to run the suite.

**Class:** RPH 202 — Fundamentals of Religion (Sophia University; instr. Tatsuo Murakami).
**Topic cluster:** cosmogony / creation myths — three sequential lecture sessions plus two
supplemental scholarly readings, all on the same theme (tight topical coverage so out-of-corpus
refusal questions can be *plausibly* in-scope rather than obviously unrelated).

| file | sha256 (first 16) | size | parsed units | role |
|---|---|---|---|---|
| `Lecture 18 Cosmogony.pptx` | `3dfdf4a444ceccac` | 468K | 7 slides | in-corpus (clean body text) |
| `Lecture 19 Types of Cosmogony.pptx` | `bd9a72b72000a0c0` | 2.0M | 8 slides | in-corpus (clean body text) |
| `Lecture 20 Evolutionist-Creationist Debate.pptx` | `712bd0974950a05d` | 6.8M | 8 slides | in-corpus (clean body text) |
| `Livingston Cosmogony.pdf` | `67ff5d723dfa5a63` | 3.5M | 15 pages | supplemental — **scanned/OCR'd, noisy** |
| `Charles H. Long The Myths of Creation.pdf` | `dd9b6b466abb35fe` | 3.9M | 14 pages | supplemental — **scanned/OCR'd, noisy** |

## Known caveat — the two PDFs are scanned + OCR'd
Both PDFs parse to *mostly* readable prose peppered with OCR corruption (mojibake runs, `Part III` →
`rt lll`, garbled headers). The suite therefore **anchors known-answer rows on the clean PPTX decks**
and cites the PDFs only where the answer sentence itself is clean (`q007` Livingston p4 — Nut/Geb/Shu;
`q008` Long p3 — Mother Earth). This is a real dogfood condition, not a bug in this bench — a candidate
**future issue** (scanned-PDF / OCR quality) that this material happens to surface.

## Suite composition (`eval/questions.jsonl`, `suites:["smoke"]`)
12 questions — 8 in-corpus (`answer`) + 4 out-of-corpus (`refuse`).

- **In-corpus** spread across all three decks and both PDFs: taxonomy of the five cosmogony types
  (q001/q004), Navaho myth (q002), definition of cosmogony (q003), evolutionist-creationist debate
  (q005/q006), Egyptian Nut/Geb/Shu (q007, PDF), Long on Mother Earth (q008, PDF).
- **Out-of-corpus** are *plausible-but-uncovered*, verified absent across all five files: Norse/Ymir
  (q009), reincarnation & karma (q010), eschatology / end-times (q011 — a deliberate inversion of the
  origins theme), Chinese Pangu (q012). Note the five taught examples — Navaho, Japanese Kojiki,
  **Babylonian Enuma Elish**, Prometheus/Demiurge, Genesis — are all *in* corpus, so none of those is a
  valid refusal.

Scoring rule: ADR 0023 (two-signal — retrieval hit vs. Grounder state; PARTIAL is a tracked bucket,
not a pass). Schema: ADR 0021 §3. Author it alongside the tracer (they land together).
