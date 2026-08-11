"""Queue - every statement that touches the `jobs` table lives here (issue #70).

This module RECORDS job state; it does not DECIDE it. The poll loop, the
retryable/terminal split, and every `files.status` transition after `queued`
belong to the worker (#71). That split is what keeps the two in disjoint files.

The `enqueue` / `claim` pair is ONE seam (ADR 0011): the boundary a V2 broker
swap drops in behind. Keep the shape swappable - no caller should need to know
the substrate is Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psycopg


@dataclass(frozen=True)
class Job:
    """One claimed unit of ingestion work - exactly what the worker needs to run it."""

    job_id: str
    file_id: str
    owner_id: str
    class_id: str
    attempts: int


def enqueue(
    conn: psycopg.Connection,
    *,
    path: str | Path,
    owner_id: str,
    class_id: str,
) -> str:
    """Create the `files` row (queued) and its `jobs` row in ONE transaction; return file_id.

    With no API adapter in Slice 2, this is the only thing that can create the
    `queued` file row the worker later claims - which is exactly what #69's
    caller-supplied `file_id` exists to support.

    `staging_ref` is the file's ABSOLUTE PATH (decided 2026-08-03). Slice 2 has
    no stager; ADR 0010's staging directory is Slice 3, which substitutes it with
    no schema or signature change.

    `path` yields TWO different column values: `filename` is the basename, because it is
    denormalized onto every chunk as the citation label (data-model.md §`chunks`) and a
    full path would render the uploader's home directory into every citation;
    `staging_ref` is the resolved absolute path, because that is what the worker opens.

    Both rows commit together or neither does. A committed `files` row with no `jobs` row
    is not a partial write anyone recovers from - it is a file stuck in `queued` that no
    worker will ever claim, and nothing in the system reports it.

    PRECONDITION ON `conn` (ADR 0025): it MUST NOT already be inside a transaction, or the
    block below degrades to a SAVEPOINT and this function returns having published nothing.
    Same precondition, same reason, as `index_file` - see its docstring.
    """
    source = Path(path)

    with conn.transaction():
        # 1. The files row. `queued` is the status the student polls until the worker moves
        #    it (#71 owns every later transition). `file_id` is DB-generated and RETURNed
        #    rather than re-queried: a `select ... where filename = ...` is neither unique
        #    (the same file may be enqueued twice) nor scoped (it can cross owners, breaking
        #    F12), and this is one round trip instead of two.
        row = conn.execute(
            """
            insert into files (owner_id, class_id, filename, staging_ref, status)
            values (%(owner_id)s, %(class_id)s::uuid, %(filename)s, %(staging_ref)s, 'queued')
            returning file_id
            """,
            {
                "owner_id": owner_id,
                "class_id": class_id,
                "filename": source.name,
                "staging_ref": str(source.resolve()),
            },
        ).fetchone()
        file_id = str(row[0])

        # 2. The jobs row. `state`, `attempts` and the timestamps take their schema defaults
        #    (`queued`, 0, now()) - spelling them out here would make this a second writer
        #    for values migrations/0001_init.sql already owns. `owner_id`/`class_id` are
        #    carried so the worker can call `ingest_file` without joining back to `files`.
        conn.execute(
            """
            insert into jobs (file_id, owner_id, class_id)
            values (%(file_id)s::uuid, %(owner_id)s, %(class_id)s::uuid)
            """,
            {"file_id": file_id, "owner_id": owner_id, "class_id": class_id},
        )

    return file_id


def claim(conn: psycopg.Connection, *, lease_seconds: int) -> Job | None:
    """Take the oldest queued job, or None. SELECT ... FOR UPDATE SKIP LOCKED (ADR 0011).

    Sets state='processing', bumps attempts, stamps leased_until.

    COMMITS BEFORE RETURNING - that is the contract, not a detail (decided
    2026-08-11): both statements run inside this function's own `conn.transaction()`,
    so the lease is visible to every other worker the moment this returns, and `conn`
    is left OUTSIDE any transaction - the precondition `index_file` demands of a
    worker that ingests on the connection it claimed with (ADR 0025;
    `ingestion-worker.md` step 1). The rejected alternative - leaving the commit to
    the caller so a crash undoes the whole attempt - loses the `attempts` bump with
    it (a poison file then retries forever, its budget never spent) and holds the
    row lock through the entire ingest, where a hung worker parks the job somewhere
    neither another claimer nor `reclaim_expired` can see it.

    None means the queue is empty OR every queued row is mid-claim by a concurrent
    worker right now (SKIP LOCKED skips, never waits). Both are normal poll-loop
    outcomes, not errors - the worker hits them most ticks.

    The returned `attempts` COUNTS THIS CLAIM (the post-bump value): it answers
    "which attempt is this?", which is what #71 compares against its retry budget.

    `lease_seconds` is a parameter rather than a constant on purpose: no lease
    duration, backoff curve, or poll interval exists anywhere in the design corpus
    yet, and #71 owns picking them (epic #73, gap 1, resolved 2026-08-03).
    """
    with conn.transaction():
        # Both statements MUST share this transaction: the row lock FOR UPDATE takes
        # lives exactly as long as the transaction that took it, so a select in its
        # own transaction would release the lock before the update ran - two workers
        # could then claim the same job in the gap.
        row = conn.execute(
            """
            select job_id::text, file_id::text, owner_id, class_id::text, attempts
            from jobs
            where state = 'queued'
            order by created_at
            for update skip locked
            limit 1
            """
        ).fetchone()
        if row is None:
            return None
        job_id, file_id, owner_id, class_id, attempts = row

        # now() on the SERVER, not Python's clock: `reclaim_expired` compares
        # `leased_until < now()` on the server's clock, so stamping the lease from a
        # worker machine's clock would let clock drift expire leases early or late.
        conn.execute(
            """
            update jobs
            set state = 'processing',
                attempts = attempts + 1,
                leased_until = now() + make_interval(secs => %(lease_seconds)s),
                updated_at = now()
            where job_id = %(job_id)s::uuid
            """,
            {"lease_seconds": lease_seconds, "job_id": job_id},
        )

    return Job(
        job_id=job_id,
        file_id=file_id,
        owner_id=owner_id,
        class_id=class_id,
        attempts=attempts + 1,
    )


def complete(conn: psycopg.Connection, *, job_id: str) -> None:
    """Terminal success: state='done'. The worker calls this after ingest_file returns."""
    raise NotImplementedError


def fail(conn: psycopg.Connection, *, job_id: str, error: str) -> None:
    """Terminal failure: state='failed' + last_error.

    `last_error` is free text - distinct from `files.failed_reason`, which is a
    closed CHECK set of five values that only #71 writes.
    """
    raise NotImplementedError


def reclaim_expired(conn: psycopg.Connection) -> int:
    """The reaper: processing rows past leased_until go back to queued. Returns the count.

    Zero cleanup by design - a crashed run committed nothing, because the ingest
    transaction wraps only the final write, never the parse/embed work (ADR 0020).
    #71 calls this on the poll loop (ADR 0011 addendum).
    """
    raise NotImplementedError
