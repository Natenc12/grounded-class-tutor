# 0029. The ingest input ceiling — counted in words, set at 250,000, refused as `too_long` — amending ADR 0020

- **Date:** 2026-08-30
- **Status:** accepted — amends **ADR 0020** (its §1 terminal/bad-input set, which enumerated three
  kinds of bad input — corrupt, password-protected, unsupported/zero-text — and now carries a
  fourth; §1's transient half, §2's all-or-nothing replace and §3's index-write-only boundary all
  stand unchanged)

## Context
Nothing bounded how large an ingested file could be. `compose` ran parse → chunk → **embed** over
whatever `parse_file` returned, and the provider adapter's sub-batching bounds each *request* while
happily emitting unlimited batches — so a single arbitrarily large PDF embedded without limit, the
only ceiling being OpenAI's own rate limits (issue #43). Every other cost axis was already closed:
generation retries are hard-capped, empty retrieval short-circuits before any paid call, and the
ingest pipeline has no retry loop by design (ADR 0020). Raw input volume was the one open lane.

Three things had to be settled together, because each constrains the others: **what to count**,
**how much**, and **what the failure is called**.

**What this is NOT.** Issue #43 called it a *cost cap*. At roughly 1.5 cents per 1,000 pages
embedded, cost is not the exposure and no number chosen for cost reasons would be defensible. The
honest framing is a **finite ceiling against pathological input** — the property that a single
upload cannot start unbounded work. Worker *time* is the other tempting justification and is equally
unavailable: no ingest throughput has ever been measured (nothing in `eval/FINDINGS.md` records
one), the same evidence gap ADR 0028 named when it ratified the lease unmeasured. A number argued
from time would be a guess wearing a unit.

## Decision

### 1. The unit is WORD COUNT
`sum(len(unit.text.split()) for unit in units)`, taken over `parse_file`'s output — the literal word
stream the chunker is about to consume, using the same split `_chunk_one` uses.

Measured on the five-file dogfood corpus, 2026-08-30, with the shipped parser:

| file | size | units | words | words / unit |
|---|---|---|---|---|
| Lecture 20 Evolutionist-Creationist Debate.pptx | 7.11 MB | 8 slides | 315 | **39** |
| Lecture 19 Types of Cosmogony.pptx | 2.13 MB | 8 slides | 550 | 69 |
| Lecture 18 Cosmogony.pptx | 0.48 MB | 7 slides | 343 | 49 |
| Charles H. Long The Myths of Creation.pdf | 4.12 MB | 14 pages | 6,529 | 466 |
| Livingston Cosmogony.pdf | 3.70 MB | 15 pages | 12,300 | **820** |
| | | | **20,037** | |

- **Bytes are decorrelated from the work.** The *largest* file in the corpus has the *fewest* words:
  Lecture 20 is 7.11 MB of mostly images and 315 words. Images make a file fat and cost nothing to
  embed.
- **Pages have a 21× density spread** across the same five files — 39 words/slide to 820
  words/page. A page cap barely bounds the real work; the same "50 pages" is 2,000 words or 41,000.
- **Chunk count moves with a knob that is still being tuned.** Chunks overlap by 40 words, so the
  count depends on the chunk window (stride = size − overlap = 210 today). A 1,500-chunk cap admits
  ~315k words at a 250-word window and ~540k at 400 — the same cap silently loosens ~70% when a
  provisional ADR 0019 knob moves, and Spike Pass 2 still has a live chunking axis (ADR 0026).
  Billing is per token, not per chunk.
- **Words are density-aware, free to measure, invariant to the chunking experiment**, and at ~1.33
  tokens/word they map near-linearly onto the bill.

### 2. The limit is 250,000 words, as one knob
`config.MAX_INGEST_WORDS = 250_000`, reached by `compose`/`ingest_file`'s `max_words` parameter,
which defaults to it — the same shape `chunk_size`/`chunk_overlap` already use (ADR 0019). One
constant; a caller may lower it without a second copy of the number existing anywhere.

250,000 is **~12× the entire current corpus** (20,037 words) and about a large textbook. It is
deliberately far above any real course upload, because the ceiling exists to make the work *finite*,
not to be tight (see *Context* — nothing measured supports tightness). It is a config default, not a
contract: raise or lower it without a new ADR.

### 3. The failure is terminal, named `too_long`
Past the ceiling, `compose` raises `ParseError("too_long", ...)` **before `embed`** — the position is
the decision, since embedding is the pipeline's only paid call and a ceiling checked after it bounds
nothing. This lands in ADR 0020 §1's terminal/bad-input class: a file is exactly as long on the next
attempt, so it skips the retry budget entirely, and `worker.py`'s existing `except ParseError` writes
`exc.reason` into `files.failed_reason` untranslated. **`files.failed_reason` is therefore widened**
from five values to six (`migrations/0003_failed_reason_too_long.sql`) — a new terminal reason is a
schema change, not a constant.

The identifier is `too_long`, **not** `too_large` or `oversize`. "Large"/"size" points a reader at
bytes, which §1 measured as the wrong mental model; the value a student sees should not contradict
the unit the guard actually uses.

This is a **precondition, not queue machinery**. It lives inside the pure pipeline, so the PM-4 seam
holds and Slice 2's worker wraps it unchanged (ADR 0020).

## Alternatives considered
- **Byte cap** — the cheapest thing to measure and the least related to the work; the corpus's
  largest file is its smallest job (§1).
- **Page/slide cap** — the unit a user would recognize, and the one N6 speaks in ("~50-page decks").
  Rejected on the 21× measured density spread: it bounds a number nobody is billed for.
- **Chunk-count cap** — closest to the paid call, and rejected for exactly that reason: it is
  downstream of a provisional knob (ADR 0019) that Spike Pass 2 is still moving, so the ceiling would
  drift ~70% without anyone editing it.
- **Token count via `tiktoken`** — the *true* billing unit, and the honest upgrade. Rejected for V1:
  it adds a dependency and a tokenizer tied to one provider, to sharpen a number chosen ~12× loose on
  purpose. Words are within a constant factor (~1.33) of it. Revisit if the ceiling is ever tightened
  to where the factor matters — the same place `chunk.py` already parks true-token sizing.
- **A new exception type instead of a `ParseError` reason** — would need the worker's terminal path
  widened to reach an outcome the existing path already produces. Nothing about the handling differs.

## Consequences
- **No worker change.** `worker.py`'s `except ParseError` already buries the job with
  `reason=exc.reason` and zero retries; the new reason passes straight through. The migration is what
  makes that write legal.
- The rejection is invisible to `retrieve`/`answer`: nothing is ever indexed, so the read path sees
  the same corpus it would have if the file were never uploaded.
- A file over the ceiling is refused **whole** — there is no partial ingest, by ADR 0020 §2.
- V2's per-caller rate limit and billing ceiling (N15) remain a separate, API-side story; this closes
  only the ingest-side lane.
