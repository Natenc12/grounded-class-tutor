# CLAUDE.md — Grounded Class Tutor

Auto-loaded each session. Sections are ordered **most stable first**; *Current status* at the bottom is
the only part expected to churn.

**Keep status thin — it must not restate the issue board.** The board is the single writer for what's
done and what's ready; duplicating it here creates two writers for one fact and they always diverge
(this file once listed a merged issue as ready to pick up, and later still named a merged issue as the
next thing to build). Update this file when a *slice's shape* changes, not when an issue closes.

**Slice granularity is the floor.** Any fact here that changes when a single issue closes is a bug in
this file — it belongs on the board, which recomputes itself. A *derived* pointer cannot go stale; a
pointer that spells out its target — an issue number, a slice label — expires exactly like a copy
does. Fetch the target, don't name it. If `gh` is unavailable you get *no* status rather than *wrong*
status, which is the better failure: a missing fact prompts a query, a stale one gets confidently
built on.

## What this is
A RAG study tutor: answers a student's question from **their own uploaded course materials**, cited to
the exact file + page/slide, or gives an **honest refusal** when the corpus doesn't cover it. Trust is
the product. Full design lives in `design/` — start with `design/START-HERE.md`, then `HANDOFF.md`.

**Source-of-truth rule:** `architecture.md` + `data-model.md` + the `decisions/` ADRs are current truth.
`vision.md` is the origin story — where it disagrees with a later doc, the later doc wins.

## Stack
- **Core:** Python 3.10+, package `gct` under `src/` (a callable library — API/eval/scripts are thin peer callers, ADR 0009).
- **DB:** Postgres 17 + pgvector (local V1 → Supabase V2, ADR 0006). DB name `grounded_class_tutor`.
- **Models:** OpenAI behind a swappable provider layer (ADR 0004/0013) — `text-embedding-3-small` (dim 1536), `gpt-4o-mini`. Defaults, not commitments.
- **Tooling:** `uv` (deps + venv). React SPA arrives in Slice 4.

## Where things live
| Path | What |
|---|---|
| `src/gct/` | the library — the product |
| `scripts/` | thin peer callers (`migrate.py`, `worker.py`, and the per-slice exit smokes) |
| `tests/gct/` | the test suite |
| `design/` | current truth: ADRs, component specs, data model |
| `.claude/skills/roadmap-to-issues/` | the one in-repo skill — projects a roadmap slice into issues |
| `understanding/` | Nate's own notes — gitignored, never project truth |

