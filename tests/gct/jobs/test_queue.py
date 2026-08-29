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

from gct.jobs.queue import Job, claim, complete, enqueue, fail, reclaim_expired, release


def _lecture(tmp_path: Path) -> Path:
    """A real file on disk, so `staging_ref`'s `resolve()` has something honest to resolve.

    `enqueue` never opens the file — it only derives two column values from the path — but a
    path that exists keeps the test from passing for a reason that would not survive Slice 3,
    when a real stager puts real bytes behind this argument.
    """
    source = tmp_path / "lecture-3.pdf"
    source.write_bytes(b"%PDF-1.4 not a real pdf")
    return source


def test_every_writer_refuses_a_connection_already_in_a_transaction(db, db_other, tmp_path):
    """The ADR 0025 precondition, enforced rather than documented (ADR 0027 §Adopted early).

    ONE bare `SELECT` is the whole setup — no write required. That is the trap: psycopg opens
    its implicit transaction on the first statement of any kind, so a caller can arm this
    without doing anything that looks like writing. ADR 0027 records two tests that were
    written against the rule and broke it anyway; this is the version that cannot be ignored.

    EVERY writer, because the guard is only worth anything if it has no gap — one
    unguarded writer is the one a worker will call. This list is a census, not a sample:
    a new verb in this module (`release` arrived with #71 PR 2) belongs on it the same day.
    And `enqueue` is checked for having written NOTHING through `db_other`: a guard that
    raised *after* doing half the work would be worse than none, since the caller would see
    an exception and a partial row.
    """
    conn, owner_id, class_id = db
    source = _lecture(tmp_path)
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    job = claim(conn, lease_seconds=3600)
    assert job is not None

    conn.execute("select 1").fetchone()  # the accidental transaction — a read, not a write
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS

    for name, call in [
        ("enqueue", lambda: enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)),
        ("claim", lambda: claim(conn, lease_seconds=60)),
        ("complete", lambda: complete(conn, job_id=job.job_id, lease_token=job.lease_token)),
        ("fail", lambda: fail(conn, job_id=job.job_id, lease_token=job.lease_token, error="x")),
        (
            "release",
            lambda: release(conn, job_id=job.job_id, lease_token=job.lease_token, error="x"),
        ),
        ("reclaim_expired", lambda: reclaim_expired(conn)),
    ]:
        with pytest.raises(RuntimeError, match=f"{name}\\(\\) requires a connection"):
            call()

    # The remedy is named in the message, not left to the reader to rediscover.
    with pytest.raises(RuntimeError, match="autocommit"):
        claim(conn, lease_seconds=60)

    conn.rollback()
    # Nothing leaked from the refused calls: still one file, and the job is untouched.
    assert (
        db_other.execute("select count(*) from files where owner_id = %s", (owner_id,)).fetchone()[
            0
        ]
        == 1
    ), "a refused enqueue still wrote a files row"
    assert db_other.execute(
        "select state, attempts from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone() == ("processing", 1), "a refused writer still moved the job"


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


def test_claim_hands_back_the_path_so_the_worker_needs_no_second_query(db, tmp_path):
    """`Job.staging_ref` is what makes the ADR 0025 precondition survive contact with a worker.

    `claim` leaving the connection IDLE is worth nothing on its own: `ingest_file` needs a
    `path`, and if the worker has to fetch it, that bare SELECT reopens psycopg's implicit
    transaction and `index_file` degrades to a SAVEPOINT that publishes nothing. Measured
    before this field existed: a real PDF through that flow wrote 62 chunks, `files.status`
    `ready` and `jobs.state` `done`, all invisible to a second connection.

    So the assertion that matters is not just "the field is populated" — it is that the
    connection is STILL IDLE at the moment the worker has everything it needs. Those two
    assertions together are the contract; either alone can hold while the other fails.
    """
    conn, owner_id, class_id = db
    source = _lecture(tmp_path)
    enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    job = claim(conn, lease_seconds=60)

    assert job is not None
    assert job.staging_ref == str(source.resolve()), (
        "claim must hand back the same absolute path enqueue stored — the worker opens this"
    )
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE, (
        "the worker now has path + scope + ids with the connection still outside a "
        "transaction — that pairing is the whole point of carrying staging_ref (ADR 0025)"
    )


def test_claim_does_not_lock_the_files_row_it_reads(db, db_open_txn, tmp_path):
    """`for update OF J` — the join must not lock `files`, only `jobs`.

    An unqualified `for update` across the join would lock the matching `files` row for the
    life of claim's transaction. That row is not the queue's to hold: under at-least-once,
    #71's `index_file` upserts it from another connection moments later. Nothing in the happy
    path notices, which is exactly why this is pinned — the mutation (dropping `of j`) leaves
    every other test in this file green. The fact is the ABSENCE of other coverage, not a
    tally: a count written here goes stale the next time anyone adds a test, and had.

    The frozen second connection takes the `files` row lock FIRST. If claim's SELECT wanted
    that row too it would block and hit the timeout; wanting only the `jobs` row, it sails past.
    """
    conn, owner_id, class_id = db
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)
    held = db_open_txn.execute(
        "select file_id from files where file_id = %s::uuid for update", (file_id,)
    ).fetchone()
    assert held is not None, "the frozen worker never got the files lock — the test is not testing"

    conn.execute("set lock_timeout = '2s'")
    conn.commit()  # `set` opened an implicit transaction; claim must start outside one

    job = claim(conn, lease_seconds=60)

    assert job is not None and job.file_id == file_id, (
        "claim blocked on (or skipped) a job whose FILES row was locked — the row lock is "
        "too broad; `for update of j` is what keeps it to the jobs row"
    )


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


