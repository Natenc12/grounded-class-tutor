"""DB-backed tests for the job queue (issue #70, ADR 0011).

Runs against the real local Postgres via the `db` fixture (seeds a `classes` row, cleans up by
`owner_id`, skips *locally* when the DB is unreachable — but hard-fails in CI, where the service
container guarantees one).

Every assertion about what `enqueue` WROTE goes through `db_other`, a second connection. A
connection sees its own uncommitted work, so reading back through `db` would hold whether or not
anything was published — the exact green-while-publishing-nothing failure ADR 0025 describes and
`db_other` exists to catch. This module is the first caller of `enqueue`, which is the table's
first writer, so there is no prior test to inherit that habit from.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest

from gct.jobs.queue import claim, enqueue


def _lecture(tmp_path: Path) -> Path:
    """A real file on disk, so `staging_ref`'s `resolve()` has something honest to resolve.

    `enqueue` never opens the file — it only derives two column values from the path — but a
    path that exists keeps the test from passing for a reason that would not survive Slice 3,
    when a real stager puts real bytes behind this argument.
    """
    source = tmp_path / "lecture-3.pdf"
    source.write_bytes(b"%PDF-1.4 not a real pdf")
    return source


def test_enqueue_publishes_both_rows(db, db_other, tmp_path):
    """The core contract: one call, two rows, both visible to a DIFFERENT connection.

    Asserts through `db_other` throughout — the point of the test is not that `enqueue` computed
    the right values but that it PUBLISHED them. Also pins the two `jobs` columns whose defaults
    the code deliberately does not spell out (`state`, `attempts`): they are owned by
    migrations/0001_init.sql, and this is where a schema change to either would be caught.
    """
    conn, owner_id, class_id = db
    source = _lecture(tmp_path)

    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    file_row = db_other.execute(
        "select owner_id, class_id::text, filename, staging_ref, status, failed_reason "
        "from files where file_id = %s::uuid",
        (file_id,),
    ).fetchone()
    assert file_row is not None, "the files row was not published to another connection"
    assert file_row == (
        owner_id,
        class_id,
        # The BASENAME, not the path: this column is denormalized onto every chunk as the
        # citation label, so a full path would render the uploader's home directory into
        # every citation the product shows.
        "lecture-3.pdf",
        # The ABSOLUTE path — what the worker opens (decided 2026-08-03). Compared against a
        # resolved literal because `resolve()` also follows symlinks: on macOS `tmp_path` sits
        # under /var, which is a symlink to /private/var, so the stored value is not the string
        # `tmp_path` prints.
        str(source.resolve()),
        "queued",
        None,
    )

    job_row = db_other.execute(
        "select file_id::text, owner_id, class_id::text, state, attempts, leased_until, last_error "
        "from jobs where file_id = %s::uuid",
        (file_id,),
    ).fetchone()
    assert job_row is not None, "the jobs row was not published to another connection"
    assert job_row == (file_id, owner_id, class_id, "queued", 0, None, None)


def test_enqueue_returns_the_id_of_the_row_it_wrote(db, db_other, tmp_path):
    """The return value is the caller's handle — it must name THIS row, not merely some row.

    The failure this guards is specific and was live in an earlier draft: recovering the id with
    `select file_id from files where filename = %s` returns an arbitrary row when the same
    filename is enqueued twice, and is not scoped to the owner at all. Two enqueues of the SAME
    file therefore have to come back with two different ids, each matching its own `jobs` row.
    """
    conn, owner_id, class_id = db
    source = _lecture(tmp_path)

    first = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    second = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    assert first != second, "two enqueues of the same file returned the same file_id"
    assert isinstance(first, str), "callers pass this straight to ingest_file, which takes a str"
    # And each job points at its own file — not both at whichever row the DB happened to return.
    job_ids = db_other.execute(
        "select file_id::text from jobs where owner_id = %s order by created_at",
        (owner_id,),
    ).fetchall()
    assert sorted(row[0] for row in job_ids) == sorted([first, second])


def test_enqueue_accepts_a_str_path(db, db_other, tmp_path):
    """`path: str | Path` means both, and a `Path` is not something psycopg can adapt.

    Passing the raw argument into the query raises `ProgrammingError: cannot adapt type
    'PosixPath'` — so the signature's promise only holds because `enqueue` converts first. A
    str-shaped call is the half a Path-shaped test cannot cover.
    """
    conn, owner_id, class_id = db
    source = _lecture(tmp_path)

    file_id = enqueue(conn, path=str(source), owner_id=owner_id, class_id=class_id)

    stored = db_other.execute(
        "select filename, staging_ref from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert stored == ("lecture-3.pdf", str(source.resolve()))


def test_enqueue_is_atomic_when_the_jobs_insert_fails(db, db_other, tmp_path, monkeypatch):
    """A failure after the files insert must leave NO files row behind.

    This is the invariant the docstring leads with, and the one whose violation is silent: a
    committed `files` row with no `jobs` row is a file stuck in `queued` that no worker will ever
    claim, that no reaper will ever notice, and that the student watches forever.

    The failure is INJECTED here, unlike `test_index.py`'s all-or-nothing tests, which get
    Postgres itself to reject a wrong-dimension vector mid-write and monkeypatch nothing. There
    is no equivalent input for this function: every column `jobs` constrains is constrained more
    tightly on `files` — `class_id` FKs to `classes` there and nowhere on `jobs`, `file_id` comes
    from the files insert's own RETURNING — so any argument bad enough to break the second
    statement breaks the first one first, and the test would prove nothing. Patching the second
    `execute` is the smallest injection that reaches the gap between the two writes.
    """
    conn, owner_id, class_id = db
    source = _lecture(tmp_path)
    real_execute = conn.execute
    calls = 0

    def fail_on_the_jobs_insert(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected: the jobs insert failed")
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(conn, "execute", fail_on_the_jobs_insert)

    with pytest.raises(RuntimeError, match="injected"):
        enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    monkeypatch.undo()
    # Read through the OTHER connection: `conn` has rolled back, so it would report an empty
    # table whether or not the row was ever committed. Only a second connection distinguishes
    # "never published" from "published then discarded on this connection".
    assert (
        db_other.execute("select count(*) from files where owner_id = %s", (owner_id,)).fetchone()[
            0
        ]
        == 0
    ), "the files row survived a failed enqueue — a file no worker will ever claim"
    assert (
        db_other.execute("select count(*) from jobs where owner_id = %s", (owner_id,)).fetchone()[0]
        == 0
    )


def test_enqueue_rejects_a_class_that_does_not_exist(db, db_other, tmp_path):
    """Scope is not this function's to invent: an unknown `class_id` is refused by the FK.

    `files.class_id` references `classes(class_id)`, so this fails loudly at the database rather
    than creating an orphan file under a class nobody owns. Worth pinning because `jobs.class_id`
    carries NO foreign key — the constraint lives entirely on the row written first, and a later
    reordering of the two inserts would silently drop it.
    """
    conn, owner_id, _ = db
    source = _lecture(tmp_path)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        enqueue(conn, path=source, owner_id=owner_id, class_id=str(uuid.uuid4()))

    conn.rollback()
    assert (
        db_other.execute("select count(*) from files where owner_id = %s", (owner_id,)).fetchone()[
            0
        ]
        == 0
    )


def test_claim_returns_the_oldest_queued_job(db, db_other, tmp_path):
    """FIFO by `created_at`: the job that has waited longest wins — and the other is untouched.

    Enqueueing two jobs in order cannot prove the ORDER BY: rows inserted microseconds apart
    come back in insertion order from a plain scan anyway, so the test would stay green with the
    clause deleted. The SECOND enqueue is therefore backdated an hour — only ordering by
    `created_at` finds it.

    The sibling assertion (first job still `queued`, no lease) is the half that catches a
    too-broad UPDATE: a WHERE that matches more than the one selected row would stamp both.
    """
    conn, owner_id, class_id = db
    source = _lecture(tmp_path)
    first = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    second = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    conn.execute(
        "update jobs set created_at = created_at - interval '1 hour' where file_id = %s::uuid",
        (second,),
    )
    conn.commit()  # `claim` must start OUTSIDE a transaction (its docstring; ADR 0025)

    job = claim(conn, lease_seconds=60)

    assert job is not None
    assert job.file_id == second, "claim did not follow created_at to the oldest job"
    assert (job.owner_id, job.class_id) == (owner_id, class_id)
    assert job.attempts == 1, "the returned attempts must COUNT this claim (post-bump)"
    stored_job_id = db_other.execute(
        "select job_id::text from jobs where file_id = %s::uuid", (second,)
    ).fetchone()[0]
    assert job.job_id == stored_job_id

    untouched = db_other.execute(
        "select state, attempts, leased_until from jobs where file_id = %s::uuid", (first,)
    ).fetchone()
    assert untouched == ("queued", 0, None), "claim touched a job it did not return"


def test_claim_publishes_the_lease_before_returning(db, db_other, tmp_path):
    """The commit-before-return contract (decided 2026-08-11), pinned from both sides.

    Through `db_other`: the processing state, the attempts bump, and a lease inside
    (now, now + lease_seconds] are visible to a DIFFERENT connection with no commit from this
    test — so `claim` published them itself. A lease only this connection can see is not a
    lease; `reclaim_expired` and every other worker read it from elsewhere.

    On `db`'s own connection: transaction status is back to IDLE. That is `index_file`'s
    ADR 0025 precondition — a claim that returned INTRANS would make the worker's very next
    ingest on this connection publish nothing while reporting success, and #74 deliberately
    left that unguarded at runtime.

    Also pins that `files.status` still reads `queued`: every transition after `queued` on
    that axis belongs to #71, so claim writing it would be a second writer.
    """
    conn, owner_id, class_id = db
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)

    job = claim(conn, lease_seconds=300)

    assert job is not None
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE, (
        "claim returned with a transaction still open — the next ingest on this "
        "connection would degrade to a savepoint and publish nothing (ADR 0025)"
    )
    published = db_other.execute(
        """
        select state, attempts, leased_until > now(),
               leased_until <= now() + make_interval(secs => %(lease_seconds)s)
        from jobs where job_id = %(job_id)s::uuid
        """,
        {"lease_seconds": 300, "job_id": job.job_id},
    ).fetchone()
    assert published == ("processing", 1, True, True), (
        "the claim was not published to another connection, or the lease is not "
        "inside (now, now + lease_seconds]"
    )
    file_status = db_other.execute(
        "select status from files where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    assert file_status == "queued", "claim wrote files.status — that axis belongs to #71"


def test_claim_on_an_empty_queue_returns_none(db):
    """Empty is the poll loop's most common outcome — a result, not an error.

    (If this ever fails with a Job, look for stray `queued` rows in the local `jobs` table —
    claim is deliberately unscoped, so leftovers from a crashed run are visible to it.)

    The IDLE check matters on this path too: the None branch still opened a read transaction,
    and leaving it open would trip ADR 0025 on the tick that DOES find a job.
    """
    conn, _, _ = db

    assert claim(conn, lease_seconds=60) is None
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_claim_skips_a_job_a_concurrent_claimer_holds_locked(db, db_open_txn, tmp_path):
    """Two workers, one job, zero double-claims — the SKIP LOCKED half of ADR 0011.

    Exactly ONE job is enqueued, so the test cannot pass because two claimers happened to be
    handed different jobs — with one job there is no accidental way through. `db_open_txn`
    plays a worker frozen mid-claim: it holds the row lock claim's SELECT takes, in a
    transaction that has not committed. (`db_other` cannot play this part — it is autocommit,
    so its locks die inside the statement that takes them; see the fixture docstring.)

    The lock_timeout is mutation insurance, not contract: with SKIP LOCKED deleted from the
    source the second claim BLOCKS on the held lock, and without the timeout that mutation
    would hang the suite instead of failing it.
    """
    conn, owner_id, class_id = db
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)

    held = db_open_txn.execute(
        "select job_id from jobs where file_id = %s::uuid for update", (file_id,)
    ).fetchone()
    assert held is not None, "the frozen worker never got the lock — the test is not testing"

    conn.execute("set lock_timeout = '2s'")
    conn.commit()  # `set` opened an implicit transaction; claim must start outside one

    assert claim(conn, lease_seconds=60) is None, (
        "claim was handed a job another worker holds locked — SKIP LOCKED is not doing its job"
    )

    db_open_txn.rollback()  # the frozen worker dies; its lock releases
    job = claim(conn, lease_seconds=60)
    assert job is not None and job.file_id == file_id, (
        "the None above was not about the lock — the job should be claimable once it releases"
    )
