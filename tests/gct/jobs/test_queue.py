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

from gct.db import connect
from gct.jobs.queue import (
    claim,
    complete,
    enqueue,
    fail,
    holds_worker_lock,
    release,
    requeue_orphans,
    try_acquire_worker_lock,
)


def _lecture(tmp_path: Path) -> Path:
    """A real file on disk, so `staging_ref`'s `resolve()` has something honest to resolve.

    `enqueue` never opens the file — it only derives two column values from the path — but a
    path that exists keeps the test from passing for a reason the real caller would not supply:
    `gct.staging.stage` (#105) writes bytes to disk and hands `enqueue` the resolved path.
    """
    source = tmp_path / "lecture-3.pdf"
    source.write_bytes(b"%PDF-1.4 not a real pdf")
    return source


def test_every_writer_refuses_a_connection_already_in_a_transaction(worker_db, db_other, tmp_path):
    """ADR 0025/0027. A census of this module's writers: a new verb belongs on it the same day."""
    conn, owner_id, class_id = worker_db
    source = _lecture(tmp_path)
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    job = claim(conn)
    assert job is not None

    conn.execute("select 1").fetchone()  # the accidental transaction — a read, not a write
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS

    for name, call in [
        ("enqueue", lambda: enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)),
        ("try_acquire_worker_lock", lambda: try_acquire_worker_lock(conn)),
        ("requeue_orphans", lambda: requeue_orphans(conn)),
        ("claim", lambda: claim(conn)),
        ("complete", lambda: complete(conn, job_id=job.job_id)),
        ("fail", lambda: fail(conn, job_id=job.job_id, error="x")),
        ("release", lambda: release(conn, job_id=job.job_id, error="x")),
    ]:
        with pytest.raises(RuntimeError, match=f"{name}\\(\\) requires a connection"):
            call()

    with pytest.raises(RuntimeError, match="autocommit"):
        claim(conn)

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
        "select file_id::text, owner_id, class_id::text, state, attempts, last_error "
        "from jobs where file_id = %s::uuid",
        (file_id,),
    ).fetchone()
    assert job_row is not None, "the jobs row was not published to another connection"
    assert job_row == (file_id, owner_id, class_id, "queued", 0, None)


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
    claim, and that the student watches forever.

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


# WHICH SPELLING ACTUALLY BITES, asserted rather than asserted-about. `uuid.UUID` parses four
# forms of one id; Postgres's `::uuid` cast parses three. Pinning the split here is what keeps the
# test below from being trivially green: without it, a `urn` case that passed would be
# indistinguishable from a `urn` Postgres had quietly started accepting.
_POSTGRES_ACCEPTS_THE_CAST = {"plain": True, "braced": True, "hex32": True, "urn": False}


