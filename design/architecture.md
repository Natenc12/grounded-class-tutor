# Architecture — Grounded Class Tutor

**Phase:** 2 — Architecture  **Rigor:** Deep (ADR 0001)
**Delivery model:** the maturity ladder (ADR 0004) — this map is the **V1 walking skeleton**, shaped
so V2/V3 *turn things on* rather than reshape them.

> Convention: this doc names the shape and the tradeoffs; where a choice is locked it **points to an
> ADR** rather than restating it (Phase-4 convention).

## Shape in one paragraph
A **callable RAG core** (library) does the work; the **API is a thin HTTP adapter** over it, and the
V3 eval harness + spike scripts are *other* thin callers of the same core. The core splits into a
**Retriever** (query → ranked chunks + similarity scores) and a **Grounder** (chunks → answer / cite
/ refuse). Ingestion is **async** (ADR 0006): uploads are staged, a **worker** parses → chunks →
embeds → indexes off the request thread, with a job/status store driving `queued→…→ready/failed`.
Both embeddings (index- and query-time) and generation sit behind **provider interfaces** (ADR 0004).
Data is **Postgres + pgvector** throughout (ADR 0006), `owner_id` on every row.

## Component map (V1 skeleton, forward-shaped for V2/V3)
```
CALLERS (thin):   React SPA   ·   [V3: eval harness]   ·   [spikes: scripts]
                         └──────────── all drive ↓ ──────────────┘
                    ┌──────────────────────────────┐
                    │  CORE (callable library)     │   API = thin HTTP adapter over this
                    └──┬────────────────────────┬──┘
         write path    │                        │   read path
        ┌──────────────▼───┐          ┌─────────▼──────────────────┐
        │ JOB QUEUE +      │          │ RETRIEVER                  │
        │ STATUS STORE     │          │ embed-q → pgvector top-k   │
        │ queued→…→ready/  │          │ [owner+class filter] →     │
        │ failed (retry)   │          │ ranked chunks + scores     │
        └────────┬─────────┘          └────────────┬───────────────┘
        ┌────────▼──────────┐               score seam (ADR 0008)
        │ INGESTION WORKER  │              ┌────────▼───────────────┐
        │ ①PARSE(meta born) │              │ GROUNDER               │
        │ →CHUNK →EMBED     │              │ ②build labeled context │
        │ →INDEX            │              │ →generate →③resolve    │
        └───┬───────────┬───┘              │ labels → cite / REFUSE │
            │           │                  └───────────┬────────────┘
    ┌───────▼──────┐ ┌──▼───────────────────────────────▼───────────┐
    │ FILE STAGING │ │ MODEL PROVIDER LAYER                          │
    │ (local dir;  │ │ Embeddings(iface) · Generation(iface)         │
    │  →Obj Store  │ │ ⚠ index & query embeddings = SAME model/ver   │
    │  in V2)      │ └───────────────────────┬───────────────────────┘
    └──────────────┘ ┌───────────────────────▼──────────────────────┐
                     │ DATA: Postgres + pgvector                     │
                     │ classes · files(status) · chunks(vec+meta)    │
                     │ owner_id on every row                         │
                     └───────────────────────────────────────────────┘
```

## Components & responsibilities
- **Callers (thin)** — React SPA (V1; minimal, scoped to the five P0 surfaces — ADR 0012), plus the
  V3 eval harness and spike scripts as *peer* callers. None own logic; they drive the core.
- **Core (callable library)** — the RAG pipeline as a library, so HTTP is not the only way in
  (eval harness + spikes need programmatic access). See ADR 0009 for why the query entry point stays
  history-ready.
- **API (FastAPI, thin adapter)** — `POST /classes`, `POST /files`, `GET /files/:id` (status),
  `POST /ask`. Translates HTTP ⇄ core calls; no business logic.
- **Job queue + status store** — decouples upload from processing; owns the `queued→processing→
  ready/failed` state machine (F3) and worker retry. **V1 substrate = DB-backed `jobs` table +
  in-process poll worker behind an `enqueue`/`claim` boundary (ADR 0011);** broker is a V2 swap.
- **Ingestion worker** — parse → chunk → embed → index. **Parse is where source metadata (page/
  slide) is born** — the start of the citation spine. Chunking is the biggest quality lever (→ spikes).
