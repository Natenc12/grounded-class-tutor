# 0010. V1 file-staging (local) vs. the V2 Object Storage component

- **Date:** 2026-07-04
- **Status:** accepted

## Context
Whether raw uploaded files need to persist in V1 was challenged directly. The obvious justification —
"keep files so an embedding-model swap can re-index from source" (C4) — **does not bite in V1**,
because the embedding model is locked for V1. That would suggest no storage in V1. But a stronger,
unavoidable reason was under-weighted.

## Decision
**V1 needs a humble file-staging location (a local directory) — not the full Object Storage
component.** The driver is a decision already locked: **async ingestion (ADR 0006)** separates the
API (producer) from the worker (consumer) in time. The API returns fast; the worker processes later.
The bytes therefore **must be durably staged** for the decoupled worker to pick up — an in-memory
handoff is impossible when producer and consumer aren't the same request. (A ≤50-page deck also
can't block the upload HTTP call while it embeds — N6.)

- **V1:** a local staging directory. Bonus: staged files let ingestion be re-run while **tuning
  chunking in spikes** without re-uploading.
- **V2:** graduates to the real **Object Storage component** — Supabase Storage, private per-user
  access, serving files to the client, delete (F4, F9, N9). All of that is V2 scope.

## Alternatives considered
- **Full Object Storage component in V1** — rejected: over-scoped; private-access, client-serving,
  and delete are all V2 concerns. V1 needs staging, not a storage subsystem.
- **No storage / synchronous ingestion** — rejected: ADR 0006 locked async ingestion, which *forces*
  durable staging; and a long embed can't block the upload request (N6).
- **Delete-after-ingest in V1** — rejected as the default: keeping staged files is free locally and
  saves re-uploads while tuning chunking in spikes.

## Consequences
- The V1 architecture box is **"file staging (local dir)"**, graduating to **"Object Storage"** at V2.
- Reinforces ADR 0006 — async ingestion is precisely what makes even V1 need durable staging.
