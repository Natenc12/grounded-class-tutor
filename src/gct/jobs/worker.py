"""Worker - claims one job at a time, runs `ingest_file`, and drives `files.status`.

`queue.py` records job state; this module decides it. Who writes which `files.status`:

    | transition                | writer                                              |
    |---------------------------|-----------------------------------------------------|
    | -> queued                 | `enqueue`                                           |
    | -> processing             | this module, on claim; also clears `failed_reason`  |
    | -> ready                  | `index_file`, inside the index transaction          |
    | -> failed + failed_reason | this module: bad input, or attempts used up         |

`ParseError` is bad input: fail the file at once. `TransientEmbeddingError` is bad luck: back
off and hand the job back. Anything else crashes the worker on purpose. The attempt counter
lives in the database, so a job that crashes the worker every time still runs out of attempts.

Exactly one worker runs (issue #187). `run` waits for the worker lock, then puts back any job a
previous worker left mid-way (`requeue_orphans`), then loops. There is no lease, heartbeat or
reaper: with one worker there is nobody to take a job away from.
"""

from __future__ import annotations

import logging
import time

import psycopg

from gct.ingest.parse import ParseError
from gct.ingest.pipeline import ingest_file
from gct.jobs.queue import (
    Job,
    claim,
    complete,
    fail,
    release,
    requeue_orphans,
    try_acquire_worker_lock,
)
from gct.providers.base import Embeddings, TransientEmbeddingError

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 2.0
DEFAULT_LOCK_RETRY_SECONDS = 5.0
DEFAULT_MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0


def backoff_seconds(attempts: int) -> float:
    """Wait before handing back a job that failed transiently: 2s, 4s, 8s ... capped at 60s."""
    return min(BACKOFF_BASE_SECONDS * 2 ** (attempts - 1), BACKOFF_MAX_SECONDS)


def _bury(conn: psycopg.Connection, job: Job, *, reason: str, error: str) -> None:
    with conn.transaction():
        conn.execute(
            """
            update files
            set status = 'failed', failed_reason = %(reason)s, updated_at = now()
            where file_id = %(file_id)s::uuid and status <> 'ready'
            """,
            {"reason": reason, "file_id": job.file_id},
        )
    fail(conn, job_id=job.job_id, error=error)


def process_one(
    conn: psycopg.Connection,
    *,
    embedder: Embeddings,
    chunk_size: int,
    chunk_overlap: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> bool:
    """Claim and process one job. Returns False if the queue was empty.

    `conn` must hold the worker lock (`claim` checks).
    """
    job = claim(conn)
    if job is None:
        return False
    logger.info(
        "job %s: claimed file %s (attempt %s/%s)",
        job.job_id,
        job.file_id,
        job.attempts,
        max_attempts,
    )

    if job.attempts > max_attempts:
        logger.warning("job %s: attempts used up - failing file %s", job.job_id, job.file_id)
        _bury(
            conn,
            job,
            reason="transient_exhausted",
            error=f"retry budget spent: {max_attempts} attempts made, none succeeded",
        )
        return True

    started = time.perf_counter()
    with conn.transaction():
        conn.execute(
            """
            update files
            set status = 'processing', failed_reason = null, updated_at = now()
            where file_id = %(file_id)s::uuid and status <> 'ready'
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
        logger.warning("job %s: terminal failure (%s) - %s", job.job_id, exc.reason, exc)
        _bury(conn, job, reason=exc.reason, error=f"{exc.reason}: {exc}")
        return True
    except TransientEmbeddingError as exc:
        delay = backoff_seconds(job.attempts) if job.attempts < max_attempts else 0.0
        logger.warning(
            "job %s: transient failure on attempt %s/%s - retrying in %.0fs - %s",
            job.job_id,
            job.attempts,
            max_attempts,
            delay,
            exc,
        )
        if delay:
            time.sleep(delay)
        release(conn, job_id=job.job_id, error=f"transient: {exc}")
        return True

    complete(conn, job_id=job.job_id)
    logger.info("job %s: done in %.1fs", job.job_id, time.perf_counter() - started)
    return True


def run(
    conn: psycopg.Connection,
    *,
    embedder: Embeddings,
    chunk_size: int,
    chunk_overlap: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    lock_retry_seconds: float = DEFAULT_LOCK_RETRY_SECONDS,
) -> None:
    """Wait for the worker lock, put back orphaned jobs, then process jobs forever.

    Waits rather than exits when the lock is taken: after a hard crash Postgres may take a
    moment to notice the old session is gone, and a fast restart should not fail on that.
    """
    while not try_acquire_worker_lock(conn):
        logger.warning(
            "another worker holds the worker lock - retrying in %.0fs", lock_retry_seconds
        )
        time.sleep(lock_retry_seconds)

    orphans = requeue_orphans(conn)
    if orphans:
        logger.warning(
            "requeued %s job(s) a previous worker left mid-way (it stopped without finishing)",
            orphans,
        )
    logger.info("worker started: poll %.1fs, budget %s attempts", poll_seconds, max_attempts)
    while True:
        if not process_one(
            conn,
            embedder=embedder,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            max_attempts=max_attempts,
        ):
            time.sleep(poll_seconds)
