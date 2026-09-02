# Component Spec — Ingestion Worker

**Phase:** 3 — Component Design  **Rigor:** Deep (ADR 0001)  **Criticality:** make-or-break (write path)

> Convention (Phase-4): names the shape + contract; where a choice is locked it **points to an ADR**
> rather than restating the rationale. Governing ADRs: **0011** (async substrate: DB-backed jobs +
> in-process poll worker, at-least-once, enqueue/claim boundary), **0010** (file staging V1),
> **0019** (chunking contract + never-span), **0020** (failure taxonomy, all-or-nothing replace,
> index-write-only transaction), **0018** (stamp `embedding_model_id`), **0005** (embeddings model +
> re-index-on-change), **0013** (thin `Embeddings` interface), **0006** (async ingestion / stack).
> Requirements: **F1** (upload), **F2** (source provenance), **F3** (status lifecycle), **N6**
> (throughput), **N10**. Completes the **citation spine at honor-point ①**.

## Responsibility
Turn *this owner's* uploaded file, within *one class*, into the queryable, provenance-carrying chunk
set the Retriever reads — off the request thread. It **parses** (where source metadata is born —
honor-point ①), **chunks** (never-span, ADR 0019), **embeds** (stamping `embedding_model_id`), and
**indexes** (all-or-nothing atomic replace), while driving the `queued→processing→ready/failed`
lifecycle (F3) with honest, differentiated failure handling (ADR 0020). It makes the read path
*possible*; **chunking quality is the biggest lever on N1/N2** and is **empirical** — settled by the
spike within the fixed contract, not here.

## Position & dependencies
- **Up — enqueue side (API adapter):** `POST /files` stages bytes to file staging (ADR 0010), creates
  `file(status=queued)`, and **enqueues** a job `{ file_id, owner_id, class_id }` across the ADR 0011
  `enqueue`/`claim` boundary. Returns fast; the worker runs async.
- **In — job queue (ADR 0011):** the worker **claims** a job (at-least-once; a lease/reaper reclaims a
  job stuck in `processing` past timeout — a **silent** worker's, since a live one renews its own
  lease while it works, ADR 0028, the lease's meaning amended per ADR 0031). *One lease past the
  claim* is therefore no longer the recovery timeout for every stall: a worker that is alive but
  making no progress goes silent only when its heartbeat hits the cap, so its job comes back at
  **cap + lease** (4500 s at the defaults) rather than at 900 s — the figures and the trade are
  ADR 0031 §5 and §Consequences. A worker that DIED is unchanged: an unwind hands the job back at
  once, a `SIGKILL` stops the beats with the process and the lease lapses on schedule. Processing is
  **idempotent** (ADR 0020), so redelivery and reclaim are safe with zero dedup logic.
- **Down — file staging (ADR 0010):** reads the staged bytes to parse.
- **Down — model provider layer:** calls `Embeddings.embed(texts) → vectors` (+ `model_id`, `dim`)
  (ADR 0013). The active embedder's `model_id` is **stamped onto every chunk** (`embedding_model_id`,
  ADR 0018) — the value the Retriever later asserts against.
- **Down — data layer:** writes `chunks` (embedding + `file`/`page_or_slide` + `owner_id`/`class_id` +
  `embedding_model_id`) and flips `files.status`.
- **Downstream consumer — Retriever:** reads exactly the chunks this box commits. An un-ingested/empty
  class (no committed chunks) is *why* retrieval returns `[]` (Retriever no-floor invariant).

## Interface / contract
Not a request/response function — a **job consumer** with a durable effect. The contract is the job
shape it claims, the row set it writes, and the status transitions it guarantees.
```
Job{ job_id, file_id, owner_id, class_id, attempts, staging_ref, lease_token }  # across the ADR 0011 boundary

Effect on commit (ready):
  chunks := [ Chunk{ chunk_id, file_id, owner_id, class_id,
                     text, file, page_or_slide,          # provenance — honor-point ①, scalar (ADR 0019)
                     embedding, embedding_model_id } ]    # ADR 0018 stamp
  files.status : queued → processing → ready | failed(reason)

Guarantee: a file's chunks are ALL-from-one-successful-run or ABSENT (ADR 0020).
           status = ready  ⟺  the full chunk set is committed & queryable
                              (publication conditional per ADR 0025 — see Invariants).
```
The written `Chunk` shape is exactly what the Retriever selects and hands up — the write path and read
path meet on this row. `page_or_slide` is **scalar** by the never-span rule (ADR 0019).