## Local dev
```sh
uv sync --extra dev                     # deps into .venv — `--extra dev` or you get NO pytest/ruff
uv run python scripts/migrate.py        # apply migrations/*.sql
uv run python scripts/smoke_slice0.py   # Slice 0 exit test → "PASS — foundation is wired."
uv run python scripts/ask_smoke.py      # Slice 1 exit gate — SPENDS MONEY (real models, real corpus)
uv run python scripts/ingest_smoke.py   # Slice 2 exit gate — SPENDS MONEY
uv run python scripts/worker.py         # the poll worker — a SEPARATE process, never in the API loop (ADR 0011)
uv run pytest tests/ -q                 # full suite
uv run pytest -m db -q                  # just the Postgres-backed tests (DB must be up)
uv run pytest -m "not live" -q          # exactly what CI runs
uv run ruff check                       # lint — no paths, to match CI (it covers scripts/ too)
uv run ruff format                      # formatting — writes; CI gates the same run as `--check`
```
A bare `uv sync` doesn't just skip the dev tools, it **uninstalls** them: `pytest`/`ruff`/`reportlab`
live in `[project.optional-dependencies].dev`, so the next two commands stop working.
Postgres 17 is keg-only; its psql/createdb live at `/opt/homebrew/opt/postgresql@17/bin`.
Secrets (`OPENAI_API_KEY`, `DATABASE_URL`) live in `.env` (gitignored).
**What "green" is worth.** CI runs `ruff`, the migrations, and `pytest -m "not live"` on every PR,
against a pgvector Postgres 17 service container (#18, extended by #32). Green proves lint, formatting
(#34), that `migrations/*.sql` applies cleanly, and every `db`-marked test — both DB paths, on fake
embedders.
The `db` fixture skips locally when Postgres is down but **hard-fails in CI**, so DB tests can't
silently skip their way to green. Locally that judgement is still yours: `pytest -m db` reporting
**skips means Postgres is down, not that the DB path passed.**

**A `db` green does not prove anything was saved.** A connection sees its own uncommitted work, so
every assertion read back through `db`'s connection holds whether or not the write was ever
published — the savepoint failure ADR 0025 describes is invisible from one connection *by
construction*. Assert through `db` for what the code computed; assert through **`db_other`** — a
second connection, same database — for what actually survives. Any test whose point is that
something was written takes it. This is not a style preference: the rule was stated in four places
and two tests written against it still published nothing, which is what `db_other` exists to have
caught. Its docstring carries the mechanism; ADR 0027 (accepted — writers now refuse a non-IDLE
connection via `gct.db.require_idle`) carries the argument that got enforcement adopted.

`live` marks a test that hits a paid API, and is excluded from the CI gate. Like `db`, it is
**derived, never hand-declared**: `pytest_collection_modifyitems` in `tests/conftest.py` applies it to
any test taking a `live_*` fixture. Constructing a real provider client through such a fixture is what
makes a test paid, so the mark cannot drift from the dependency.

Hand-declaring it failed in the dangerous direction — **two** ways at once, in opposite directions:
- a test that FORGETS the mark runs in CI against an empty `OPENAI_API_KEY`, spending money or going
  red for the wrong reason;
- and a test that carries it stops being covered by CI, silently.

Deriving removes both, and leaves one edge it cannot see: a test that builds a provider client
**inline** rather than through a fixture. New paid tests MUST take a `live_*` fixture — the same "take
the fixture and it stays true" rule `db` has. How many carriers there are *today* is a fetched fact,
not one this file stores: `uv run pytest -m live -q --collect-only`.

## Cloud sessions
The README's Homebrew setup does not apply in a cloud container: Postgres 16 binaries are
installed but there is no cluster and nothing is running, and `.env` is gitignored so a fresh
clone has no `DATABASE_URL`. **Before anything that touches the database, run:**
```sh
bash scripts/cloud-bootstrap.sh         # creates the cluster, DB, pgvector, .env, deps, schema
```
Minutes on a cold container, instant afterwards. It refuses to run anywhere but a Linux
container as root, because it rewrites `.env` — `GCT_FORCE_BOOTSTRAP=1` overrides that.

`OPENAI_API_KEY` comes from the cloud environment's variables, not from the repo. Without it,
migrations and non-live tests still run; ingest and ask do not.

## Conventions / invariants (do not violate)
- **Hand-rolled RAG** (ADR 0003) — no LangChain/LlamaIndex; we build the pipeline to learn it.
- **Provider-agnostic** — grounding logic sits *above* the provider interfaces; swapping models never changes product behavior.
- **Embedding consistency** (ADR 0018) — two distinct rules; don't collapse them:
  - *Which embedder gets constructed* — sourced only from `gct.config.ACTIVE_EMBEDDING_MODEL_ID`. Never hardcode a model id.
  - *What gets recorded about a run* — `chunks.embedding_model_id` is stamped from `embedder.model_id`, i.e. "the model that **actually produced** the stored vectors" (ADR 0018). **Not** from config: the Retriever's guard compares the stamp against the active embedder, so sourcing both from config would make it compare config to itself and never fire.
- **The PM-4 seam** — build the ingest pipeline (parse→chunk→embed→index) *pure and separate* from any job/queue/lease machinery, so Slice 2 wraps it instead of rewriting it.
- **`owner_id` on every row**; retrieval always filters `owner_id AND class_id` (F6/F12).
- **Citation spine** — source metadata born at parse ①, rendered to `[S#]` labels ②, resolved back to citations ③; the model only ever cites labels we handed it.
- **Cite ADRs, don't re-argue them.** A comment may name an ADR and state what the code must do; restating the ADR's *argument* makes the comment a second writer for that fact, and the copy is the one that drifts. `_to_score`'s docstring argued its clamp deviated from ADR 0017; ADR 0024 ratified that clamp **25 minutes later**, and the stale argument then survived a deliberate docstring-correction pass and two later 0024 edits — nine days, three chances to catch it. Drift is not slow rot; it is invisible to people who are looking. Same reasoning as *Current status* below: a pointer that spells out its target expires exactly like a copy does.
  - **Where an ADR is amended, cite both, in the form the design corpus uses:** `(ADR 0017, clamped per ADR 0024)` — never the slash form `(ADR 0017/0024)`, which says nothing about which ADR owns what. A bare cite of the amended ADR is the drift.
  - **Which ADRs are amended is derived, never listed here** — a stored list is exactly the copy-with-a-second-writer this bullet is about, and it goes stale the next time anyone writes an amendment. `decisions/0000-template.md` owns *how* the relationship is declared (both status-line halves); one legacy ADR predates it and uses an `**Amends:**` header, so a census must catch that form too: `grep -rniE '^\- \*\*Amends|amend(s\|ed)? .*ADR [0-9]{4}' design/decisions/`.
  - **A citation census covers every writer** — `src/`, `tests/`, `scripts/`, and `design/`, not just the library. Fixing one and leaving the others is a half-fix, and deleting the last corroborating copy of a term can orphan a reference that reads fine in isolation.
  - This is **not** a density limit. Depth is welcome wherever the code cannot hold the fact itself — non-obvious runtime behavior, a **rejected alternative** (the thing no ADR carries and no reader can recover), an invariant a future edit would silently break. `grounder/answer.py` is the reference for how deep that can honestly go.

## Current status
The slice name below is **stored, not derived** — a deliberate exception to the rule above.
`/roadmap-to-issues` reads this section to know which slice to project and halts if it disagrees with
`design/roadmap.md`, so deriving it away would remove the anchor it reads by default. It moves only a
handful of times in the project's life, which is the declared floor. Everything finer-grained than a
slice still belongs on the board.

**Slice 0 — Foundation: COMPLETE.** Schema + provider interfaces + the embedding-consistency anchor;
4 tables, vector column, scope + HNSW indexes, smoke test green end-to-end against live models.

**Slice 1 — the tracer bullet: COMPLETE.** Ingest ONE real file inline (parse→chunk→embed→index, no
queue) → Retriever → Grounder → cited answer / refusal, script-driven, over `eval/questions.jsonl`.
The differentiator, proven on real course materials before any HTTP/UI — see `eval/FINDINGS.md` for
what the live runs showed. See `design/roadmap.md` and
`design/components/{grounder,retriever,ingestion-worker}.md`.

The seams it draws, which later slices wrap rather than rewrite:
- **Write path** — `ingest_file(path, owner_id, class_id, embedder=, conn=)` takes a real PDF/PPTX to a
  `ready`, queryable, provenance-carrying chunk set in one atomic transaction, pure of job/queue
  machinery (PM-4 seam, ADR 0020).
- **Read path** — `retrieve()` returns scoped, ranked `RetrievedChunk[]` with normalized scores;
  `answer()` consumes exactly that shape and returns one of five states, deciding cite/partial/refuse
  and validating everything the model returned (ADR 0014/0015/0016).
- **Exit gate** — `ask(class, question)` returns a cited answer for an in-corpus question and an honest
  refusal for an out-of-corpus one, demonstrated over the smoke suite.

**Spike Pass 1 — validation, not optimization: COMPLETE.** Chunking + generation run on the tracer +
seed smoke suite; the differentiator grounds and refuses on real course materials. **The verdict is
ADR 0026** — read it rather than this line: it names the *configuration* validated (validation here
is not a general claim), states the bars it does not clear, and records the red the pass caught as a
bound on Pass 2's chunking axis. Evidence lives in `eval/FINDINGS.md`, which stays the writer of the
measurements — the ADR reproduces none of them except where the figure is its own decision content.

**Slice 2 — Real write path: CURRENT.** Wrap the *proven* inline pipeline in the async worker + job
queue + status store (DB-backed `jobs`, in-process poll worker, `enqueue`/`claim`, ADR 0011) plus
failure/idempotency — retryable/terminal split, all-or-nothing atomic replace, index-write-only
transaction (ADR 0020, precondition per ADR 0025). Additive, not a rewrite: the PM-4 seam exists so
this slice *wraps* `ingest_file` rather than reshaping it. **Exit:** upload →
`queued→processing→ready/failed` is real; at-least-once + reaper safe; no partial index ever visible.
See `design/roadmap.md` → *Slice 2* and `design/components/ingestion-worker.md`.

**Issue-level state is NOT recorded in this file.** Never write "#N is done" or "#N is next" here — it
is wrong within the week, and this file is not the writer of that fact. Fetch it instead:

```sh
gh issue list --repo Natenc12/grounded-class-tutor --state open --json number,title,labels \
  --jq 'sort_by([.labels[].name]|index("ready")==null)[]
        | "#\(.number) [\([.labels[].name]|join(","))] \(.title)"'
```

`ready` rows sort first, so the frontier is on line one. No slice filter on purpose: later-slice and
cross-cutting work reports itself instead of being invisible — it just sorts below the pickable work.
A `slice-N` label is not guaranteed — chores and spikes may carry none, so open the row rather than
inferring its slice from the listing. The **epic for the current slice** carries the dependency graph,
the ready-frontier, and the open design flags — it is whichever row above is labeled `epic`
(add `--label epic` to isolate it), never a number written down here. Re-run `/roadmap-to-issues` after
**closing** a blocking issue — closed, not merely merged — and the board advances itself.
`design/HANDOFF.md` →
*Working the issue board* is the single writer for the claim-by-assign / reconcile rules — including
which rows are pickable, and how far the recompute actually reaches.
