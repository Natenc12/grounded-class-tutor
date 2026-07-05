# 0004. Delivery strategy: API-first, provider-agnostic, staged maturity ladder

- **Date:** 2026-07-04
- **Status:** accepted

## Context
We initially considered a **local-first** build (run a local model in v1, swap to APIs later) to
avoid dogfooding cost, and set auth/RLS "from day one" assuming a team of 3. On inspection two
assumptions broke:
1. **Cost.** Embedding a whole class corpus on OpenAI `text-embedding-3-small` is **~2–6 cents,
   one time**; re-embedding it ~50× while tuning chunking is **under $3**; generation at
   solo-dogfood volume is single-digit dollars. The "very expensive" fear was off by ~2 orders of
   magnitude.
2. **Tenancy.** Dogfooding is realistically **solo**, not a team of 3 — so multi-user isolation is
   not a v1 need.

Separately, **local models are weakest at exactly this product's core** — instruction-following
for citation format and *faithful refusal* — so a local-first v1 would make the grounding look bad
for reasons unrelated to the design, corrupting the read on the make-or-break.

## Decision
- **Run on hosted APIs from v1** — no local-model stage.
- **Abstract both generation and embeddings behind provider interfaces** so the model is a
  swappable dependency, not a hardcoded assumption. No lock-in to any single vendor (including
  Anthropic).
- **Organize delivery as a maturity ladder:**
  - **V1 — local-dev walking skeleton (on API):** upload PDFs/slides → ingest → **cited Q&A with
    the grounding guardrail**. Single hardcoded user, no auth, no deploy, no formal eval harness —
    **but the grounding loop exists**.
  - **V2 — deployed:** Supabase (Postgres + pgvector + Storage), PWA hosted.
  - **V3 — evaluation + accounts:** build the formal eval harness and tune the numeric bars
    (retrieval recall, faithfulness, refusal, citation correctness); turn on per-user auth +
    Supabase RLS.
  - **V4+ —** Socratic mode, OCR, quizzes, multi-class.
- **Schema carries an `owner_id` column from V1** (hardcoded single user) so enabling real
  auth/RLS in V3 is "turn on enforcement," not a schema reshape.

## Alternatives considered
- **Local-first (defer API)** — rejected: the cost saving is pennies (not worth a build stage), and
  a weak local model would corrupt the grounding read. The provider interface delivers the real
  benefit (swap freely) without the local-model penalty.
- **Vendor-locked stack (hardcode Claude + one embeddings model)** — rejected: abstraction is cheap
  insurance and is what lets the make-or-break spike bake off models.
- **RLS/auth from day one** — rejected for v1: solo dogfood doesn't need it; the `owner_id` hedge
  prevents a later retrofit.

## Consequences
- Collapses the earlier local→deploy→API ladder into **skeleton → deploy → evals+auth**.
- The **provider interface** becomes a first-class component in Architecture (Phase 2).
- The numeric grounding bars move from a **v1 gate → V3**; V1 keeps the grounding loop but does
  **not** measure it. **Amends ADR 0002** (tenancy) and reframes eval timing in `requirements.md`.
- Grounding quality in V1 reflects the chosen API model, not the ceiling — real tuning is V3.
