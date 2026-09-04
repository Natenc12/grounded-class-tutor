# START HERE — Grounded Class Tutor

**Who this is for:** anyone landing on this repo cold — a reviewer, a fellow engineer, a future
collaborator. Zero context is the expected starting point, not a gap: Nate worked through the whole
design one-on-one with an AI architect (more on that below), so even someone joining the team starts
from scratch. This doc's only job is to get you from "no idea" to "I understand what we're building,
why, and where to dig in." **You don't need to read the whole design to start** — read Part 1, then
go into Part 2 when you want the implementation-level detail.

> **A note on using AI:** the design lives in a pile of markdown docs (`vision.md`, `architecture.md`,
> the `components/` specs, the `decisions/` folder, etc.). Any of them can be pasted into Claude or
> ChatGPT with "explain this to me like I'm new" — that's an intended way to read this project, not a
> crutch. This doc gives you the map so the AI's answers land in context.

---

# Part 1 — For everyone (read this)

## What we're building, in one breath
A study tutor that answers your questions using **your own course materials** — your prof's slides,
your PDFs, your notes — and shows you **exactly which slide or page** each answer came from. And when
your materials *don't* cover the question, it **says so** instead of making something up.

## Why that's the whole point
Students already study with ChatGPT. The problem: it doesn't know *your* class. It uses the wrong
notation, invents facts, and gives generic answers instead of what your professor actually taught.

Our product's entire value is **trust**: every answer is either (a) grounded in your materials and
cited to the source, or (b) an honest "that's not in your materials." That's it. If we nail that, we
have something real. If we let it invent facts like a generic chatbot, we've built nothing special.
This trustworthiness has a name in AI engineering — it's the canonical use case for **RAG** (see the
glossary). Building it well is also the point of the project: it's how we learn and demonstrate the
core skills of building trustworthy AI over private data.

## The system in your head
Only two things really happen. **Putting materials in** (ingest), and **asking questions** (answer):

```
  PUT MATERIALS IN                          ASK A QUESTION
  ────────────────                          ──────────────
  Upload a PDF/slides                       You type a question
        │                                         │
        ▼                                         ▼
  Break into chunks                         Find the chunks that
  Turn each chunk into                      best match your question
  an "embedding" (a                         (retrieval)
  searchable number-                              │
  print of its meaning)                           ▼
        │                                   Hand ONLY those chunks to
        ▼                                   the AI and say: "answer using
  Store in the database,                    only this; cite it; if it's
  remembering which file                    not here, refuse" (grounding)
  and page each came from                         │
                                                  ▼
                                            Cited answer  ─or─  honest refusal
```

The magic — and the hard part — is the right-hand side: fetch the *right* passage, cite it correctly,
and *refuse* when the material doesn't cover the question. That can't be proven on paper; it only
shows up once we build it on real course materials. Everything else (the upload screen, the database,
the API) is plumbing in service of that one promise.

## How this design came to be (so the docs make sense)
Nate designed this with an AI architect over six phases, roughly: **vision** (what/why) → **requirements**
(the specific things it must do) → **architecture** (the system's shape) → **component specs** (build-level
detail for the tricky parts) → **roadmap** (what to build in what order) → **pre-mortem + handoff** (stress-test,
then package it up). It's done — the design is finished and ready to build.

Three things that'll save you confusion:
- **"Done" above means the design phase, not the build.** The docs are finished and ready to build
  from — that's not the same claim as "the product is built." See `CLAUDE.md` → *Current status* for
  where building actually stands.
- **ADRs** ("Architecture Decision Records", the numbered files in `decisions/`) each record *one*
  decision and *why* — e.g. "we're hand-rolling RAG instead of using a framework, because we want to
  learn the fundamentals." There are a couple dozen. You don't read them front to back; you consult one
  when a choice looks arbitrary and you want the reasoning.
- **The ADRs and `architecture.md` are the current truth. `vision.md` is the origin story** — the
  first proposal. A few specifics changed after it was written (e.g. it names Claude + Supabase from
  day one; we actually settled on a *swappable* model layer with OpenAI as the starting default, and
  plain local Postgres first, Supabase later). **Where vision.md and a later doc disagree, the later
  doc wins.**

## The words we use (glossary)
Skim this, then refer back. These decode almost all the jargon in the other docs.

- **Corpus** — the collection of materials for one class. "In-corpus" = covered by the uploaded
  materials; "out-of-corpus" = not, and the tutor should refuse.
- **RAG** (Retrieval-Augmented Generation) — the whole technique: instead of letting the AI answer
  from memory, you *retrieve* relevant passages from your own documents and make the AI answer *from
  those*. It's what makes answers grounded and citable.
