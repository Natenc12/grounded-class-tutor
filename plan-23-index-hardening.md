# plan-23 — index-hardening: atomicity test + write-path guards

> **TEMP — delete before opening the PR.** This doc IS committed with the prep scaffold (so a branch
> switch or `git clean` can't destroy it), and is removed by an explicit **deletion commit** before the
> PR opens. Do not `git rm` it until the promotion pass (build step 8) has run — the pass reads this doc.

Issue: [#23](https://github.com/Natenc12/grounded-class-tutor/issues/23) · Branch: `nate/index-hardening`

---

## Goal

Make `index_file`'s all-or-nothing guarantee **regression-tested** rather than merely true, close the
one path that can publish `status='ready'` with zero chunks, and turn the `live` marker from decoration
into a fence that actually holds.

## Scope & non-goals

**In scope** — three write-path guards plus the test-reliability fixes that matter while #5/#6/#8 are
being built against this chunk set:

1. Two mid-write-failure tests proving atomicity (re-index path + first-index path).
2. Rename `test_reindex_replaces_atomically` → what it actually proves (replacement).
3. `index_file` raises on an empty `chunks` list.
4. `order by page_or_slide::int` in the index tests (the column is `text`).
5. `FakeEmbeddings` uses a stable digest instead of per-process-randomized `hash()`.
6. `@pytest.mark.live` on the DB-backed tests, so CI's `-m "not live"` filter fences them deliberately.

**Explicitly NOT in scope — do not touch:**

- **#24 (re-index publish columns)** — the `DO UPDATE` set-list leaving `failed_reason` and scope columns
  stale. Parked for Slice 2; its right *location* depends on the Slice 2 worker's claim path. Do not pull
  it onto this branch even though you will be editing the exact SQL statement it concerns.
- **A Postgres service container in CI.** The issue names this as the stronger fix and explicitly defers
  it: *"worth doing when Slice 2 makes the write path load-bearing, but out of scope here."*
- **`src/gct/retriever/`** — that is #5, parallel-safe with this issue. Leave it alone.
- **Any `design/` file.** Findings are staged into the promotion pass (step 8), not written now.
- **The `index_file` signature.** #5/#6/#8 and `ingest_file` all bind to it. Adding a guard changes
  behavior on an input that was previously (wrongly) accepted; it must not change the parameter list.

## Invariants this build rests on

| invariant | source span |
|---|---|
| **No partial index is ever visible** — a file's chunks are all-from-one-successful-run or absent; `status=ready` ⟺ full chunk set committed & queryable | `design/components/ingestion-worker.md:103-104`; ADR 0020 Consequences (`0020-…md:83-84`) |
| **A job that dies mid-index must not leave a half-indexed file** — correctness hazard for the differentiator, since the Retriever would read an incomplete corpus and the Grounder would ground/refuse against it *silently* | ADR 0020 Context (`0020-…md:13-16`) |
| **`files.status='ready'` flips inside the same transaction as the chunk insert** — status **is** the publish signal, so it must commit with what it publishes | ADR 0020 §3 (`0020-…md:61-63`) |
| **Re-index of an already-`ready` file** — old full set stays queryable until COMMIT, then swaps atomically; never flickers empty/partial | spec §Failure modes (`ingestion-worker.md:94`) |
| **Idempotent by construction** — delete-then-replace, no dedup keys | ADR 0020 §2 (`0020-…md:33-39`) |
| **PM-4 seam** — the pipeline stays pure of job/queue/lease machinery so Slice 2 wraps it | repo `CLAUDE.md` §Conventions |

The invariant that motivates the whole issue, stated plainly: **the current test suite proves
`index_file` *replaces* correctly, and proves nothing at all about what happens when a write fails
partway.** `index_file` DELETEs the old chunk set before INSERTing the new one — safe only under the
transaction. If that transaction ever stopped holding, a mid-write failure would destroy the old chunks
*and* fail to write the new ones: strictly worse than never re-indexing at all.

## Grounding facts (verified against the repo, this session)

Every claim below was checked live, not read off a doc.

**F1 — `page_or_slide` is `text`, so the default sort is lexicographic.**
`migrations/0001_init.sql` declares it `text`; `PreparedChunk.page_or_slide` is `int`
(`pipeline.py:47`), converted at the SQL boundary via `str(...)` (`index.py:88`). Probed:

```
A.  text sort: ['1', '10', '2']
A2. ::int sort: ['1', '2', '10']
```

The existing `test_index_file_lands_full_set_and_flips_ready` uses pages 1/2/3, where the two sorts
agree — which is exactly why the bug is invisible today.

**F2 — a wrong-dimension embedding is rejected server-side, by the column.**

```
B. raised: psycopg.errors.DataException | expected 1536 dimensions, not 8
```

`psycopg.errors.DataException` (a subclass of `psycopg.DataError`). This is the failure-injection lever:
it needs no monkeypatching, no fault-injection library, and it fires *during* the `executemany`, i.e.
genuinely mid-write. Put the bad chunk in the **middle** of the list so rows on both sides of it are
in play.

**F3 — the transaction holds; the old set survives intact.**

```
D. old set survives: ['old-A', 'old-B', 'old-C']
E. status: ('ready',)
```

`index.py:53` wraps all three statements in `with conn.transaction()`, which rolls back on exception.

**F4 — the connection stays usable after the failure.** `conn.info.transaction_status` is `INTRANS`
after the raise, and subsequent `SELECT`s (F3) succeeded on the *same* connection. **The tests do not
need a fresh connection to assert against.**

**F5 — a failed *first* index leaves no `files` row at all.**

```
G. files row for never-succeeded file: None
```

The `files` upsert (`index.py:56-63`) is inside the same transaction, so it rolls back with the chunks.
**This is the only path on which the status half of the invariant is provable** — see *Resolved
decision D1*.

**F6 — the empty-chunks bug is live today.**

```
H. empty-list today -> status: ('ready',) chunks: (0,)
```

`index_file(chunks=[])` publishes `ready` with zero chunks right now. `executemany` over an empty
sequence is a no-op, so steps 1 and 2 commit alone. Unreachable via `compose` (`parse_file` raises
`ParseError("empty", …)` — `pipeline.py:69-70`), but `index_file` is a public entry point.

**F7 — `hash()` is per-process randomized, as the issue claims.** Three separate interpreters:

```
808 940
997 67
874 41
```

`conftest.py:135` does `float(hash(text) % 1000)`. The docstring at `conftest.py:113-118` is already
honest that it's "stable within a test run, not across runs" — the defect is the `% 1000`, which
collapses the space to 1000 buckets and makes the alignment assertion pass **vacuously** on collision.

**F8 — the `live` marker currently fences nothing.**

```
$ uv run pytest -m "not live" --collect-only -q
55 tests collected
```

All 55, including the 3 DB-backed ones. The marker is registered (`pyproject.toml:34`) and nothing
carries it.

**F9 — there are THREE DB-backed tests, and one is outside the issue's `Touches:` line.**

| test | file |
|---|---|
| `test_index_file_lands_full_set_and_flips_ready` | `tests/gct/ingest/test_index.py:31` |
| `test_reindex_replaces_atomically` | `tests/gct/ingest/test_index.py:74` |
| `test_ingest_file_end_to_end` | `tests/gct/ingest/test_pipeline.py:58` |

The issue's `Touches:` names only `test_index.py` + `conftest.py`. **`test_pipeline.py` is in scope** —
the issue body says "the 3 DB-backed ones", so the `Touches:` line is simply stale. Marking two of three
would leave the fence half-built.

**F10 — two in-repo comments become false the moment the markers land.**
- `pyproject.toml:34`: *"No tests carry this marker yet — it fences off the live/DB tests arriving in later slices."*
- `.github/workflows/ci.yml` header: *"the `live` marker … fences off the OpenAI/Postgres tests arriving in later slices"* — already false when written (#22 merged an hour after #21 shipped those tests).

Both must be updated in this PR. These are code-adjacent comments, **not** `design/` files, so they are
in scope.

**F11 — no design-doc defect found.** Unlike #5 (where ADR 0017's `[0,1]` claim was checkably
impossible), ADR 0020 and `ingestion-worker.md` agree with the live behavior on every checkable claim.
Nothing to stage as a doc-defect promotion candidate.

**F12 — repo precedent for a guard's exception type.** `compose` raises a bare `ValueError` for its
vector/chunk alignment guard (`pipeline.py:78-81`), with a comment noting it raises rather than
`assert`s so the guard survives `python -O`. The empty guard is the same class of thing and follows it.

## Resolved decisions

**D1 — Test shape: two mid-write tests, not one.** *(Nate decided, this session)*

- Fork: the issue's bullet says "assert `files.status` did not flip to `ready`". On a **re-index**, status
  was *already* `ready` before the failure (F3, line `E`), so that assertion is vacuous — it would pass
  against a completely broken implementation.
- Options: one test (re-index only, literal to the issue) · one test (first-index only, drops the
  spec-named re-index failure mode) · **two tests, one per path**.
- Chosen: **two tests.** The re-index path is where "the old set survives" is provable; the first-index
  path (F5) is where "status never reached `ready`" is provable. Each asserts only what it can actually
  prove; together they cover the invariant.
- Consequence: `test_index.py` gains two tests, not one, and the issue's checkbox list maps to them
  2-to-1. Say so in the PR body so the reviewer isn't hunting for a single test.

**D2 — Empty-chunks guard raises `ValueError`.** *(Nate decided, this session)*

- Options: bare `ValueError` · a new domain error type (`EmptyChunkSetError`) a Slice 2 worker could
  catch and map to `failed_reason='empty'` · defer.
- Chosen: **`ValueError`**, following the `compose` alignment-guard precedent (F12). No new type until
  something actually catches it.
- Noted for Slice 2, **not acted on here:** the schema already allows `failed_reason='empty'`
  (`migrations/0001_init.sql:29`), so the worker has an obvious mapping when it needs one. If Slice 2
  wants to catch this specifically, promoting to a typed error is a clean, additive change.
- Message should name the invariant, not just the input — a bare "chunks is empty" tells a future reader
  nothing about *why* it's forbidden:

```python
if not chunks:
    raise ValueError(
        f"index_file called with zero chunks for file_id={file_id}; "
        "publishing 'ready' with no chunks violates ADR 0020"
    )
```

**D3 — `live` marker applied explicitly, per test.** *(Nate decided, this session)*

- Options: explicit `@pytest.mark.live` decorators · a `pytest_collection_modifyitems` hook that
  auto-marks anything requesting the `db` fixture · both.
- Chosen: **explicit decorators.** Obvious at the call site; a reader of the test knows instantly it's
  fenced, with no collection-time magic to discover.
- **Accepted cost, tracked in Risks:** a future DB test can be added without the marker, and that
  failure is silent (CI stays green). The auto-mark hook remains available if it ever bites.

**D4 — `FakeEmbeddings` gets a stable digest, same vector shape.** *(Nate decided, this session)*

- Options: sha256 seed in the existing `[seed, 0, 0, …]` shape · additionally spread/normalize the
  digest across all dims so cosine distances between fakes are meaningful for #5 · defer.
- Chosen: **stable digest, same dumb shape.** Fixes exactly the flake F7 names. #5 (Retriever) is on the
  ready frontier and parallel-safe with this issue; guessing at its retrieval-fixture needs before it is
  designed is scope creep into someone else's branch.
- Drop the `% 1000` entirely — the modulo is the collision source, and there is no reason to bucket:

```python
h = hashlib.sha256(text.encode()).digest()
seed = float(int.from_bytes(h[:6], "big"))   # 48 bits, fits float64's mantissa; no modulo
return [seed] + [0.0] * (self._dim - 1)
```

## Module shape

No new modules. Four files change.

### `src/gct/ingest/index.py`

`index_file`'s signature is a **public contract** — `ingest_file` (`pipeline.py:113`) and, shortly,
#5/#6/#8 bind to it. **Do not change it.** The only edit is a guard at the top of the body, *before*
`with conn.transaction():`:

```python
def index_file(conn, *, file_id, filename, owner_id, class_id, chunks) -> None:   # unchanged
    # guard goes here — before the transaction opens
    with conn.transaction():
        ...
```

Raising before the transaction opens (rather than inside it) is deliberate: nothing to roll back, and
`files` gets no row at all — matching F5's shape for the other failure path.

The docstring must gain a line documenting the raise; a public entry point whose contract lives only in
the code is the thing this repo's docstring style exists to prevent.

### `tests/gct/ingest/test_index.py`

Prep has laid **skipped stubs** for the three new tests (signature + docstring + `pytest.skip`) so the
suite starts green and the shape is fixed. Replace each `pytest.skip` with the real body.

| test | proves | status |
|---|---|---|
| `test_index_file_lands_full_set_and_flips_ready` | existing — full set + `ready` + text storage | edit: pages → 1/2/10, `order by page_or_slide::int` |
| `test_reindex_replaces_the_set` | replacement, no duplicates | **rename** from `…_replaces_atomically` |
| `test_midwrite_failure_leaves_old_set_intact` | re-index path: old set complete & queryable after failure | new (stubbed) |
| `test_midwrite_failure_on_first_index_publishes_nothing` | first-index path: no chunks **and** no `files` row | new (stubbed) |
| `test_index_file_rejects_empty_chunk_set` | `ValueError`, and no `files` row created | new (stubbed) |

Helper `_chunk(...)` gains a `dim` parameter (default `EMBEDDING_DIM`) so a wrong-dimension chunk is one
argument, not a second builder. Already laid by prep.

### `tests/gct/ingest/conftest.py`

`FakeEmbeddings.embed` — swap `hash()` for `hashlib.sha256`, drop the `% 1000`, and **update the
docstring at lines 113-118**, which currently explains the per-run-hash behavior that will no longer be
true. A stale docstring here is worse than none: it is the fixture every ingest test reads through.

### `pyproject.toml` + `.github/workflows/ci.yml`

Comment-only edits per F10. Both currently assert no tests carry the marker.

## Build order

One step at a time, purest first. Each names the test that proves it. TDD-optional — write the test
first if you like; the stubs are already there either way.

1. **`FakeEmbeddings` stable digest** (`conftest.py`) — pure, no DB, nothing depends on the old values.
   *Proven by:* `test_fake_embedder_is_stable_across_processes` (prep laid this stub) — asserts a known
   constant, so a future "improvement" that reintroduces randomization fails loudly. Plus the existing
   `test_compose_stamps_provenance_model_and_embeds_all` must stay green.
2. **`::int` ordering** (`test_index.py`) — change the fixture pages to 1/2/**10** *and* the `order by`.
   Changing only the sort proves nothing; changing only the pages turns the test red. Do both together.
   *Proven by:* `test_index_file_lands_full_set_and_flips_ready` — it must fail if you revert the
   `::int` while keeping page 10.
3. **Empty-chunks guard** (`index.py`) — the one source change. *Proven by:*
   `test_index_file_rejects_empty_chunk_set`.
4. **Rename** `test_reindex_replaces_atomically` → `test_reindex_replaces_the_set`. Mechanical; frees
   the atomicity name for step 5. Grep for the old name first (docstrings/PR bodies reference tests).
5. **`test_midwrite_failure_leaves_old_set_intact`** — index a good 3-chunk set, commit, then re-index
   with a set whose **middle** chunk has `dim=8`; assert the raise, then assert all three original texts
   are present and `status` is still `ready`. Per F4, keep using the same connection.
6. **`test_midwrite_failure_on_first_index_publishes_nothing`** — same injection against a fresh
   `file_id`; assert zero chunks **and** no `files` row (F5).
7. **`live` markers** — decorate the DB-backed tests, then update the two stale comments (F10).
   **Six, not three:** F9's 3 existing ones (remember `test_pipeline.py`) **plus the 3 new stubs from
   steps 3/5/6, which all take the `db` fixture.** Marking only F9's three would leave the new
   atomicity tests running unfenced in CI — the exact failure this step exists to close.
   *Proven by:* `uv run pytest -m "not live" --collect-only -q` reports **53**, and `-m live` reports **6**
   (59 total: 55 before this PR + 4 new).
8. **Run the promotion pass** (next section). Must happen before step 9 — this doc is its input.
9. **Commit this doc's deletion** (it is tracked, so `git rm plan-23-index-hardening.md` + commit), then
   push and open the PR.

## Decision promotion (build step 8 — do this before deleting this doc)

Walk **every** Resolved decision above against the code that actually got built, and sort each one:
*survived unchanged* · *changed during the build* · *dropped*. For anything that **changed**, write down
what was expected, what actually happened, and why the choice moved — that delta is the most valuable
thing this pass produces, and it exists nowhere else once this file is deleted.

Then re-judge each candidate below on its merits. **The table is a starting point, not an authority —
do not promote something because this table lists it.**

| candidate | proposed home | judgment |
|---|---|---|
| `index_file` rejects an empty chunk set (D2) — a public-entry-point contract, and the guard that makes `status=ready ⟺ full chunk set queryable` true *by construction* rather than by luck of the caller | **`design/components/ingestion-worker.md`** — likely §Failure modes (a new row) or §Invariants | **Mandatory if built.** A spec that describes the merged code's contract incompletely is worse than no spec, because it is trusted. |
| D2's typed-error question — whether Slice 2 wants `EmptyChunkSetError` to map onto `failed_reason='empty'` | nothing yet | **Do not promote.** It is an open question, not a decision. If it still feels live, it is a Slice 2 issue, not an ADR. |
| D1 (two tests, not one) — the *reason* the status assertion is vacuous on the re-index path | test docstrings + commit message | **Do not promote to `design/`.** This is test design. Git history is its home. |
| D3 (explicit markers over an auto-mark hook), D4 (fake-vector shape) | commit message | **Do not promote.** Test infrastructure mechanics. |
| F8/F10 — the `live` marker fenced nothing and two comments said otherwise | fixed in this PR's code | **Do not promote.** The fix ships in the diff; an ADR about a stale comment is noise. |

**No ADR is expected from this build.** A new ADR earns its keep by recording *why*, among real
alternatives, with downstream consequences. Every decision here either implements an existing ADR (0020)
or is test-infra taste. If the build genuinely surprises you — if the empty guard turns out to need a
typed error, or the atomicity test exposes something ADR 0020 did not anticipate — then reconsider, and
prefer a **new** ADR amending 0020 over editing 0020 in place (repo precedent: ADR 0002's status line
reads *"accepted — tenancy clause amended 2026-07-04 by ADR 0004"*).

**Nothing else goes into `design/`.** Volume is what makes a design folder stop being read, and that
failure is silent — nobody announces they have started skimming.

## Risks & things to watch

- **The rename in step 4 can silently orphan a reference.** `test_reindex_replaces_atomically` may be
  named in other docstrings, the #4 PR body, or `design/` prose. Grep before renaming; a doc pointing at
  a test that no longer exists is a small, permanent lie.
- **A test that passes for the wrong reason.** The mid-write tests fail the write via a `DataException`
  from the `vector(1536)` column. If a future change validates dimensions *in Python* before the SQL, the
  exception would fire **before** the transaction opens — and both tests would still pass, while proving
  nothing about atomicity. Assert on the failure *shape* (old set intact / no `files` row), and note in
  the test docstring that the injection must fail mid-`executemany`, not earlier. This is the same class
  of failure as the vacuous assertion in D1: green, and empty.
- **Step 2's two edits are load-bearing together.** `::int` with pages 1/2/3 is a no-op; pages 1/2/10
  without `::int` is red. Landing one without the other in a partial commit produces a test that passes
  and guards nothing.
- **D3's accepted cost is real.** Nothing structurally prevents a 4th DB test landing unmarked, and CI
  would stay green while silently running it against no database. If a second unmarked test ever appears,
  revisit the auto-mark hook.
- **This PR makes CI collect *fewer* tests (55 → 53) while *adding* four.** That reads as coverage loss
  in the diff. It is the point — the 6 now-fenced tests were never actually running in CI, they were
  skipping on the `db` fixture. Say so plainly in the PR body or a reviewer will read the number as a
  regression.
- **Miscounting the marker set is easy, and I did it while writing this plan.** The first draft said
  "mark the 3 DB-backed tests" — but steps 3/5/6 *add three more* DB-backed tests. Recount against
  `-m live` at build time rather than trusting any number written here.
- **CI still cannot catch a real `index_file` breakage.** After this PR the fencing is *deliberate*
  rather than accidental, but the DB path remains untested per-PR. **Run the full suite locally before
  trusting green CI on anything touching the write path** — including this PR.
- **The empty guard changes behavior on a previously-accepted input.** Unreachable via `compose` today
  (F6), but if any test or script anywhere calls `index_file(chunks=[])` deliberately, it now raises.
  Grep for callers before building step 3.
- **Do not fix #24 while you are in that SQL.** Step 3 puts you inside the exact statement #24 concerns.
  It is parked for Slice 2 on purpose.

## Definition of done

- [ ] All prep stubs implemented; no `pytest.skip` placeholders remain in `test_index.py`.
- [ ] `uv run pytest` — full suite green **locally, with Postgres up** (not just CI green; see Risks).
- [ ] `uv run pytest -m "not live" --collect-only -q` reports **53**; `-m live` reports **6**.
- [ ] `uv run ruff check src/ tests/` clean.
- [ ] Every issue-#23 checkbox is either done or explicitly answered in the PR body (D1 maps two of them
      to a different test split than the issue's literal wording).
- [ ] **Promotion pass (step 8) done** — every Resolved decision walked against the built code and sorted;
      **anything that changed during the build is written down as a change, not silently overwritten**;
      the `ingestion-worker.md` spec edit landed if the empty guard shipped. This gate is not a tick-box:
      its input is deleted one step later, so skipping it is unrecoverable.
- [ ] Self-review against ADR 0020 §2-3 and `ingestion-worker.md` §Failure modes / §Invariants.
- [ ] `plan-23-index-hardening.md` deleted **via a commit**, and only after the promotion line is true.
- [ ] *(Not on this list: `understanding/prep-23-index-hardening.html`. It lives in gitignored
      `understanding/` and stays.)*

## Fixtures / infra notes

Laid down by prep, already committed:

- **`test_index.py::_chunk` gains `dim: int = EMBEDDING_DIM`** — the wrong-dimension failure injection is
  now `_chunk(owner, cls, "BAD", 2, dim=8)`. No separate builder, no monkeypatching, no fault-injection
  dependency: the `vector(1536)` column is the fault injector (F2).
- **Three skipped test stubs** in `test_index.py` — signature + docstring + `pytest.skip("stub — see
  plan-23")`. They keep the suite green while fixing the shape and the names.
- **One skipped stub** `test_fake_embedder_is_stable_across_processes` in `tests/gct/ingest/test_pipeline.py`.
- **Do not touch:** the `db` fixture's teardown (`conftest.py:169-174`). It deletes by `owner_id` across
  chunks → files → classes in FK order. The new tests create extra `files` rows under the same
  `owner_id`, which it already handles.
