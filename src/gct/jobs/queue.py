"""Queue - every statement that WRITES the `jobs` table lives here (issue #70).

WRITES, not touches: a caller may READ that table inside its own UPDATE, where an
answer this module returned would already be stale by the time it is used (#86).

This module RECORDS job state; it does not DECIDE it. The poll loop, the
retryable/terminal split, and every `files.status` transition after `queued`
belong to the worker (#71). That split is what keeps the two in disjoint files.

Only one worker may run (issue #187). `claim` refuses a connection that does not
hold the worker lock, so a second worker, a test or a script cannot take jobs
without it. Because a second worker cannot exist, a job found `processing` when
the lock is taken was left by a worker that stopped mid-job, and goes back to
`queued` (`requeue_orphans`).

Every writer here REFUSES a connection that is already inside a transaction, rather
than silently degrading to a SAVEPOINT that publishes nothing (`gct.db.require_idle`;
ADR 0025, guarded per ADR 0027). This module adopted the guard first - measured at
zero firings, ADR 0027 §Adopted early in the queue module - and `index_file` followed
when #75 accepted the ADR, which is when the helper moved to `gct.db` so both modules
call one writer of the rule.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg

from gct.db import require_idle
from gct.ids import require_canonical_uuid


@dataclass(frozen=True)
class Job:
    """One claimed unit of ingestion work - everything `ingest_file` needs, so the worker can
    call it without touching the database in between (a bare SELECT after `claim` would leave
    the connection inside a transaction; ADR 0025)."""

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

    This is the ONLY thing that creates the `queued` file row the worker later claims:
    `POST /files` reaches it rather than inserting a row of its own, so there is one writer
    of that row however the upload arrived - which is exactly what #69's caller-supplied
    `file_id` exists to support.

    `staging_ref` is the file's ABSOLUTE PATH (decided 2026-08-03; confirmed when the stager
    arrived, #105). `gct.staging.stage` returns exactly the string a caller then passes here
    as `path`, so `enqueue` and `worker.process_one` (which opens `job.staging_ref` as a path)
    were both unchanged by Slice 3. It stops being a local path only at ADR 0010's V2 move to
    Object Storage, which is a V2 change.

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
    than an exception thrown back at a caller with no UI. Checking here would also be a
    second writer for a fact the stager owns: `stage` returns only after the bytes are
    flushed, fsynced and renamed into place (#105), so a path it produced exists by
    construction, and the one case where a stat could disagree with it is ADR 0010's V2
    Object Storage, where `staging_ref` stops being a local path and the guard would go from
    useless to wrong. Recorded because "considered and routed to the parse-terminal path" and
    "nobody looked" are indistinguishable from the code alone.

    `class_id` IS PARSED AND THE CANONICAL SPELLING IS BOUND (#121), and this is now the shape
    EVERY id-taking boundary in the library takes - `class_exists`, `retrieve`, `index_file` and
    `ingest_file` for a caller's `class_id`, `get_file_status` for a `file_id` (#126: two sites
    bound raw, two caught too little of what the parser raises; all four now go through
    `gct.ids`), and this module's own settle verbs - `complete`, `fail` and `release` - for a
    `job_id`, strictly, because it reaches a caller out of `claim`'s `Job`.
    This function keeps its own copy of that parse, and `gct.ids`'s docstring is the
    writer of why.

    `get_file_status` is the writer of WHY - which spellings `uuid.UUID` accepts that Postgres's
    `::uuid` cast does not, and why validating the raw string and then binding THAT is not the
    same thing - and this is a cite, not a second copy of that argument.

    HOW LENIENT THIS ACTUALLY IS, said out loud because "the four spellings" under-states it and
    the guard test enumerates exactly four. `uuid.UUID` is not a spelling whitelist: it strips
    `urn:` and `uuid:` prefixes, strips surrounding braces, and removes hyphens WHEREVER they fall.
    So an id with a misplaced or extra hyphen - which Postgres's own cast refuses - now resolves to
    a real class here (measured on #121: six such forms accepted). That is the leniency this
    function chose, not an accident of it, and it does not widen who can reach what: the ownership
    check the upload route runs first (`class_exists`) parses with the same `uuid.UUID` and has
    always accepted these forms. The change is that the two calls now AGREE about an id instead of
    one saying yes and the other aborting, which was the whole defect.

    Lenient here, strict one layer up in the pipeline, and the asymmetry is about WHERE THE ID
    COMES FROM rather than about writers being stricter than readers. `ingest_file`
    (`gct/ingest/pipeline.py`) refuses a non-canonical `file_id` outright because the worker reads
    that id back out of the database, where it is canonical by construction, so any other spelling
    is an upstream bug. Everything that calls `enqueue` - a script, the Slice 3 upload route, the
    exit smoke - hands it a string a person typed. `ingest_file` applies BOTH rules on one call,
    which is the clearest statement of the criterion available: strict on `file_id`, lenient on
    `class_id`, the second added by #126 before the embedding run rather than after it.

    What is NOT inherited from those siblings is their REASON, and copying their sentence would
    make this docstring wrong. Theirs is a poisoned connection: they are readers with no
    transaction of their own, so a failed cast aborts the caller's implicit transaction and takes
    every later statement with it. This one has a transaction - the block below - and `require_idle`
    makes an IDLE connection the only kind that reaches it, so psycopg rolls that transaction back
    and leaves the connection IDLE. Measured on #121: the caller's next statement succeeds. The
    cost of letting the cast fail is narrower and later - one rejected write, surfacing as a
    psycopg `DataError` that names a Postgres type rather than anything a caller can act on, and
    for the upload route, after `stage` has already written the bytes.

    PRECONDITION ON `conn` (ADR 0025): it MUST NOT already be inside a transaction, or the
    block below degrades to a SAVEPOINT and this function returns having published nothing.
    Same precondition, same reason, as `index_file` - see its docstring.
    """
    require_idle(conn, "enqueue")
    try:
        canonical_class_id = str(uuid.UUID(class_id))
    except (ValueError, AttributeError, TypeError) as exc:
        # THREE exception types, not just `ValueError`, and the set is copied from `ingest_file`'s
        # guard (`gct/ingest/pipeline.py`) rather than invented: `uuid.UUID` reports a bad argument
        # in three different ways depending on what it was handed. A malformed STRING is the
        # `ValueError`; `None` is a `TypeError` ("one of the hex, bytes, ... arguments must be
        # given"); an `int`, a `list`, or an already-parsed `uuid.UUID` instance is an
        # `AttributeError` from the `.replace` call inside the parser. Catching only `ValueError`
        # let the last two escape as the parser's own message, which names `uuid.UUID`'s internals
        # and no remedy - the opposite of what this guard exists to do. Nothing shipped can reach
        # them today (the upload route's `class_id` is a Form field, so always a `str`), which is
        # what makes this a trap rather than a bug: it waits for the first caller that passes the
        # `uuid.UUID` it already has.
        raise ValueError(
            f"enqueue() requires a uuid class_id; got {class_id!r}. Pass the id `create_class` "
            "returned for the class this file belongs to - an id that is not a uuid cannot name "
            "any class, so nothing would be queued and the caller's staged bytes would be "
            "orphaned."
        ) from exc
    source = Path(path)

    with conn.transaction():
        # 1. The files row. `queued` is the status the student polls until the worker moves
        #    it (the worker owns every later transition). `file_id` is DB-generated and RETURNed
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
                "class_id": canonical_class_id,
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
            {"file_id": file_id, "owner_id": owner_id, "class_id": canonical_class_id},
        )

    return file_id