def _stall_and_rehand(conn, file_id: str) -> tuple[Job, Job]:
    """Claim, let the lease expire, reap, re-claim — the `(stalled, holder)` pair at the heart
    of every at-least-once test in this file.

    Named because it is the branch's central scenario and was spelled out four different ways.
    Both jobs are returned because which one a test needs differs: the STALLED one is the
    zombie whose writes must be refused, the HOLDER is the run that must survive them. They
    share a `job_id` and differ in `lease_token` — that difference is the whole point, so the
    assertion that they do is here rather than repeated at every call site.
    """
    stalled = claim(conn, lease_seconds=3600)
    assert stalled is not None
    _backdate_lease(conn, file_id)
    assert reclaim_expired(conn) == 1
    holder = claim(conn, lease_seconds=3600)
    assert holder is not None and holder.job_id == stalled.job_id, "the same job came back around"
    assert holder.lease_token != stalled.lease_token, (
        "claim must mint a FRESH token per claim - reusing it would make the guard decorative"
    )
    return stalled, holder


def _backdate_lease(conn, file_id: str) -> None:
    """Push a claimed job's lease an hour into the past — a worker that died mid-job.

    Expiry is simulated by editing the timestamp rather than claiming with a tiny
    `lease_seconds` and sleeping: no wall-clock wait, and the lease is unambiguously
    expired rather than racing the test's own speed. Commits, so `reclaim_expired`
    starts outside a transaction (its contract; ADR 0025).
    """
    conn.execute(
        "update jobs set leased_until = now() - interval '1 hour' where file_id = %s::uuid",
        (file_id,),
    )
    conn.commit()


def test_reclaim_expired_requeues_only_the_expired_lease(db, db_other, tmp_path):
    """The WHERE's precision, from both sides: expired requeued, live lease untouched.

    The untouched half is the one the handoff warns is easy to skip: a too-broad WHERE
    yanks a job from a healthy worker mid-run, and a test with only expired jobs in the
    table would never notice. So one lease is pushed into the past and one is live, and
    the assertions are about BOTH.

    Also pins what reclaim writes (through `db_other` — publication, not computation):
    state back to `queued`, lease CLEARED, and `attempts` NOT reset — the crashed run
    spent an attempt, and zeroing the trail would hand a poison file a fresh budget
    after every crash.
    """
    conn, owner_id, class_id = db
    source = _lecture(tmp_path)
    first = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    second = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    crashed = claim(conn, lease_seconds=3600)
    healthy = claim(conn, lease_seconds=3600)
    assert crashed is not None and crashed.file_id == first
    assert healthy is not None and healthy.file_id == second
    _backdate_lease(conn, first)

    moved = reclaim_expired(conn)

    assert moved == 1, "exactly one lease was expired, so exactly one row moves"
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    requeued = db_other.execute(
        "select state, leased_until, attempts from jobs where job_id = %s::uuid",
        (crashed.job_id,),
    ).fetchone()
    assert requeued == ("queued", None, 1), (
        "the expired job must be republished as queued, lease cleared, attempts KEPT"
    )
    untouched = db_other.execute(
        "select state, leased_until is not null from jobs where job_id = %s::uuid",
        (healthy.job_id,),
    ).fetchone()
    assert untouched == ("processing", True), (
        "reclaim touched a live lease — it just yanked a job from a healthy worker"
    )