@pytest.mark.parametrize("spelling", ["plain", "braced", "hex32", "urn"])
def test_every_uuid_spelling_python_accepts_publishes_one_canonical_row(
    db, db_other, tmp_path, spelling
):
    """`enqueue` canonicalises `class_id` at its own boundary, so no caller has to (#121).

    `class_exists` normalises before it binds and answers True for all four spellings; `enqueue`
    bound the caller's string RAW into `%(class_id)s::uuid`, so `urn:uuid:<id>` cleared every
    ownership check in the library and then aborted the INSERT with an `InvalidTextRepresentation`
    naming a Postgres type. The route (#110) worked around it by parsing once at the top, which
    protected exactly one caller; the next one - a script, the Slice 3 exit smoke - hit it again.

    PARAMETRIZED, not looped. Parametrizing gives each spelling its OWN `db` fixture, hence its
    own `owner_id`, which is what makes "exactly one row" a real assertion instead of a running
    count.

    WHAT EACH CASE ACTUALLY BUYS, measured rather than assumed, because a maintainer trimming this
    test will trim it on whatever this docstring says. Only `urn` is sensitive to the fix under
    review: revert `enqueue` to binding raw and that case alone goes red. Postgres normalises the
    braced and bare-32-hex spellings ITSELF, so any fix that leaves them raw still publishes a
    canonical row - a urn-only special case passes all four, which was measured and is why the
    earlier claim that it "would still mangle the braced form" is not the reason to keep them.
    They pin the OTHER decision: that `enqueue` is LENIENT. Swap in `ingest_file`-style strict
    rejection of any non-canonical spelling and braced, hex32 and urn all go red together. Two
    different guards, one parametrization.

    Read back through `db_other`, a SECOND connection: `db`'s own connection sees its uncommitted
    work, so the canonical value would read back correctly whether or not it was ever published.
    """
    conn, owner_id, class_id = db
    source = _lecture(tmp_path)
    canonical = uuid.UUID(class_id)
    written = {
        "plain": str(canonical),
        "braced": f"{{{canonical}}}",
        "hex32": canonical.hex,
        "urn": canonical.urn,
    }[spelling]
    assert uuid.UUID(written) == canonical, "the spelling under test names a different class"

    # Measured on `db_other`, which is autocommit - a refused cast is its own transaction there and
    # cannot poison anything this test does later.
    try:
        db_other.execute("select %s::uuid", (written,)).fetchone()
        postgres_accepts = True
    except psycopg.errors.InvalidTextRepresentation:
        postgres_accepts = False
    assert postgres_accepts is _POSTGRES_ACCEPTS_THE_CAST[spelling], (
        f"Postgres's own handling of the {spelling} spelling changed; this test's premise is stale"
    )

    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=written)

    published = db_other.execute(
        "select file_id::text, class_id::text, status from files where owner_id = %s",
        (owner_id,),
    ).fetchall()
    assert len(published) == 1, f"the {spelling} spelling published {len(published)} files rows"
    assert published[0] == (file_id, str(canonical), "queued")
    assert db_other.execute(
        "select class_id::text from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone() == (str(canonical),), "the jobs row carries a different class_id than the file"


def test_enqueue_refuses_a_class_id_that_is_not_a_uuid_and_leaves_the_connection_alone(
    db, db_other, tmp_path
):
    """The other half of #121's acceptance: refused with a remedy, never an opaque `DataError`.

    The remedy names what to PASS - the id `create_class` returned - because a caller holding a
    non-uuid string has no way to derive one. It deliberately does NOT carry the siblings'
    "would abort the connection's open transaction" sentence: `class_exists` and `get_file_status`
    are readers with no transaction of their own, and that IS their reason, but `enqueue` wraps its
    inserts in `conn.transaction()` on a connection `require_idle` has already proven IDLE. The
    two assertions after the raise are that measurement, not decoration - the connection is still
    IDLE and still answering, so a docstring claiming otherwise would be a false one.
    """
    conn, owner_id, _ = db
    source = _lecture(tmp_path)

    with pytest.raises(ValueError, match=r"enqueue\(\) requires a uuid class_id") as caught:
        enqueue(conn, path=source, owner_id=owner_id, class_id="intro-to-religion")

    assert "create_class" in str(caught.value), "the refusal does not name what to pass instead"
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert conn.execute("select 1").fetchone() == (1,), "the refusal cost the caller its connection"
    conn.rollback()
    assert (
        db_other.execute("select count(*) from files where owner_id = %s", (owner_id,)).fetchone()[
            0
        ]
        == 0
    ), "a refused enqueue published a files row"


# WHAT `uuid.UUID` RAISES DEPENDS ON WHAT IT WAS HANDED, and the three cases are not
# interchangeable — which is the whole reason the guard catches three types rather than one.
# The comment on each is the parser's own failure mode, recorded so a future reader can tell
# a deliberate set from a copied one.
_NOT_EVEN_A_STRING = {
    "none": None,  # TypeError: no hex/bytes/fields/int argument was given
    "int": 12345,  # AttributeError: int has no `.replace`
    "list": ["not", "an", "id"],  # AttributeError: list has no `.replace`
    "already_parsed": uuid.UUID("6f1e1b4a-0f0e-4a3e-9a7d-2f0a1b2c3d4e"),  # AttributeError
}


@pytest.mark.parametrize("kind", sorted(_NOT_EVEN_A_STRING))
def test_enqueue_refuses_a_class_id_that_is_not_even_a_string(db, db_other, tmp_path, kind):
    """A non-`str` `class_id` gets the SAME remedy-naming refusal a malformed string gets (#121).

    `uuid.UUID` reports a bad argument three different ways: a malformed string is a `ValueError`,
    `None` is a `TypeError`, and anything without `.replace` — an `int`, a `list`, an already-parsed
    `uuid.UUID` — is an `AttributeError` raised from inside the parser. A guard that caught only
    `ValueError` therefore refused *some* bad ids with a remedy and let the rest escape carrying
    `uuid.UUID`'s own message, which names the parser's internals and nothing a caller can act on.
    `ingest_file` (`gct/ingest/pipeline.py`) already catches all three; this is the same set, and
    the test exists so the set cannot be quietly narrowed back.

    `already_parsed` is the case that will actually happen: a caller holding the `uuid.UUID` it
    just built passes it instead of `str(...)`, and before this it got
    `'UUID' object has no attribute 'replace'`.

    Nothing shipped can reach any of these today — the upload route's `class_id` is a FastAPI Form
    field, hence always a `str` — so this pins a boundary contract, not a live path.
    """
    conn, owner_id, _ = db
    source = _lecture(tmp_path)

    with pytest.raises(ValueError, match=r"enqueue\(\) requires a uuid class_id") as caught:
        enqueue(conn, path=source, owner_id=owner_id, class_id=_NOT_EVEN_A_STRING[kind])

    assert "create_class" in str(caught.value), "the refusal does not name what to pass instead"
    # The connection is untouched because the raise happens before `conn.transaction()` opens.
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert conn.execute("select 1").fetchone() == (1,), "the refusal cost the caller its connection"
    conn.rollback()
    assert (
        db_other.execute("select count(*) from files where owner_id = %s", (owner_id,)).fetchone()[
            0
        ]
        == 0
    ), "a refused enqueue published a files row"


def test_claim_returns_the_oldest_queued_job(worker_db, db_other, tmp_path):
    """The second enqueue is backdated, so only ORDER BY created_at finds it."""
    conn, owner_id, class_id = worker_db
    source = _lecture(tmp_path)
    first = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    second = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    conn.execute(
        "update jobs set created_at = created_at - interval '1 hour' where file_id = %s::uuid",
        (second,),
    )
    conn.commit()  # `claim` must start OUTSIDE a transaction (ADR 0025)

    job = claim(conn)

    assert job is not None
    assert job.file_id == second, "claim did not follow created_at to the oldest job"
    assert (job.owner_id, job.class_id) == (owner_id, class_id)
    assert job.attempts == 1, "the returned attempts must COUNT this claim (post-bump)"
    stored_job_id = db_other.execute(
        "select job_id::text from jobs where file_id = %s::uuid", (second,)
    ).fetchone()[0]
    assert job.job_id == stored_job_id

    untouched = db_other.execute(
        "select state, attempts from jobs where file_id = %s::uuid", (first,)
    ).fetchone()
    assert untouched == ("queued", 0), "claim touched a job it did not return"


def test_claim_skips_a_queued_job_whose_file_row_is_gone(worker_db, tmp_path):
    conn, owner_id, class_id = worker_db
    with conn.transaction():
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "insert into jobs (file_id, owner_id, class_id, created_at)"
            " values (gen_random_uuid(), %s, %s::uuid, now() - interval '1 hour')",
            (owner_id, class_id),
        )
    good = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)

    job = claim(conn)

    assert job is not None and job.file_id == good


