# WIP — Issue #2 `chunk` (never-span chunking)

> **TEMPORARY working doc — delete before opening the PR.** This is scaffolding for building
> issue #2 on branch `nate/chunk-never-span`, not a permanent design doc. Canonical truth stays in
> `design/decisions/0019-chunking-contract-never-span.md` and `design/components/ingestion-worker.md`.
> Nate is building this; Claude assists — do **not** implement it wholesale.

Issue: https://github.com/Natenc12/grounded-class-tutor/issues/2 (assigned: Natenc12)

---

## The job, in one line
Cut each parsed page/slide into smaller, self-contained, embeddable passages — **without any chunk
ever spanning a page/slide boundary.** Pure function, no DB/job state (PM-4 seam).

## Input → Output
```
parse.py  ──▶  chunk.py  ──▶  (embed, later)
list[ParsedUnit]        list[TextChunk]
(text, file,            (text, file,
 page_or_slide)          page_or_slide)
```
`ParsedUnit` = one whole page/slide (already merged, in `src/gct/ingest/parse.py`).
`TextChunk`  = one sub-page passage (what we build here).

## The contract (LOCKED — ADR 0019, downstream binds to this)
1. **Carries provenance** — every chunk keeps `(file, page_or_slide)`. Honor-point ① — no chunk
   without a source.
2. **Self-contained / embeddable** — stands on its own as a retrieved unit and as a citation.
3. **Exactly ONE page/slide** — `page_or_slide` stays a scalar `int`, never a range. **Never-span.**
4. **Pure function** — `list[ParsedUnit] -> list[TextChunk]`. No DB, no scope IDs, no job state, no
   `chunk_id`. Identity/scope/embedding are attached by later stages.

## The spike (NOT locked — tune against eval later, ADR 0019 / 0021)
Boundary method, target size, overlap, tokenizer tooling. We pick reasonable provisional defaults
now and make them trivially swappable.

---

## Decisions we locked in this session
| # | Decision | Choice | Why |
|---|----------|--------|-----|
| 1 | Output type | **New `TextChunk`** (frozen dataclass; `text, file, page_or_slide`) — NOT reuse `ParsedUnit` | Type answers "has this been chunked yet?"; prevents passing raw pages into embed. Matches parse's taste. Name avoids colliding with the full-row `Chunk` in `ingestion-worker.md`. |
| 2 | Split unit | **Word-based** (`str.split()`), size/overlap as module constants | Dep-free, no mid-word cuts, decent token proxy. `tiktoken` true-token sizing is the spike upgrade, entering naturally at #3 embed-adapter (the real per-request token cap). |
| 3 | Provisional size/overlap | **~250 words / ~40 overlap (~15%)** | Focused enough for a clean embedding, big enough to be self-contained; 15% overlap keeps a boundary-split sentence whole in a neighbor. Defaults to beat, not commitments. |
| 4 | Text fidelity | **Simple/flattening** — `" ".join(window)` | Flattening (loses `\n` bullet structure) costs **nothing** on retrieval and only cosmetic polish on citation display. Revisit with the rendered `ask()` output in front of us (Slice 1), not by guessing now. Faithful offset-slicing = deferred upgrade. |

## Never-span is FREE
We chunk **within each `ParsedUnit` independently** and never merge text across units. Since a
`ParsedUnit` is already exactly one page/slide, a chunk physically cannot span a boundary. A short
slide (< size) → one chunk; a dense PDF page → several. All scalar `page_or_slide` by construction.

## The seam — who fills each `chunks` table column (data-model.md)
| Column | Filled by | Stage |
|--------|-----------|-------|
| `text`, `file`, `page_or_slide` | **chunk** | now |
| `embedding`, `embedding_model_id` | embed (from `config.ACTIVE_EMBEDDING_MODEL_ID`) | later |
| `owner_id`, `class_id`, `file_id` | pipeline (from `Job`) | row-prep |
| `chunk_id`, `created_at` | DB (`gen_random_uuid()` / default) | insert |

---

## Planned shape
```python
# src/gct/ingest/chunk.py
CHUNK_SIZE_WORDS = 250       # spike-tunable (ADR 0019 / 0021)
CHUNK_OVERLAP_WORDS = 40

@dataclass(frozen=True)
class TextChunk:
    text: str
    file: str
    page_or_slide: int       # scalar, never-span (ADR 0019)

def chunk_units(units: list[ParsedUnit]) -> list[TextChunk]: ...
```

## Edge cases to handle (turn into tests)
- [ ] Unit shorter than `CHUNK_SIZE_WORDS` → exactly **one** chunk (the whole unit), still emitted.
- [ ] Overlap must be `< size` — guard against no-progress / infinite loop (assert at module load).
- [ ] Every emitted chunk carries the **same** `(file, page_or_slide)` as its source unit.
- [ ] No chunk ever mixes text from two different units (never-span — verify provenance stays put).
- [ ] Multiple units → chunks are grouped per unit, in order.
- [ ] Windowing math: last window covers the tail (no dropped trailing words); stride = size − overlap.
- [ ] Empty / whitespace-only `text` shouldn't happen (parse skips zero-text pages) — defensive: a
      unit with real text always yields ≥ 1 chunk.

## Test file
`tests/gct/ingest/test_chunk.py` — mirror parse's test style (happy paths · boundaries · provenance).
No fixtures needed beyond hand-built `ParsedUnit` lists (pure function, no files/DB).

## Definition of done (this issue's slice of work)
- [ ] `src/gct/ingest/chunk.py` with `TextChunk` + `chunk_units`, matching the locked contract.
- [ ] Unit tests green (`uv run --extra dev pytest tests/gct/ingest/test_chunk.py`).
- [ ] Never-span + provenance-carried asserted in tests.
- [ ] Pure — no DB/job imports.
- [ ] **Delete this WIP doc**, commit, open PR referencing #2 (`Closes #2`), reconcile the board.

## Coordination (from the issue)
Depends on: — · Blocks: #4 pipeline · Parallel-safe with #1/#3/#7 · Touches only `src/gct/ingest/chunk.py`.
