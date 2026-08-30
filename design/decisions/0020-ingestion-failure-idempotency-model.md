# 0020. Ingestion failure & idempotency — retryable/terminal split, all-or-nothing replace, index-write-only transaction

- **Date:** 2026-07-04
- **Status:** accepted — **publication claim amended 2026-07-27 by ADR 0025** (§2–3's "commits its
  entire chunk set" holds only if the caller's connection is not already in a transaction; psycopg
  turns a nested `conn.transaction()` into a SAVEPOINT that commits nothing. The atomicity and
  index-write-only reasoning below stand unchanged — and ADR 0025's own status line carries what
  later became of that precondition); §1's **classification of a DB blip as
  transient amended, and its dangling "ADR 0011 budget" reference resolved, 2026-08-29 by
  ADR 0028** (a DB blip takes the crash path in V1 — nothing classifies psycopg errors, and a
  handler wide enough to catch one absorbs every programming error with it; the budget ADR 0011
  never held is now four ratified numbers. §1's terminal/bad-input half, §2 and §3 stand
  unchanged); §1's **terminal/bad-input set extended 2026-08-30 by ADR 0029** (it enumerated three
  kinds of bad input — corrupt, password-protected, unsupported/zero-text — and now carries a
  fourth, `too_long`: input past a configured word ceiling, refused before the embed call. The
  terminal *handling* below is unchanged, which is why the new reason needed no worker change; §1's
  transient half, §2 and §3 stand unchanged)

## Context
`architecture.md` locked the async substrate (ADR 0011: DB-backed `jobs` + in-process poll worker,
at-least-once) and the `queued→processing→ready/failed` lifecycle (F3), but **explicitly deferred**
the substance to component design: *"the `failed` status implies real handling — API rate-limits/
timeouts, partial-index recovery, idempotent retry."* This ADR settles that, for the ingestion
worker whose pipeline is `parse → chunk → embed → index`.

Three things are tangled in "failed handling": (1) not every failure should retry; (2) a job that
dies mid-index must not leave a half-indexed file — which is a **correctness hazard for the
differentiator**, since the Retriever would read an incomplete corpus and the Grounder would ground/
refuse against it silently; (3) an at-least-once worker (ADR 0011) can deliver the same job twice, so
processing must be safe to repeat.

## Decision

### 1. Failure taxonomy — retryable vs terminal
`failed` carries a **reason kind**; the two classes are handled differently:
- **Transient / retryable** — embedding-provider 429 / timeout / transient 5xx, DB blip. Retry with
  backoff up to the ADR 0011 budget; land on `failed(reason=transient_exhausted)` **only when the
  budget is spent.**
- **Terminal / bad-input** — corrupt or password-protected file, unsupported format, zero extractable
  text. Go **straight to `failed`, skipping the retry budget**, with an *actionable* reason surfaced
  to the user.

This is the **write-side mirror of ADR 0016**: there, infra ERROR ≠ corpus judgment; here, transient
infra ≠ bad input — we neither retry terminal failures nor present them identically.

### 2. Partial-index recovery — all-or-nothing transactional replace
Processing a file is **atomic**: it commits the file's *entire* new chunk set or **none** of it. A
failed job commits **zero** chunks; retry reprocesses from scratch. Idempotency is therefore free —
"process file X" always delete-then-replaces X's chunk set, which is what makes the at-least-once
worker safe against double-delivery **without any dedup logic**, and makes a crash-mid-`processing`
job safe for a lease/reaper (ADR 0011) to reclaim → `queued` with **zero cleanup** (it committed
nothing).

### 3. Transaction boundary — wrap the index write, NOT the pipeline
The slow, external, failure-prone work runs with **no transaction open**; the transaction is a short,
local, atomic swap once a *complete, valid* row set is in hand:
```
# NO transaction — failure here leaves the DB untouched; "rollback" = abandon the in-memory buffer
parse → chunk (never-span, ADR 0019) → embed all chunks → prepare rows
        (parse fail → terminal; embed 429/timeout → transient retry)

BEGIN                                             # opens only now, with the full row set ready
  DELETE FROM chunks WHERE file_id = :file_id     # old set (re-index case)
  INSERT <full new chunk set>                      # embeddings + (file, page_or_slide) + embedding_model_id
  UPDATE files SET status = 'ready' WHERE file_id = :file_id
COMMIT
```
- The transaction **never spans the embedding-API calls** — holding a connection/locks across ~N
  network round-trips invites idle-in-transaction timeouts and couples DB health to provider latency.
- Because no DB write happens until the row set is complete, a failure during parse/chunk/embed has
  **nothing to roll back**.
- The `DELETE`+`INSERT` swap is atomic under READ COMMITTED: readers see the **old full set** until
  COMMIT, then the **new full set** — never empty, never partial (protects re-index of a `ready` file).
- **`files.status='ready'` flips inside the same transaction** as the chunk insert (across tables):
  `status=ready` ⟺ `chunks committed`, one atomic fact. Status **is** the publish signal, so it must
  commit with what it publishes.

## Alternatives considered
- **Resumable / checkpointed indexing** (skip already-embedded chunks on retry) — rejected for V1: a
  file sits half-present between attempts, so the Retriever reads an incomplete corpus and the
  Grounder grounds/refuses against it silently — spends the trust promise to save re-embedding cost.
  Cost objection (all-or-nothing re-embeds succeeded chunks on retry) is a V1 non-issue (retries are
  the exception; class files are lecture-sized); checkpointed resume is a clean V2 turn-on (ADR 0004
  ladder) if cost data ever justifies it.
- **Transaction around the whole pipeline** (BEGIN → parse/chunk/embed → COMMIT) — rejected: holds a
  connection and locks across all embedding calls; idle-in-transaction timeouts; DB coupled to
  provider latency. The transaction should wrap the *write*, not the work.
- **Set `status='ready'` in a separate write after inserting chunks** — rejected: opens a window where
  status and chunk presence disagree (ready-but-empty or chunks-but-not-ready). Same transaction closes it.
- **One undifferentiated `failed` status** — rejected: retries futile terminal failures (wasted
  budget) and denies the user an actionable reason. The reason kind is cheap and load-bearing.

## Consequences
- Feeds `components/ingestion-worker.md`: the failure taxonomy, the atomic-replace invariant, and the
  index-write-only transaction boundary are locked spec.
- **Invariant — no partial index is ever visible.** A file's chunks are always all-from-one-successful
  -run or absent; `status=ready` ⟺ its full chunk set is queryable.
- **Idempotent by construction** — reprocessing a file fully replaces its chunk set; safe under
  at-least-once delivery and lease/reaper reclaim (ADR 0011), no dedup keys.
- Terminal reasons need a small enumerated set surfaced through `GET /files/:id` (F3 status) so the
  React SPA can show an actionable message — a thin extension of the status surface (ADR 0012).
- V2 turn-ons left clean: checkpointed resume (cost), richer terminal-reason taxonomy, broker-based
  retry (ADR 0011). None reshape this contract.