def test_reclaim_expired_leaves_terminal_states_alone(db, db_other, tmp_path):
    """`state = 'processing'` in the WHERE is load-bearing: done jobs stay done.

    `complete`/`fail` never clear `leased_until`, so every terminal row carries an
    expired lease forever. A reclaim that filtered on the lease alone would resurrect
    the entire job history back to `queued` on every tick — infinite reprocessing.
    The terminal row here is written directly (state='done', stale lease left in
    place), which is exactly the shape `complete` will publish.
    """
    conn, owner_id, class_id = db
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)
    job = claim(conn, lease_seconds=3600)
    assert job is not None
    conn.execute("update jobs set state = 'done' where job_id = %s::uuid", (job.job_id,))
    conn.commit()
    _backdate_lease(conn, file_id)

    assert reclaim_expired(conn) == 0, "a terminal job with a stale lease was resurrected"

    state = db_other.execute(
        "select state from jobs where job_id = %s::uuid", (job.job_id,)
    ).fetchone()[0]
    assert state == "done"


def test_attempts_accumulate_across_a_claim_reclaim_claim_cycle(db, db_other, tmp_path):
    """The retry trail: claim → crash → reclaim → claim again reads as attempt 2.

    This is the budget #71 spends. Each half of the guarantee lives in a different
    function — `claim` bumps, `reclaim_expired` preserves — and only the cycle
    exercises them together: a reset hiding in reclaim passes every single-function
    test and still hands a poison file an infinite budget.
    """
    conn, owner_id, class_id = db
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)

    first_try = claim(conn, lease_seconds=3600)
    assert first_try is not None and first_try.attempts == 1
    _backdate_lease(conn, file_id)
    assert reclaim_expired(conn) == 1

    second_try = claim(conn, lease_seconds=3600)

    assert second_try is not None
    assert second_try.job_id == first_try.job_id, "the same job came back around"
    assert second_try.attempts == 2, "the reclaim reset the attempts trail"
    published = db_other.execute(
        "select attempts from jobs where job_id = %s::uuid", (second_try.job_id,)
    ).fetchone()[0]
    assert published == 2