def test_claim_hands_back_the_path_with_the_connection_still_idle(worker_db, tmp_path):
    """A SELECT for the path after `claim` would reopen a transaction (ADR 0025)."""
    conn, owner_id, class_id = worker_db
    source = _lecture(tmp_path)
    enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    job = claim(conn)

    assert job is not None
    assert job.staging_ref == str(source.resolve())
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_claim_does_not_block_on_a_locked_files_row(worker_db, db_open_txn, tmp_path):
    """`index_file` upserts the files row from elsewhere; claim must not need its lock."""
    conn, owner_id, class_id = worker_db
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)
    held = db_open_txn.execute(
        "select file_id from files where file_id = %s::uuid for update", (file_id,)
    ).fetchone()
    assert held is not None

    conn.execute("set lock_timeout = '2s'")
    conn.commit()  # `set` opened an implicit transaction; claim must start outside one

    job = claim(conn)

    assert job is not None and job.file_id == file_id


def test_claim_publishes_processing_and_leaves_files_status_alone(worker_db, db_other, tmp_path):
    conn, owner_id, class_id = worker_db
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)

    job = claim(conn)

    assert job is not None
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert db_other.execute(
        "select state, attempts from jobs where job_id = %s::uuid", (job.job_id,)
    ).fetchone() == ("processing", 1), "the claim was not published to another connection"
    assert (
        db_other.execute(
            "select status from files where file_id = %s::uuid", (file_id,)
        ).fetchone()[0]
        == "queued"
    ), "claim wrote files.status - that axis belongs to the worker"


