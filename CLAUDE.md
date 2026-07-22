# CLAUDE.md — Grounded Class Tutor

Auto-loaded each session. Sections are ordered **most stable first**; *Current status* at the bottom is
the only part expected to churn.

**Keep status thin — it must not restate the issue board.** The board is the single writer for what's
done and what's ready; duplicating it here creates two writers for one fact and they always diverge
(this file once listed a merged issue as ready to pick up). Update this file when a *slice's shape*
changes, not when an issue closes.

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
| `scripts/` | thin peer callers (`migrate.py`, `smoke_slice0.py`) |
| `tests/gct/` | the test suite |
| `design/` | current truth: ADRs, component specs, data model |
| `.claude/skills/roadmap-to-issues/` | the one in-repo skill — projects a roadmap slice into issues |
| `understanding/` | Nate's own notes — gitignored, never project truth |

## Local dev
```sh
uv sync                                 # install deps into .venv
uv run python scripts/migrate.py        # apply migrations/*.sql
uv run python scripts/smoke_slice0.py   # Slice 0 exit test → "PASS — foundation is wired."
uv run pytest tests/ -q                 # full suite
uv run ruff check src/ tests/           # lint
```
Postgres 17 is keg-only; its psql/createdb live at `/opt/homebrew/opt/postgresql@17/bin`.
Secrets (`OPENAI_API_KEY`, `DATABASE_URL`) live in `.env` (gitignored).
**What "green" is worth.** CI runs `ruff`, the migrations, and `pytest -m "not live"` on every PR,
against a pgvector Postgres 17 service container (#18, extended by #32). Green proves lint, that
`migrations/*.sql` applies cleanly, and every `db`-marked test — both DB paths, on fake embedders.
It does **not** prove anything `live`-marked (paid OpenAI calls) — those run locally only, with
`.env` secrets. The `db` fixture skips locally when Postgres is down but **hard-fails in CI**, so
DB tests can't silently skip their way to green.

## Conventions / invariants (do not violate)
- **Hand-rolled RAG** (ADR 0003) — no LangChain/LlamaIndex; we build the pipeline to learn it.
- **Provider-agnostic** — grounding logic sits *above* the provider interfaces; swapping models never changes product behavior.
- **Embedding consistency** (ADR 0018) — two distinct rules; don't collapse them:
  - *Which embedder gets constructed* — sourced only from `gct.config.ACTIVE_EMBEDDING_MODEL_ID`. Never hardcode a model id.
  - *What gets recorded about a run* — `chunks.embedding_model_id` is stamped from `embedder.model_id`, i.e. "the model that **actually produced** the stored vectors" (ADR 0018). **Not** from config: the Retriever's guard compares the stamp against the active embedder, so sourcing both from config would make it compare config to itself and never fire.
- **The PM-4 seam** — build the ingest pipeline (parse→chunk→embed→index) *pure and separate* from any job/queue/lease machinery, so Slice 2 wraps it instead of rewriting it.
- **`owner_id` on every row**; retrieval always filters `owner_id AND class_id` (F6/F12).
- **Citation spine** — source metadata born at parse ①, rendered to `[S#]` labels ②, resolved back to citations ③; the model only ever cites labels we handed it.

## Current status
**Slice 0 — Foundation: COMPLETE.** Schema + provider interfaces + the embedding-consistency anchor;
4 tables, vector column, scope + HNSW indexes, smoke test green end-to-end against live models.

**Slice 1 — the tracer bullet: IN PROGRESS.** Ingest ONE real file inline (parse→chunk→embed→index, no
queue) → Retriever → Grounder → cited answer / refusal, script-driven, over `eval/questions.jsonl`.
The differentiator, proven before any HTTP/UI. See `design/roadmap.md` and
`design/components/{grounder,retriever,ingestion-worker}.md`.

- **Write path: done end-to-end** (#4). `ingest_file(path, owner_id, class_id, embedder=, conn=)` takes
  a real PDF/PPTX to a `ready`, queryable, provenance-carrying chunk set in one atomic transaction —
  pure of job/queue machinery, so Slice 2 wraps it rather than rewriting it (PM-4 seam, ADR 0020).
- **Read path: half done.** Retriever shipped (#5) — `retrieve()` returns scoped, ranked
  `RetrievedChunk[]` with normalized scores, the exact shape the Grounder consumes. Remaining:
  #6 Grounder → #8 ask-smoke, in that order.
- **Exit gate:** `ask(class, question)` returns a cited answer for an in-corpus question and an honest
  refusal for an out-of-corpus one, demonstrated over the smoke suite.

**The board is the source of truth for what's ready** — [epic #9](https://github.com/Natenc12/grounded-class-tutor/issues/9)
has the dependency graph. `gh issue list --repo Natenc12/grounded-class-tutor --state open` for the
current frontier; `design/HANDOFF.md` → *Working the issue board* for the ready-frontier /
claim-by-assign / reconcile rules.

**Standing warning — do not pull #24 onto the Slice 1 frontier.** `index-publish-columns` (the re-index
`DO UPDATE` set-list leaving `failed_reason` and scope columns stale) is unreachable in Slice 1 and
parked for Slice 2: the right *location* for the fix depends on the Slice 2 worker's claim path, so
doing it now means guessing at a component that doesn't exist. This is the kind of thing the board
can't tell you on its own, which is why it lives here.
