# HANDOFF — Grounded Class Tutor

> **History note (2026-11-22):** This handoff was written for building the retired hosted web app,
> removed in stage 2 of the desktop move
> ([#216](https://github.com/Natenc12/grounded-class-tutor/issues/216)). Still true: the Grounder
> and its five states, the citation spine, and the two-signal eval scoring of ADR 0023. Everything
> else here, including the shape, the constraints, the reading order, the build order, the board
> workflow and the open assumptions, is history; the component docs for the retriever, the ingestion
> worker and the API were deleted (git history keeps them). For where things stand, see the Status
> section of the [README](../README.md); for the plan, see #216.

**Status:** Build-ready ✅ (gate green — *buildable*; validated **at one named configuration** per ADR 0026,
which is still not *proven faithful* — see caveat).  **Rigor:** Deep (ADR 0001).
**Primary reader:** a build-agent implementing V1. All design decisions settled (PM-1 ratified as ADR 0023).

## Build brief (the 60-second version)
- **What:** A tutor grounded in *your own* course materials. Upload PDFs/slides to a class; ask a
  question scoped to that class; get an answer **cited to the exact source (file + page/slide)**, or an
  honest **refusal** when the corpus doesn't cover it. RAG done well — the trust promise *is* the product.
- **The shape (one line each):**
  - **Callable RAG core (library)** — does the work; the API and the V3 eval harness are thin *peer callers*.
  - **Ingestion worker** — off-thread parse → chunk → embed → index; all-or-nothing atomic replace; source
    metadata (file, page/slide) born at parse = citation-spine honor-point ①.
  - **Retriever** — embed query → pgvector top-k under `owner_id AND class_id` → ranked chunks + similarity
    scores. No judgment (no re-rank, no relevance gate in V1).
  - **Grounder** — labeled context → single model call → resolve `[S#]` labels back to citations →
    `GROUNDED | PARTIAL | REFUSAL | INTEGRITY_FLAGGED | ERROR`. The refusal decision lives here.
  - **Provider layer** — thin `Embeddings.embed` + `Generation.generate` interfaces; OpenAI defaults,
    swappable. Grounding logic sits *above* it, so swapping providers never changes product behavior.
  - **Async substrate** — DB-backed `jobs` + status behind an `enqueue`/`claim` boundary; a **separate**
    poll-worker process (not in the API event loop). Broker is a V2 swap.
  - **Data** — Postgres + pgvector; `classes · files · chunks · jobs`; `owner_id` on **every** row.
  - **Client** — minimal React SPA: create class · upload · ingest status · ask · view cited answer.
- **Hard constraints:** hand-rolled RAG (C1) · provider-agnostic model layer (C1/ADR 0004) · pgvector
  throughout, local Postgres V1 → Supabase V2 with no data-layer rewrite (C4) · index-time and query-time
  embeddings **must** be the identical model+version (invariant) · V1 = single hardcoded user, no auth, no
  deploy (F12/ADR 0004) · validate on **real** course materials, not synthetic (C5).
- **Where to start:** whatever stage the README's [Status](../README.md#status) section names — the slices below are the
  full arc, not a to-do list to begin at the top of. The order was Slice 0 (schema + provider
  interfaces), then Slice 1 (the synchronous tracer bullet) — that's the differentiator, proven
  programmatically before any HTTP or UI exists.

## Reading order (manifest — pointers into the artifacts)
1. **vision.md** — why this exists, who it's for, scope.
2. **requirements.md** — F1–F15 / N1–N15 (tagged V1/V2/V3, P0/P1) + success-criteria rollup.
3. **architecture.md** — system shape, component map, data flows, the **citation spine** (3 honor points),
   cross-cutting invariants.
4. **data-model.md** — the row shapes (`classes · files · chunks · jobs`); single source of truth for the schema.
5. **components/** — make-or-break build specs, in this order:
   **grounder.md** (the trust core) → **retriever.md** (read path) → **ingestion-worker.md** (write path)
   → **api.md** (the thin HTTP adapter; composition root + the route contracts).
6. **roadmap.md** — the V1 build sequence (Slices 0–4) + the two-pass spike slate.
7. **eval/questions.jsonl** — the smoke-suite benchmark (authored; grows over the project's life, ADR 0021); the differentiator's only V1 test.
8. **pre-mortem.md** — the adversarial ledger (PM-1…PM-5) behind this handoff.
9. **decisions/** — the numbered ADRs; the "why." Consult when a choice looks arbitrary.

## Build order (first slice → full system)  *(full detail: roadmap.md)*
- **Slice 0 — Foundation:** migrate `classes · files · chunks · jobs`; `Embeddings`/`Generation` interfaces
  (3-small/`dim=1536`, GPT). Wire the embedding-consistency invariant. *Exit:* schema clean + a smoke
  round-trip through both interfaces.
- **Slice 1 — Tracer bullet (the differentiator, ASAP):** ingest ONE file **inline** (parse→chunk→embed→
  index, no queue) → Retriever → Grounder → cited answer / refusal, script-driven. Ships with
  `eval/questions.jsonl` (~12-question smoke suite). **Build the pure pipeline separate from any job shell
  (PM-4).** *Exit:* programmatic `ask(class, q)` → cited answer for in-corpus, refusal for out-of-corpus,
  over the smoke suite. **This is the walking skeleton's spine.**
- **Spike Pass 1 — validate (not optimize):** chunking + generation on the tracer; confirm it grounds and
  refuses *before* thickening. Scored by ADR 0023's two-signal rule (retrieval ∥ grounding). A red result here is a cheap signal.
- **Slice 2 — Real write path:** wrap the *proven* inline pipeline in the async worker + `jobs` queue +
  failure/idempotency (retryable/terminal split, atomic replace). *Exit:* upload → `queued→…→ready/failed`,
  reaper-safe, no partial index ever visible.
- **Slice 3 — API (thin):** FastAPI over the core — `POST /classes`, `POST /files`, `GET /files/:id`,
  `POST /ask`. No business logic. *Exit:* full loop drivable over HTTP.
- **Slice 4 — Client (minimal):** React SPA, the five P0 surfaces, clean inline citation rendering.
  *Exit — V1 done:* upload → ingest → cited answer → refuses, end-to-end in the UI.
- **Spike Pass 2 — tune:** full bake-off (chunking, k, embedder, generator) on a representative corpus,
  scored against the grown eval file; provisional defaults → evidence-backed choices.

## Working the issue board
The board runs on labels:
- **`needs-triage`**: captured, not yet reviewed with the owner. Don't pick these up.
- **`ready`**: signed off by the owner, clearly defined, nothing blocking. Only these are pickable.
- **`blocked`**: signed off, but waiting on another issue first. Leave it until that issue closes.
- **`in-progress`**: someone is working the ticket. Add it when you start; one claim at a time.

Issues closed at triage get `duplicate` or `wontfix`.

## Readiness gate ✅  (all true → handoff valid)
- [x] **No load-bearing design questions left open.** PM-1 (eval scoring rule) was the last one →
      resolved and **ratified** as **ADR 0023** (two-signal scoring: retrieval ∥ grounding; PARTIAL a
      tracked bucket, not a pass). The empirical unknowns (chunking, k, embedder, prompt) are **planned
      spike work with a bench**, not open design questions (ADR 0004/0022).
- [x] **Every component has a build spec with a defined interface.** Grounder / Retriever / Ingestion-worker
      specs carry interface contracts; provider layer = ADR 0013; API = thin adapter (endpoints listed);
      data shapes = data-model.md.
- [x] **Success criteria are testable.** V1 "done" = upload→ingest→cited answer→refuse over the smoke suite,
      now *scoreable* via ADR 0023. N1–N4 carry numeric V3 bars.
- [x] **Key tradeoffs recorded as ADRs.** See `decisions/`.
- [x] **Assumptions & explicit non-goals stated.** Scope boundary (ADR 0002); V1 crude-grounding ladder
      (ADR 0004); carried assumptions below.

> ⚠️ **Green = buildable. Validated ≠ proven faithful.** The differentiator (grounding/refusal quality,
> N1–N4) is **empirical** — it ships crude and unmeasured by design (ADR 0004). **Spike Pass 1 was the
> first real de-risking**, and it validated the core at **one named configuration** (ADR 0026) — a
> narrower claim than trustworthy, and one that does not retire this caveat. Treat a demoable V1 as
> *buildable*, never as *proven faithful*. This caveat is load-bearing — do not over-read a passing gate.

## Open assumptions (the builder may proceed on these)
- **Eval scoring is two-signal (PM-1 / ADR 0023, ratified).** Score retrieval (`expected_sources` in
  top-k → N1) separately from grounding (the Grounder state → N2/N3). Grounding: GROUNDED=pass,
  **PARTIAL=tracked bucket (not a pass)**, REFUSAL/INTEGRITY_FLAGGED=fail (in-corpus), ERROR=excluded+logged.
  Rank on `grounded_pass_rate` with `partial_rate` reported alongside over the same denominator. Build
  the `state→outcome` map + retrieval check as a pure function in the core (one definition, V1 & V3).
- **Provider adapter owns embed sub-batching (PM-2).** The `Embeddings` adapter splits `texts` under the
  provider cap (2048 inputs / ~300k tokens) below the interface; the worker's "embed all" stays honest.
- **Worker runs as a separate process (PM-3 / ADR 0011 addendum)** against the same Postgres — not inside
  the API event loop.
- **No corpus-wide re-index operation in V1 (PM-5).** The Pass-2 embedder swap re-indexes via an ops script
  that re-enqueues every `file_id`; changing the embedder without it bricks a class (mixed
  `embedding_model_id` → Retriever ERRORs). Single dogfooder avoids the foot-gun; real re-index op = V2/V3.
- **Spike defaults are provisional:** 3-small/1536 · pypdf + python-pptx · fixed-size+overlap chunking ·
  k=5 · GPT. Each is a tunable default, not a commitment (roadmap spike slate).
- **Prompt content is empirical** — the specs fix the *shape* (labeled context, `[S#]` vocabulary, coverage
  marker), not the wording; tuned on the tracer against the smoke suite.
