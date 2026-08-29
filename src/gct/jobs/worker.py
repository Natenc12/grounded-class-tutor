"""Worker - polls `jobs`, claims one, runs the unchanged `ingest_file`, drives `files.status`.

The other half of the split `queue.py` declares in its first lines: that module RECORDS job
state, this one DECIDES it. The poll loop, the retryable/terminal split (issue #71 PR 2), and
every `files.status` transition after `queued` live here.

Status ownership - who writes which `files.status` transition (issue #71):

    | transition                | writer                                                     |
    |---------------------------|------------------------------------------------------------|
    | -> queued                 | `enqueue` (#70)                                            |
    | -> processing             | THIS module, on claim - the first-ever writer              |
    | -> ready                  | `index_file`, inside the index transaction (ADR 0020 §3)   |
    | -> failed + failed_reason | THIS module - terminal input, or budget exhausted          |

`jobs.state` is the OTHER axis - claim/lease/retry machinery - and `queue.py` owns every
statement that touches that table.

The retryable/terminal split (ADR 0020 §1) is this module's, and it is a two-line rule:
`ParseError` is bad INPUT - straight to `failed(reason)`, retry budget untouched, because a
corrupt file is exactly as corrupt on the next attempt. `TransientEmbeddingError` is bad LUCK -
back off, hand the job back to `queued` via `release`, and let a later claim retry it. Anything
else propagates and crashes the worker, deliberately: an unclassified exception is a bug we do
not want swallowed into a `failed_reason` that names the wrong cause, and the durable budget
below terminates the resulting crash loop anyway.

WHY THE BUDGET IS COUNTED IN THE DATABASE, not in a `for` loop around the ingest. `jobs.attempts`
survives the worker process; a loop counter does not. The failure that most needs bounding is the
one where the worker DIES mid-job - a crash, an OOM, a `kill` - and no in-process handler ever
runs. Those attempts still have to count, or a poison file is re-handed out forever by the reaper
(`claim`: "a poison file then retries forever, its budget never spent"; `reclaim_expired`:
"`attempts` is deliberately NOT reset - it is the retry trail #71 compares against its budget").
One durable counter bounds transient failures and crash loops with one rule.

WHY THE CHECK IS HERE AND NOT INSIDE `claim`. An over-budget job still has to be CLAIMED to be
buried: burying it means writing `files.status = failed` + `failed_reason`, a table `queue.py` is
not allowed to touch, so a `claim` that refused to hand the job over would leave the student with
a file stuck mid-flight and no reason for it. It is also policy, which that module declares it
does not own; and `claim` is the seam a V2 broker swaps in behind (ADR 0011), where the universal
shape is exactly this - the substrate hands you a delivery count, the consumer decides
dead-lettering. `claim` returning the post-bump `attempts` is what makes that split work.

Runs as a separate OS process via `scripts/worker.py`: ADR 0011's addendum is explicit that
"in-process" means no broker, NOT inside an API event loop; the script is a thin peer caller
over this module (ADR 0009).

Connection contract (ADR 0025, guarded per ADR 0027): every entry point here needs a
connection that is idle between statements. Every writer on the worker path commits
itself inside its own `conn.transaction()` (`claim`/`complete` in queue.py, the status
write here, `index_file`), so a plain connection works end to end. `scripts/worker.py`
still sets `conn.autocommit = True` at wiring as defense in depth: it protects the next
statement someone adds OUTSIDE a transaction block from silently opening the implicit
transaction this module no longer contains.
"""

from __future__ import annotations

import logging
import time

import psycopg

from gct.ingest.parse import ParseError
from gct.ingest.pipeline import ingest_file
from gct.jobs.queue import Job, claim, complete, fail, release
from gct.providers.base import Embeddings, TransientEmbeddingError

logger = logging.getLogger(__name__)