# Any fixed number. Postgres reports a one-number advisory lock as classid 0, objid KEY.
WORKER_LOCK_KEY = 187_187


def try_acquire_worker_lock(conn: psycopg.Connection) -> bool:
    """Take the worker lock for this connection's session, or return False if another holds it.

    The lock lives as long as the connection. The worker writes its results on the same
    connection, so the lock and the writes cannot outlive each other.
    """
    require_idle(conn, "try_acquire_worker_lock")
    with conn.transaction():
        return bool(
            conn.execute("select pg_try_advisory_lock(%s)", (WORKER_LOCK_KEY,)).fetchone()[0]
        )


def holds_worker_lock(conn: psycopg.Connection) -> bool:
    with conn.transaction():
        return bool(
            conn.execute(
                """
                select exists (
                    select 1 from pg_locks
                    where locktype = 'advisory' and pid = pg_backend_pid() and granted
                      and classid = 0 and objid = %s and objsubid = 1
                )
                """,
                (WORKER_LOCK_KEY,),
            ).fetchone()[0]
        )


def _require_worker_lock(conn: psycopg.Connection, fn: str) -> None:
    if not holds_worker_lock(conn):
        raise RuntimeError(
            f"{fn}() needs the worker lock on this connection. Call "
            "try_acquire_worker_lock(conn) first; only one worker may run (issue #187)."
        )