**The `Job` shape carries four fields beyond the identity triple, and `staging_ref` is the
load-bearing one.** `job_id` and `attempts` are the queue handle and the retry trail the queue module
owns. `lease_token` is the worker's proof that it still holds the lease it was handed: `claim` mints a
fresh uuid per claim, and `complete`/`fail`/`release` match on it, so a worker whose lease expired and
whose job was re-handed out is **refused** rather than allowed to write over the run that now holds it.
The state alone could not say that — `processing` is equally true of a job re-handed to someone else,
which let a stalled worker's `release` return a job another worker was still embedding to `queued`
(migrations/0002_lease_token.sql carries the sequence). `staging_ref` is here because of step 1's
ADR 0025 precondition, not for convenience: a worker
that had to look the path up would run a statement *after* its claim committed, and a bare `SELECT`
reopens psycopg's implicit transaction exactly as a write does — putting the connection back `INTRANS`
and degrading step 6's transaction to a SAVEPOINT that publishes nothing. Handing the path over at
claim time removes the worker's *reason* to touch the database between claim and ingest, which is a
stronger guarantee than a rule telling it not to. (The Down-dependency on file staging below is
unchanged: the worker still *reads the bytes* itself. What it no longer does is ask the database
where they are.)

## Internal approach (pipeline)
Slow/external work runs with **no transaction open**; the transaction is a short local swap once a
*complete, valid* row set is in hand (ADR 0020).

> **The claim transaction must COMMIT before step 2 begins (ADR 0025, guarded per ADR 0027).**
> Leasing the job is a write, so it opens a transaction on the worker's connection; carrying that
> transaction into the pipeline used to degrade step 6's `conn.transaction()` to a mere SAVEPOINT
> that published nothing, silently. The index write now REFUSES that call outright
> (`gct.db.require_idle` raises before anything is written), so the failure is loud on the first
> ingest instead of silent forever — but the worker's obligation is unchanged: claim, commit, then
> process on a connection that is not in a transaction, which also keeps the connection from being
> held open across every embedding round-trip (the hazard the paragraph above already forbids).
> Slice 1's runner does this with `conn.autocommit = True`; a worker may instead commit the claim
> explicitly.
>
> Why the rule is sharper than tidiness: ADR 0020 makes the reaper safe by arguing a crash-mid-
> `processing` job "committed nothing" — under savepoint nesting a *successful* run had also
> committed nothing, so success and crash stopped being distinguishable by DB state, the only
> signal the reaper reads. The guard is what makes that state unreachable rather than merely
> forbidden.

1. **Claim** the job (ADR 0011); set `status=processing`, **and commit it** — see the note above.
2. **Parse** staged bytes → text + structure (pypdf / python-pptx / `unstructured` — tooling is a
   spike). **Source metadata (file, page/slide) is born here** — honor-point ① (F2). Unparseable /
   password-protected / zero-text → **terminal failure** (ADR 0020), no retry.
   - **Input ceiling** — the parsed word count is checked against `MAX_INGEST_WORDS` here, one step
     before the only paid call in the pipeline; over it → **terminal failure**
     `too_long`, no retry, nothing embedded (ADR 0020, terminal set extended per ADR 0029). A
     precondition on the pure pipeline, not queue machinery — the PM-4 seam holds.
3. **Chunk** under the fixed contract (ADR 0019): each chunk carries `(file, page_or_slide)`, is
   self-contained/embeddable, maps to **exactly one** page/slide (**never spans**). *Strategy /
   size / overlap = spike.*