def test_a_zombie_worker_cannot_overwrite_the_run_that_won(db, db_other, tmp_path):
    """Only the current leaseholder may finish a job — the two-condition guard in `_settle`.

    The scenario `reclaim_expired`'s own docstring predicts, driven end to end: worker A
    claims, stalls past its lease, the reaper re-queues, worker B claims the SAME job and
    succeeds. Then A wakes up and calls `fail` — with the job_id it was legitimately handed.
    Every argument here is correct; nothing is malformed. Without that guard the row ends
    `failed` for a file that was ingested successfully, and `jobs.state` becomes permanently
    wrong about a run that worked.

    WHAT THIS TEST DOES AND DOES NOT PIN, because the distinction is easy to lose: it drives
    the whole sequence end to end, which is its value — but it survives dropping EITHER half of
    the guard on its own, because here the winner has already FINISHED, so the state condition
    alone refuses the zombie and so does the token condition alone. The halves are pinned
    individually elsewhere: `test_complete_and_fail_distinguish_a_lost_lease_from_a_job_that_
    never_existed` for the state half, and `test_release_refuses_a_job_another_worker_now_holds`
    — where the winner is still RUNNING — for the token. Do not read this as coverage of either.

    Both directions are asserted, because they cost differently. A zombie `fail` over a
    genuine success is the damaging one; a zombie `complete` over a genuine failure is the
    mirror, and a guard that only covered `fail` would pass a one-sided test forever.
    """
    conn, owner_id, class_id = db
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)
    zombie, winner = _stall_and_rehand(conn, file_id)

    complete(conn, job_id=winner.job_id, lease_token=winner.lease_token)  # the run that holds it
    fail(
        conn,
        job_id=zombie.job_id,
        lease_token=zombie.lease_token,
        error="zombie woke up long after losing its lease",
    )

    survived = db_other.execute(
        "select state, last_error from jobs where job_id = %s::uuid", (winner.job_id,)
    ).fetchone()
    assert survived == ("done", None), (
        "a worker whose lease had already been reclaimed overwrote the terminal state of the "
        "run that won — jobs.state now reports `failed` for a file that ingested fine"
    )

    # The mirror: a zombie `complete` must not bury a genuine failure either.
    second = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)
    ghost, retry = _stall_and_rehand(conn, second)
    assert ghost.file_id == second

    fail(conn, job_id=retry.job_id, lease_token=retry.lease_token, error="the real run failed")
    complete(conn, job_id=ghost.job_id, lease_token=ghost.lease_token)

    assert db_other.execute(
        "select state, last_error from jobs where job_id = %s::uuid", (retry.job_id,)
    ).fetchone() == ("failed", "the real run failed"), (
        "a zombie's `complete` buried the real run's failure — the student would be told the "
        "file is fine while nothing was indexed"
    )


def test_complete_and_fail_distinguish_a_lost_lease_from_a_job_that_never_existed(
    db, db_other, tmp_path
):
    """Writing nothing has two causes, and reporting them identically is the defect.

    A lost lease is ROUTINE — the state guard makes it so, and a reaper reclaiming a slow
    worker is a normal poll cycle, not an error. Raising on it would turn ordinary operation
    into an exception. So: `False`.

    An unknown `job_id` is a PROGRAMMING ERROR — the caller believes a job finished that does
    not exist, and the cost of staying quiet is a re-ingest per lease expiry until #71's retry
    budget terminal-fails the file for a reason unrelated to what went wrong. So: `LookupError`.

    Both are pinned for BOTH functions. A three-way split that held for `complete` and not
    `fail` would be read as meaning something, and the `fail` direction is the one that
    discards evidence — the error message goes nowhere if nothing is written.
    """
    conn, owner_id, class_id = db
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)
    job = claim(conn, lease_seconds=3600)
    assert job is not None

    # 1. The happy path returns True — this call is the one that finished it.
    assert complete(conn, job_id=job.job_id, lease_token=job.lease_token) is True

    # 2. A second call writes nothing: the row is no longer `processing`. Not an error.
    assert complete(conn, job_id=job.job_id, lease_token=job.lease_token) is False, (
        "a worker that no longer holds the lease must be told so, not told it succeeded"
    )
    assert fail(conn, job_id=job.job_id, lease_token=job.lease_token, error="too late") is False
    assert db_other.execute(
        "select state, last_error from jobs where job_id = %s::uuid", (job.job_id,)
    ).fetchone() == ("done", None), "a False return still wrote something"

    # 3. A job_id naming nothing at all is loud, on both paths.
    ghost = str(uuid.uuid4())
    with pytest.raises(LookupError, match=ghost):
        complete(conn, job_id=ghost, lease_token=str(uuid.uuid4()))
    with pytest.raises(LookupError, match=ghost):
        fail(conn, job_id=ghost, lease_token=str(uuid.uuid4()), error="into the void")

    # The raise must not leave the connection mid-transaction — every writer here owes
    # `index_file` an IDLE connection (ADR 0025), and an exception path is the easiest
    # place to lose that.
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE

    # And the real fail path still returns True and records its message.
    second = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)
    live = claim(conn, lease_seconds=3600)
    assert live is not None and live.file_id == second
    assert (
        fail(conn, job_id=live.job_id, lease_token=live.lease_token, error="parse blew up") is True
    )
    assert file_id != second


