# 0006. Tech stack rationale (full-stack, deliberate not defaulted)

- **Date:** 2026-07-04
- **Status:** accepted

## Context
The proposal listed a plausible stack, but a Deep-rigor project shouldn't inherit choices by
default. This ADR walks **every layer**, states why it's chosen over its real alternatives, and
points to the layer-specific ADRs where one already exists. Scrutinizing the stack surfaced two
changes (deployment topology; an ingestion-is-async architecture flag) — captured below.

## Decision — layer by layer

### Frontend — **React PWA**
Chosen. The interaction is web-shaped (chat + upload + citation reading); PWA minimizes the
frontend tax so effort stays on the RAG core, ships from one codebase to iOS/Android/desktop, and
has zero app-store friction for a solo dogfooder.
- **vs React Native** — the one native advantage (camera scanning) maps to photo/OCR, deferred to
  V4; V4 is the honest reconsideration point. See **ADR 0002**.
- **vs Vue/Svelte** — React for ecosystem depth + hiring/portfolio signal.

### Backend language — **Python**
Chosen because it's product-determined, not defaulted: the two things that actually live in the
backend — **ingestion/parsing** (`unstructured`, pypdf, python-pptx) and the **V3 eval ecosystem** —
are overwhelmingly Python-native, while model/embedding/DB calls are language-agnostic HTTP.
- **vs TypeScript/Node** — would unify language with the React frontend, but parsing is weaker in JS
  and the eval tooling is thinner; those are core to *this* product. Team Python fluency isn't a
  blocker, so ecosystem fit wins. (A TS frontend + Python backend split is standard and clean.)

### Backend framework — **FastAPI**
Best-in-class Python API layer: async (matters for I/O-bound LLM/embedding calls), Pydantic typing,
auto-generated OpenAPI docs.
- **vs Flask** — sync-first, less typing/validation out of the box.
- **vs Django** — too heavy; we don't need its ORM/admin batteries, and Supabase owns the data layer.

### Database + Vector store — **Supabase Postgres + pgvector**
One engine holds both relational data *and* embeddings — the vector DB is already in the stack.
- **vs dedicated vector DB (Pinecone / Weaviate / Qdrant)** — those earn their extra cost + separate
  infra only at millions of vectors. At per-user-course-corpus scale (thousands of chunks) pgvector
  is ample, and keeping vectors beside the relational rows means **one** migration/backup/consistency
  story. Revisit only if scale demands (ADR-able later).
- **Lock-in risk: low** — it's standard Postgres, self-hostable, portable.

### Auth — **Supabase Auth**
Consolidation win, and **RLS is the killer feature** — it enforces the corpus-isolation requirement
(F13 / N8) at the data layer, which is exactly the security boundary we refused to leave to app code.
- **vs Clerk / Auth0** — nicer DX but another vendor for zero v1 gain, and they don't give us RLS.
- v1 runs single-user with an `owner_id` hedge; auth turns on in V3 (ADR 0004).

### Storage — **Supabase Storage**
Same platform, private per-user buckets, no public URLs (N9).
- **vs raw S3** — more setup, no benefit at this scale.

### Generation LLM — **provider-agnostic, OpenAI (GPT) default**
Behind a provider interface; the model is a swappable dependency. Default is OpenAI for operational
consolidation with the embeddings vendor (one key/billing/SDK) — swappable, spike-confirmed, with
Claude as a first-class swap candidate. See **ADR 0004** (abstraction) + **ADR 0007** (the default).

### Embeddings — **OpenAI `text-embedding-3-small` (default, swappable)**
Cheapest credible option, strong retrieval-for-cost; final model confirmed by the retrieval spike.
See **ADR 0005**.

### RAG orchestration — **hand-rolled** (no framework)
Control over the citation-metadata path + the learning/portfolio signal. See **ADR 0003**.

### Ingestion / parsing — **pypdf + python-pptx + `unstructured`**
Python's genuine strength (a reason the backend is Python). The *granular* choice among these — and
the chunking strategy — is a component-design decision; here we just fix the ecosystem.

### Deployment — **Vercel (frontend) + persistent container host (backend) + Supabase (managed)**
- Frontend static/PWA → **Vercel** (or Expo EAS if we ever go native).
- Backend → a **persistent container host (Render / Railway / Fly)**, **not** serverless functions.
  **This is a change from the vague proposal:** ingestion (parse + embed a ~50-page deck, ~1–2 min
  per N6) would blow serverless timeouts. A container host also lets ingestion run as a background
  worker. Specific host chosen at V2 deploy time — minor, deferrable.
- DB/Auth/Vectors/Storage → **Supabase managed**.

## Consequences
- **Architecture flag (Phase 2):** ingestion must be an **async background job**, not an inline HTTP
  request — this shapes the backend's component boundaries and the "ingestion status" UX (F3).
- Three model/infra vendors (OpenAI embeddings, a generation vendor, Supabase) — acceptable and
  abstracted where it matters (model vendors behind interfaces, ADR 0004; Supabase is portable Postgres).
- Backend host is the one deferred sub-choice (Render vs Railway vs Fly) — decided at V2.
