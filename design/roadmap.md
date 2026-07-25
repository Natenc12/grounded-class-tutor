# Roadmap — Grounded Class Tutor

**Phase:** 5 — Roadmap  **Rigor:** Deep (ADR 0001)
**Delivery model:** the maturity ladder (ADR 0004). This roadmap sequences the **V1 walking skeleton**
and the **spike slate** that tunes it. V2/V3 are *turn-ons* the architecture already shaped — listed
here for continuity, not detailed.

> Convention (Phase-4): names the sequence and points to the ADR/spec that owns each contract; does
> not restate them.

## The core sequencing insight
The core is **callable/library-first** (ADR 0009/0012), so the grounding loop can be proven
programmatically before any HTTP or UI exists. Two facts drive the order:
1. **Value-first vertical slice.** The differentiator (grounding quality, N1–N4) is *empirical and
   unproven*. Get to a cited-answer/refusal ASAP; defer robustness machinery.
2. **The build gates the spikes, not the reverse.** No spike blocks a slice — every slice builds on a
   provisional default. The tracer bullet (Slice 1) is the **bench** the spikes run on, and the eval
   artifact (ADR 0021 / F15) is what makes their outputs *rankable*.

---

## V1 build — the walking skeleton

### Slice 0 — Foundation *(blocks everything)*
- **Build:** schema migration for `classes · files · chunks · jobs` (from `data-model.md`); provider
  layer — `Embeddings.embed()` + `Generation.generate()` interfaces (ADR 0004/0013) with OpenAI
  defaults (3-small / GPT).
- **Provisional defaults set here:** embedder = `text-embedding-3-small`, **`dim=1536`** (the one
  schema-coupled spike variable — see spike slate).
- **Invariant wired in:** index-time = query-time embedding model/version (ADR 0018).
- **Exit:** schema applies clean; a smoke call round-trips an embedding and a generation through the
  interfaces.

### Slice 1 — Tracer bullet: the grounding loop, synchronous *(the differentiator, ASAP)*
- **Build:** ingest **one** file *inline* — parse → chunk → embed → index, **no queue** — then
  Retriever → Grounder → **cited answer / refusal**, driven by a script.
  - **Seam to draw now (PM-4):** build the ingest steps as a **pure pipeline** (parse→chunk→embed→
    prepare-rows→index-tx) *separate* from any job/status/lease machinery. Slice 1 calls it directly and
    creates a minimal `files` row that goes straight to `ready` inside the index tx; **Slice 2 wraps this
    exact pipeline** in the worker shell (claim/lease/retry). Bake status/claim *into* the pipeline and
    Slice 2 becomes a rewrite instead of a wrap.
  - Parse (metadata born, honor-point ①) · chunk (contract ADR 0019, never-span) · Retriever
    (`owner_id AND class_id`, ranked chunks + scores, ADR 0017) · Grounder (labeled context →
    generate → resolve citations → partial-support/refuse, ADR 0008 / 0014–0016). The labeling step
    is also where **N13** lands — source blocks are quoted material, never instructions.
- **Ships with it — `eval/questions.jsonl` (ADR 0021 / F15):** the tracer needs a bench; the bench
  needs the tracer. Seed **~12-question smoke suite** (`suites:["smoke"]`), in-corpus + out-of-corpus.
- **Provisional defaults:** parser pypdf + python-pptx · chunking fixed-size + overlap · k=5 · GPT.
- **Exit (the key V1 milestone):** programmatic `ask(class, question)` → a **cited answer** for an
  in-corpus question and a **refusal** for an out-of-corpus one, demonstrated over the smoke suite.
  *This is the walking skeleton's spine and the reason-to-exist, proven.*

### Spike Pass 1 — validation, not optimization *(ADR 0022; immediately after Slice 1)*
- On the tracer + seed smoke suite, run **chunking + generation only**. Goal: confirm the
  differentiator *actually works* — grounds and refuses correctly — **before** investing in Slices 2–4.
- **Exit:** the smoke suite passes by eyeball (in-corpus → cited answer, out-of-corpus → refusal). A
  red result here is a cheap design signal, caught before the skeleton is thickened.

### Slice 2 — Real write path *(robustness, additive — not a rewrite)*
> Slices 2–4 build on the **validated** core (ADR 0022).
- **Build:** wrap the *proven* inline pipeline in the async worker + job queue + status store
  (DB-backed `jobs`, in-process poll worker, `enqueue`/`claim`, ADR 0011) + failure/idempotency
  (retryable/terminal split, all-or-nothing atomic replace, index-write-only transaction, ADR 0020).
