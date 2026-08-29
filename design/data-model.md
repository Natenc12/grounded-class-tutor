# Data Model — Grounded Class Tutor

**Phase:** 3 — Component Design (consolidation)  **Rigor:** Deep (ADR 0001)

> Single source of truth for the V1 row shapes, now stable across the three closed component specs.
> This doc **consolidates**, it does not decide — every shape here is already pinned by a spec/ADR;
> where a column exists *because* of a decision it **points to the ADR**. Governing: **0006**
> (Postgres+pgvector, local V1→Supabase V2), **0011** (jobs substrate), **0010** (file staging),
> **0019** (chunk contract), **0020** (atomic replace, status semantics), **0018/0005** (embedding
> model id), **0008/0017** (score computed, not stored). Requirements: **F3** (status), **F6**
> (class scope), **F12/N8** (owner isolation).

## Entities at a glance
```
classes ──1:N── files ──1:N── chunks          (owner_id on every row)
                  │
                  └──1:1── job                 (one ingestion job per file; ADR 0011 substrate)
```
Four tables. `owner_id` is on **every** row (F12/N8) — the isolation seam that ships as app-level
filtering in V1 and gets RLS *underneath it* at V3 (F13/N8), not a reshape.

## `classes`
The unit an ask is scoped to (F6). Minimal in V1.
| column | type | notes |
|---|---|---|
| `class_id` | uuid PK | |
| `owner_id` | id, **not null** | isolation seam (F12); one hardcoded user in V1 |
| `name` | text | display |
| `created_at` | timestamptz | |

## `files`
An uploaded source document + its **user-facing** processing status (F3). This status is the domain
truth `GET /files/:id` returns — distinct from the `jobs` row, which is the execution substrate.
| column | type | notes |
|---|---|---|
| `file_id` | uuid PK | |
| `owner_id` | id, **not null** | isolation (F12) |
| `class_id` | uuid FK → classes | scope (F6) |
| `filename` | text | original name (e.g. `lecture-3.pdf`); source of the chunk's citation label |
| `staging_ref` | text | pointer into file staging (local dir V1 → Object Storage V2, ADR 0010) |
| `status` | enum | `queued · processing · ready · failed` (F3, ADR 0011). **`ready` ⟺ full chunk set committed** (flipped inside the index transaction — ADR 0020, publication conditional per ADR 0025 on the caller not already being in a transaction) |
| `failed_reason` | enum \| null | populated on `failed`; **terminal** (`unparseable · protected · unsupported · empty`) vs **transient_exhausted** (ADR 0020); drives the actionable UI message |
| `created_at` / `updated_at` | timestamptz | |

## `chunks`
The queryable, provenance-carrying unit — written **only** by the ingestion worker via all-or-nothing
atomic replace (ADR 0020), read by the Retriever. This row is where the write path and read path meet.
| column | type | notes |
|---|---|---|
| `chunk_id` | uuid PK | the `S#→chunk_id` server-side map resolves to this (ADR 0015) |
| `file_id` | uuid FK → files | the atomic-replace key (`DELETE WHERE file_id`, ADR 0020) |
| `owner_id` | id, **not null** | isolation (F12); Retriever filters on it |
| `class_id` | uuid | scope (F6); Retriever filters `owner_id AND class_id` |
| `text` | text | chunk body; self-contained/embeddable (ADR 0019) |
| `file` | text | **citation filename**, carried on the chunk (honor-point ①) — see denormalization note |
| `page_or_slide` | int/text, **scalar** | provenance born at parse; **scalar by never-span** (ADR 0019) — the honesty guarantee |
| `embedding` | `vector(dim)` | pgvector; `dim` set by the active embedder (ADR 0005) |
| `embedding_model_id` | text | the ADR 0018 stamp — the Retriever **asserts** this == active embedder, fails loud |
| `created_at` | timestamptz | |

- **Not stored: `score`.** Similarity is **computed at query time** (`max(0.0, 1 - cosine_distance)`
  — ADR 0017, clamped per ADR 0024: pgvector's `<=>` returns distance in `[0, 2]`, so the unclamped
  form would yield `[-1, 1]` and break the `[0, 1]` range the seam promises); it is a per-query
  property, never a column.
