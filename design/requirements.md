# Requirements — Grounded Class Tutor

**Phase:** 1 — Requirements  **Rigor:** Deep (ADR 0001)
**Delivery model:** the **maturity ladder** (ADR 0004) — build a working skeleton, then deploy,
then measure/tune. Requirements below are tagged with the stage they must land in:

- **V1 — local-dev walking skeleton (on API):** the grounded core exists and runs locally. Single
  hardcoded user, no auth, no deploy, no formal eval harness.
- **V2 — deployed:** hosted on Supabase + PWA.
- **V3 — evaluation + accounts:** formal eval harness + tuned numeric bars; auth + RLS on.
- **V4+ —** roadmap (Socratic, OCR, quizzes, multi-class).

> The guiding line (ADR 0004): **the grounding loop — retrieval, citations, and refusal — exists in
> V1, even crude and unmeasured.** What V3 adds is *measuring and tuning* it, not *having* it.
> Skipping it in V1 would make this a generic-chatbot skeleton, not this product.

Priorities within a stage: **P0** must-have, **P1** nice-if-cheap.

---

## Functional requirements

### Corpus management
- **F1 · V1 · P0** — Create a **class** and upload one or more **PDF / slide (PPTX)** files to it.
- **F2 · V1 · P0** — Ingest uploads: parse → chunk → embed → index, with **source metadata
  preserved** (file, page, slide) on every chunk.
- **F3 · V1 · P0** — Ingestion status is visible (queued / processing / ready / failed per file).
- **F4 · V2 · P1** — Delete a file (and its chunks) or a whole class.
- **F5 · V4 · —** Photo/OCR capture of handwritten notes. *Deferred (ADR 0002).*

### Ask / retrieve / ground — the core loop
- **F6 · V1 · P0** — Ask a natural-language question scoped to one class; system **retrieves** the
  top-k relevant chunks from that user's corpus and generates an answer from them.
- **F7 · V1 · P0** — Every answer carries **citations** to the exact source (file + page/slide) of
  the chunks used.
- **F8 · V1 · P0 — the grounding guardrail** — when retrieved context doesn't support an answer,
  the system **says so and refuses to invent** rather than using general knowledge. No web/general
  search (non-goal). *(Crude in V1; calibrated in V3 — see N3.)*
- **F9 · V2 · P1** — Tap a citation → jump to / preview the source slide or page.
- **F10 · V4 · —** Socratic tutoring mode. *Deferred (ADR 0002).*
- **F11 · V4 · —** Auto-quizzes; retrieval re-ranking; multi-class-per-user; shared libraries.
  *Deferred (ADR 0002).*

### Accounts & isolation
- **F12 · V1 · P0** — Data model carries an **`owner_id`** on class/file/chunk rows; V1 runs as a
  **single hardcoded user** (no login UI). *(ADR 0004 hedge.)*
- **F13 · V3 · P0** — Per-user **authentication (Supabase Auth)** + a user can only ever
  retrieve/see **their own** corpus, enforced by **Supabase RLS** (fails closed). Turning this on
  is enabling enforcement over the V1 schema, not reshaping it.

### Model layer
- **F14 · V1 · P0** — **Generation and embeddings sit behind provider interfaces** — the model is a
  swappable dependency, not hardcoded (ADR 0004). Default embeddings = OpenAI `text-embedding-3-small`
  (ADR 0005); default generation = OpenAI GPT tier (ADR 0007) — swappable, Claude a swap candidate.

### Evaluation infrastructure
- **F15 · V1 · P0 (file) / P1 (execution)** — a **version-controlled `eval/questions.jsonl`** artifact:
  known-answer + known-out-of-corpus questions with per-question `expectation` (answer/refuse),
  `expected_sources`, and `suites` for subset selection (ADR 0021). **The file + a seed smoke suite
  (~12 questions) is P0** — it's the benchmark the entire spike slate scores against and the only V1
  test of the differentiator, so without it the spikes can't conclude. **Running the manual pass +
  growing coverage is P1.** Forward-shaped so the V3 harness (N1–N4) reads the *same* file — the
  scoring method matures, the schema does not.

---

## Non-functional requirements

### Retrieval quality & grounding — the make-or-break (the V3 quality gate)
These are **empirical** — validated only by the ingestion→retrieval spike on the **real** dogfood
corpus. The grounding loop ships crude in V1; these **numeric bars are the V3 gate**, and the eval
set + harness is itself a **V3 P0 deliverable**. (Baselines captured before tuning so improvement
is demonstrable.)
- **N1 · V3 · P0 — Retrieval relevance:** supporting passage in top-k. Target **recall@k ≥ 0.85**;
  report precision@k.
- **N2 · V3 · P0 — Answer faithfulness:** answers assert only what retrieved context supports.
  Target **≥ 0.90** (human review and/or LLM-judge with spot-checks).
- **N3 · V3 · P0 — Refusal calibration:** on out-of-corpus questions, refuse. Target **≥ 0.90**
  correct-refusal **and ≤ 0.10** false-refusal on in-corpus (over-refusing is also failure).
- **N4 · V3 · P0 — Citation correctness:** a cited source actually contains the claim. Target
  **≥ 0.95** on spot-check.

