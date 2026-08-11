"""Queue - every statement that touches the `jobs` table lives here (issue #70).

This module RECORDS job state; it does not DECIDE it. The poll loop, the
retryable/terminal split, and every `files.status` transition after `queued`
belong to the worker (#71). That split is what keeps the two in disjoint files.

The `enqueue` / `claim` pair is ONE seam (ADR 0011): the boundary a V2 broker
swap drops in behind. Keep the shape swappable - no caller should need to know
the substrate is Postgres.

Every writer here REFUSES a connection that is already inside a transaction, rather
than silently degrading to a SAVEPOINT that publishes nothing (`_require_idle`;
ADR 0025, adopted for this module per ADR 0027 §Adopted early in the queue module).
The `index_file` half of that guard is still open on #75 - this module went first
because the guard costs nothing here, measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psycopg


def _require_idle(conn: psycopg.Connection, fn: str) -> None:
    """Reject a connection that is already inside a transaction. Every writer here calls this.

    ADR 0025's failure in one line: psycopg opens an implicit transaction on a connection's
    first statement, and `conn.transaction()` on an already-open connection issues a SAVEPOINT
    rather than BEGIN - so the write commits NOTHING while the function returns successfully.
    Silent, and invisible to any test that reads back through the same connection.

    ADR 0025 chose documentation over a guard, on ONE empirical claim: that a guard "fires on
    benign code". ADR 0027 disputed that for `index_file` and measured 7 firings, none benign.
    Nobody had measured THIS module. Measured 2026-08-11, guard live on all five writers:

        pytest -m "not live"  ->  313 passed, 0 failed

    Zero. Every existing caller already satisfies the precondition, so here the blast-radius
    objection is not merely outweighed - it is empty. That is why the guard lands in `queue.py`
    ahead of `index_file`, whose own adoption still costs 7 test edits and is open on #75.
    Recorded in ADR 0027 (§Adopted early in the queue module) so this is a decision on the
    record, not enforcement nobody chose.

    Deliberately NOT caught anywhere: a caller in the wrong transaction state is a programming
    error at wiring, not a runtime condition to handle. The message names the remedy because an
    error that only says "no" costs the reader the same debugging session twice.
    """
    status = conn.info.transaction_status
    if status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError(
            f"{fn}() requires a connection that is NOT already inside a transaction; "
            f"psycopg reports {status.name}. Its write would degrade to a SAVEPOINT and "
            "publish nothing while returning successfully (ADR 0025). Fix at the caller: "
            "set `conn.autocommit = True` at wiring, or commit before calling. NB a bare "
            "SELECT opens the implicit transaction exactly as a write does."
        )


@dataclass(frozen=True)
class Job:
    """One claimed unit of ingestion work - everything `ingest_file` needs, so the worker
    can call it without touching the DB in between.

    That last clause is the whole point of `staging_ref` being here rather than looked up.
    `ingest_file` takes a `path`, and the only record of it is `files.staging_ref`; a worker
    that had to fetch it would run a statement AFTER `claim` committed, and a bare SELECT
    reopens psycopg's implicit transaction exactly as a write does - putting the connection
    back INTRANS and degrading `index_file`'s transaction to a SAVEPOINT that publishes
    nothing (ADR 0025, and ADR 0027 §Context for the two tests this already cost). Carrying
    the value removes the worker's REASON to talk to the database between claim and ingest,
    which is a stronger guarantee than telling it not to.

    Wider than `ingestion-worker.md`'s `Job{ file_id, owner_id, class_id }` by three fields:
    `job_id` and `attempts` are the queue handle and the retry trail this module owns, and
    `staging_ref` is the above. The spec's §Interface/contract records the widening.
    """

    job_id: str
    file_id: str
    owner_id: str
    class_id: str
    attempts: int
    staging_ref: str


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

    `path` NEED NOT EXIST, and that is deliberate rather than an oversight. `Path.resolve()`
    is non-strict, so a path to nothing enqueues cleanly and #71 fails it at parse - which is
    the designed route: ADR 0020 makes an unopenable file a TERMINAL failure carrying an
    actionable reason the student sees, and a reason on the file row is worth more to them
    than an exception thrown back at a caller with no UI. Checking here would also expire:
    at Slice 3 `staging_ref` becomes a stager handle, not a path anything can stat, so the
    guard would go from useless to wrong. Recorded because "considered and routed to the
    parse-terminal path" and "nobody looked" are indistinguishable from the code alone.

    PRECONDITION ON `conn` (ADR 0025): it MUST NOT already be inside a transaction, or the
    block below degrades to a SAVEPOINT and this function returns having published nothing.
    Same precondition, same reason, as `index_file` - see its docstring.
    """
    _require_idle(conn, "enqueue")
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
        #    (`queued`, 0, now()) because those are QUEUE MACHINERY: migrations/0001_init.sql
        #    owns them, and restating them here would be a second writer for values this
        #    module does not decide.
        #    `files.status` above is deliberately NOT the same case, and the rule is about
        #    machinery defaults only. That column is published domain truth - the thing the
        #    student polls - so naming it at the write site is worth the one line, and the
        #    duplication is safe in a way these are not: its legal values are pinned by a
        #    CHECK constraint AND by this module's own tests, so a schema change cannot drift
        #    past it quietly.
        #    `owner_id`/`class_id` are carried so `claim` can hand a worker everything
        #    `ingest_file` needs from one row - see `Job`.
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

    PRECONDITION ON `conn` (ADR 0025), same as `enqueue` and every writer here: it MUST
    NOT already be inside a transaction. The commit promised above is CONDITIONAL on it -
    hand this an open connection and the block below is a SAVEPOINT, so the lease is
    published to nobody, `conn` comes back still in a transaction, and a crash takes the
    `attempts` bump with it. All three guarantees fail together and silently. `_require_idle`
    now refuses that call rather than letting it look like it worked; the docstring states
    it too, because the guard tells you THAT you are wrong and this tells you WHY.
    """
    _require_idle(conn, "claim")
    with conn.transaction():
        # Both statements MUST share this transaction: the row lock FOR UPDATE takes
        # lives exactly as long as the transaction that took it, so a select in its
        # own transaction would release the lock before the update ran - two workers
        # could then claim the same job in the gap.
        # The join reaches `files` for ONE column - `staging_ref`, the path `ingest_file`
        # opens. It is here rather than in the worker so the worker runs no statement between
        # this commit and the ingest; see `Job`'s docstring for why that is load-bearing and
        # not a round-trip optimisation.
        # `for update OF J` is deliberate: an unqualified `for update` across a join locks the
        # matching `files` row too, and nothing here wants that row held - #71's `index_file`
        # upserts it moments later, on another connection under at-least-once.
        row = conn.execute(
            """
            select j.job_id::text, j.file_id::text, j.owner_id, j.class_id::text,
                   j.attempts, f.staging_ref
            from jobs j
            join files f using (file_id)
            where j.state = 'queued'
            order by j.created_at
            for update of j skip locked
            limit 1
            """
        ).fetchone()
        if row is None:
            return None
        job_id, file_id, owner_id, class_id, attempts, staging_ref = row

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
        staging_ref=staging_ref,
    )


def complete(conn: psycopg.Connection, *, job_id: str) -> bool:
    """Terminal success: state='done'. The worker calls this after ingest_file returns.

    Commits before returning, same contract as `claim` (its docstring has the
    argument). `leased_until` is deliberately left in place - `reclaim_expired`
    filters on state, so a stale lease on a terminal row is inert, and clearing it
    would erase the record of when the winning run held the job.

    ONLY THE CURRENT LEASEHOLDER MAY FINISH A JOB - hence `and state = 'processing'`.
    Without it, a worker whose lease expired and whose job was re-handed out can wake
    up and write a terminal state over the run that actually won. That is not
    hypothetical: `reclaim_expired`'s own docstring predicts this caller ("a
    stalled-but-alive worker keeps running and may finish after its job is re-handed
    out"), and the sequence claim -> reclaim -> re-claim -> the WINNER completes ->
    the zombie fails leaves the row `failed` for a file that is `ready`. Every
    argument in that sequence is correct; the guard is the only thing separating the
    two writers. It does NOT decide retryable-vs-terminal policy, which stays #71's -
    it only says a lease means what it says.

    THE TWO WAYS THIS CAN WRITE NOTHING ARE NOT THE SAME EVENT, so they are not
    reported the same way:
      - the job exists but is no longer `processing` -> this worker LOST ITS LEASE.
        Routine under at-least-once (the guard above is what makes it routine), so it
        returns False. Raising here would turn an expected outcome into an exception on
        a normal poll cycle.
      - no such `job_id` at all -> a programming error, and the caller's belief that a
        job finished is simply false. `LookupError`, loudly. Same reasoning as
        `index_file`'s zero-chunk `ValueError`: a public entry point does not trust its
        caller (#23), and silence here costs a re-ingest per lease expiry until #71's
        retry budget terminal-fails the file for the wrong reason.

    Returns True when this call is the one that finished the job.
    """
    _require_idle(conn, "complete")
    with conn.transaction():
        cursor = conn.execute(
            """
            update jobs
            set state = 'done', updated_at = now()
            where job_id = %(job_id)s::uuid and state = 'processing'
            """,
            {"job_id": job_id},
        )
        # Only asked when the update wrote nothing, and inside the same transaction so the
        # answer cannot be overtaken between the two statements.
        job_exists = cursor.rowcount == 1 or (
            conn.execute(
                "select 1 from jobs where job_id = %(job_id)s::uuid", {"job_id": job_id}
            ).fetchone()
            is not None
        )
        finished = cursor.rowcount == 1

    # Raised AFTER the block so the connection is left IDLE either way - the ADR 0025
    # contract this module's writers all keep.
    if not job_exists:
        raise LookupError(
            f"complete() called with job_id={job_id}, which names no row in `jobs`; "
            "the caller believes a job finished that does not exist"
        )
    return finished


def fail(conn: psycopg.Connection, *, job_id: str, error: str) -> bool:
    """Terminal failure: state='failed' + last_error.

    `last_error` is free text - distinct from `files.failed_reason`, which is a
    closed CHECK set of five values that only #71 writes.

    Commits before returning, same contract as `claim`. `attempts` is untouched:
    the trail of how many tries this job burned is exactly what a human reading a
    failed row wants to see.

    `and state = 'processing'` for the same reason `complete` carries it, and this is
    the direction that actually costs something: a zombie's `fail` overwriting a
    genuine success is worse than a zombie's `complete` overwriting a genuine
    failure, because the second is at least true of some run. See `complete`.

    Returns False on a lost lease, raises `LookupError` on an unknown `job_id`, returns
    True when this call is the one that failed the job - the same three-way split
    `complete` makes, for the same reasons. Kept symmetrical deliberately: a worker
    handles both outcomes in one place, and a split that differed between the success
    and failure paths would be read as meaningful.
    """
    _require_idle(conn, "fail")
    with conn.transaction():
        cursor = conn.execute(
            """
            update jobs
            set state = 'failed', last_error = %(error)s, updated_at = now()
            where job_id = %(job_id)s::uuid and state = 'processing'
            """,
            {"error": error, "job_id": job_id},
        )
        job_exists = cursor.rowcount == 1 or (
            conn.execute(
                "select 1 from jobs where job_id = %(job_id)s::uuid", {"job_id": job_id}
            ).fetchone()
            is not None
        )
        recorded = cursor.rowcount == 1

    if not job_exists:
        raise LookupError(
            f"fail() called with job_id={job_id}, which names no row in `jobs`; "
            "the error message it carried has been discarded, not recorded"
        )
    return recorded


def reclaim_expired(conn: psycopg.Connection) -> int:
    """The reaper: processing rows past leased_until go back to queued. Returns the count.

    Zero cleanup by design - a crashed run committed nothing, because the ingest
    transaction wraps only the final write, never the parse/embed work (ADR 0020).
    #71 calls this on the poll loop (ADR 0011 addendum).

    ONE statement on purpose: a select-then-update pair would need its own row
    locking to close the gap between reading a lease and resetting it; a single
    UPDATE is that whole move, atomically. Both WHERE conditions are load-bearing:
    - without `state = 'processing'`, terminal rows get resurrected - `complete`/
      `fail` never clear `leased_until`, so every done and failed job carries an
      expired lease forever and would requeue on every tick;
    - without `leased_until < now()`, a live lease is yanked from a healthy worker
      mid-job. (Same server clock `claim` stamped the lease with.)

    `attempts` is deliberately NOT reset - it is the retry trail #71 compares
    against its budget. A reclaim that zeroed it would hand a poison file a fresh
    budget after every crash.

    Reclaiming is NOT killing: a stalled-but-alive worker keeps running and may
    finish after its job is re-handed out. That duplicate run is safe by design -
    at-least-once, absorbed by `index_file`'s all-or-nothing replace (ADR 0020).

    Commits before returning, same contract as `claim` (its docstring has the
    argument). Returns how many rows moved; 0 is the normal tick, not an error.
    `jobs_state_lease_idx (state, leased_until)` exists precisely for this scan.
    """
    _require_idle(conn, "reclaim_expired")
    with conn.transaction():
        cursor = conn.execute(
            """
            update jobs
            set state = 'queued',
                leased_until = null,
                updated_at = now()
            where state = 'processing' and leased_until < now()
            """
        )
    return cursor.rowcount