def test_claim_on_an_empty_queue_returns_none(worker_db):
    # A Job here means stray `queued` rows in the local `jobs` table: claim is unscoped.
    conn, _, _ = worker_db

    assert claim(conn) is None
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_claim_without_the_worker_lock_raises_and_takes_nothing(db, db_other, tmp_path):
    conn, owner_id, class_id = db
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)

    with pytest.raises(RuntimeError, match=r"claim\(\) needs the worker lock"):
        claim(conn)

    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert db_other.execute(
        "select state, attempts from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone() == ("queued", 0)


def test_a_second_connection_cannot_take_the_worker_lock_while_the_first_holds_it(
    worker_db, db_other
):
    conn, _, _ = worker_db

    assert try_acquire_worker_lock(db_other) is False
    assert holds_worker_lock(db_other) is False
    assert holds_worker_lock(conn) is True


def test_the_worker_lock_is_released_when_the_holding_connection_closes(db, db_other):
    holder = connect()
    try:
        assert try_acquire_worker_lock(holder) is True
        assert try_acquire_worker_lock(db_other) is False
    finally:
        holder.close()

    assert try_acquire_worker_lock(db_other) is True


def test_requeue_orphans_moves_only_processing_jobs_back_to_queued(worker_db, db_other, tmp_path):
    conn, owner_id, class_id = worker_db
    source = _lecture(tmp_path)
    orphan_file = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    done_file = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    waiting_file = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    orphan = claim(conn)
    finished = claim(conn)
    assert orphan is not None and orphan.file_id == orphan_file
    assert finished is not None and finished.file_id == done_file
    assert complete(conn, job_id=finished.job_id) is True

    assert requeue_orphans(conn) == 1

    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    states = dict(
        db_other.execute(
            "select file_id::text, (state, attempts)::text from jobs where owner_id = %s",
            (owner_id,),
        ).fetchall()
    )
    assert states == {
        orphan_file: "(queued,1)",
        done_file: "(done,1)",
        waiting_file: "(queued,0)",
    }


def test_requeue_orphans_without_the_worker_lock_raises_and_moves_nothing(db, db_other, tmp_path):
    conn, owner_id, class_id = db
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)
    db_other.execute("update jobs set state = 'processing' where file_id = %s::uuid", (file_id,))

    with pytest.raises(RuntimeError, match=r"requeue_orphans\(\) needs the worker lock"):
        requeue_orphans(conn)

    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert db_other.execute(
        "select state from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone() == ("processing",)


def test_a_crashed_workers_job_is_requeued_and_claimed_again_as_attempt_2(db, db_other, tmp_path):
    conn, owner_id, class_id = db
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)

    crashed = connect()
    try:
        assert try_acquire_worker_lock(crashed)
        first_try = claim(crashed)
        assert first_try is not None and first_try.file_id == file_id
        assert first_try.attempts == 1
    finally:
        crashed.close()  # never settles

    assert try_acquire_worker_lock(conn), "the crashed worker's lock outlived its connection"
    assert requeue_orphans(conn) == 1
    second_try = claim(conn)

    assert second_try is not None and second_try.job_id == first_try.job_id
    assert second_try.attempts == 2
    assert db_other.execute(
        "select state, attempts from jobs where job_id = %s::uuid", (second_try.job_id,)
    ).fetchone() == ("processing", 2)


def test_complete_and_fail_return_false_for_a_settled_job_and_raise_for_an_unknown_one(
    worker_db, db_other, tmp_path
):
    conn, owner_id, class_id = worker_db
    enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)
    job = claim(conn)
    assert job is not None

    assert complete(conn, job_id=job.job_id) is True
    assert complete(conn, job_id=job.job_id) is False
    assert fail(conn, job_id=job.job_id, error="too late") is False
    assert db_other.execute(
        "select state, last_error from jobs where job_id = %s::uuid", (job.job_id,)
    ).fetchone() == ("done", None), "a False return still wrote something"

    ghost = str(uuid.uuid4())
    with pytest.raises(LookupError, match=ghost):
        complete(conn, job_id=ghost)
    with pytest.raises(LookupError, match=ghost):
        fail(conn, job_id=ghost, error="into the void")
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE

    second = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)
    live = claim(conn)
    assert live is not None and live.file_id == second
    assert fail(conn, job_id=live.job_id, error="parse blew up") is True


def test_complete_publishes_done_and_touches_only_its_job(worker_db, db_other, tmp_path):
    conn, owner_id, class_id = worker_db
    source = _lecture(tmp_path)
    first = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    second = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    job = claim(conn)
    assert job is not None and job.file_id == first

    complete(conn, job_id=job.job_id)

    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert db_other.execute(
        "select state, last_error from jobs where job_id = %s::uuid", (job.job_id,)
    ).fetchone() == ("done", None)
    assert db_other.execute(
        "select state from jobs where file_id = %s::uuid", (second,)
    ).fetchone() == ("queued",), "complete touched a job it was not given"
    assert db_other.execute(
        "select status from files where file_id = %s::uuid", (first,)
    ).fetchone() == ("queued",), "complete wrote files.status - that axis belongs to the worker"


