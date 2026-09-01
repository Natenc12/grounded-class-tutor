# 0030. The publish is guarded by entitlement, not repaired afterwards — a caller-supplied predicate crosses the PM-4 seam

- **Date:** 2026-09-01
- **Status:** accepted — amends ADR 0020 (§2–3's atomic index write now admits a caller-supplied
  **veto** evaluated inside that transaction, and the PM-4 seam is restated as a rule about
  *knowledge* rather than about parameters: the ingest pipeline may accept a predicate it cannot
  interpret, and still may not write a job-layer column. §1's retryable/terminal split is
  untouched — a refused publish is neither, and buys no retry. Everything ADR 0025 and ADR 0027
  say about the connection precondition stands unchanged; the veto runs inside the same
  transaction those two exist to guarantee is real.)

## Context

`files.status='ready'` and a non-null `files.failed_reason` must not coexist. The reason is the
actionable message F3 puts in front of a student, so a queryable file wearing one contradicts the
answers it is already grounding. `components/ingestion-worker.md` carries the invariant.

Three interleavings reach the forbidden state. ADR 0011's queue is at-least-once, so a worker that
stalls past its lease is reaped, its job re-handed out, and it keeps running — two workers, one
file. `_bury`'s docstring partitions the three by where the bury falls relative to the publisher's
claim. #24 closed bury-then-claim: the later claim's `processing` write clears the reason on the
way past. #86 closed claim-then-bury where the burier had **lost** its lease. The third — the
burier **holds** a live lease, so #86's guard passes exactly as designed, and the *reaped* worker
is the one that publishes — was left open as #92, and named as an open residual in five places
rather than asserted away.

It was reachable on `main` and demonstrated by execution, driven entirely through real ticks with
nothing injected but the lease expiry:

```
3. B's tick burys (lease LIVE, #86 guard passes)     files='failed'   failed_reason='transient_exhausted'
4. A's tick finishes ingest -> index_file publishes  files='ready'    failed_reason='transient_exhausted'
```

The asymmetry underneath is small and total: **`index_file` was the only write to `files` that read
no lease.** `claim`'s `processing` write, `_bury`'s `failed` write, `complete`, `fail` and `release`
all check ownership. The publish did not, so the one worker with no right to write was the one
nothing stopped.

## Decision

**`index_file` takes an optional `publish_guard: Callable[[Connection], bool]`, calls it once inside
its transaction before any write, and raises `PublishRefused` — rolling the transaction back — on a
False answer.** `worker.process_one` supplies the predicate; it is `_settle`'s two ownership
conditions verbatim (`state='processing'` AND our `lease_token`) plus `FOR UPDATE`.

Three things make it work, and each is load-bearing:

1. **Inside the transaction, on that connection.** The check and the writes are one atomic unit.
   Evaluated on any other connection the answer would be true outside the transaction and
   unenforceable inside it.
2. **`FOR UPDATE`, or this narrows the race instead of closing it.** Guard-then-publish is a
   check-then-act across two statements, and under READ COMMITTED the publish takes a fresh
   snapshot — so the reaper, a second claim and a bury landing in that gap reproduce #92 in a
   window microseconds wide. The row lock is held to COMMIT, and the lease can only move through
   `reclaim_expired`, an UPDATE of that row, which then blocks. `_bury` solves the identical
   problem by folding its check into its UPDATE's own WHERE; that is unavailable here, because the
   write lives in `index.py` and giving that statement a `jobs` conjunct is the seam crossing §3
   declines.
3. **The predicate is opaque.** `index.py` never learns that jobs, leases or workers exist.

**The PM-4 seam is hereby a rule about knowledge, not about parameters.** ADR 0020 drew it to keep
the ingest pipeline pure of job/queue machinery so Slice 2 could wrap it rather than rewrite it.
A predicate the pipeline cannot interpret does not teach it anything, so it does not breach the
seam; a `failed_reason` write would. This is the distinction §3 turns on and it is the whole
amendment.

## Alternatives considered

- **Clear `failed_reason` in `index_file`'s upsert.** One line; measured, it turns both
  reproductions green and the suite to 545 passed / 1 failed —
  `test_index_file_does_not_clear_failed_reason`, which exists to pin exactly this. **Declined on
  three counts.** It makes the publisher a second writer for a column owned by the worker's settle
  path, which is the seam crossing itself rather than a permitted form of it. It repairs the
  symptom downstream of the asymmetry instead of removing it — the publish would still be the one
  write to `files` that does not check ownership, and the next column added to that row inherits
  the same hole. And its better-looking student outcome is a coincidence of timing, not a
  guarantee: it publishes `('ready', None)` only when the reaped worker happens to finish, and
  leaves `jobs='failed'` beside `files='ready'`, two tables disagreeing about one file.
- **A lease check in the worker immediately before calling `ingest_file`.** Cheap and useless: the
  window this defect lives in is the parse/chunk/embed work itself, which runs *after* such a check.
- **Accept and document.** The state was already documented as NOT YET HELD at five sites, honestly.
  Declined because the path is reachable in production and the invariant is a trust claim, which is
  the product.
- **Make `_bury`'s two writes one transaction** so the bury is atomic across `files` and `jobs`.
  Rejected for the reason `_bury`'s docstring already gives — the write order was chosen for what a
  crash *between* them leaves behind, and only files-then-jobs self-heals. It would also close the
  lock-ordering cycle this ADR's design currently avoids.

## Consequences

- The invariant now **holds for the worker path**, and `components/ingestion-worker.md` says so
  rather than NOT YET HELD.
- **The guard is opt-in, and that bound is real.** A caller passing no `publish_guard` is unguarded
  by construction. Correct for Slice 1's direct callers — they hold no lease and race no bury — and
  a trap for any future caller that acquires one. `worker.process_one` is the only guarded caller
  today. Whoever adds the second should not need to rediscover this: it is stated at `index_file`'s
  step 0 as well as here.
- **`reclaim_expired`'s contract changed and its docstring was corrected in the same diff.** A
  duplicate run used to be *absorbed* by the all-or-nothing replace; for a guarded caller it is now
  *discarded*. Absorption was only ever harmless for the chunk set — the publish also flips
  `files.status`, which is how the forbidden state was reached.
- **A lock-ordering cycle is now one refactor away.** The guard takes `jobs` then `files`; `_bury`
  writes `files` then `jobs`. There is no deadlock today only because `_bury`'s two writes are
  separate transactions. Anyone merging them reintroduces the cycle; both docstrings say so.
- **What this does NOT fix, and it is the thing a student actually feels:** a lease expiry burns a
  retry attempt identically to a genuine transient failure (`claim` bumps `attempts`,
  `reclaim_expired` deliberately does not reset it). A file that is merely slow can therefore
  exhaust its budget and be reported `failed` when nothing about it is wrong. This ADR makes that
  outcome *consistent* rather than a coin-flip on timing; it does not make it *correct*. Tracked
  separately — the naive fix (skip the bump on a reclaim) reintroduces the poison-file loop
  `reclaim_expired`'s docstring exists to prevent, so it needs a decision of its own.