# Provisional numbers (issue #71) - all four of them live here now. The four-numbers ADR
# (lease / retry budget / backoff / poll) lands in PR 3, ratifies or changes these, and repoints
# ingestion-worker.md §4's dangling "ADR 0011 budget" citation at itself. Nothing has MEASURED
# any of them: they are chosen to be safe-if-wrong in the direction that costs least, and the
# per-constant comments say which direction that is.
# The lease errs LONG on purpose: too short reclaims a job a healthy worker is still ingesting
# and pays the embedding bill twice; too long only delays reclaim after a genuine crash.
DEFAULT_LEASE_SECONDS = 15 * 60
DEFAULT_POLL_SECONDS = 2.0
# How many times a job may be CLAIMED before it is buried as `transient_exhausted`. Counted in
# `jobs.attempts`, so a crash costs an attempt exactly as a caught 429 does - see the module
# docstring for why that is the point rather than an approximation.
DEFAULT_MAX_ATTEMPTS = 5
# Backoff between attempts: doubling, and capped. The cap is what keeps the delay a delay - an
# uncapped curve reaches hours by the fifth attempt for a provider blip that cleared in seconds.
# Both sit far under DEFAULT_LEASE_SECONDS on purpose: the worker serves the backoff while still
# HOLDING the lease (see `process_one`), so a backoff that could outlast the lease would let the
# reaper hand the job to someone else mid-wait, which is the double-embed ADR 0011's lease number
# exists to prevent.
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0


def backoff_seconds(attempts: int) -> float:
    """Delay before the attempt AFTER `attempts` - exponential, capped (ADR 0020).

    `attempts` is the post-bump value `claim` returns, so the first failure (attempts=1) waits
    `BACKOFF_BASE_SECONDS` and each later one doubles until the cap. Retrying a provider that
    just said "slow down" with no delay at all is the one answer ADR 0020 calls genuinely wrong;
    the curve is the cheapest thing that is not that.

    No jitter, deliberately. Jitter exists to de-synchronise a fleet of clients retrying in
    lockstep; V1 is ONE in-process poll worker (ADR 0011) with nothing to de-synchronise from,
    so it would add a moving part with no failure to prevent. Revisit with worker concurrency
    (`ingestion-worker.md` §Open/deferred), not before.
    """
    return min(BACKOFF_BASE_SECONDS * 2 ** (attempts - 1), BACKOFF_MAX_SECONDS)


def _bury(conn: psycopg.Connection, job: Job, *, reason: str, error: str) -> None:
    """Terminal failure on BOTH axes: `files.status='failed'` + `failed_reason`, then `jobs`.

    The two writes are one event and always travel together, which is why they are one
    function rather than two calls a future handler could get half-right.

    `reason` goes into a CHECK-constrained column of five values (migrations/0001_init.sql:28)
    and is passed through UNTRANSLATED from `ParseError.reason`, which is drawn from the same
    taxonomy for exactly this reason (ADR 0020; `parse.py`'s docstring promises the
    pass-through). `error` is the free-text `jobs.last_error` - the diagnostic detail the
    closed set cannot carry.

    THE FILES ROW GOES FIRST, and the order is chosen for what a crash between the two writes
    leaves behind. Files-then-jobs: the file reads `failed` while the job is still `processing`
    under a lease, so the reaper requeues it and a later attempt overwrites both - self-healing.
    Jobs-then-files: the job is terminally `failed`, nothing will ever claim it again, and the
    file is stranded in `processing` forever with no reason for the student. Only one of those
    two recovers on its own.

    `status <> 'ready'` keeps the student-facing status monotonic, the same guard and the same
    argument as the `processing` write in `process_one`: under at-least-once a zombie whose
    lease expired can reach this line after the run that actually won already published the
    file, and flipping a queryable file to `failed` is the one direction that costs the student
    something real. `fail`'s own `state = 'processing'` guard covers the jobs half; this covers
    the half `queue.py` is not allowed to touch.

    A lost lease is reported, not raised: it is the routine at-least-once outcome (`fail`'s
    docstring), and the guard above already made it harmless.
    """
    with conn.transaction():
        conn.execute(
            """
            update files
            set status = 'failed',
                failed_reason = %(reason)s,
                updated_at = now()
            where file_id = %(file_id)s::uuid
              and status <> 'ready'
            """,
            {"reason": reason, "file_id": job.file_id},
        )

    if not fail(conn, job_id=job.job_id, lease_token=job.lease_token, error=error):
        logger.warning(
            "job %s: lease lost before it could be failed - another worker owns it now",
            job.job_id,
        )


