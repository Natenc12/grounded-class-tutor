# plan-5-retriever — build spec

> **TEMP DOC — delete before opening the PR.** This file *is* committed with the prep scaffold (so a
> branch switch or `git clean` can't destroy it), and is removed by a **deletion commit** as the last
> build step. It is not untracked; do not "clean it up" with `git clean`.
>
> Issue: [#5 — [S1] retriever — scoped search + normalized scores](https://github.com/Natenc12/grounded-class-tutor/issues/5)
> Depends on: #4 (closed ✓, PR #21) · Blocks: #6 (Grounder)

---

## Goal

Embed the student's question and run a scoped pgvector top-k over `chunks`, returning ranked
`RetrievedChunk[]` with normalized similarity scores — the exact input shape the Grounder consumes.

## Scope & non-goals

**In scope**
- `src/gct/retriever/` package: consistency guard → embed query → scoped vector search → score
  conversion → ranked return.
- `tests/conftest.py` (new): hoisted `db` fixture.
- `tests/gct/retriever/` : new suite + a ranking-capable fake embedder.

**Explicitly NOT in this build** (scope-creep guard for the build agent)
- **No relevance gate / threshold τ.** ADR 0008 puts the refusal decision wholly in the Grounder;
  the score is *plumbed, not gated* in V1. τ is a V3 turn-on (`retriever.md:85-94`).
- **No re-ranking.** That is F11/V4 (`retriever.md:62`).
- **No answer/refuse/relevance judgment of any kind** (`retriever.md:16`).
- **No retry loop.** Embedding errors propagate; the ask-level retry budget lives in the Grounder
  (`retriever.md:69`).
- **Do not touch** `src/gct/ingest/**`, `src/gct/config.py`, or `migrations/`. The schema is
  sufficient as-is.
- **Do not modify** existing ingest tests beyond the mechanical `db`-fixture hoist.

---

## Invariants this build rests on

| Invariant | Source | What it forces here |
|---|---|---|
| **Isolation filter from V1** | `retriever.md:76-77`, F6/F12 | *Every* query — the guard's DISTINCT **and** the vector search — carries `WHERE owner_id = … AND class_id = …`. Absent scope never widens the search. |
| **Embedding-model consistency (enforced)** | ADR 0018, `retriever.md:78-80` | Compare stored `chunks.embedding_model_id` against the **active embedder's** `model_id`; fail loud on mismatch. Never auto-reconcile (ADR 0018 rejects it explicitly). |
| **Which embedder gets constructed** | CLAUDE.md, ADR 0018 | Sourced only from `gct.config.ACTIVE_EMBEDDING_MODEL_ID`. **Never hardcode a model id.** |
| **Guard compares against the embedder, not config** | CLAUDE.md invariants | The comparison's right-hand side is `embedder.model_id` — the model that *actually* produced/would produce the vectors. Sourcing both sides from config makes the guard compare config to itself and it can never fire. |
| **Score is normalized similarity** | ADR 0017, `retriever.md:74-75` | `[0,1]`, higher = more relevant, converted **at this boundary**. Rank order and score consistent by construction. |
| **No judgment in this box** | ADR 0008, `retriever.md:81-82` | No re-rank, no gate, no refusal. The Retriever reports; the Grounder decides. |
| **Return shape == Grounder input** | `retriever.md:41`, `grounder.md` interface | `{chunk_id, text, file, page_or_slide, score}` — the two boxes compose directly. Public contract; #6 binds to it. |

### The V1 no-floor consequence (state it so it isn't an accident)
With no threshold gate, the Retriever returns *k* chunks for **every** question over a non-empty
corpus. Therefore `retrieved == []` ⟺ **empty/un-ingested class** — never "matched nothing relevant"
(`retriever.md:84-94`). The entire refusal guarantee rests on the Grounder in V1.

---

## Grounding facts (verified against the repo)

Every line below was read off the real code/DB, not inferred.

- **`chunks` columns** (`migrations/0001_init.sql`, confirmed via `\d chunks`):
  `chunk_id uuid` · `file_id uuid` · `owner_id text` · `class_id uuid` · `text text` ·
  `file text` · `page_or_slide text` · `embedding vector(1536)` · `embedding_model_id text`.
- **Schema quirk — `page_or_slide` is `text`,** while `PreparedChunk.page_or_slide` is `int`
  (`pipeline.py:47`). `index.py:88` converts `int -> str(...)` on write. **This build converts back
  `str -> int` on read** (see Decision 4).
- **Schema quirk — `class_id` is `uuid`, `owner_id` is `text`.** Bind `class_id` as `%(class_id)s::uuid`
  and `owner_id` as plain text, exactly as `index.py:59` does.
- **`conn` must come from `gct.db.connect()`** (`db.py:11-19`) — it registers the pgvector type
  adapter. Without it, a `list[float]` query vector will not adapt to `vector(1536)`.
- **⚠ The query vector needs an explicit `::vector` cast** — found by smoke-testing the fixtures
  during prep, *not* by reading the docs. Passing a `list[float]` as a parameter to the `<=>`
  operator fails outright:

  ```
  psycopg.errors.UndefinedFunction:
      operator does not exist: vector <=> double precision[]
  ```

  A bare list adapts as `double precision[]`. `index.py`'s INSERT works only because the *column*
  supplies the target type; in a `<=>` expression there is no column to infer from. **Two verified
  fixes** (both confirmed against the live DB, identical results):
  - **`embedding <=> %(q_vec)s::vector`** ← **use this.** Explicit at the SQL boundary, matching the
    existing `%(class_id)s::uuid` style in `index.py:59`. No extra import.
  - `from pgvector import Vector` and pass `Vector(q_vec)` — works identically; rejected only for
    consistency with the repo's existing cast-at-the-boundary idiom.

  Verified ranking through the fix: query at angle `0.0` vs chunks at `0.05` / `1.4` rad →
  distances `0.0012` / `0.83` → scores `0.9988` / `0.17`. Correct order, correct range.
- **Scope index exists:** `chunks_owner_class_idx btree (owner_id, class_id)` — the guard's DISTINCT
  rides this.
- **ANN index exists:** `chunks_embedding_idx hnsw (embedding vector_cosine_ops)` — matches the `<=>`
  cosine operator the search uses. Using a different operator (`<->` L2, `<#>` inner product) would
  silently bypass this index.
- **Embeddings protocol** (`providers/base.py:22-35`): `model_id: str` (property) · `dim: int`
  (property) · `embed(texts: Sequence[str]) -> list[list[float]]`. Note `embed` takes a **sequence and
  returns a list of vectors** — a single query is `embedder.embed([question])[0]`.
- **`TransientEmbeddingError`** (`providers/base.py:11`) is raised by the adapter on retryable
  failures. The Retriever does **not** catch it (`retriever.md:69`).
- **`ACTIVE_EMBEDDING_MODEL_ID = "text-embedding-3-small"`, `EMBEDDING_DIM = 1536`** (`config.py:21-22`).
- **Repo precedent for spike constants:** they live in the module that uses them, not `config.py` —
  `CHUNK_SIZE_WORDS`/`CHUNK_OVERLAP_WORDS` sit in `chunk.py:34-35`. `config.py` is reserved for the
  embedding-invariant anchor + settings.
- **⚠ pgvector cosine distance ranges `[0, 2]`, not `[0,1]`** — verified live against
  `grounded_class_tutor`:

  | vectors | `<=>` distance | `1 - d` |
  |---|---|---|
  | identical direction (`[3,0,0]`,`[7,0,0]`) | `0` | `1.0` |
  | orthogonal (`[1,0,0]`,`[0,1,0]`) | `1` | `0.0` |
  | opposite (`[1,0,0]`,`[-1,0,0]`) | `2` | `-1.0` ⚠ |
  | zero vector involved | `NaN` | `NaN` ⚠ |

  ADR 0017 writes the conversion as `1 - cosine_distance` *and* promises `[0,1]`. Those cannot both
  hold. See Decision 1.
- **⚠ The existing `FakeEmbeddings` fixture cannot test ranking** (`tests/gct/ingest/conftest.py:132-137`):
  it returns `[hash(text) % 1000] + [0.0] * 1535`. Every such vector is a *positive scalar multiple of
  the same direction* → cosine distance `0` for every pair, so rank order is arbitrary. And
  `hash(text) % 1000 == 0` yields a zero vector → `NaN` distance. Adequate for ingest (which never
  ranks); unusable here. See Fixtures.
- **Fixture visibility:** `db` and `fake_embedder` live in `tests/gct/ingest/conftest.py`; there is
  **no** `tests/conftest.py`. pytest conftest scoping means they are invisible to
  `tests/gct/retriever/`. See Decision 3.

---

## Resolved decisions

### Decision 1 — Score conversion clamps at zero
- **Fork:** ADR 0017's `1 - distance` yields `[-1,1]` over pgvector's real `[0,2]` distance domain,
  contradicting the ADR's own `[0,1]` contract.
- **Options:** (a) clamp at 0 · (b) emit un-clamped `1-d`, accepting `[-1,1]` · (c) rescale `(2-d)/2`.
- **Chosen: (a)** — `score = max(0.0, 1.0 - distance)`.
- **Rationale:** keeps the ADR's formula *and* its stated range. A negative cosine similarity means
  "semantically opposite," which for a **relevance** seam is indistinguishable from "not relevant" —
  so flooring discards nothing V3's `score >= τ` would ever want to distinguish. (b) silently breaks
  the contract every downstream reader binds to; (c) preserves information but compresses all real
  scores toward `0.5` and diverges from the ADR's literal formula, so a τ calibrated later would not
  mean what ADR 0017 describes. Clamping is monotonic, so **rank order is unaffected** — the ADR's
  "ordering unchanged" property still holds.
- **Who/when:** Nate decided, prep interview.
- **Build note:** put a comment at the conversion citing the ADR-0017 range gap, so the next reader
  finds the reasoning rather than re-deriving it. Consider filing a follow-up to amend ADR 0017's
  stated formula — **not in this PR's scope.**

### Decision 2 — Guard reads the stored side via a separate scoped `DISTINCT`
- **Fork:** how the Retriever reads the corpus's stored `embedding_model_id`.
- **Options:** (a) separate pre-query `SELECT DISTINCT` over scope · (b) fold the stamp into the
  top-k SELECT and check the returned rows.
- **Chosen: (a).**
- **Rationale:** the roadmap's PM-5 foot-gun is explicit that a partial re-index leaves a class with
  **mixed** `embedding_model_id` and "the Retriever's consistency guard then ERRORs the whole class"
  (`roadmap.md:85`). Option (b) only inspects the *k rows that came back* — a stale chunk ranked #6
  stays invisible, and the resulting corruption is silent, which is precisely the failure mode ADR
  0018 exists to eliminate ("it throws no error… the system looks healthy"). The extra query is one
  indexed scan on `chunks_owner_class_idx`.
- **Who/when:** Nate decided, prep interview.
- **Consequence (falls out of this choice):** when the DISTINCT returns **no rows**, the class is
  empty/un-ingested — there is nothing to mismatch, so the guard passes vacuously and the correct
  return is `[]`. Short-circuit there, **before embedding the query**, which also saves a pointless
  paid API call. This is still spec-faithful: `[]` for an empty class is the documented behavior
  (`retriever.md:67`).
- **Mismatch semantics:** raise if the DISTINCT set contains *anything other than exactly* the active
  `embedder.model_id` — this catches both the wrong-model case and the mixed-model case in one check.

### Decision 3 — Hoist the `db` fixture to `tests/conftest.py`
- **Fork:** the shared fixtures aren't visible from the new test package.
- **Options:** (a) hoist `db` to a root `tests/conftest.py` · (b) duplicate `db` in a retriever-local
  conftest.
- **Chosen: (a).**
- **Rationale:** one seeded-class + FK-ordered-teardown implementation. Two copies drift, and the
  teardown ordering (chunks → files → classes) is exactly the kind of detail that rots in a copy.
- **Who/when:** Nate decided, prep interview.
- **Boundary:** hoist **only** `db`. The PDF/PPTX factories and `FakeEmbeddings` stay in
  `tests/gct/ingest/conftest.py` — they are ingest-specific. This is a *move*, not a rewrite; the
  ingest tests must keep passing unchanged.

### Decision 4 — `page_or_slide` converts back to `int` on read
- **Fork:** the column is `text`; `PreparedChunk` carries `int`.
- **Chosen:** cast to `int` at the SQL boundary, mirroring `index.py:88`'s `str(...)` on write.
- **Rationale:** one Python-side type for the concept across the whole citation spine, so the Grounder
  renders `p.4` from a number. ADR 0019's never-span contract is about the value being a *scalar*, and
  an `int` expresses that most directly. Only ingest writes this column and it only ever writes
  `str(int)`, so a non-numeric value would be a genuine corruption worth failing on.
- **Who/when:** Nate decided, prep interview.

### Settled by the docs — not interview questions
- **`k` default = 5** — roadmap fixes the provisional (`roadmap.md:47`, `roadmap.md:99`: "k=5 →
  tuned", Spike Pass 2). Interface fixes the param; the *value* is empirical.
- **Constant location:** `DEFAULT_K` lives in `retriever/retrieve.py`, per the `chunk.py` spike-constant
  precedent. **Not** `config.py`.
- **Package, not a single file:** the issue's `Touches: src/gct/retriever/**` specifies a package,
  and "one module, single owner — kept whole" says the pipeline stays in **one** module inside it.
- **Failure taxonomy** (`retriever.md:64-71`): empty class → `[]` · corpus < k → return all, not an
  error · embedding provider error → propagate untouched · model mismatch → fail loud.

---

## Module shape

```
src/gct/retriever/__init__.py     # empty, mirroring src/gct/ingest/__init__.py
src/gct/retriever/retrieve.py     # the whole pipeline — one module, kept whole
```

```python
DEFAULT_K = 5                     # provisional; spike Pass 2 (roadmap.md:99)


class EmbeddingModelMismatchError(RuntimeError):
    """Stored embedding_model_id != the active embedder (ADR 0018). Fail loud."""


@dataclass(frozen=True)
class RetrievedChunk:            # ← PUBLIC CONTRACT: #6 (Grounder) binds to this shape
    chunk_id: str
    text: str
    file: str
    page_or_slide: int
    score: float                 # normalized similarity, [0,1], higher-better (ADR 0017)


def _assert_embedding_consistency(conn, *, owner_id, class_id, embedder) -> bool:
    """Guard (ADR 0018). Returns False when the class is empty (→ caller returns [])."""


def _to_score(distance: float) -> float:
    """Cosine distance → normalized similarity, clamped (ADR 0017 + Decision 1)."""


def retrieve(
    conn: psycopg.Connection,
    question: str,
    owner_id: str,
    class_id: str,
    *,
    embedder: Embeddings,
    k: int = DEFAULT_K,
) -> list[RetrievedChunk]:
    """Scoped top-k retrieval. Ranked score-desc, len ≤ k, [] only for an empty class."""
```

### Pipeline — the step order `retrieve` executes

Stated explicitly because the order is load-bearing: the guard runs **before** the paid embed call, so an
empty class costs nothing, and a mismatched class fails before it can produce garbage similarity.

```
1. consistency guard   SELECT DISTINCT embedding_model_id WHERE owner_id AND class_id   (ADR 0018)
     ├─ zero rows                  → empty/un-ingested class → return []   ← short-circuit, no embed call
     ├─ set != {embedder.model_id} → raise EmbeddingModelMismatchError     ← fail loud → Grounder ERROR
     └─ set == {embedder.model_id} → continue
2. embed query         embedder.embed([question])[0]                                     (ADR 0013)
3. vector search       ORDER BY embedding <=> q_vec, WHERE owner_id AND class_id, LIMIT k (F6/F12)
4. convert scores      score = max(0.0, 1.0 - distance)                        (ADR 0017 + Decision 1)
5. return              list[RetrievedChunk], score-desc, len <= k
```

Both DB queries in steps 1 and 3 carry the full `owner_id AND class_id` scope — the guard is not exempt
from the isolation filter.

**Signature note:** `conn` first + `embedder` keyword-only mirrors `ingest_file`
(`pipeline.py:96-103`), so the read path reads like the write path. `retrieve` and `RetrievedChunk`
are the public contracts #6 binds to — do not drift them without updating the issue.

---

## Build order

One function at a time, purest first. Each names the test that proves it.

| # | Build | Test that proves it | Notes |
|---|---|---|---|
| 1 | `_to_score` | `test_to_score_normalizes_and_clamps` — `0.0→1.0`, `1.0→0.0`, `2.0→0.0` (clamped), and a monotonicity check that `d1 < d2 ⟹ score1 ≥ score2` | Pure, no DB, no network. Start here. |
| 2 | `RetrievedChunk` | (shape only — covered by #4/#5 tests) | Data contract; define fully, it's shape not logic. |
| 3 | `_assert_embedding_consistency` | `test_guard_passes_on_matching_model` · `test_guard_raises_on_mismatch` · `test_guard_raises_on_mixed_models` · `test_guard_reports_empty_class` | Needs `db`. Seed chunks directly via SQL — no ingest run needed. |
| 4 | `retrieve` — happy path | `test_retrieve_returns_ranked_scored_chunks` — asserts descending score, `len ≤ k`, and correct `(file, page_or_slide)` provenance | Needs `db` + the ranking embedder. |
| 5 | `retrieve` — isolation | `test_retrieve_filters_by_owner_and_class` — seed two classes and two owners, assert neither leaks | **The F6/F12 test.** Do not skip. |
| 6 | `retrieve` — edges | `test_retrieve_empty_class_returns_empty` · `test_retrieve_corpus_smaller_than_k` · `test_retrieve_propagates_embedding_error` | Covers the whole failure table. |
| 7 | Suite green + `uv run ruff check src/ tests/` | — | Includes the previously-passing ingest suite (fixture hoist). |
| 8 | **Commit this doc's deletion** (it's tracked), then open the PR | — | `git rm plan-5-retriever.md` |

---

## Risks & things to watch

1. **The guard that can never fire.** The single most likely subtle bug: sourcing *both* sides of the
   comparison from `config.ACTIVE_EMBEDDING_MODEL_ID`. That compares config to itself and passes
   forever. The right-hand side must be `embedder.model_id`. **The mismatch test is what proves the
   guard is real** — write it with an embedder whose `model_id` differs from the seeded stamp, and
   confirm it *fails* before the guard exists.
2. **`NaN` distances poison ordering silently.** A zero vector in either position gives `NaN`, and SQL
   `ORDER BY` places `NaN` unpredictably while `max(0.0, 1.0 - nan)` returns `nan` (not `0.0`) in
   Python. The ingest path can't produce a zero vector from real text, but a *fixture* can — which is
   exactly how the existing `FakeEmbeddings` breaks. Keep test vectors well away from zero.
3. **The fixture hoist can quietly break the ingest suite.** Moving `db` changes fixture resolution
   for four existing test modules. Run the **full** suite after step 7, not just the retriever tests.
4. **CI green does not prove this path.** Per CLAUDE.md, DB-backed tests are not marked `live`, so CI
   collects them and they **self-skip** when Postgres is unreachable. Essentially this entire build is
   DB-backed. **Run `uv run pytest tests/ -q` locally and read the skip count** — a green CI badge on
   this PR means almost nothing.
5. **The `::vector` cast is not optional** (see Grounding facts). Omitting it doesn't degrade
   quietly — it raises `UndefinedFunction: operator does not exist: vector <=> double precision[]`.
   Loud, but only at runtime against a real DB, so it will not surface until a DB test actually runs.
6. **`<=>` is load-bearing, not cosmetic.** The HNSW index is built `vector_cosine_ops`. Writing
   `<->` or `<#>` still returns rows and still ranks *something*, so tests may pass while the index is
   bypassed and the score means a different quantity than ADR 0017 specifies.
7. **Scope drift toward the Grounder.** The pull to "just skip the obviously irrelevant chunks" is
   real and is exactly what ADR 0008 forbids in V1. `retriever.md:92-94` warns specifically against
   letting this box become where the differentiator quietly gets deferred.
8. **Don't let the empty-class short-circuit swallow real errors.** It must trigger only on "the
   DISTINCT returned zero rows," never as a bare `except`.

---

## Definition of done

- [ ] Every stub implemented; no `NotImplementedError` remains in `src/gct/retriever/`.
- [ ] `uv run pytest tests/ -q` green **locally with Postgres up** — skip count read and understood.
- [ ] `uv run ruff check src/ tests/` clean.
- [ ] Isolation test (`owner_id` AND `class_id`) present and genuinely failing without the filter.
- [ ] Guard test proves the mismatch path raises — verified it fails before the guard exists.
- [ ] `plan-5-retriever.md` deleted **via a commit**.
- [ ] Self-review of the diff against ADR 0017, ADR 0018, ADR 0008, and `design/components/retriever.md`.

> `understanding/prep-5-retriever.html` is **not** on this list — it lives in gitignored
> `understanding/` and stays there alongside the brief/check records.

---

## Fixtures / infra notes

Created by prep (step 4) — **do not rebuild these mid-build:**

- **`tests/conftest.py`** — the `db` fixture, moved verbatim from `tests/gct/ingest/conftest.py`
  (Decision 3). Yields `(conn, owner_id, class_id)` with a seeded `classes` row and unique per-test
  `owner_id`; tears down chunks → files → classes in FK order; skips when Postgres is unreachable.
- **`tests/gct/retriever/conftest.py`** — a **ranking-capable** fake embedder plus a chunk-seeding
  helper. The embedder maps text to vectors with genuinely *different directions* (not scalar
  multiples), so cosine distance is meaningful and rank order is deterministic and assertable. The
  seeding helper inserts `chunks` rows directly via SQL — retriever tests must not depend on a real
  parse/embed run.
- **Leave `tests/gct/ingest/conftest.py`'s `FakeEmbeddings` alone.** It is correct for ingest (which
  never ranks); "fixing" it is out of scope and would churn a passing suite.
