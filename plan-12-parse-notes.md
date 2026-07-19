# plan-12 — parse-notes: extract PPTX speaker notes

> **TEMP build spec — delete before opening the PR.** Untracked on purpose (never `git add` this).
> This is the context bridge: any fresh session or subagent building #12 should read this first
> instead of re-deriving the design from the ADRs.

**Issue:** [#12](https://github.com/Natenc12/grounded-class-tutor/issues/12) · **Branch:** `nate/parse-notes` · **Touches:** `src/gct/ingest/parse.py`, `tests/gct/ingest/{test_parse.py,conftest.py}`

---

## Goal

Extend `_parse_pptx` to also extract each slide's **speaker notes** and merge them into that
slide's existing `ParsedUnit`, so decks whose real teaching lives in the professor's notes stop
being invisible to retrieval.

## Scope & non-goals

**In scope**
- Per-slide speaker-notes extraction, merged into the same slide's unit.
- A notes-only slide (empty body) still yields a unit.
- Guard slides with no notes part present.
- Unit tests: notes-only · body+notes · no-notes · never-span-still-holds.

**Explicitly NOT in this build** (guard against scope creep):
- **Do not touch `_parse_pdf`** — PDFs have no notes concept.
- **Do not touch `ParsedUnit`'s shape.** No new field for notes. The whole reason this issue is
  small is that notes ride inside the existing `text` field; adding a field would ripple into
  `chunk_units` and the Grounder's context builder, both downstream of a locked contract.
- **Do not touch chunking.** `chunk_units` splits `unit.text` into word windows and is agnostic to
  where the words came from.
- **Do not do tables** — that's #13, which this unblocks.
- **Do not filter professor asides** ("skip if short on time"). The issue accepts this as minor
  retrieval noise for the coverage gained.

## Invariants this build rests on

| Invariant | Source span | How this build honors it |
|---|---|---|
| Never-span: a unit maps to **exactly one** `page_or_slide` (scalar) | ADR 0019 §Decision, "Cross-boundary rule — (a) NEVER span" | Notes merge into **their own slide's** unit. Still one unit per slide; the rule is satisfied *by construction*, not by a check. |
| Provenance born at parse, never lost — honor-point ① | ADR 0019 §Decision SPEC bullet 1; `parse.py` module docstring | Notes text inherits the same `(file, page_or_slide)` stamp as the body. No new provenance path. |
| Zero-text is terminal only for the **whole file** | ADR 0020 (terminal taxonomy); `parse_file:76` | A slide counts as non-empty if body **or** notes has text. `ParseError("empty")` fires only when every slide has neither. This build *shrinks* the set of files that raise `empty`. |
| Terminal reasons come from the `files.failed_reason` taxonomy | `parse.py:25` `TERMINAL_REASONS` | Unchanged — no new reason introduced. |
| Parse stays pure (PM-4 seam) | `parse.py` module docstring; ADR 0020 | No DB, no status, no I/O beyond reading the file. |

## Grounding facts (verified against the repo, this session)

1. **`slide.notes_slide` auto-creates the notes part as a side effect.** Verified live on
   python-pptx 1.0.2: `has_notes_slide` reads `False`, then flips to `True` merely from accessing
   `.notes_slide`. **Therefore the guard must be `if not slide.has_notes_slide: return ""` — checked
   *before* touching `.notes_slide`.** Getting this backwards silently mutates the in-memory deck
   and makes a "no notes" test tautologically pass.
2. `notes_slide.notes_text_frame` is **not** `None` on a fresh notes slide; it returns a frame whose
   `.text` is `""`. So an existence check on the frame is not enough — check the *text*. It can be
   `None` on decks from other authoring tools, so guard both.
3. **Existing body extraction joins paragraph runs**, not `shape.text` (`parse.py:132-135`) — it
   walks `text_frame.paragraphs` and concatenates `run.text`. Notes extraction should read
   `notes_text_frame.text` directly; the run-walking exists for grouped-shape flattening, which
   notes frames don't have.
4. **The blank-slide test asserts survivors are not renumbered** (`test_parse.py:73`,
   `[1, 3]` from `["First", None, "Third"]`). Numbering comes from `enumerate(deck.slides, start=1)`,
   independent of whether a unit is emitted — this build does not change that, but a notes-only
   slide will now *stop* being skipped, so re-read that test's intent before touching it.
5. **`[S#]` is the citation-label vocabulary** — ADR 0015 §② renders `[S1] (lecture-3.pdf, p.4)`,
   and the validator parses labels out of the model's output. This is why the notes marker below is
   **unbracketed**.
6. **The repo has zero logging** — `grep -rn "logging" src/gct/` returns nothing. See the flagged
   decision below.

## Resolved decisions

**D1 — Notes merge format: `Speaker notes:` prefix, unbracketed.** *(Nate decided)*

```
Aquinas: five ways
Motion, causation, contingency

Speaker notes:
The third way is the one they always miss on the exam — walk it slowly.
```

Body first, blank line, then the marker line, then notes. Rationale: the model (and you, debugging a
retrieval result) can tell the professor's spoken framing from an authored slide claim. Deliberately
**not** `[Speaker notes]` — bracketed tokens live in the same text the ADR-0015 citation validator
parses, and buying a scannability nicety with a vocabulary collision against the product's trust
mechanism is a bad trade. Rejected alternative: plain join with no marker (simpler, but loses the
body/notes distinction permanently — it's unrecoverable once merged).

**D2 — Notes-extraction failure degrades; it is not terminal.** *(Nate decided)*

The notes read gets its **own** try/except, *inside* the existing per-slide block. A failure yields
`""` and the slide still emits its body text. Rationale: notes are a coverage add-on — one corrupt
notes part shouldn't turn a perfectly readable deck into a terminal `unparseable` that the student
experiences as a refusal. Body-text extraction failure stays terminal, unchanged.

**D3 — Stay silent in V1. No logging; Slice 2 owns it.** *(Nate decided, at plan review)*

The except swallows the failure with an **explicit comment** saying it's deliberate and naming the
tradeoff — no `logging` import, no logger, no convention introduced.

**Nate's rationale:** "what is even using the logs if we make them now?" Nothing consumes them in V1 —
no aggregator, no dashboard, no unattended worker. Adding a log line now is writing an output with no
reader, and the convention it sets would be set without knowing what Slice 2 actually needs from it.
Defer to Slice 2, where the ingestion worker runs unattended and the consumer is real.

**Honest note for the record (not a re-litigation — the decision stands):** during Slice 1 there *is* one
consumer, which is Nate running scripts in a terminal. The cost of D3 is that a corrupt-notes case will
present as an inexplicable refusal with no trail. This is accepted as low-likelihood in a V1 dogfood
corpus. **Add "log the D2 swallow" to the Slice 2 observability list** so it isn't lost.

<details><summary>Superseded proposal (kept for the reasoning, not the outcome)</summary>

**The scenario, in plain English:** a student uploads a deck whose slide-12 notes part is corrupt. Per
D2 we deliberately skip those notes and keep going (good — that's the decision). The student then asks
a question whose answer *was in slide 12's notes*, and the tutor says "I can't find that in your
materials." **That refusal is indistinguishable from an honest one** — the content was in the file, a
bug ate it, and nothing anywhere records that it happened.

"Logging" here just means the program writes a one-line note as it runs (`WARNING: couldn't read notes
on slide 12 of lecture-4.pptx`). It changes no behavior and no student ever sees it; it only means that
when you're debugging a refusal you think is wrong, there's a trail instead of a guess.

"Introduces a convention" just means: no file in this project writes log lines today, so this would be
the first, and the style picked here becomes the one later modules copy. Small architectural precedent
— hence your call, not mine (grounding fact 6).

**Scope: two lines. There is no system to set up.** Verified live this session — `logging` is stdlib and
works with zero configuration (no config file, no dependency, no init):

```
>>> logger = logging.getLogger("gct.ingest.parse")
>>> logger.warning("could not read notes on slide 12")
could not read notes on slide 12          <- stderr, no setup
>>> logger.info("routine chatter")
                                          <- silent; default threshold is WARNING
```

So the whole diff is `import logging` + `logger = logging.getLogger(__name__)` at module level, and one
`logger.warning(...)` in the except. The *big* version (handlers, formatters, structured JSON, log
aggregation) is a real project and belongs in **Slice 2**, when the worker runs unattended — not here.
`getLogger(__name__)` is exactly what that later system plugs into, by stdlib design.

**The alternative is a bare `except: notes = ""` with an explanatory comment** — smaller diff, but
genuinely silent. Say the word at review and I'll switch it.

</details>

## Module shape

`src/gct/ingest/parse.py` — one new private helper + edits to `_parse_pptx`. No public API change;
`parse_file`'s signature and `ParsedUnit`'s shape are untouched (both are contracts #2/#4 already bind to).

```python
_NOTES_MARKER = "Speaker notes:"          # unbracketed by design — see D1 / ADR 0015

def _slide_notes(slide) -> str:
    """Return a slide's speaker-notes text, or "" if it has none."""
    # Guard on has_notes_slide FIRST — reading .notes_slide auto-creates the part.
```

`_parse_pptx`'s loop becomes: extract body (terminal on failure, unchanged) → extract notes
(degrading, D2) → join with the marker if notes are non-empty → emit a unit if the **combined** text
is non-empty.

## Build order

One function at a time. Each step names the test that proves it.

| # | Build | Test that proves it |
|---|---|---|
| 1 | `make_pptx` in `conftest.py` grows an optional `notes` parameter (fixture work — do this first so every later test has data) | existing `test_parse.py` still green — the parameter must default to no-notes and change nothing |
| 2 | `_slide_notes(slide)` — the guard + text read | `test_slide_with_no_notes_returns_empty` · `test_notes_slide_not_created_as_a_side_effect` (asserts `has_notes_slide` is still `False` after the call — this is grounding fact 1, and it's the one that bites) |
| 3 | Merge into `_parse_pptx` with the marker | `test_body_and_notes_merged_into_one_unit` · `test_notes_marker_present_and_unbracketed` |
| 4 | Notes-only slide still yields a unit | `test_notes_only_slide_yields_a_unit` · `test_file_empty_only_when_no_slide_has_body_or_notes` |
| 5 | Degrading failure (D2) | `test_corrupt_notes_part_keeps_body_text` (monkeypatch `notes_slide` to raise, mirroring `test_malformed_slide_content_raises_unparseable:183`) |
| 6 | Never-span regression | `test_notes_do_not_leak_across_slides` — slide 1's notes must not appear in slide 2's unit |
| 7 | **Delete `plan-12-parse-notes.md` + `plan-12-parse-notes.html`, open the PR** | — |

## Risks & things to watch

- **The side-effect trap** — reading `.notes_slide` before the guard makes the no-notes test
  tautological. Grounding fact 1; build step 2 has a dedicated test for it.
- **Existing test intent shifts** — `test_blank_slide_skipped_without_renumbering_survivors`
  (`test_parse.py:73`) asserts `[1, 3]`. A notes-only slide will now *stop* being skipped. Grounding
  fact 4; re-read that test's intent before touching it.
- **Backwards compatibility** — files that previously raised `ParseError("empty")` may now parse
  successfully. That is the intended behavior change, but it means the `empty` terminal is strictly
  **narrower** than it was, and `files.failed_reason` histories from before this change aren't
  comparable to ones after it.

## Definition of done

- [ ] All stubs implemented; no `NotImplementedError` left in `parse.py`
- [ ] `uv run pytest tests/ -q` green (full suite — this touches a function three other modules read through)
- [ ] `uv run ruff check src/ tests/` clean
- [ ] Existing `test_parse.py` tests pass **unmodified** where possible — if one had to change, be able to say why
- [ ] `plan-12-parse-notes.md` and `.html` deleted
- [ ] Self-review against ADR 0019 (never-span) and ADR 0020 (zero-text terminal) — the two this build could plausibly violate
- [ ] No `logging` import in the diff (D3) — the D2 except carries an explanatory comment instead

## Fixtures / infra notes

**Created by prep:**
- `make_pptx` / `pptx_factory` in `tests/gct/ingest/conftest.py` gained an optional positional
  `slide_notes` list. Backward-compatible by default (no slide gets notes), so no existing test changed.
  A `None` entry means **no notes part at all** — distinct from empty notes, because `notes_slide`
  creates the part on access. It raises on a length mismatch with `slide_texts`.
- `_NOTES_MARKER` + the `_slide_notes` stub in `src/gct/ingest/parse.py`.
- A `TODO(#12, build step 3)` block in `_parse_pptx` marking exactly where the merge goes, with the
  three constraints (own try/except · marker join · combined-text emptiness check) inline.

**`_parse_pptx` was deliberately left functionally intact**, so you start from a green suite
(55 passed) rather than a broken one. Build step 2 is the first thing that turns anything red.

**Do not touch:** the `db` and `fake_embedder` fixtures (issue #4's, unrelated to this build), and
`make_pdf` / `make_ole_stub`.

This issue needs **no DB and no API key** — it's pure. Preflight confirmed both are available anyway.