- **Exit:** upload → `queued→processing→ready/failed` (F3) is real; at-least-once + reaper safe; no
  partial index ever visible.

### Slice 3 — API adapter *(thin)*
- **Build:** FastAPI over the core — `POST /classes`, `POST /files` (stage + enqueue), `GET /files/:id`
  (status incl. terminal reason), `POST /ask`. Local file staging (ADR 0010). No business logic.
- **Exit:** the full loop is drivable over HTTP; status surface exposes actionable terminal reasons.

### Slice 4 — Client *(minimal)*
- **Build:** minimal React SPA — the five P0 surfaces (ADR 0012): create class, upload, ingest status,
  ask, view cited answer. Clean inline citation rendering (N11, the trust surface).
- **Exit — V1 done:** upload → ingest → **cited answer** → **refuses** end-to-end in the UI. Demoable,
  not yet measured (ADR 0004).

### Spike Pass 2 — tuning *(ADR 0022; after Slice 4 + dogfooding on a larger corpus)*
- Full bake-off slate, now that the corpus is **representative enough** to justify tuning decisions.
  Scores against the grown `eval/questions.jsonl`.
- **Exit:** provisional defaults replaced by evidence-backed choices (chunking, k, embedder, generator).
- **Open Assumption (PM-5) — corpus re-index is an ops script, not a feature.** The embeddings bake-off
  changes the active embedder, which requires re-embedding **every** file (C4; the worker only
  re-indexes *per file* on re-delivery). V1 does this via a script that re-enqueues all `file_id`s under
  the new embedder. **Foot-gun:** changing the embedder *without* running it leaves a class with mixed
  `embedding_model_id`, and the Retriever's consistency guard then ERRORs the whole class (retriever
  §Failure). Acceptable for a single dogfooder who knows the rule; a real "re-index corpus" operation is
  a clean V2/V3 turn-on.

---

## The spike slate — runs *on* the tracer, ranked *by* the eval artifact, in two passes
None blocks the build; each tunes a provisional default. All score against `eval/questions.jsonl`.
Sequencing is two-pass — **validate then tune** (ADR 0022).

| Spike | Tunes | Provisional → decide by | Pass |
|---|---|---|---|
| **Chunking strategy/size/overlap** (biggest N1/N2 lever) | Slice 1 chunk step | fixed-size+overlap → best on suite | **1** (validate) + **2** (tune) |
| **Generation bake-off** GPT vs Claude | Slice 1 Grounder | GPT → winner (faithful refusal + citation formatting) | **1** (validate) + **2** (tune) |
| **top-k (`k`)** | Slice 1 Retriever | k=5 → tuned | **2** |
| **Parsing tooling** pypdf/python-pptx vs unstructured | Slice 1 parse | pypdf+python-pptx → winner | **2** |
| **Embeddings bake-off** 3-small vs voyage-3 | `dim` (Slice 0) + retrieval | 3-small/1536 → winner (re-index if voyage, C4) | **2** |
| **Relevance threshold τ** | Retriever score seam (ADR 0008/0017) | *deferred to V3* — seam exists, value empirical | V3 |

**Prompt content** (Grounder wording, coverage-marker syntax, few-shot) is empirical, tuned on the
tracer against the smoke suite — spec fixes the *shape* (ADR 0014–0016), not the wording.

---

## Bridge to V2 / V3 (turn-ons, already shaped)
- **V2 — deploy:** Postgres→Supabase (C4, no data-layer rewrite), file staging→Object Storage
  (ADR 0010), broker swap for the job queue (ADR 0011), PWA. Latency/throughput bars (N5/N6).
  Deploy is also where the exposure requirements turn on: secrets server-side (N14) and rate limit +
  spend ceiling on the ask path (N15) — V2 is reachable *before* auth lands in V3, by design.
- **V3 — evaluate + accounts (the real quality gate):** formal **eval harness = automated scoring over
  the *same* `eval/questions.jsonl`** (ADR 0021) — N1 recall@k/precision@k (Retriever telemetry),
  N2 faithfulness, N3 refusal calibration, N4 citation correctness; auth + RLS on (F13/N8, enforcement
  over the V1 scope seam, not a reshape); relevance-filter τ calibration; the generation
  structured-output/provider-lock trigger (ADR 0013, pre-registered ≥10% quality / ≥30% cost).

## V1 "done" (from requirements rollup)
The grounded core runs locally end-to-end: upload → ingest → **cited answer** → **refuses** when the
corpus doesn't cover it, demonstrated over the seed smoke suite. Demoable, not yet measured.
