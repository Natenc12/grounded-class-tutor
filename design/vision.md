# Vision / Proposal — Grounded Class Tutor

**One-liner:** A study tutor that answers questions and teaches Socratically — always grounded in
*your own course materials*, with citations back to the source, and refusing to make things up
beyond what's there.

**Bucket:** A — Retrieval / RAG engineering
**Primary technical challenge:** RAG done well — retrieval quality, citations, grounding
**Status:** Discovery proposal (Phase 0). Not yet greenlit for full design.
**Rigor (proposed):** Standard leaning Deep — real production app, team of 3; the retrieval/
grounding core deserves adversarial depth. Confirm at kickoff.

---

## The landscape (why this problem, why now)
Students already study with ChatGPT — but it doesn't know *their* class. It uses the wrong
notation, invents facts, and answers with generic material instead of what the professor actually
taught. Course-specific help today is either **office hours** (scarce, synchronous) or **generic
AI** (available, but untrustworthy for a specific course).

The gap: a tutor **grounded in your own materials** — your prof's slides, your notes, the
assigned readings — that cites where every answer comes from and *refuses to answer* when the
material doesn't cover it. That trustworthiness is the whole point, and it's the canonical use
case for **RAG**. It's practical now because embeddings are cheap and **Supabase ships pgvector**,
so a private per-student corpus needs no separate vector database.

**Who it's for:** you and your team first (dogfood on real classes), then any student who wants
help anchored to what their course actually says.

---

## What it is (the core loop)
1. **Create a class** and **upload its materials** (slides, PDFs, notes; photo capture later).
2. **Ingest** — parse → chunk → embed → index into pgvector (with source metadata: file, page,
   slide).
3. **Ask** — questions, or start a Socratic session. Answers are **retrieved** from your
   materials and returned **with citations** to the exact slide/page.
4. **Ground** — if the answer isn't in your materials, it says so plainly instead of inventing.

The whole product rests on step 3–4 being *trustworthy*.

---

## Scope
**MVP spine (smallest valuable, demoable slice):**
- One class → upload PDFs/slides → ingest (chunk + embed + index) → **cited Q&A** with a
  **grounding guardrail** (refuse when not in corpus). Single user.

**Roadmap (later, not v1):**
- **Socratic tutoring mode** (teach, don't just answer).
- **Photo / OCR** of handwritten notes.
- **Auto-generated quizzes** — the natural bridge to the "concept-mastery tutor" idea.
- Multi-class, shared/class-wide libraries, retrieval **re-ranking**.

**Explicit non-goals:** not a general-knowledge chatbot; not a note-taking app; **no web/general
search in v1** — grounding to *your* corpus is the differentiator, and mixing in general knowledge
would blur exactly the thing we're demonstrating.

---

## The stack (fully fleshed)
> **⚠️ Origin-story caveat (drift):** this table is the Phase-0 proposal. Several choices were later
> refined in design — the model layer became **provider-agnostic with OpenAI as the V1 default** (not
> Claude-first), and V1 runs on **local Postgres, moving to Supabase in V2** (not Supabase day one).
> `architecture.md` + the ADRs are the current truth; where they disagree with this table, they win.

| Layer | Choice | Why |
|---|---|---|
| **Frontend** | React, mobile-first. RN (Expo) or PWA | Here **capture** (upload PDFs, photograph slides) matters more than push; Expo gives clean camera/file access. Lighter than the autopilot's mobile needs — PWA is a defensible v1. |
| **Backend** | **FastAPI** (Python) — ingestion pipeline + retrieval endpoint + generation endpoint | Python owns the RAG ecosystem (parsing, embeddings, vector search); FastAPI is async and typed. |
| **Data / Auth / Vectors** | **Supabase** — Postgres + **pgvector** (embeddings live *in* the DB) + Auth + Storage (uploaded files) | **pgvector means the vector DB is already in your stack** — no Pinecone/Weaviate. Big scope win. RLS isolates each student's corpus. |
| **Generation LLM** | **Claude** — a mid/high-tier model (Sonnet-tier default, Opus-tier for hard tutoring; Haiku-tier for cheap ops) | Strong instruction-following for citation formatting + refusal behavior; model routing controls cost. |
| **Embeddings** | Anthropic has **no native embeddings** → use a dedicated model: **Voyage AI** (Anthropic-recommended) or OpenAI `text-embedding-3`. Pick one at kickoff | Retrieval quality starts here; a good, cheap embeddings model is the foundation of the whole pipeline. |
| **RAG orchestration** | **Recommend hand-rolled** (parse → chunk → embed → cosine search over pgvector → cited prompt). Alt: LlamaIndex / LangChain | You want to *learn RAG* — hand-rolling the core loop teaches the fundamentals a framework would hide. Reach for a framework only if velocity demands it. |
| **Ingestion** | PDF/slide parsing (pypdf, python-pptx, `unstructured`); chunking strategy; OCR (Tesseract or a vision model) for photos — **later** | Ingestion quality upper-bounds retrieval quality; chunking is where much of the win/loss lives. |
| **Deployment** | Frontend: Expo EAS or Vercel. Backend: Render / Railway / Fly.io. DB/Auth/Vectors: Supabase managed | Cheap, simple, real CI/CD; the whole RAG stack fits a small managed footprint. |

---

## Primary challenge, per part
- **Frontend:** a chat UI that shows **citations** cleanly (tap a claim → jump to the source
  slide) and a low-friction upload flow.
- **Ingestion:** the **chunking strategy** — the single biggest lever on retrieval quality.
- **Data:** pgvector indexing + carrying **source metadata** (file/page/slide) through to
  citations.
- **Retrieval + generation:** **retrieval quality** (right passage, top-k) and the **grounding
  guardrail** (faithful citations; refuse when the corpus doesn't cover it).

---

## AI concepts you'll learn / demonstrate
- **Embeddings** and **vector search** (pgvector)
- **Chunking strategies** and their effect on retrieval
- **Retrieval** (top-k, and later re-ranking)
- **RAG prompt construction** (grounding the generation on retrieved context)
- **Citation / attribution**
- **Grounding & hallucination prevention** (refusal when out-of-corpus)
- **Evaluation of retrieval** (precision/recall, answer faithfulness)

---

## The make-or-break (empirical — can't be settled on paper)
**Retrieval quality + faithful grounding.** Does it fetch the right passage, cite it correctly,
and *refuse* when the material doesn't cover the question? The whole value proposition rests here,
and it can only be proven by building an ingestion→retrieval spike on real course materials.

---

## Why this project (rationale)
The cleanest way to learn and demonstrate core AI fundamentals: RAG, embeddings, vector
databases, retrieval, citations, hallucination prevention.

**Recruiter takeaway:** *"This person understands how to build trustworthy AI over proprietary
data."*

---

## Open questions (resolve at kickoff / Architecture)
- Mobile approach: React Native (Expo) vs. React PWA?
- Embeddings provider: **Voyage** vs. OpenAI `text-embedding-3`?
- RAG: **hand-rolled** vs. LlamaIndex/LangChain?
- MVP inputs: PDFs/slides only, or include photo/OCR from day one?
- MVP interaction: cited Q&A only, or Socratic tutoring mode too?
- Single class vs. multi-class in v1?
