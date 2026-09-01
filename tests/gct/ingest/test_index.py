"""DB-backed tests for the atomic index transaction (issue #4, ADR 0020).

Runs against the real local Postgres via the `db` fixture (seeds a `classes` row, cleans up by
`owner_id`, skips *locally* when the DB is unreachable — but hard-fails in CI, where the service
container guarantees one). Proves the invariants this box exists to hold: the
full set lands with `status='ready'` in one shot, a re-index replaces the set cleanly (no dups),
and — the all-or-nothing half, added by issue #23 — a write that fails partway publishes nothing
and destroys nothing.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from gct.config import EMBEDDING_DIM
from gct.ingest.index import index_file
from gct.ingest.pipeline import PreparedChunk

MODEL_ID = "fake-embed-3"


def _chunk(
    owner_id: str,
    class_id: str,
    text: str,
    page: int,
    dim: int = EMBEDDING_DIM,
    file: str = "lecture.pdf",
) -> PreparedChunk:
    """A PreparedChunk with a dim-1536 vector by default, so the `vector(1536)` INSERT succeeds.

    `file` is the citation label denormalized onto the chunk. It defaults to the same
    `"lecture.pdf"` every caller here used before #24 needed it to vary; pass it when the test's
    subject is the `files` row agreeing with its own chunks, and leave it alone otherwise.

    Pass a `dim` other than `EMBEDDING_DIM` to build a chunk the column will REJECT — that is the
    mid-write failure injection the atomicity tests use (issue #23). The rejection comes from
    Postgres itself (`psycopg.errors.DataException: expected 1536 dimensions, not N`), so it fires
    *during* the `executemany`, with rows on both sides of it already sent — a genuine mid-write
    failure, with no monkeypatching and no fault-injection dependency.
    """
    return PreparedChunk(
        text=text,
        file=file,
        page_or_slide=page,
        embedding=[0.0] * dim,
        owner_id=owner_id,
        class_id=class_id,
        embedding_model_id=MODEL_ID,
    )


def test_index_file_lands_full_set_and_flips_ready(db):
    """After one call: N chunk rows for the file, `files.status='ready'`, every row carries scope +
    stamp, and `page_or_slide` is stored as text."""
    conn, owner_id, class_id = db
    file_id = str(uuid.uuid4())
    chunks = [
        _chunk(owner_id, class_id, "first passage", 1),
        _chunk(owner_id, class_id, "second passage", 2),
        _chunk(owner_id, class_id, "third passage", 10),
    ]

    index_file(
        conn,
        file_id=file_id,
        filename="lecture.pdf",
        owner_id=owner_id,
        class_id=class_id,
        chunks=chunks,
    )

    # files row published as ready.
    status = conn.execute(
        "select status from files where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    assert status == "ready"

    # Full chunk set landed, each row carrying scope + stamp; page_or_slide stored as text.
    rows = conn.execute(
        """
        select owner_id, class_id, embedding_model_id, page_or_slide
        from chunks where file_id = %s::uuid order by page_or_slide::int
        """,  # ::int is load-bearing: the column is text, so page 10 sorts before 2 without it.
        (file_id,),
    ).fetchall()
    assert len(rows) == 3
    for row_owner, row_class, row_model, row_page in rows:
        assert row_owner == owner_id
        assert str(row_class) == class_id
        assert row_model == MODEL_ID
        assert isinstance(row_page, str)  # int -> text at the SQL boundary
    assert [r[3] for r in rows] == ["1", "2", "10"]


def test_reindex_replaces_the_set(db):
    """Calling twice on the same file_id with a different set leaves ONLY the second set - the
    DELETE-then-insert replace, no duplicates (idempotent by construction).

    Both writes SUCCEED here, so this proves *replacement*, not atomicity — the all-or-nothing
    guarantee is proven by the mid-write-failure tests below (issue #23).
    """
    conn, owner_id, class_id = db
    file_id = str(uuid.uuid4())

    first = [
        _chunk(owner_id, class_id, "old-A", 1),
        _chunk(owner_id, class_id, "old-B", 2),
        _chunk(owner_id, class_id, "old-C", 3),
    ]
    second = [
        _chunk(owner_id, class_id, "new-A", 1),
        _chunk(owner_id, class_id, "new-B", 2),
    ]

    index_file(
        conn,
        file_id=file_id,
        filename="lecture.pdf",
        owner_id=owner_id,
        class_id=class_id,
        chunks=first,
    )
    index_file(
        conn,
        file_id=file_id,
        filename="lecture.pdf",
        owner_id=owner_id,
        class_id=class_id,
        chunks=second,
    )

    texts = conn.execute(
        "select text from chunks where file_id = %s::uuid order by text", (file_id,)
    ).fetchall()
    assert [t[0] for t in texts] == ["new-A", "new-B"]  # only the second set, no leftovers


# --- Atomicity + write-path guards (issue #23) ------------------------------------------------
#
# The two tests above run only SUCCESSFUL writes, so they prove *replacement* and never prove
# *all-or-nothing*. The gap is load-bearing: `index_file` DELETEs the old chunk set before it
# INSERTs the new one, which is safe only under the transaction. If that transaction ever stopped
# holding, a mid-write failure would destroy the old chunks AND fail to write the new ones —
# strictly worse than never re-indexing (ADR 0020 §2-3, publication half amended by ADR 0025;
# ingestion-worker.md §Failure modes).


def test_midwrite_failure_leaves_old_set_intact(db):
    """RE-INDEX path: a write that fails partway leaves the OLD chunk set complete and queryable.

    Index a good 3-chunk set and commit, then re-index with a set whose MIDDLE chunk carries a
    wrong-dimension vector (`_chunk(..., dim=8)`). Assert the raise, then assert all three original
    texts are still present and `status` is still 'ready' — readers see old-full → new-full, never
    empty or partial (ADR 0020 §3; publication conditional per ADR 0025, which this test does not
    exercise - see the first-index test below).

    The injection must fail mid-`executemany`, NOT before the transaction opens — otherwise this
    test goes green while proving nothing about atomicity. That is why the
    assertions below are on the SHAPE of the survivors (all three original texts, complete, still
    `ready`) rather than merely on "something raised": a dimension check moved into Python would
    still raise, but would prove nothing about the transaction.
    """
    conn, owner_id, class_id = db
    file_id = str(uuid.uuid4())

    good = [
        _chunk(owner_id, class_id, "old-A", 1),
        _chunk(owner_id, class_id, "old-B", 2),
        _chunk(owner_id, class_id, "old-C", 3),
    ]
    index_file(
        conn,
        file_id=file_id,
        filename="lecture.pdf",
        owner_id=owner_id,
        class_id=class_id,
        chunks=good,
    )

    # Re-index with a doomed set: the MIDDLE chunk's vector is dim 8, not 1536. Rows on BOTH sides
    # of it are in play, so a leaky transaction would show up as a partial set either way.
    doomed = [
        _chunk(owner_id, class_id, "new-A", 1),
        _chunk(owner_id, class_id, "BAD", 2, dim=8),
        _chunk(owner_id, class_id, "new-C", 3),
    ]
    with pytest.raises(psycopg.errors.DataException):
        index_file(
            conn,
            file_id=file_id,
            filename="lecture.pdf",
            owner_id=owner_id,
            class_id=class_id,
            chunks=doomed,
        )

    # Same connection (it stays usable after the rollback — no fresh connection needed).
    # The OLD set is complete and queryable: the DELETE rolled back with the failed INSERT.
    texts = conn.execute(
        "select text from chunks where file_id = %s::uuid order by text", (file_id,)
    ).fetchall()
    assert [t[0] for t in texts] == ["old-A", "old-B", "old-C"]

    # And the file is still published — readers see old-full, never empty or partial.
    status = conn.execute(
        "select status from files where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    assert status == "ready"


def test_midwrite_failure_on_first_index_publishes_nothing(db):
    """FIRST-INDEX path: a write that fails partway publishes NOTHING — no chunks, no `files` row.

    This is the path on which the status half of the invariant is actually provable. On a re-index
    `status` was already 'ready' before the failure, so asserting it "did not flip" proves nothing;
    on a first index the `files` upsert rolls back with the chunks, so the row is absent entirely.
    What this proves is the ATOMICITY half of `status=ready` ⟺ full chunk set committed & queryable
    (ADR 0020 §3), which ADR 0025 leaves unconditional. It does NOT prove the PUBLICATION half, and
    cannot: savepoint writes are fully visible to the connection that made them, so a test asserting
    through the same `conn` passes whether or not the caller precondition held (ADR 0025, "invisible
    to the existing tests, by construction" - catching it needs a SECOND connection).

    As above, the injection must fail mid-`executemany`, NOT before the transaction opens — a
    dimension check moved into Python would raise without ever exercising the rollback, and this
    test would stay green while proving nothing. The load-bearing assertion is
    the ABSENT `files` row: it can only be absent because the upsert rolled back.
    """
    conn, owner_id, class_id = db
    file_id = str(uuid.uuid4())  # fresh — never successfully indexed

    doomed = [
        _chunk(owner_id, class_id, "new-A", 1),
        _chunk(owner_id, class_id, "BAD", 2, dim=8),
        _chunk(owner_id, class_id, "new-C", 3),
    ]
    with pytest.raises(psycopg.errors.DataException):
        index_file(
            conn,
            file_id=file_id,
            filename="lecture.pdf",
            owner_id=owner_id,
            class_id=class_id,
            chunks=doomed,
        )

    # Nothing published. No chunks...
    count = conn.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    assert count == 0

    # ...and no `files` row AT ALL: the upsert (step 1) is inside the same transaction as the
    # insert (step 3), so it rolled back too. `ready` was never published for this file.
    row = conn.execute("select status from files where file_id = %s::uuid", (file_id,)).fetchone()
    assert row is None


def test_index_file_rejects_empty_chunk_set(db):
    """`index_file(chunks=[])` raises rather than publishing 'ready' with zero chunks.

    Before the #23 guard it published: `executemany` over an empty sequence is a no-op, so the
    `files` upsert and the DELETE committed alone — contradicting `status=ready` ⟺ full chunk set
    queryable. Unreachable
    via `compose` (`parse_file` raises `ParseError("empty", ...)` first), but `index_file` is a
    public entry point. Assert the raise AND that no `files` row was created (the guard runs before
    the transaction opens, so there is nothing to roll back).
    """
    conn, owner_id, class_id = db
    file_id = str(uuid.uuid4())

    with pytest.raises(ValueError):
        index_file(
            conn,
            file_id=file_id,
            filename="lecture.pdf",
            owner_id=owner_id,
            class_id=class_id,
            chunks=[],
        )

    # Nothing published: the guard fires before the transaction opens, so the files upsert
    # never ran.
    row = conn.execute("select status from files where file_id = %s::uuid", (file_id,)).fetchone()
    assert row is None


def test_index_file_refuses_a_connection_already_in_a_transaction(db, db_other):
    """`index_file` on an INTRANS connection raises instead of degrading to a SAVEPOINT.

    The ADR 0025 failure (ADR 0025, guarded per ADR 0027): psycopg opens an implicit
    transaction on a connection's first statement — a bare SELECT is enough — and
    `conn.transaction()` then issues a SAVEPOINT that publishes NOTHING while the call
    returns successfully. ADR 0027's probe found seven tests in exactly that state; #75
    accepted the guard.

    Arms the trap with a bare SELECT (the subtle half — no write anywhere), and asserts the
    refusal wrote nothing through `db_other`: a guard that raised after doing half the work
    would be worse than none. Reading back through `db`'s own connection could not make that
    claim — it sees its own uncommitted rows, which is the very failure this guard exists
    to catch.
    """
    conn, owner_id, class_id = db
    file_id = str(uuid.uuid4())
    conn.execute("select 1")  # arms the implicit transaction exactly as a write would

    with pytest.raises(RuntimeError, match="index_file.*already inside a transaction"):
        index_file(
            conn,
            file_id=file_id,
            filename="lecture.pdf",
            owner_id=owner_id,
            class_id=class_id,
            chunks=[_chunk(owner_id, class_id, "some text", page=1)],
        )
    conn.rollback()  # leave the fixture's teardown a clean connection

    row = db_other.execute("select 1 from files where file_id = %s::uuid", (file_id,)).fetchone()
    assert row is None, "a refused index_file call must publish nothing"


# --- The `files` row must describe the chunk set it publishes (issue #24) ----------------------
#
# Step 3 replaces this file's chunks WHOLESALE from the arguments of the current call. The `files`
# row's `DO UPDATE` used to set only `status` and `updated_at`, discarding `excluded.filename`,
# `excluded.owner_id` and `excluded.class_id` - so a re-index under different scope published a
# row describing a chunk set that no longer existed. Retrieval filters chunks on
# `owner_id AND class_id` (F6/F12) and cites `chunks.file`, so the disagreement is not cosmetic:
# a status read and an answer end up describing different files, silently.
#
# `test_ingest_file_uses_a_caller_supplied_file_id` (test_pipeline.py) does NOT cover this. It
# seeds with the same `owner_id` it later passes, so "keep the old value" and "write the new one"
# are indistinguishable there. Every test below VARIES the value, or it proves nothing.


def _purge(conn, owner_id: str, file_id: str | None = None) -> None:
    """Delete rows the `db` fixture's teardown cannot see.

    Teardown deletes by the FIXTURE's `owner_id`. A test that re-indexes into a different owner
    moves the `files` row and its chunks out of that scope, so they would survive the test and
    accumulate in the developer's database. FK order: chunks -> files -> classes.

    THE `file_id` PASS IS NOT OPTIONAL POLISH - it is what makes this function correct when the
    test it serves FAILS. Deleting the file by its new `owner_id` only reaches it if the refresh
    actually moved it, which is the very thing under test: drop the `owner_id` refresh and the
    `files` row stays under owner A while still pointing at owner B's class, so
    `delete from classes where owner_id = B` raises `ForeignKeyViolation` from inside `finally`.
    pytest then headlines the run with the FK error and the real assertion survives only as a
    chained `__context__` - destroying the diagnostic these tests exist to give, and leaking the
    rows besides. `file_id` is the key that does NOT move, so this holds either way.
    """
    if file_id is not None:
        conn.execute("delete from chunks where file_id = %s::uuid", (file_id,))
        conn.execute("delete from files where file_id = %s::uuid", (file_id,))
    conn.execute("delete from chunks where owner_id = %s", (owner_id,))
    conn.execute("delete from files where owner_id = %s", (owner_id,))
    conn.execute("delete from classes where owner_id = %s", (owner_id,))


def _seed_class(conn, owner_id: str) -> str:
    class_id = str(uuid.uuid4())
    conn.execute(
        "insert into classes (class_id, owner_id, name) values (%s::uuid, %s, %s)",
        (class_id, owner_id, "another class"),
    )
    return class_id


def test_reindex_refreshes_filename_owner_and_class_from_the_incoming_call(db, db_other, tmp_path):
    """The core of #24: a re-index republishes the `files` row under the CALL's scope, not the old.

    Seed the file under A, then re-index the SAME `file_id` under a different owner, a different
    class and a different filename, with chunks carrying B's scope. All three must move, and the
    sharp assertion is not "they equal B" but "they equal what the CHUNKS say" - that is the
    invariant, and it cannot be satisfied by a refresh that reads the wrong argument.

    Read back through `db_other`: the subject is what was PUBLISHED, and `db`'s own connection
    would report its own uncommitted work either way (ADR 0025).
    """
    conn, owner_a, class_a = db
    conn.autocommit = True
    owner_b = f"test-owner-{uuid.uuid4()}"
    class_b = _seed_class(conn, owner_b)
    file_id = str(uuid.uuid4())

    try:
        index_file(
            conn,
            file_id=file_id,
            filename="old.pdf",
            owner_id=owner_a,
            class_id=class_a,
            chunks=[_chunk(owner_a, class_a, "the old passage", 1, file="old.pdf")],
        )
        index_file(
            conn,
            file_id=file_id,
            filename="new.pdf",
            owner_id=owner_b,
            class_id=class_b,
            chunks=[_chunk(owner_b, class_b, "the new passage", 1, file="new.pdf")],
        )

        files_row = db_other.execute(
            "select owner_id, class_id::text, filename from files where file_id = %s::uuid",
            (file_id,),
        ).fetchone()
        assert files_row == (owner_b, class_b, "new.pdf"), (
            "the re-index kept the FIRST call's scope - the row now describes a chunk set that "
            "was deleted by this very call"
        )
        chunk_scopes = db_other.execute(
            "select distinct owner_id, class_id::text, file from chunks where file_id = %s::uuid",
            (file_id,),
        ).fetchall()
        assert chunk_scopes == [files_row], (
            "the `files` row and its own chunks disagree, which no reader can detect and no "
            "reader can recover from"
        )
    finally:
        _purge(conn, owner_b, file_id)


@pytest.mark.parametrize("varying", ["filename", "owner_id", "class_id"])
def test_the_refreshed_files_row_and_its_chunks_never_disagree(db, db_other, varying):
    """One arm per column, each varying EXACTLY one - so a half-done refresh is localised.

    The combined test above goes red if any of the three is missing and says nothing about which.
    These say which. A builder who adds `filename` and `owner_id` but forgets `class_id` gets one
    named failure instead of one ambiguous one, and the arm's id is the diagnosis.

    The `class_id` arm keeps the fixture's `owner_id`, so its extra `classes` row is cleaned up by
    the fixture's own teardown; only the `owner_id` arm needs `_purge` (see its docstring).
    """
    conn, owner_a, class_a = db
    conn.autocommit = True
    file_id = str(uuid.uuid4())
    second = {"filename": "old.pdf", "owner_id": owner_a, "class_id": class_a}
    if varying == "owner_id":
        second["owner_id"] = f"test-owner-{uuid.uuid4()}"
    elif varying == "class_id":
        second["class_id"] = _seed_class(conn, owner_a)
    else:
        second["filename"] = "renamed.pdf"

    try:
        index_file(
            conn,
            file_id=file_id,
            filename="old.pdf",
            owner_id=owner_a,
            class_id=class_a,
            chunks=[_chunk(owner_a, class_a, "the old passage", 1, file="old.pdf")],
        )
        index_file(
            conn,
            file_id=file_id,
            chunks=[
                _chunk(
                    second["owner_id"],
                    second["class_id"],
                    "the new passage",
                    1,
                    file=second["filename"],
                )
            ],
            **second,
        )

        published = db_other.execute(
            "select owner_id, class_id::text, filename from files where file_id = %s::uuid",
            (file_id,),
        ).fetchone()
        assert published == (second["owner_id"], second["class_id"], second["filename"]), (
            f"`{varying}` was not refreshed from the incoming call"
        )
        chunk_scopes = db_other.execute(
            "select distinct owner_id, class_id::text, file from chunks where file_id = %s::uuid",
            (file_id,),
        ).fetchall()
        assert chunk_scopes == [published]
    finally:
        if varying == "owner_id":
            _purge(conn, second["owner_id"], file_id)


def test_index_file_does_not_clear_failed_reason(db, db_other):
    """`index_file` leaves `failed_reason` ALONE, and that is a decision, not an oversight.

    The column has exactly one writer - `worker._bury` - and the worker also owns the CLEAR,
    inside its own `status = 'processing'` update at the claim. That keeps this box pure of
    job/queue/retry machinery (the PM-4 seam, ADR 0020) and keeps the column's set and unset
    halves with the same owner. Adding `failed_reason = null` to the `DO UPDATE` below would make
    the publisher a second writer for a column it knows nothing about, and this test is what goes
    red if someone "fixes" the omission.

    Where the clear IS proven, so this test does not read as a bug being pinned:
    `test_the_claim_clears_the_previous_attempts_failed_reason` and
    `test_a_retry_that_succeeds_leaves_no_failed_reason_on_the_ready_file`, both in
    `tests/gct/jobs/test_worker.py`.

    THE ROW IS SEEDED DIRECTLY, FOR CONTROL - NOT BECAUSE PRODUCTION CANNOT REACH IT. An earlier
    version of this docstring said the latter, and #92 is the counterexample: a worker whose lease
    expired mid-ingest reaches this function on a `('failed', <a reason>)` row with no claim of its
    own in between, because a second worker burys with a LIVE lease while the first is still
    working. So the `("ready", "unparseable")` asserted below is this statement's correct behavior
    AND one step of a live defect. Both readings are true at once; #92 is where the conflict gets
    resolved, by amending the seam or the invariant.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    file_id = str(uuid.uuid4())
    conn.execute(
        """
        insert into files (file_id, owner_id, class_id, filename, status, failed_reason)
        values (%s::uuid, %s, %s::uuid, 'lecture.pdf', 'failed', 'unparseable')
        """,
        (file_id, owner_id, class_id),
    )

    index_file(
        conn,
        file_id=file_id,
        filename="lecture.pdf",
        owner_id=owner_id,
        class_id=class_id,
        chunks=[_chunk(owner_id, class_id, "a passage", 1)],
    )

    assert db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone() == ("ready", "unparseable"), (
        "`index_file` published the file (correct) and also cleared a column it does not own "
        "(not correct) - the clear belongs to the worker's claim"
    )


def test_the_first_index_still_inserts_rather_than_updates(db, db_other):
    """The INSERT branch is untouched by the refresh: a fresh `file_id` lands all four columns.

    The `DO UPDATE` list grew; the `VALUES` list did not, and a builder rewriting the statement
    could plausibly disturb either. Asserted through `db_other` because the claim is that the row
    was PUBLISHED, and `status` is checked alongside the scope: `ready` is `index_file`'s own
    decision, written as a literal rather than taken from `excluded`.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    file_id = str(uuid.uuid4())

    index_file(
        conn,
        file_id=file_id,
        filename="first-time.pdf",
        owner_id=owner_id,
        class_id=class_id,
        chunks=[_chunk(owner_id, class_id, "a passage", 1)],
    )

    assert db_other.execute(
        """
        select owner_id, class_id::text, filename, status, failed_reason
        from files where file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone() == (owner_id, class_id, "first-time.pdf", "ready", None)


def test_reindex_into_a_class_that_does_not_exist_is_refused_and_publishes_nothing(db, db_other):
    """The refresh cannot HALF-apply: a rejected `class_id` leaves scope, status and chunks intact.

    `files.class_id` FKs to `classes`, so refreshing it introduces a way for the upsert itself to
    fail - a failure mode the old set-list could not have. The transaction is what makes that
    safe, and this is where that is measured: step 1 raises, so steps 2 and 3 never run and the
    OLD chunk set is still complete and queryable. Extends the `test_midwrite_failure_leaves_old
    _set_intact` family (#23) to the new failure point.

    The raise is asserted through `db` because the exception is the subject; everything after it
    is asserted through `db_other`, because "nothing was published" is a claim about what other
    connections can see.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    file_id = str(uuid.uuid4())
    ghost_class = str(uuid.uuid4())  # no `classes` row - never seeded

    index_file(
        conn,
        file_id=file_id,
        filename="old.pdf",
        owner_id=owner_id,
        class_id=class_id,
        chunks=[
            _chunk(owner_id, class_id, "old-A", 1, file="old.pdf"),
            _chunk(owner_id, class_id, "old-B", 2, file="old.pdf"),
        ],
    )

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        index_file(
            conn,
            file_id=file_id,
            filename="new.pdf",
            owner_id=owner_id,
            class_id=ghost_class,
            chunks=[_chunk(owner_id, ghost_class, "new-A", 1, file="new.pdf")],
        )

    assert db_other.execute(
        """
        select owner_id, class_id::text, filename, status
        from files where file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone() == (owner_id, class_id, "old.pdf", "ready"), (
        "a refused re-index moved the row's scope - the refresh applied without its chunks"
    )
    texts = db_other.execute(
        "select text from chunks where file_id = %s::uuid order by text", (file_id,)
    ).fetchall()
    assert [t[0] for t in texts] == ["old-A", "old-B"], (
        "readers must see old-full -> new-full, never empty or partial (ADR 0020 §3)"
    )