- **Denormalization — `file` on the chunk.** The citation filename is copied onto every chunk (not
  joined from `files`) so citation render needs no join and provenance travels *with* the unit
  (honor-point ①; the Retriever SELECTs `file` directly). Safe because the worker is the **single
  writer** and atomic replace keeps it consistent; V1 has no file rename. Revisit only if rename lands.

## `jobs`
The **execution substrate** (ADR 0011) — one ingestion job per file. Distinct layer from
`files.status`: this row is the queue/lease/retry mechanism; the job's outcome *drives* `files.status`.
| column | type | notes |
|---|---|---|
| `job_id` | uuid PK | |
| `file_id` | uuid FK → files | the work unit |
| `owner_id` / `class_id` | id / uuid | carried for scope; written onto the chunks |
| `state` | enum | `queued · processing · done · failed` (queue-level; not the same axis as `files.status`) |
| `attempts` | int | retry count vs the ADR 0028 budget (transient only; terminal skips retries, ADR 0020). counts CLAIMS, so a crash costs one exactly as a caught 429 does - and the last claim on a doomed job exists only to bury it (ADR 0028 §1) |
| `leased_until` | timestamptz \| null | visibility timeout; a `processing` job past this is reclaimed → `queued` by the reaper (ADR 0011) |
| `last_error` | text \| null | diagnostics for the most recent failure |
| `lease_token` | uuid \| null | proof of holding, minted per `claim`; the settle verbs match on it so a worker whose lease was reclaimed cannot write over the run that now holds the job. Cleared by the reaper — the lease and its proof die together |
| `created_at` / `updated_at` | timestamptz | |

**Why two status axes (`files.status` vs `jobs.state`) are not redundant:** `files.status` is the
*published domain state* the user sees and the `ready ⟺ chunks committed` invariant guards;
`jobs.state` is the *at-least-once claim/lease/retry machinery*. Idempotent replace (ADR 0020) makes
redelivery and reaper reclaim safe, so the two can diverge transiently (a job re-`processing` while
the file is still `queued`) without corruption.

## Indexes (V1)
- `chunks.embedding` — pgvector ANN index (ivfflat/hnsw; parameters are tuning, not contract).
- `chunks (owner_id, class_id)` — the retrieval scope filter (F6/F12).
- `files (class_id)`, `jobs (state, leased_until)` — status listing + reaper scan.

## Cross-cutting invariants (consolidated)
- **`owner_id` on every row** — app-level filter V1 → RLS enforcement V3, same seam (F12/N8/F13).
- **Retrieval scope = `owner_id AND class_id`** — every read carries both (F6); absent scope never
  widens (Retriever invariant).
- **`page_or_slide` scalar everywhere** — never-span (ADR 0019); keeps the Grounder/Retriever/Citation
  contracts unreopened.
- **`ready ⟺ full chunk set committed`** — `files.status='ready'` flips inside the index transaction
  with the chunk insert (ADR 0020); no partial index is ever visible. Atomicity is unconditional;
  **publication is conditional per ADR 0025** on the caller not already being inside a transaction.
- **Embedding-model consistency** — `chunks.embedding_model_id` (write, ADR 0018) is asserted by the
  Retriever (read); index- and query-time embeddings are the identical model+version (ADR 0005).

## Open / deferred
- **`dim`** — concrete embedding dimension is set by the winning embedder (bake-off spike, ADR 0005/0021).
- **Corpus re-index (PM-5, carried)** — no whole-corpus re-embed operation in V1; the embedder bake-off
  (Pass 2) re-indexes via an ops script that re-enqueues every `file_id`. Changing the embedder without
  it leaves mixed `embedding_model_id` → Retriever ERRORs the class. Real re-index op = V2/V3 turn-on.
- **Multi-turn history store** — V1 is single-shot; the history table is additive (ADR 0009), not here.
- **Object Storage** — `staging_ref` abstracts local dir → Object Storage at V2 (ADR 0010).
- **Supabase migration + RLS** — local Postgres V1 → Supabase V2 (ADR 0006); RLS over `owner_id` V3.
- **ANN index tuning** — ivfflat vs hnsw + params are empirical (tied to corpus size / recall spike).