- **Chunk / chunking** — a document is too big to search as one blob, so we split it into small pieces
  ("chunks"). *How* we split (size, overlap) is one of the biggest levers on quality.
- **Embedding** — a chunk (or a question) turned into a list of numbers that captures its *meaning*.
  Similar meanings → similar numbers. This is what makes "search by meaning" possible.
- **Vector search / pgvector** — finding the chunks whose embeddings are closest to the question's
  embedding. `pgvector` is the tool that lets our normal database do this, so we don't need a separate
  search product.
- **Retrieval / top-k** — the step of fetching the best-matching chunks. "top-k" = "give me the k best"
  (e.g. the top 5).
- **Grounding** — forcing the answer to come *only* from the retrieved chunks. The opposite of the AI
  free-styling from memory.
- **Refusal** — when nothing in the corpus answers the question, the tutor declines instead of inventing.
  A refusal is a *feature*, not a failure.
- **Citation / citation spine** — every answer points back to its source (file + page/slide). The
  "citation spine" is just our name for making sure that source info is carried carefully through every
  step, from upload to final answer, so citations are never guessed.
- **Slice** — one build step that produces something working end-to-end (see the roadmap). We build in
  slices instead of building all of one layer at a time.
- **Tracer bullet** — the very first slice: the thinnest possible version that goes all the way through
  (one file in → one cited answer out), so we prove the hard part works before building anything fancy.
- **Two-signal eval** — how we score whether it's working: we measure *retrieval* (did we fetch the
  right source?) and *grounding* (did the answer stay faithful / refuse correctly?) **separately**, so a
  retrieval miss can't hide behind a decent-looking answer.

---

# Part 2 — When you're ready to build

## Where the design lives (read in this order)
You do **not** need all of these before contributing. Start at the top; go as deep as your task needs.

1. **`vision.md`** — *(everyone, ~10 min)* the why and the scope. Origin story — see the drift note above.
2. **`requirements.md`** — *(everyone)* the specific things V1 must do, tagged by version and priority.
3. **`architecture.md`** — *(implementers)* the system's shape, how the pieces fit, and the "citation
   spine." **This is the current source of truth for the stack.**
4. **`data-model.md`** — *(implementers)* the exact database tables (`classes · files · chunks · jobs`).
5. **`components/`** — *(whoever owns that piece)* deep build specs for the make-or-break parts:
   **grounder.md** (the trust core) → **retriever.md** (the read path) → **ingestion-worker.md** (the
   write path) → **api.md** (the thin HTTP adapter over all three).
6. **`roadmap.md`** — *(everyone, before starting)* the build sequence, slice by slice.
7. **`HANDOFF.md`** — the dense, complete build manifest. It's written in shorthand for an AI assistant —
   **paste it (or a slice of it) into Claude when you're implementing** and it'll have the full picture.
8. **`pre-mortem.md`** / **`decisions/`** — the stress-test and the "why" behind every choice. Consult, don't read cover-to-cover.

## The build sequence (plain version — full detail in roadmap.md)
- **Slice 0 — Foundation:** set up the database tables and the swappable model interfaces.
- **Slice 1 — Tracer bullet:** ingest ONE file and get a cited answer / refusal, driven by a script.
  This is *the differentiator*, proven first, before any web page or fancy UI exists.
- **Spike Pass 1 — validate:** confirm it actually grounds and refuses on real materials. A bad result
  here is *cheap* — that's the point of proving it early.
- **Slice 2 — Real upload path:** wrap the proven pipeline in a background worker so uploads process
  reliably.
- **Slice 3 — API:** a thin web API over the core (create class, upload, ask).
- **Slice 4 — Client:** the minimal React screens — create a class, upload, see status, ask, read the
  cited answer. **V1 is done here.**
- **Spike Pass 2 — tune:** now optimize quality (chunking, how many chunks, which embedding/generation
  model) with evidence.

## The one caveat to keep honest
"It builds and demos" was never the same claim as "it's trustworthy" — that's the one thing we have to
*earn* with evidence, not assume. Slice 1 ran the grounding loop against real course materials and
recorded what happened — see [`eval/FINDINGS.md`](../eval/FINDINGS.md) for the actual measurements
(retrieval hit rate, hallucination rate, honest-refusal rate, and where the noise floor sits), not a
restated verdict here. Spike Pass 1 — the pass that puts those numbers under deliberate pressure —
has now run; its verdict is [`decisions/0026`](decisions/0026-spike-pass-1-verdict.md), and it is
careful about what it claims: the core is validated *at a named configuration*, and that is still not
the same claim as "trustworthy" (the ship bar is V3's). Read those files, or the open issue board, for
where things currently stand — this caveat is about the *kind* of claim to trust, not a snapshot of a
number that will move.