4. **Embed** all chunks via `Embeddings.embed` (ADR 0013); attach vectors + stamp `embedding_model_id`
   (ADR 0018). Provider 429 / timeout / transient 5xx → **transient failure** → retry w/ backoff
   (budget, backoff + lease: **ADR 0028**). *No DB write yet — a failure here leaves the DB
   untouched.*
   - **Sub-batching is the adapter's job, not the worker's (PM-1 / correctness floor).** A real deck can
     produce more chunks than the provider's per-request cap (OpenAI: 2048 inputs / ~300k tokens); a single
     over-cap `embed()` call is a **hard failure**, not a throughput issue. The `Embeddings` adapter
     therefore splits `texts` into provider-legal sub-batches **below** the interface — the worker's
     "embed all" stays honest, the `embed(texts)→vectors` contract is unchanged (ADR 0013). Batch *sizing*
     for throughput (N6) remains tuning; staying under the cap is contract.
5. **Prepare rows** in memory — the full chunk set (text + provenance + embedding + model id + scope).
6. **Index (atomic swap)** — short transaction. An **empty row set never reaches here**: the index
   write rejects a zero-chunk call outright (see §Failure modes), so `ready` is never published
   over nothing.
   ```
   BEGIN
     UPSERT files ... SET status = 'ready' WHERE file_id = :file_id  -- publish signal, same tx
     DELETE FROM chunks WHERE file_id = :file_id                     -- old set (re-index case)
     INSERT <full new chunk set>
   COMMIT
   ```
   Readers see old-full → new-full, never empty/partial (READ COMMITTED). `status=ready` commits
   **with** the chunks it publishes.
   The **files row is written first, and that order is FK-forced** — `chunks.file_id` references
   `files(file_id)`, so the row must exist before the chunk insert. Slice 1's `index_file` upserts it
   (`INSERT ... ON CONFLICT (file_id) DO UPDATE`) because no API adapter has created it yet; once the
   Slice 2 worker claims a job, the row already exists as `queued` and the upsert lands on UPDATE —
   wrap, not rewrite (PM-4 seam).