def test_complete_publishes_done_and_touches_only_its_job(db, db_other, tmp_path):
    """Terminal success is published, scoped to ONE job, and leaves `files.status` alone.

    The sibling job is the test's teeth: with a single row in the table, a `complete`
    that lost its WHERE clause — every job marked done — would still pass. The
    `files.status` assertion pins the two-axes split one more time: flipping the
    student-facing status to `ready` is `index_file`'s job, inside the same
    transaction as the chunk insert, never the queue's.
    """
    conn, owner_id, class_id = db
    source = _lecture(tmp_path)
    first = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    second = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    job = claim(conn, lease_seconds=3600)
    assert job is not None and job.file_id == first

    complete(conn, job_id=job.job_id, lease_token=job.lease_token)

    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    done = db_other.execute(
        "select state, last_error from jobs where job_id = %s::uuid", (job.job_id,)
    ).fetchone()
    assert done == ("done", None), "complete was not published, or invented an error"
    sibling = db_other.execute(
        "select state from jobs where file_id = %s::uuid", (second,)
    ).fetchone()[0]
    assert sibling == "queued", "complete touched a job it was not given"
    file_status = db_other.execute(
        "select status from files where file_id = %s::uuid", (first,)
    ).fetchone()[0]
    assert file_status == "queued", "complete wrote files.status — that axis belongs to #71"


def test_fail_publishes_failed_with_the_error_and_keeps_attempts(db, db_other, tmp_path):
    """Terminal failure carries its evidence: the message survives, and so does the trail.

    `last_error` is the free-text WHY a human reads off a dead job; `attempts` is how
    many tries it burned. A `fail` that dropped either would leave a corpse with no
    autopsy. Scoping teeth again via the untouched sibling.
    """
    conn, owner_id, class_id = db
    source = _lecture(tmp_path)
    first = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    second = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    job = claim(conn, lease_seconds=3600)
    assert job is not None and job.file_id == first

    fail(
        conn,
        job_id=job.job_id,
        lease_token=job.lease_token,
        error="embed exploded: dimension mismatch",
    )

    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    dead = db_other.execute(
        "select state, last_error, attempts from jobs where job_id = %s::uuid", (job.job_id,)
    ).fetchone()
    assert dead == ("failed", "embed exploded: dimension mismatch", 1), (
        "fail was not published with its error and the attempts trail intact"
    )
    sibling = db_other.execute(
        "select state, last_error from jobs where file_id = %s::uuid", (second,)
    ).fetchone()
    assert sibling == ("queued", None), "fail touched a job it was not given"


def test_release_requeues_the_job_and_keeps_the_attempts_it_burned(db, db_other, tmp_path):
    """The non-terminal outcome: claimable again, with the retry trail intact (#71 PR 2).

    Three things have to be true together, and each one is a different bug if it is not:
      - `state` back to `queued`, PUBLISHED — a release visible only to the releasing
        connection strands the job in `processing` for every other reader (ADR 0025);
      - `leased_until` cleared — a requeued row carrying a live lease is a lie about a worker
        that no longer holds it, and unlike `complete`/`fail` this row is going somewhere a
        reader acts on;
      - `attempts` NOT given back — it is the budget the worker counts against, and a release
        that decremented it would hand a poison file an unlimited retry allowance, the same
        failure `reclaim_expired` avoids by not resetting it.
    The re-claim at the end is the one that proves it: the job comes back with attempts == 2,
    so the budget is genuinely spending down across claims rather than resetting each time.
    """
    conn, owner_id, class_id = db
    source = _lecture(tmp_path)
    first = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    second = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    job = claim(conn, lease_seconds=3600)
    assert job is not None and job.file_id == first

    assert (
        release(
            conn,
            job_id=job.job_id,
            lease_token=job.lease_token,
            error="transient: provider said 429",
        )
        is True
    )

    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    requeued = db_other.execute(
        """
        select state, leased_until, last_error, attempts
        from jobs where job_id = %s::uuid
        """,
        (job.job_id,),
    ).fetchone()
    assert requeued == ("queued", None, "transient: provider said 429", 1), (
        "release must publish a claimable row that still remembers what it cost"
    )
    # Scoping teeth, same as complete/fail: with one row in the table a lost WHERE clause
    # would pass. The sibling's `last_error` is the half that catches a stray UPDATE.
    sibling = db_other.execute(
        "select state, last_error from jobs where file_id = %s::uuid", (second,)
    ).fetchone()
    assert sibling == ("queued", None), "release touched a job it was not given"
    # `files.status` is the other axis and stays where #71's worker left it — release is queue
    # machinery and writes nothing a student sees.
    assert (
        db_other.execute("select status from files where file_id = %s::uuid", (first,)).fetchone()[
            0
        ]
        == "queued"
    ), "release wrote files.status — that axis belongs to the worker"

    again = claim(conn, lease_seconds=3600)
    assert again is not None and again.job_id == job.job_id
    assert again.attempts == 2, "the retry budget did not spend down across the release"