> These targets are *starting* numbers — real numbers only exist after the spike; revisit at V3.

### Performance & cost
- **N5 · V2 · P1 — Answer latency:** target **≤ 5 s** p50 / **≤ 10 s** p95 (retrieval + generation).
- **N6 · V2 · P1 — Ingestion throughput:** a ≤ ~50-page deck/reading askable within **~1–2 min**.
- **N7 · V3 · P1 — Cost control:** model routing across generation tiers keeps per-answer cost
  tracked/bounded. (At dogfood scale cost is trivial — cents; this matters at deploy scale.)

### Security & privacy
- **N8 · V3 · P0** — Corpus isolation enforced at the DB layer (RLS); absent/failing policy must
  **deny**, not leak. Gets an explicit adversarial check (cross-user read attempt that fails closed)
  before the V3 gate.
- **N9 · V2 · P1** — Uploaded files stored privately (Supabase Storage, per-user access); no public
  URLs.
- **N13 · V1 · P1 — Corpus content is data, never instruction.** Uploaded materials are *untrusted
  input*: a slide or PDF can contain text shaped like a command, and the Grounder feeds retrieved
  chunks straight into the prompt. Retrieved text must be delimited and labeled as **quoted source
  material**, and the system prompt must state that anything inside a source block is never an
  instruction. This bites at **V1, not at deploy** — it is an answer-trust property, and trust is
  the product. Complements ADR 0015: that validates what the model *returns*; this constrains what
  it is *given*. (At dogfood scale the only uploader is the dogfooder, hence P1 — escalates to P0
  at V2, when someone else can upload.)
- **N14 · V2 · P0 — Provider credentials are server-side only.** At deploy the API key moves from a
  local gitignored `.env` to server-side secret storage. No browser code path ever holds it, and
  rotation is a config change, not a code change.
- **N15 · V2 · P0 — The ask path is not an open-ended way to spend money.** Deployed, both `ask` and
  ingest cost real API spend per call — and auth does not arrive until V3 (F13), so **V2 is
  deployed without a user gate by design**. Needs a per-caller rate limit plus a hard account-level
  spend ceiling; exceeding either **fails closed with a clear message**, never degrades silently.
  (Distinct from N7, which is cost *efficiency* via model routing — this is cost *abuse*.)

### UX
- **N10 · V1 · P0** — Low-friction upload; clear "ready to ask" signal (ties to F3).
- **N11 · V1 · P0** — Citations render **cleanly and legibly** inline — they're the trust surface.
- **N12 · V2 · P1** — Mobile-first PWA usable on a phone.

---

## Constraints
- **C1** — Stack: React PWA, FastAPI (Python), Supabase (Postgres + pgvector + Auth + Storage),
  **hand-rolled** RAG core (ADR 0003), **provider-agnostic** model layer (ADR 0004).
- **C2** — Anthropic ships **no native embeddings** → dedicated embeddings model required (ADR 0005).
- **C3** — **API-first from v1** (ADR 0004); dogfooding cost is negligible (cents–single-digit $).
- **C4** — Vector store is **pgvector** throughout (local Postgres in V1 → Supabase in V2) so
  deploying doesn't rewrite the data layer. Embedding-model changes force a full re-index (ADR 0005).
- **C5** — v1 runs on **real course materials** the dogfooder owns — no synthetic-only validation.

---

## How we'll know it's working (rollup)
- **V1 done** = the grounded core runs locally end-to-end: upload → ingest → **cited answer** →
  **refuses** when the corpus doesn't cover it. Demoable, not yet measured.
- **V2 done** = the same, deployed and phone-usable.
- **V3 done (the real quality gate)** = on the real corpus, **N1 recall@k ≥ 0.85, N2 faithfulness
  ≥ 0.90, N3 refusal ≥ 0.90 / false-refusal ≤ 0.10, N4 citations ≥ 0.95**, plus F13/N8 isolation
  proven by an adversarial cross-user read that fails closed.

---

## Open items to resolve in Architecture / the retrieval spike
Deliberately **not** locked — several are empirical:
- **Final embeddings model** — `3-small` default (ADR 0005); bake off vs. `voyage-3` in the spike.
- **Chunking strategy** — biggest lever on N1/N2; tuned against the eval set, not guessed.
  *Contract now locked (ADR 0019: provenance-carrying, self-contained, never-span page/slide → scalar
  `page_or_slide`); only the strategy / size / overlap stays empirical.*
- **k (top-k)** and the relevance-threshold *value* (N3). *Placement now decided (ADR 0008): a
  per-chunk relevance filter feeding the Grounder, calibrated at V3; only the value stays empirical.*
- **Parsing tooling** — pypdf / python-pptx / `unstructured`. *Component design (ingestion-worker
  spec) deferred this to the **spike** — tooling stays empirical, not locked on paper.*
- **Faithfulness scoring method** — human vs. LLM-judge vs. both — decided when the harness is specced.
- **Generation model/tier** — OpenAI GPT default (ADR 0007); pick the tier at Architecture and
  bake off vs. Claude in the spike (faithful refusal + citation formatting on the real corpus).

*(These flow into ADRs as decided.)*