## Failure modes
| mode | class | V1 behavior |
|---|---|---|
| **Unparseable / password-protected / unsupported / zero-text file** | terminal | → `failed(reason)` immediately, **skip retry**; actionable reason surfaced via `GET /files/:id` (ADR 0020) |
| **Input past the configured word ceiling** | terminal | → `failed(reason=too_long)` immediately, **skip retry**; refused BEFORE the embed call, so nothing is bought and nothing is written. Counted in words, not bytes/pages/chunks (ADR 0020, terminal set extended per ADR 0029) |
| **Embedding provider 429 / timeout / transient 5xx** | transient | retry w/ backoff up to budget → else `failed(reason=transient_exhausted)`. DB untouched until success (ADR 0020 §1, budget numbers per ADR 0028) |
| **DB connection error mid-job** | infra | propagates uncaught — the worker crashes. Nothing classifies psycopg errors, and a handler wide enough to catch a blip absorbs every programming error with it, putting a wrong `failed_reason` in front of a student. The run committed nothing, the lease expires, the reaper requeues, and the `attempts` budget bounds the loop (ADR 0020 §1, DB-blip class amended per ADR 0028) |
| **Ingest slower than its lease** | normal | **not a failure** — the worker renews its own lease from a separate thread on a separate connection while it works, so a slow file reaches `ready` on attempt 1 and the reaper never sees it. Before this it was reaped mid-flight, its publish refused, its work discarded and one `attempts` spent per overrun — a file always slower than its lease was buried as `transient_exhausted` having never failed (ADR 0028, the lease's meaning amended per ADR 0031). The beats stop at a bounded cap, so a **wedged** worker still returns to the reaper and to the `attempts` budget — later than before, and by a stated amount: **cap + lease = 4500 s** per attempt rather than one 900 s lease, so a reliably-wedging file is buried in ~6 h 15 m instead of ~75 min (ADR 0031 §Consequences) |
| **Worker crash mid-`processing`** | infra | committed nothing (tx not reached); job → `queued`, **zero cleanup** (ADR 0011/0020). Which writer requeues it turns on whether the stack unwound: any death that raises — Ctrl-C, the SIGTERM `scripts/worker.py` routes onto the same unwind, an unclassified exception — is handed back by the worker's own shutdown release and is claimable **at once**; `SIGKILL`/OOM/power loss run no handler, so those wait out the lease and the reaper collects them (ADR 0028 §Consequences, shutdown-release bullet) |
| **Duplicate job delivery** (at-least-once) | infra | safe **for the chunk set** — idempotent replace re-does the same delete-then-insert; no dedup needed (ADR 0020). A redelivered run is now **discarded rather than absorbed**: the publish asks the worker's lease predicate first and is refused if the job moved on, so a reaped worker cannot republish `ready` over a live-leased bury's reason (#92, ADR 0030; the invariant below, and `reclaim_expired`'s corrected docstring) |
| **Re-index of an already-`ready` file** | normal | old full set stays queryable until COMMIT, then swapped atomically — never flickers empty/partial |
| **Partial embed then failure** | transient | nothing committed; retry reprocesses from scratch (accepted re-embed cost, ADR 0020) |
| **Index write handed a connection already inside a transaction** | programming error | the index write **raises `RuntimeError` before opening the transaction** — nothing is written. The alternative was a SAVEPOINT that publishes nothing while reporting success (ADR 0025, guarded per ADR 0027); the error names the remedy (autocommit at wiring, or commit first) |
| **Zero-chunk row set handed to the index write** | programming error | the index write **raises `ValueError` before opening the transaction** — nothing is written, not even the `files` row. Publishing `ready` over zero chunks would break `status=ready` ⟺ full chunk set queryable. Unreachable through the pipeline (parse fails an empty file first), but the index write is a public entry point and does not trust its caller (#23) |

## Invariants

These are what the box is BUILT to hold, and all of them are enforced today. Any that is not
must be marked **NOT YET HELD** inline, with the issue that owns it — an invariant this section
silently over-claimed would be worse than one it admits to. None carries that mark at present;
the last to (#92) was closed by ADR 0030, and its entry below records the bound that closure
does and does not reach.

- **Provenance is born at parse and never lost** — every chunk carries `(file, page_or_slide)`;
  honor-point ① of the citation spine (F2, ADR 0019).
- **`page_or_slide` is scalar** — never-span guarantees exactly-honest citation provenance with zero
  ripple into the closed Grounder/Retriever/Citation specs (ADR 0019).
- **No partial index is ever visible** — a file's chunks are all-from-one-successful-run or absent;
  `status=ready` ⟺ full chunk set committed & queryable (ADR 0020, publication conditional per
  ADR 0025). Held from **both** ends inside the box: a write that fails partway rolls back whole
  (regression-tested on the re-index and first-index paths, #23), and a zero-chunk write is refused
  before the transaction opens rather than published as `ready`. **Atomicity is unconditional;
  PUBLICATION is not** — it needs a caller whose connection is not already inside a transaction,
  and the box now ENFORCES that: `index_file` refuses a non-IDLE connection before writing anything
  (ADR 0025, guarded per ADR 0027). §Internal approach step 1 states the precondition and what it
  costs a worker to get wrong.
- **A `ready` file carries no failure reason** — held for the worker path, and **the bound is
  part of the invariant, not a footnote to it** (ADR 0030). `status='ready'` and a non-null
  `failed_reason` must not coexist. The reason is the actionable message F3 puts in front of a
  student (ADR 0020 §1), so a queryable file wearing one contradicts the answers it is already
  grounding — the same trust cost as a partial index, reached through the status column instead of
  the corpus. **All three of its interleavings are now closed.** At-least-once redelivery reaches
  the row from more than one direction (ADR 0011), and the three are partitioned by where the bury
  falls relative to the PUBLISHER's claim — the bury is the only writer that sets the reason, the
  claim's `processing` write the only one that clears it. Either the publisher claimed *after* the
  bury, and its clear removes the reason on the way past (**#24**); or it was already past its
  claim, and then the burier either **lost** its lease, which the terminal write refuses on the
  same guard the job settle verbs use (**#86**), or still **held** it — in which case the PUBLISH
  is the write with no entitlement, and it is now refused too (**#92**). The third was reachable
  and demonstrated by execution until ADR 0030; #86 neither introduced it nor widened it.
  **What closed it was symmetry, not repair.** `index_file` was the only write to `files` that
  read no lease, so it took a caller-supplied predicate it evaluates inside its own transaction —
  `_settle`'s two ownership conditions plus `FOR UPDATE`, supplied by `process_one`. A reaped
  worker's publish raises `PublishRefused` and writes nothing; its finished work is thrown away.
  Clearing `failed_reason` in the publish was the alternative and ADR 0030 §3 records why it lost.
  **THE BOUND: the predicate is opt-in.** A caller that passes none is unguarded by construction —
  correct for Slice 1's direct callers, which hold no lease and race no bury, and a trap for any
  future caller that acquires one. `process_one` is the only guarded caller today, so this
  invariant is a claim about the WORKER path and not about `index_file` in isolation.
- **Idempotent by construction** — reprocessing a file fully replaces its chunk set; safe under
  at-least-once delivery + lease/reaper reclaim, no dedup keys (ADR 0011/0020).
- **Transaction wraps the write, not the work** — never held across embedding-API calls (ADR 0020).
- **`embedding_model_id` stamped at index time** — the value the Retriever asserts against; index- and
  query-time embeddings must be the identical model+version (ADR 0018/0005).
- **Scope on every row** — `owner_id` and `class_id` written on every chunk (F6/F12; the Retriever's
  isolation filter reads these).

## The spec / spike line (what this box does *not* pin here)
- **SPEC (locked):** chunk **contract** (provenance-carrying, self-contained, single-page, never-span);
  `embedding_model_id` stamp; failure taxonomy; all-or-nothing atomic replace; lifecycle + retry.
- **SPIKE (empirical, eval-set-tuned — ADR 0021 design-level):** chunking **strategy** (boundary
  method / size / overlap) — the biggest N1/N2 lever; **parsing tooling** (pypdf / python-pptx /
  `unstructured`); the **embeddings bake-off** (3-small vs voyage-3). The spec fixes the *contract*
  these operate within; they cannot change the shape the rest of the system reads.

## Open / deferred (out of this spec)
- **Chunking strategy / size / overlap** — spike; the single biggest lever on N1/N2 recall.
- **Parsing tooling** — partly empirical (format coverage, metadata fidelity) → spike.
  *Known Pass-2 input:* scanned / OCR-noisy PDFs (dogfood corpus — Long, Livingston) parse to garbled
  text under pypdf; the concrete representative case that makes the pypdf-vs-`unstructured` bake-off
  (roadmap Spike Pass 2) decide something. Note the boundary: *re-OCR* of scanned PDFs is the deferred
  photo/OCR subsystem (ADR 0002, V4) — out of scope here; this is extraction-quality on an allowed
  input, not the capture subsystem.
- **Terminal-reason taxonomy** — V1 ships a small enumerated set (unparseable / protected / unsupported
  / empty / too_long); richer taxonomy is a clean V2 extension (ADR 0020, terminal set extended per
  ADR 0029).
- **Throughput / batching (N6)** — embed-call batch *sizing* + worker concurrency are tuning, not
  contract; revisit with real corpus sizes. (Staying *under* the provider cap is contract, owned by the
  adapter — see pipeline §4 / PM-1.) Broker-based queue is the V2 substrate swap (ADR 0011).
- **Checkpointed resume** — V2 turn-on if retry re-embed cost is ever shown to hurt (ADR 0020/0004).
- **Eval hooks** — emit parse/chunk counts + failure/retry telemetry for the spike + N1 measurement
  (flag at Phase 5 — Roadmap).
