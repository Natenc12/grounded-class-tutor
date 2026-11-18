# 0011. Async substrate — DB-backed job/status + in-process worker (V1)

- **Date:** 2026-07-04
- **Status:** accepted

## Context
ADR 0006 locked async ingestion; this ADR settles *what runs it*. Open decision ② framed the axis:
a DB-backed job/status table driven by an in-process worker vs. a real broker (arq / RQ / Celery,
which pull in Redis). The deciding lens is forward-compat to V2, not raw throughput — V1 corpus and
traffic are tiny, and ingestion latency is dominated by parse + embed API calls, not by queue
mechanics.

## Decision
**V1 uses a DB-backed job/status table in Postgres, claimed by an in-process poll worker**
(`FOR UPDATE SKIP LOCKED`), **behind an explicit enqueue / claim boundary** so the substrate can be
swapped for a broker at V2 without reshaping the write path.

- **Status *is* job state.** F3's `queued→processing→ready/failed` must be queryable by the API for
  `GET /files/:id`. That belongs in the DB anyway — a broker's queue is opaque to that read, so a
  broker would force a *second* source of truth. One store, in Postgres, is simpler and already owned.
- **No second stateful infra.** A broker adds Redis to a V1 walking skeleton for zero V1 payoff;
  ADR 0006 already deferred Redis. Keep V1 to one datastore.
- **The seam that matters is `enqueue(job)` / `claim()→job`.** Name it as an interface. Swapping the
  DB-poll worker for arq-on-Redis at V2 is then a substrate swap behind a stable boundary — the same
  discipline as the provider layer (ADR 0004). The write path does not change.

## Alternatives considered
- **Broker now (arq / RQ / Celery + Redis)** — rejected for V1: adds a second stateful component and
  a second source of job state, for throughput V1 doesn't need. Reconsider at V2 only if traffic or a
  *separate* Redis need (caching, rate-limit buckets) actually materializes.
- **DB-poll worker with no named boundary** — rejected: works, but bakes the substrate into the write
  path and makes the V2 broker swap a rewrite instead of a config change. The interface is cheap
  insurance and costs nothing at V1.

## Addendum (2026-07-04, PM-3) — worker process topology
"In-process" here means *no separate broker/queue infra* — **not** "inside the API's event loop." V1
runs the poll worker as a **separate OS process** (`python worker.py`) against the same Postgres
(`FOR UPDATE SKIP LOCKED` on `jobs`). Rationale: parse/embed are slow **blocking** calls; running them
as an asyncio task in the uvicorn process would starve request handling (status polls, asks hang). A
separate process can't block the API, keeps the single-datastore property (both talk to one Postgres),
and the `enqueue`/`claim` boundary is unchanged — V2's broker swap still drops in behind it. The reaper
runs on the worker's poll loop. (A background thread is an acceptable fallback but has no upside here.)

## Consequences
- V1 has one datastore (Postgres) carrying both vectors and job state; the "job queue + status store"
  box is a `jobs`/`files.status` table, not a broker.
- Phase 3 must specify the `enqueue` / `claim` interface and the poll worker's claim semantics
  (`SKIP LOCKED`, visibility timeout / lease, idempotent retry — ties to the worker failure/retry
  invariant in `architecture.md`).
- Reinforces ADR 0006 (async ingestion) and mirrors ADR 0004's provider-boundary discipline.