- **Retriever** — embed query → pgvector top-k over *this owner's* chunks **within one class**
  (`owner_id AND class_id`, F6) → ranked chunks **with similarity scores** (always emitted, even
  though V1 doesn't gate on them — ADR 0008; normalized similarity per ADR 0017).
- **Grounder** — build labeled context → generate → resolve labels back to citations → **partial-
  support answer / flag / refuse** (ADR 0008). The refusal decision lives here.
- **Model provider layer** — thin `Embeddings` + `Generation` interfaces (ADR 0004); OpenAI default
  (ADR 0005 / 0007), swappable. Grounding logic lives *above* it — swapping providers never changes
  product behavior.
- **File staging (V1) → Object Storage (V2)** — durable landing for uploads so the async worker can
  read them; graduates to a real storage component at V2 (ADR 0010).
- **Data layer** — Postgres + pgvector (local V1 → Supabase V2, ADR 0006). `classes`, `files`
  (with status), `chunks` (embedding + file/page/slide metadata). `owner_id` on every row.

## Data flows
**Ingest (write):** `upload → stage bytes + create file(status=queued), return fast → worker: parse
→ chunk → embed → index → status=ready|failed`. Async is the point (ADR 0006).

**Ask (read):** `question → Retriever: embed-q → pgvector top-k (owner_id + class_id filter) → ranked chunks+
scores → Grounder: labeled context → generate → resolve citations → partial-support answer / flag /
refuse`.

## The citation spine (the trust surface)
One metadata contract, honored at **three points**: ① **born** in PARSE (page/slide) → ② **rendered**
into labels (`[S1] file p.3`) in the Grounder's context builder → ③ **resolved** back into structured
citations after generation. The model only ever references labels we handed it; it never invents a
citation. Distinct failure modes at each point (dropped page number / cite to nonexistent `[S5]` /
no citation at all).

## Cross-cutting invariants
- **Embedding-model consistency** — index-time and query-time embeddings **must** use the identical
  model + version, or similarity search is meaningless. Owned by the provider-layer config.
- **Scope filter seam** — the retrieval query path carries `WHERE owner_id = … AND class_id = …`
  **from V1** (F6 class-scoping + F12 owner isolation), even with one hardcoded user, so V3 turns on
  RLS (F13/N8) as *enforcement* over the same seam, not a rewrite.
- **Worker failure/retry** — the `failed` status implies real handling: API rate-limits/timeouts,
  partial-index recovery, idempotent retry. *(Resolved in component design — ADR 0020 +
  `components/ingestion-worker.md`: retryable/terminal split, all-or-nothing atomic replace,
  index-write-only transaction.)*

## Key decisions (pointers)
- Retriever/Grounder split · callable core · citation spine · file-staging box — **this doc**.
- Refusal seam (partial-support, laddered) — **ADR 0008**.
- V1 single-shot; multi-turn additive — **ADR 0009**.
- File-staging V1 vs. Object Storage V2 — **ADR 0010**.
- Async substrate: DB-backed job/status + in-process poll worker, broker deferred to V2 — **ADR 0011**.
- V1 client: minimal React SPA, scoped, off critical path — **ADR 0012**.
- Generation interface thin/text-parse V1; structured-output + provider-lock on a pre-registered V3
  trigger; embeddings stay agnostic — **ADR 0013**.
- Ingestion worker: chunking contract + never-span page/slide — **ADR 0019**; failure taxonomy +
  all-or-nothing atomic replace + index-write-only transaction — **ADR 0020**. Row shapes
  consolidated in **`data-model.md`**.
- Prior: hand-rolled RAG **0003** · provider-agnostic layer **0004** · embeddings **0005** · stack
  (async ingestion, container host, pgvector) **0006** · generation default **0007**.

## Open (Phase 2 continues)
- ~~**② Async substrate**~~ — **resolved: ADR 0011** (DB-backed job/status + in-process poll worker
  behind an `enqueue`/`claim` boundary; broker deferred to V2).
- ~~**③ V1 client thinness**~~ — **resolved: ADR 0012** (minimal React SPA, not PWA; scoped to the
  five P0 surfaces; off the critical path — harness alternative was redundant with the core's
  programmatic entry point).
- ~~**Provider-interface exact shape**~~ — **resolved: ADR 0013.** Thin: `Embeddings.embed(texts)→
  vectors` (+ `model_id`/`dim`), `Generation.generate(messages)→text`; Grounder owns prompt + citation
  parsing; text-parse V1, structured-output/lock deferred to V3 trigger; embeddings stay agnostic.

**All Phase-2 open decisions resolved → Phase 2 ready to close.** Next: Phase 3 — Component Design,
triaged by criticality (Grounder + citation spine + Retriever first).

## Empirical → spikes (not architecture)
Chunking strategy, top-k, threshold value, parsing tooling (pypdf/python-pptx/unstructured),
faithfulness scoring method. The V3 lock trigger (ADR 0013) also depends on the eval set + harness.
Sequenced in `roadmap.md` (Phase 5): spikes run **two-pass** — validate then tune (ADR 0022) — and
the **eval set is a V1 artifact** (`eval/questions.jsonl`, ADR 0021 / F15), not V3-only; the *automated
harness* over it is the V3 turn-on. The eval runner is a peer caller of the core (as diagrammed above).