def test_fail_publishes_failed_with_the_error_and_keeps_attempts(worker_db, db_other, tmp_path):
    conn, owner_id, class_id = worker_db
    source = _lecture(tmp_path)
    first = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    second = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    job = claim(conn)
    assert job is not None and job.file_id == first

    fail(conn, job_id=job.job_id, error="embed exploded: dimension mismatch")

    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert db_other.execute(
        "select state, last_error, attempts from jobs where job_id = %s::uuid", (job.job_id,)
    ).fetchone() == ("failed", "embed exploded: dimension mismatch", 1)
    assert db_other.execute(
        "select state, last_error from jobs where file_id = %s::uuid", (second,)
    ).fetchone() == ("queued", None), "fail touched a job it was not given"


def test_release_requeues_the_job_and_keeps_the_attempts_it_burned(worker_db, db_other, tmp_path):
    conn, owner_id, class_id = worker_db
    source = _lecture(tmp_path)
    first = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    second = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    job = claim(conn)
    assert job is not None and job.file_id == first

    assert release(conn, job_id=job.job_id, error="transient: provider said 429") is True

    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert db_other.execute(
        "select state, last_error, attempts from jobs where job_id = %s::uuid", (job.job_id,)
    ).fetchone() == ("queued", "transient: provider said 429", 1)
    assert db_other.execute(
        "select state, last_error from jobs where file_id = %s::uuid", (second,)
    ).fetchone() == ("queued", None), "release touched a job it was not given"
    assert db_other.execute(
        "select status from files where file_id = %s::uuid", (first,)
    ).fetchone() == ("queued",), "release wrote files.status - that axis belongs to the worker"

    again = claim(conn)
    assert again is not None and again.job_id == job.job_id
    assert again.attempts == 2, "the retry budget did not spend down across the release"


def test_release_returns_false_for_a_settled_job_and_raises_for_an_unknown_one(
    worker_db, db_other, tmp_path
):
    conn, owner_id, class_id = worker_db
    enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)
    job = claim(conn)
    assert job is not None
    assert complete(conn, job_id=job.job_id) is True

    assert release(conn, job_id=job.job_id, error="too late") is False
    assert db_other.execute(
        "select state, last_error from jobs where job_id = %s::uuid", (job.job_id,)
    ).fetchone() == ("done", None), "a False return still wrote something"

    ghost = str(uuid.uuid4())
    with pytest.raises(LookupError, match=ghost):
        release(conn, job_id=ghost, error="into the void")
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


# Parametrized as data so a new settle verb cannot be added without a row here (#126).
_SETTLE_VERBS = {
    "complete": lambda conn, **ids: complete(conn, **ids),
    "fail": lambda conn, **ids: fail(conn, error="boom", **ids),
    "release": lambda conn, **ids: release(conn, error="boom", **ids),
}

# STRICT: two of these cast cleanly in Postgres and are still refused, because `job_id` comes back
# out of `claim`'s `Job`, so any other spelling is an upstream bug (`gct.ids`).
_NOT_CANONICAL = {
    "urn": lambda u: u.urn,
    "braced": lambda u: f"{{{u}}}",
    "hex32": lambda u: u.hex,
    "already_parsed": lambda u: u,  # a `uuid.UUID` passed instead of `str(...)`
}


@pytest.mark.parametrize("verb", sorted(_SETTLE_VERBS))
@pytest.mark.parametrize("spelling", sorted(_NOT_CANONICAL))
def test_the_settle_verbs_refuse_a_non_canonical_job_id_and_leave_the_connection_alone(
    worker_db, db_other, tmp_path, verb, spelling
):
    conn, owner_id, class_id = worker_db
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)
    job = claim(conn)
    assert job is not None

    with pytest.raises(ValueError, match=rf"{verb}\(\) requires a canonical uuid job_id") as caught:
        _SETTLE_VERBS[verb](conn, job_id=_NOT_CANONICAL[spelling](uuid.UUID(job.job_id)))

    assert "claim" in str(caught.value), "the refusal does not name what to pass instead"
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert conn.execute("select 1").fetchone() == (1,), "the refusal cost the caller its connection"
    conn.rollback()
    assert db_other.execute(
        "select state from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone() == ("processing",), "a refused settle still wrote the job it named"