def test_release_distinguishes_a_lost_lease_from_a_job_that_never_existed(db, db_other, tmp_path):
    """The same three-way split `complete` and `fail` make, for the same reasons.

    Kept symmetrical deliberately: a worker handles all three outcomes in one place, and a
    split that differed for the requeue path would be read as meaning something. The lost-lease
    direction matters more here than for `fail` — a zombie whose lease expired dragging a
    FINISHED job back to `queued` would re-ingest a file that is already `ready`, paying the
    embedding bill again for nothing.
    """
    conn, owner_id, class_id = db
    enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)
    job = claim(conn, lease_seconds=3600)
    assert job is not None
    assert complete(conn, job_id=job.job_id, lease_token=job.lease_token) is True

    assert (
        release(conn, job_id=job.job_id, lease_token=job.lease_token, error="too late") is False
    ), "a zombie must not be able to resurrect the job the winning run finished"
    assert db_other.execute(
        "select state, last_error from jobs where job_id = %s::uuid", (job.job_id,)
    ).fetchone() == ("done", None), "a False return still wrote something"

    ghost = str(uuid.uuid4())
    with pytest.raises(LookupError, match=ghost):
        release(conn, job_id=ghost, lease_token=str(uuid.uuid4()), error="into the void")
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_release_refuses_a_job_another_worker_now_holds(db, db_other, tmp_path):
    """The half the lost-lease test above does NOT cover: the winner is still RUNNING.

    `test_release_distinguishes_a_lost_lease_from_a_job_that_never_existed` proves a zombie
    cannot resurrect a job the winner already FINISHED - there the row reads `done`, so any
    guard at all refuses it. This is the other, likelier shape: worker B claimed the job
    seconds ago and is still embedding, so the row reads `processing` again. Under the
    state-only guard that shipped in #70/#71 PR 1 the zombie's write passed - measured, not
    argued: it returned True and left the row `queued` with B's lease erased.

    That is the expensive direction, and it is why `release` is the verb the token was added
    for: it is the one that puts a job back into circulation rather than ending it.
    migrations/0002_lease_token.sql carries what that costs — not restated here, because
    three copies of one argument is three places for it to drift.

    The sibling assertions matter as much as the return value: a guard that refused the write
    but had already clobbered `leased_until` or `last_error` would satisfy `is False` and still
    have taken B's lease away.
    """
    conn, owner_id, class_id = db
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)

    stalled, holder = _stall_and_rehand(conn, file_id)

    # The stalled worker wakes up mid-ingest, hits a transient error, and hands "its" job back.
    assert (
        release(
            conn,
            job_id=stalled.job_id,
            lease_token=stalled.lease_token,
            error="stale worker's transient error",
        )
        is False
    ), "a worker whose lease was reclaimed requeued a job another worker is running"

    state, leased_until, last_error = db_other.execute(
        "select state, leased_until, last_error from jobs where job_id = %s::uuid",
        (holder.job_id,),
    ).fetchone()
    assert state == "processing", "the holder's job was dragged back into the claimable pool"
    assert leased_until is not None, "the holder's lease was erased by a worker that lost it"
    assert last_error is None, "a refused release still wrote its error over the holder's row"

    # And the current holder is unaffected - it can still settle its own job normally.
    assert (
        release(conn, job_id=holder.job_id, lease_token=holder.lease_token, error="transient: 429")
        is True
    ), "the token guard refused the worker that actually holds the lease"
