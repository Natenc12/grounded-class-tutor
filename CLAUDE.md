# CLAUDE.md — Grounded Class Tutor

Auto-loaded each session. Keep the **Current status** section updated as slices complete — it's how a
cold session (or a collaborator) learns where we are.

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

## Local dev
```sh
uv sync                                 # install deps into .venv
uv run python scripts/migrate.py        # apply migrations/*.sql
uv run python scripts/smoke_slice0.py   # Slice 0 exit test → "PASS — foundation is wired."
```
Postgres 17 is keg-only; its psql/createdb live at `/opt/homebrew/opt/postgresql@17/bin`.
Secrets (`OPENAI_API_KEY`, `DATABASE_URL`) live in `.env` (gitignored).

## Conventions / invariants (do not violate)
- **Hand-rolled RAG** (ADR 0003) — no LangChain/LlamaIndex; we build the pipeline to learn it.
- **Provider-agnostic** — grounding logic sits *above* the provider interfaces; swapping models never changes product behavior.
- **Embedding consistency** (ADR 0018) — index-time and query-time embeddings use the *same* model id, sourced only from `gct.config.ACTIVE_EMBEDDING_MODEL_ID`. Never hardcode it elsewhere.
- **The PM-4 seam** — build the ingest pipeline (parse→chunk→embed→index) *pure and separate* from any job/queue/lease machinery, so Slice 2 wraps it instead of rewriting it.
- **`owner_id` on every row**; retrieval always filters `owner_id AND class_id` (F6/F12).
- **Citation spine** — source metadata born at parse ①, rendered to `[S#]` labels ②, resolved back to citations ③; the model only ever cites labels we handed it.

## Current status
**Slice 0 — Foundation: COMPLETE** (schema + provider interfaces + embedding-consistency anchor).
DB migrated & verified (4 tables, vector column, scope + HNSW indexes). Smoke test's final green PASS
is pending Nate adding his `OPENAI_API_KEY` to `.env`.

**Next up → Slice 1 (the tracer bullet):** ingest ONE real file inline (parse→chunk→embed→index, no
queue) → Retriever → Grounder → cited answer / refusal, script-driven, shipping `eval/questions.jsonl`
(~12-question smoke suite). This is the differentiator, proven before any HTTP/UI. See `design/roadmap.md`
and `design/components/{grounder,retriever,ingestion-worker}.md`.
