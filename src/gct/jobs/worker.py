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
    | -> failed + failed_reason | THIS module - terminal input, or budget exhausted (PR 2)   |

`jobs.state` is the OTHER axis - claim/lease/retry machinery - and `queue.py` owns every
statement that touches that table.

Runs as a separate OS process via `scripts/worker.py`: ADR 0011's addendum is explicit that
"in-process" means no broker, NOT inside an API event loop; the script is a thin peer caller
over this module (ADR 0009).

Connection contract (ADR 0025, guarded per ADR 0027): every entry point here needs a
connection that is idle between statements. `scripts/worker.py` satisfies it with
`conn.autocommit = True` at wiring, exactly as `ask_smoke.py` does - its wiring comment
carries the full argument for why autocommit is load-bearing rather than style.
"""

from __future__ import annotations

import logging
import time

import psycopg

from gct.ingest.pipeline import ingest_file
from gct.jobs.queue import claim, complete
from gct.providers.base import Embeddings

logger = logging.getLogger(__name__)

# Provisional numbers (issue #71). PR 1 needs a lease and a poll interval to run at all; the
# four-numbers ADR (lease / retry budget / backoff / poll) lands in PR 3, ratifies or changes
# these, and repoints ingestion-worker.md §4's dangling "ADR 0011 budget" citation at itself.
# The lease errs LONG on purpose: too short reclaims a job a healthy worker is still ingesting
# and pays the embedding bill twice; too long only delays reclaim after a genuine crash.
DEFAULT_LEASE_SECONDS = 15 * 60
DEFAULT_POLL_SECONDS = 2.0


def process_one(
    conn: psycopg.Connection,
    *,
    embedder: Embeddings,
    chunk_size: int,
    chunk_overlap: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """One tick of work: claim a job, ingest its file, mark the job done.

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
      4. `complete(conn, job_id=...)` - False means the lease was lost to another writer;
         handle it (at minimum, say so) rather than ignoring the return, so PR 3's reaper
         does not have to revisit this line.

    `chunk_size` / `chunk_overlap` are REQUIRED and forwarded verbatim to `ingest_file` -
    never hardcoded here. They are provisional spike parameters (ADR 0019) and Spike Pass 2
    still has a live chunking axis (ADR 0026); same discipline as `ask_smoke._converge_corpus`.

    PR 1 scope: any exception out of the pipeline propagates - the worker crashes, the lease
    expires, the reaper (PR 3) requeues. Crash-mid-processing committed nothing (ADR 0020),
    so zero cleanup. The retryable/terminal split arrives in PR 2.
    """

    job = claim(conn, lease_seconds=lease_seconds)

    if job is None:
        return False

    # Deliberately nothing logged on the empty tick above: at a 2s poll that is 30 lines a
    # minute of "nothing happened", which buries the lines that matter.
    logger.info("job %s: claimed file %s (attempt %s)", job.job_id, job.file_id, job.attempts)
    # Timed from the claim, not the ingest, because the LEASE covers claim->complete: this
    # duration is the evidence PR 3's lease number is chosen from, and `attempts` above is the
    # same for PR 2's retry budget. Both are provisional today precisely because nothing has
    # measured them (see the constants' comment).
    started = time.perf_counter()

    # Keyed on `file_id`, the primary key - not `staging_ref`, which is nullable and carries
    # no uniqueness constraint. `updated_at` is bumped by hand: `files` has no trigger.
    #
    # `status <> 'ready'` keeps the student-facing status monotonic. Under at-least-once a job
    # can be reclaimed a SECOND time after an earlier worker's `index_file` already published
    # `ready` - that worker's `complete` returned False, so the job never went `done`. Without
    # the guard, this claim flips a finished file back to `processing` in the UI. The retry
    # path is unaffected: `failed -> processing` (PR 2) still passes.
    # UNTESTED until PR 3 - `reclaim_expired` is what makes the sequence reachable, so the
    # test belongs with the reaper, not here.
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

    elapsed = time.perf_counter() - started
    if complete(conn, job_id=job.job_id):
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
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> None:
    """The poll loop: `process_one` forever, sleeping `poll_seconds` after each empty tick.

    A tick that DID process a job polls again immediately - a non-empty queue means there is
    likely more work, and sleeping between real jobs just adds latency the student sees.

    PR 3 adds `reclaim_expired` to this loop (the reaper, ADR 0011 addendum).
    """

    logger.info("worker started: poll %.1fs, lease %ss", poll_seconds, lease_seconds)
    while True:
        if not process_one(
            conn,
            embedder=embedder,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            lease_seconds=lease_seconds,
        ):
            time.sleep(poll_seconds)
