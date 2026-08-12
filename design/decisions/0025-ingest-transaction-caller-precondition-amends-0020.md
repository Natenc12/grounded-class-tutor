# 0025. The index transaction's atomicity is conditional on the caller's connection — amending ADR 0020

- **Date:** 2026-07-27
- **Status:** accepted — amends **ADR 0020** (its unconditional publication claim; the
  index-write-only boundary and the all-or-nothing rationale stand unchanged); its
  "documented, not enforced" decision and the *Guard inside `index_file`* rejection amended
  2026-08-11 by **ADR 0027** (writers now refuse a non-`IDLE` connection via
  `gct.db.require_idle`, and the `seed_class` counter-example no longer holds — ADR 0027
  §Context). The precondition itself, the autocommit remedy, and the atomicity-vs-publication
  distinction stand.

## Context
ADR 0020 §2–3 state the ingest guarantee without qualification: processing a file "commits its
*entire* new chunk set or **none** of it", and `files.status='ready'` flips "inside the same
transaction as the chunk insert" so that `status=ready ⟺ chunks committed` is "one atomic fact".
`index_file` implements that with `with conn.transaction():` around the delete/insert/publish.

Building the Slice 1 exit runner (#8) surfaced that **`with conn.transaction()` does not always
open a transaction.** psycopg3 opens an *implicit* transaction on a connection's first statement,
and `Connection.transaction()` checks the connection's state: if it is already `INTRANS`, the block
issues a **SAVEPOINT** instead of `BEGIN`, and releasing that savepoint commits nothing. Measured
directly against `grounded_class_tutor`:

```
after SELECT, A status: TransactionStatus.INTRANS
after `with A.transaction()`, A status: TransactionStatus.INTRANS
rows visible from a SEPARATE connection: 0 -> NOT committed (savepoint only)
rows surviving a rollback of the outer txn: 0
```

`scripts/ask_smoke.py` hit this for real. `_resolve_class` runs a `SELECT` before any ingest, so
every subsequent `ingest_file` on that connection wrote into a savepoint. Nothing was durable until
the enclosing `with connect() as conn:` block exited cleanly — and `Connection.__exit__` rolls back
on exception. Measured: three files parsed, chunked, embedded and reported "ingested", then a
`SetupError` propagated and **zero rows survived**. The embedding spend was paid and discarded.

**What is and is not broken.** Per-file *atomicity* survives — a savepoint rollback still discards
exactly its own writes, so a failed file never leaves a partial chunk set, and `status=ready` still
moves with the chunks it publishes. What does not survive is **publication**: at the moment
`ingest_file` returns, the work may be invisible to every other connection and destructible by an
unrelated later failure. ADR 0020 treats "committed" and "returned successfully" as the same event.
They are not, and nothing in the code can tell them apart from the inside.

**Why this needs writing down rather than just fixing.** ADR 0020 exists to serve the PM-4 seam:
Slice 2 wraps this pipeline in job/queue machinery (ADR 0011's DB-backed `jobs` + lease/reaper). The
natural implementation of a worker is *lease a job* — a `SELECT ... FOR UPDATE` or an `UPDATE` to
`processing` — *and then ingest on the same connection*. That sequence reproduces this bug exactly,
and it does so silently. Worse, it corrupts the reasoning ADR 0020 uses to make the reaper safe: the
ADR argues a crash-mid-`processing` job needs "**zero cleanup** (it committed nothing)". Under
savepoint nesting a *successful* ingest has also committed nothing until the worker's outer
transaction commits — so "succeeded" and "crashed" become indistinguishable by database state, which
is the one signal the reaper reads.

## Decision
**The index transaction's publication guarantee is a precondition on the caller, stated as part of
the contract: `ingest_file`/`index_file` must receive a connection that is not already inside a
transaction** — either freshly connected, or `autocommit = True`, or explicitly committed since its
last statement.

`scripts/ask_smoke.py` satisfies it with `conn.autocommit = True` at wiring. Under autocommit the
implicit transaction never opens, so `index_file`'s block is the real top-level `BEGIN`/`COMMIT` ADR
0020 §3 describes, and per-file publication is restored.

The precondition is **documented and asserted by the callers, not enforced inside `index_file`.**

## Alternatives considered
- **Guard inside `index_file`** (raise if the connection is already `INTRANS`) — the most attractive
  option, and it would have caught this on the first run. Rejected on blast radius: the guard's
  correctness depends on *statement order within the caller*, not on caller intent, so it fires on
  benign code. The test suite's own `seed_class` fixture calls `index_file` directly on the shared
  `db` connection, and whether that connection is `IDLE` or `INTRANS` at that moment depends on
  whether the test happened to run a statement first. A guard that a test passes or fails based on
  an unrelated earlier `SELECT` teaches nothing and would be silenced rather than heeded.
- **`index_file` commits for itself** (`conn.commit()` after the block) — rejected: it seizes
  transaction control from the caller, which is exactly what the injected-`conn` design (ADR 0009's
  callable-library shape, and the PM-4 seam) exists to leave with the caller. It would also make
  `index_file` uncomposable with any caller that legitimately wants to sequence several writes.
- **`index_file` opens its own connection** — rejected outright: it breaks `conn` injection, so the
  ingest pipeline could no longer participate in a caller's unit of work at all, and the offline
  test path would lose the shared-connection teardown the `db` fixture depends on.
- **A trailing `conn.commit()` after each `ingest_file` in the runner** (the first fix measured, and
  a correct one) — rejected in favour of `autocommit` at wiring. It patches one call site and leaves
  the mechanism live for the next writer who forgets one; autocommit removes the implicit
  transaction entirely. It also does not address the second symptom below.

## Consequences
- ADR 0020's status line points here; its §2–3 publication claims should be read as holding **given
  the precondition above**. Its atomicity and index-write-only reasoning are unchanged.
- `components/ingestion-worker.md` and any Slice 2 worker must state how the lease and the ingest
  are separated. **The lease transaction must commit before ingest begins** — which ADR 0020 §3
  already implies for a different reason ("the transaction never spans the embedding-API calls"),
  and which this ADR makes the *same* rule rather than a coincidence. A worker that holds its lease
  transaction across the embed calls violates both at once.
- **A second symptom, same mechanism, fixed by the same change.** Before the fix, `ask_smoke` also
  held an `idle-in-transaction` connection across the entire question loop: `_print_census`'s
  `SELECT` reopened the implicit transaction and nothing closed it, so the connection stayed open
  through every paid embed and generation round-trip. That is precisely the hazard ADR 0020 §3 names
  in bold on the write path, reproduced on the read path. Autocommit leaves the connection `IDLE`
  between statements.
- **This class of bug is invisible to the existing tests, by construction.** Savepoint writes are
  fully visible to the connection that made them, so any test asserting through the same `conn`
  passes either way. Catching it requires a *second* connection, or an unclean exit — the two things
  a single-connection test does not do. `tests/scripts/test_ask_smoke.py` inherits this limitation
  and does not pin the durability property; it was verified by probe instead (recorded above).
- Nothing about the ADR 0020 retryable/terminal split, the delete-then-replace idempotency, or the
  `status=ready` publish semantics changes.