def process_one(
    conn: psycopg.Connection,
    *,
    embedder: Embeddings,
    chunk_size: int,
    chunk_overlap: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> bool:
    """One tick of work: claim a job, ingest its file, and settle it - done, failed, or requeued.

    Returns False when the queue has nothing claimable (the normal idle tick - `run` sleeps),
    True when this call processed a job.

    The sequence, and why the order is the contract:
      1. `claim(conn, lease_seconds=...)` - commits before returning (queue.py's contract), so
         the connection comes back idle and the lease is already visible to other workers.
      2. Write `files.status = 'processing'` for the claimed file - this module's own SQL, the
         one worker-owned statement (see the ownership table above). Wrapped in its own
         `conn.transaction()` so it commits under EITHER wiring - autocommit or plain - and
         leaves the connection idle for step 3, the same shape as every `queue.py` writer.
      3. `ingest_file(job.staging_ref, ..., file_id=job.file_id, embedder=..., conn=conn,
         chunk_size=..., chunk_overlap=...)` - the UNCHANGED Slice 1 pipeline (PM-4 seam:
         wrap, do not rewrite). It publishes `ready` itself, inside the index transaction.
      4. `complete(conn, job_id=..., lease_token=job.lease_token)` - False means the lease
         was lost to another writer (`claim` handed the token out for exactly this check);
         handle it (at minimum, say so) rather than ignoring the return, so PR 3's reaper
         does not have to revisit this line.

    Steps 2-4 only run if the job still HAS a budget: `job.attempts > max_attempts` is checked
    first, before the `processing` write, so an exhausted job is buried without a status
    flicker and without buying an embed it will never use. The module docstring carries why
    the counter is `jobs.attempts` and why this check is here rather than inside `claim`.

    Three ways a tick that claimed something can end, all returning True - the caller only asks
    "was there work", never "did it succeed":
      - `complete` - the pipeline returned;
      - `_bury` - terminal input (`ParseError`), or the budget was already spent;
      - `release` - transient failure with budget left, after the backoff is served.

    Returns True for a failed job on purpose: `run` sleeps only on an EMPTY tick, and a queue
    with a failing job in it is not idle - the next job should be claimed immediately.

    `chunk_size` / `chunk_overlap` are REQUIRED and forwarded verbatim to `ingest_file` -
    never hardcoded here. They are provisional spike parameters (ADR 0019) and Spike Pass 2
    still has a live chunking axis (ADR 0026); same discipline as `ask_smoke._converge_corpus`.

    An exception that is NEITHER `ParseError` nor `TransientEmbeddingError` still propagates,
    and the worker still crashes - unchanged, and deliberate. Classifying an unknown exception
    would mean guessing a `failed_reason` from the closed set, and the wrong reason shown to a
    student is worse than none. Crash-mid-processing committed nothing (ADR 0020), so there is
    zero cleanup; the lease expires, the reaper (PR 3) requeues, and the durable budget bounds
    the loop instead of the handler doing it. A DB blip - which ADR 0020 lists as transient -
    lands here rather than on the retry path: psycopg errors are not classified anywhere in the
    corpus today, so the honest thing is to leave them loud rather than silently absorb every
    programming error alongside them.
    """

    job = claim(conn, lease_seconds=lease_seconds)

    if job is None:
        return False

    # Deliberately nothing logged on the empty tick above: at a 2s poll that is 30 lines a
    # minute of "nothing happened", which buries the lines that matter.
    logger.info(
        "job %s: claimed file %s (attempt %s/%s)",
        job.job_id,
        job.file_id,
        job.attempts,
        max_attempts,
    )

    # The budget check, before ANY work and before the `processing` write. `attempts` counts
    # this claim (queue.py), so `>` rather than `>=`: at `attempts == max_attempts` this IS the
    # last permitted attempt and it gets to run. `transient_exhausted` is the only reason in the
    # closed set that means "we gave up after retrying", and it is written even when the
    # attempts were burned by CRASHES rather than caught 429s - the taxonomy has no reason for
    # "kept dying" (ADR 0020 §Open: a richer taxonomy is a clean V2 extension), and `last_error`
    # carries the detail that distinguishes them.
    if job.attempts > max_attempts:
        logger.warning(
            "job %s: retry budget spent (%s attempts) - failing file %s",
            job.job_id,
            max_attempts,
            job.file_id,
        )
        _bury(
            conn,
            job,
            reason="transient_exhausted",
            error=f"retry budget spent: {max_attempts} attempts made, none succeeded",
        )
        return True

    # Timed from the claim, not the ingest, because the LEASE covers claim->complete: this
    # duration is the evidence PR 3's lease number is chosen from, and `attempts` above is the
    # same for the retry budget. Both are provisional today precisely because nothing has
    # measured them (see the constants' comment).
    started = time.perf_counter()

    # Keyed on `file_id`, the primary key - not `staging_ref`, which is nullable and carries
    # no uniqueness constraint. `updated_at` is bumped by hand: `files` has no trigger.
    #
    # `status <> 'ready'` keeps the student-facing status monotonic. Under at-least-once a job
    # can be reclaimed a SECOND time after an earlier worker's `index_file` already published
    # `ready` - that worker's `complete` returned False, so the job never went `done`. Without
    # the guard, this claim flips a finished file back to `processing` in the UI.
    # `failed -> processing` IS permitted here, and that is load-bearing rather than an
    # accident. The retry path does not need it - a retryable failure never writes `failed` at
    # all, it leaves the file `processing` and requeues the job - but `_bury` does: it writes
    # `files` BEFORE `jobs` precisely so that a crash between the two leaves a `failed` file
    # under a job that is still claimable. The reaper requeues it, this line writes `processing`
    # back over `failed`, and a later attempt settles both axes. Tightening this guard to
    # exclude `failed` would strand exactly that file forever, which is the recovery `_bury`'s
    # write order was chosen to buy. Only `ready` is protected, because only `ready` is a
    # promise already made to the student.
    # (An earlier version of this comment called the transition UNREACHABLE, reasoning that a
    # `failed` file always has a terminally-failed job. That is true only when `_bury` completed
    # BOTH its writes - the crash window between them is the whole point of its ordering, and
    # `update files set status='processing' ... and status <> 'ready'` writes 1 row against a
    # `failed` row when you run it.)
    # This write's guard is UNTESTED until PR 3 - `reclaim_expired` is what makes the sequence
    # reachable, and the guard's effect here is transient (a later `ready` overwrites it either
    # way), so the test belongs with the reaper. The IDENTICAL guard on the failure write is
    # testable today and is tested - see `_bury`.
    #
    # `conn.transaction()` rather than a bare execute: a bare statement only commits if the
    # caller wired autocommit, and on a plain connection it would sit unpublished in the
    # implicit transaction - then `index_file` refuses the INTRANS connection AFTER the embed
    # was paid (ADR 0025). The block commits under either wiring, like every `queue.py`
    # writer. No `require_idle` here on purpose: `claim` just committed and nothing runs
    # between it and this statement, so the connection cannot be anything but idle.
    with conn.transaction():
        conn.execute(
            """
            update files
            set status = 'processing',
                updated_at = now()
            where file_id = %(file_id)s::uuid
              and status <> 'ready'
            """,
            {"file_id": job.file_id},
        )

    try:
        ingest_file(
            job.staging_ref,
            job.owner_id,
            job.class_id,
            file_id=job.file_id,
            embedder=embedder,
            conn=conn,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    except ParseError as exc:
        # TERMINAL: bad input (ADR 0020 §1). Zero retries - the file is exactly as corrupt on
        # the next attempt, and the budget exists for bad luck, not bad bytes. `exc.reason` is
        # already one of `files.failed_reason`'s legal values, so it passes straight through.
        # Note this DID cost one `attempts` bump, from the claim; that is the trail of what
        # happened, not a retry spent - nothing will claim this job again.
        logger.warning(
            "job %s: terminal failure (%s) after %.1fs - %s",
            job.job_id,
            exc.reason,
            time.perf_counter() - started,
            exc,
        )
        _bury(conn, job, reason=exc.reason, error=f"{exc.reason}: {exc}")
        return True
    except TransientEmbeddingError as exc:
        # TRANSIENT: bad luck (ADR 0020 §1). Nothing was committed - the index transaction never
        # opened (ADR 0020 §3) - so the job goes back to `queued` with no cleanup, and
        # `files.status` stays `processing`, which is still TRUE: the file is mid-flight and the
        # student has nothing new to learn from a status flicker. Only budget exhaustion moves
        # it to `failed`.
        #
        # THE BACKOFF IS SERVED BEFORE THE RELEASE, while this worker still holds the lease, so
        # the delay applies to every worker and not just to this one's next poll (see
        # `release`'s docstring). It is skipped on the last permitted attempt: the next claim's
        # budget check refuses the job, so the wait would buy a retry that never comes. That is
        # a READER of `max_attempts`, not a second decider - the outcome is still settled in the
        # one check above.
        #
        # ACCEPTED COST: this worker is blocked for the delay, so one flaky file holds up every
        # other queued file for up to BACKOFF_MAX_SECONDS. That is a real head-of-line block,
        # and it is affordable only because V1 is one worker serving one user (ADR 0011) with a
        # cap measured in seconds. Worker concurrency (`ingestion-worker.md` §Open/deferred) is
        # what makes it stop being affordable - at which point the delay wants to live in the
        # database (a `visible_after` column claim filters on) rather than in a sleeping
        # process. Recorded so that change is a decision someone makes, not a bug someone finds.
        delay = backoff_seconds(job.attempts) if job.attempts < max_attempts else 0.0
        logger.warning(
            "job %s: transient failure on attempt %s/%s after %.1fs, retrying in %.0fs - %s",
            job.job_id,
            job.attempts,
            max_attempts,
            time.perf_counter() - started,
            delay,
            exc,
        )
        if delay:
            time.sleep(delay)
        if not release(
            conn, job_id=job.job_id, lease_token=job.lease_token, error=f"transient: {exc}"
        ):
            logger.warning(
                "job %s: lease lost before it could be requeued - another worker owns it now",
                job.job_id,
            )
        return True

    elapsed = time.perf_counter() - started
    if complete(conn, job_id=job.job_id, lease_token=job.lease_token):
        logger.info("job %s: done in %.1fs", job.job_id, elapsed)
    else:
        # The lease was too short for this file - the one signal that says so. `elapsed`
        # against `lease_seconds` is the whole diagnosis.
        logger.warning(
            "job %s: lease lost after %.1fs (lease was %ss) - another worker owns it now",
            job.job_id,
            elapsed,
            lease_seconds,
        )

    return True


def run(
    conn: psycopg.Connection,
    *,
    embedder: Embeddings,
    chunk_size: int,
    chunk_overlap: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> None:
    """The poll loop: `process_one` forever, sleeping `poll_seconds` after each empty tick.

    A tick that DID process a job polls again immediately - a non-empty queue means there is
    likely more work, and sleeping between real jobs just adds latency the student sees.

    PR 3 adds `reclaim_expired` to this loop (the reaper, ADR 0011 addendum).
    """

    logger.info(
        "worker started: poll %.1fs, lease %ss, budget %s attempts",
        poll_seconds,
        lease_seconds,
        max_attempts,
    )
    while True:
        if not process_one(
            conn,
            embedder=embedder,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
        ):
            time.sleep(poll_seconds)
