-- Slice 0 — Foundation schema.  Source of truth: design/data-model.md.
-- Four tables: classes · files · chunks · jobs.  owner_id on EVERY row (F12/N8 isolation seam).
-- Written to be idempotent (IF NOT EXISTS) so re-running migrate.py is safe.

create extension if not exists vector;    -- pgvector: the `vector` column type + ANN search
create extension if not exists pgcrypto;  -- gen_random_uuid()

-- classes — the unit an ask is scoped to (F6). Minimal in V1.
create table if not exists classes (
    class_id    uuid primary key default gen_random_uuid(),
    owner_id    text not null,                 -- isolation seam (F12); one hardcoded user in V1
    name        text not null,
    created_at  timestamptz not null default now()
);

-- files — an uploaded source document + its user-facing processing status (F3).
-- files.status is the DOMAIN truth GET /files/:id returns — distinct from jobs.state below.
create table if not exists files (
    file_id        uuid primary key default gen_random_uuid(),
    owner_id       text not null,                                  -- isolation (F12)
    class_id       uuid not null references classes(class_id),     -- scope (F6)
    filename       text not null,                                  -- source of the chunk's citation label
    staging_ref    text,                                           -- pointer into file staging (ADR 0010)
    status         text not null default 'queued'
                       check (status in ('queued','processing','ready','failed')),
    -- populated on `failed`; terminal reasons vs transient_exhausted (ADR 0020); drives the UI message
    failed_reason  text
                       check (failed_reason in
                           ('unparseable','protected','unsupported','empty','transient_exhausted')),
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

-- chunks — the queryable, provenance-carrying unit; written ONLY by the ingestion worker via
-- all-or-nothing atomic replace (ADR 0020), read by the Retriever. Where write path meets read path.
create table if not exists chunks (
    chunk_id            uuid primary key default gen_random_uuid(),
    file_id             uuid not null references files(file_id),  -- atomic-replace key (ADR 0020)
    owner_id            text not null,                            -- isolation; Retriever filters on it
    class_id            uuid not null,                            -- scope; Retriever filters owner_id AND class_id
    text                text not null,                            -- self-contained / embeddable (ADR 0019)
    file                text not null,                            -- citation filename, denormalized on the chunk (honor-point ①)
    page_or_slide       text not null,                            -- scalar provenance, never-span (ADR 0019) — the honesty guarantee
    embedding           vector(1536) not null,                    -- dim coupled to ACTIVE_EMBEDDING_MODEL_ID (config.py)
    embedding_model_id  text not null,                            -- the ADR 0018 stamp; Retriever asserts this == active embedder
    created_at          timestamptz not null default now()
);

-- jobs — the execution substrate (ADR 0011); one ingestion job per file.
-- Distinct axis from files.status: this is the at-least-once claim/lease/retry machinery.
-- Present from Slice 0 per the roadmap, but UNUSED until Slice 2 wraps the pipeline in the worker.
create table if not exists jobs (
    job_id        uuid primary key default gen_random_uuid(),
    file_id       uuid not null references files(file_id),
    owner_id      text not null,
    class_id      uuid not null,
    state         text not null default 'queued'
                      check (state in ('queued','processing','done','failed')),
    attempts      int not null default 0,             -- retry count vs the ADR 0011 budget
    leased_until  timestamptz,                         -- visibility timeout; reaper reclaims past this
    last_error    text,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

-- Indexes (data-model.md §Indexes).
create index if not exists chunks_owner_class_idx on chunks (owner_id, class_id);  -- retrieval scope (F6/F12)
create index if not exists files_class_idx        on files  (class_id);            -- status listing
create index if not exists jobs_state_lease_idx   on jobs   (state, leased_until); -- reaper scan
-- pgvector ANN index over the embedding (ivfflat/hnsw + params are tuning, not contract).
create index if not exists chunks_embedding_idx
    on chunks using hnsw (embedding vector_cosine_ops);