def requeue_orphans(conn: psycopg.Connection) -> int:
    """Put every `processing` job back to `queued`. Only the lock holder may call this, and
    only before it claims anything: with one worker, any `processing` job is orphaned."""
    require_idle(conn, "requeue_orphans")
    with conn.transaction():
        _require_worker_lock(conn, "requeue_orphans")
        cursor = conn.execute(
            "update jobs set state = 'queued', updated_at = now() where state = 'processing'"
        )
    return cursor.rowcount


def claim(conn: psycopg.Connection) -> Job | None:
    """Take the oldest queued job, or None. Counts one attempt, so a job whose worker crashes
    every time still runs out of attempts instead of looping forever."""
    require_idle(conn, "claim")
    with conn.transaction():
        _require_worker_lock(conn, "claim")
        row = conn.execute(
            """
            update jobs j
            set state = 'processing', attempts = j.attempts + 1, updated_at = now()
            from files f
            where f.file_id = j.file_id
              and j.job_id = (
                  select job_id from jobs where state = 'queued' order by created_at limit 1
              )
            returning j.job_id::text, j.file_id::text, j.owner_id, j.class_id::text,
                      j.attempts, f.staging_ref
            """
        ).fetchone()
    if row is None:
        return None
    job_id, file_id, owner_id, class_id, attempts, staging_ref = row
    return Job(
        job_id=job_id,
        file_id=file_id,
        owner_id=owner_id,
        class_id=class_id,
        attempts=attempts,
        staging_ref=staging_ref,
    )


def _settle(
    conn: psycopg.Connection,
    *,
    verb: str,
    job_id: str,
    assignments: str,
    params: dict[str, object],
) -> bool:
    job_id = require_canonical_uuid(
        job_id,
        fn=verb,
        param="job_id",
        remedy="Pass `job.job_id` from the `Job` `claim` returned.",
    )
    require_idle(conn, verb)
    with conn.transaction():
        cursor = conn.execute(
            f"""
            update jobs
            set {assignments}, updated_at = now()
            where job_id = %(job_id)s::uuid and state = 'processing'
            """,
            {"job_id": job_id, **params},
        )
        settled = cursor.rowcount == 1
        if (
            not settled
            and conn.execute(
                "select 1 from jobs where job_id = %(job_id)s::uuid", {"job_id": job_id}
            ).fetchone()
            is None
        ):
            raise LookupError(f"{verb}() called with job_id={job_id}, which names no row in `jobs`")
    return settled


def complete(conn: psycopg.Connection, *, job_id: str) -> bool:
    return _settle(conn, verb="complete", job_id=job_id, assignments="state = 'done'", params={})


def fail(conn: psycopg.Connection, *, job_id: str, error: str) -> bool:
    return _settle(
        conn,
        verb="fail",
        job_id=job_id,
        assignments="state = 'failed', last_error = %(error)s",
        params={"error": error},
    )


def release(conn: psycopg.Connection, *, job_id: str, error: str) -> bool:
    """Hand a job back to the queue after a transient failure."""
    return _settle(
        conn,
        verb="release",
        job_id=job_id,
        assignments="state = 'queued', last_error = %(error)s",
        params={"error": error},
    )
