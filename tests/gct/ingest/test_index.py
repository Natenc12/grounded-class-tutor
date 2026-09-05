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
from gct.ingest.index import PublishRefused, index_file
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
    version of this docstring said the latter and #92 was the counterexample: a worker whose lease
    expired mid-ingest reached this function on a `('failed', <a reason>)` row with no claim of its
    own in between, because a second worker burys with a LIVE lease while the first is still
    working. #92 was resolved WITHOUT changing this statement - ADR 0030 refused that worker's
    publish by entitlement (`publish_guard`) instead of clearing the column here, and §3 records
    why. So the `("ready", "unparseable")` asserted below remains this statement's correct
    behavior, and is no longer a step of a live defect on the worker path. It still is one for an
    UNGUARDED caller, which is what this test is: passing no guard is exactly how a Slice 1 caller
    reaches the publish, and ADR 0030's opt-in bound is the reason that stays legal.
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


# --- #92: the publish guard — an entitlement check evaluated inside the index transaction -------
#
# ADR 0030. `index_file` was the only write to `files` that read no lease, which is how a worker
# whose lease had been reaped could publish `ready` over a reason the live-leased winner had just
# committed. The fix is a predicate the CALLER supplies and this box cannot interpret, so the
# PM-4 seam (ADR 0020) holds: `index.py` still knows nothing about jobs, leases or workers.
#
# These tests are deliberately about the MECHANISM and not about leases — a lease-shaped guard
# would test `worker._publish_guard` through this file and pin neither properly. What the guard
# reads is `tests/gct/jobs/test_worker.py`'s subject; that it runs inside the transaction, before
# every write, exactly once, on THIS connection, and rolls everything back on refusal, is this
# file's.


def _seed_failed_file_with_chunks(conn, owner_id: str, class_id: str) -> str:
    """A `('failed', 'transient_exhausted')` row that ALREADY HAS a chunk set, committed.

    Both halves matter for what the refusal tests assert. The row is what the winning `_bury`
    leaves behind, and the pre-existing chunks are what makes `index_file`'s step 2 (`delete from
    chunks`) destructive: a guard that ran after it, or outside the transaction, would leave the
    file stripped of the chunks it is still serving even though the publish was refused.
    """
    file_id = str(uuid.uuid4())
    conn.execute(
        """
        insert into files (file_id, owner_id, class_id, filename, status, failed_reason)
        values (%s::uuid, %s, %s::uuid, 'lecture.pdf', 'failed', 'transient_exhausted')
        """,
        (file_id, owner_id, class_id),
    )
    index_file(
        conn,
        file_id=file_id,
        filename="lecture.pdf",
        owner_id=owner_id,
        class_id=class_id,
        chunks=[_chunk(owner_id, class_id, "the previous run's passage", 1)],
    )
    # `index_file` published it `ready` on the way in; put the bury's row back so the refusal is
    # tested against the state the defect actually produces.
    conn.execute(
        """
        update files set status = 'failed', failed_reason = 'transient_exhausted'
        where file_id = %s::uuid
        """,
        (file_id,),
    )
    return file_id


def test_a_refused_publish_writes_nothing_at_all(db, db_other):
    """A False guard raises `PublishRefused` and leaves every row exactly as it was (#92).

    Read back on `db_other` because the claim is about what SURVIVES, not what this connection
    computed: a rollback that only looked like one is invisible from the connection that issued it.

    Asserts THREE things, and the third is the one a careless implementation fails. The `files`
    row still reads `('failed', 'transient_exhausted')` — the publish did not happen. No new chunk
    landed. And the OLD chunk set is still there: `index_file` deletes before it inserts, so a
    guard placed after step 2 would refuse the publish and still have destroyed the corpus the
    file was serving. Rolling back is not the same as never writing, but it is the same OUTCOME
    only if the guard sits inside the transaction — which is what this pins.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    file_id = _seed_failed_file_with_chunks(conn, owner_id, class_id)

    with pytest.raises(PublishRefused) as excinfo:
        index_file(
            conn,
            file_id=file_id,
            filename="lecture.pdf",
            owner_id=owner_id,
            class_id=class_id,
            chunks=[_chunk(owner_id, class_id, "the zombie's passage", 2)],
            publish_guard=lambda _conn: False,
        )
    assert excinfo.value.file_id == file_id

    status, reason = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (status, reason) == ("failed", "transient_exhausted"), (
        "the refused publish wrote `files` anyway - the guard is not inside the transaction"
    )

    texts = [
        r[0]
        for r in db_other.execute(
            "select text from chunks where file_id = %s::uuid", (file_id,)
        ).fetchall()
    ]
    assert "the zombie's passage" not in texts, "a refused publish inserted its chunks"
    assert texts == ["the previous run's passage"], (
        "the refused publish DESTROYED the chunk set it did not replace - the guard runs after "
        "`delete from chunks` instead of before it"
    )


def test_publish_refused_is_an_ordinary_exception():
    """`PublishRefused` must be catchable by `except Exception`, and nothing pins that but this.

    Found by the mutation run: rebasing it on `BaseException` left the whole suite green, because
    the one handler in the repo names the class explicitly. A `BaseException` is the wrong shape
    for what this is - the ROUTINE at-least-once outcome (ADR 0011), not an interrupt or a
    shutdown - and `process_one`'s outer `except BaseException` treats those two categories very
    differently. A caller that reasonably writes `except Exception` around a publish would stop
    seeing refusals at all, and see its process die instead.
    """
    assert issubclass(PublishRefused, Exception)
    assert not issubclass(PublishRefused, (KeyboardInterrupt, SystemExit))


def test_a_guard_that_allows_publishes_exactly_as_an_unguarded_call_does(db, db_other):
    """True is transparent: the guard is a veto, not a second condition on the publish (#92)."""
    conn, owner_id, class_id = db
    conn.autocommit = True
    file_id = _seed_failed_file_with_chunks(conn, owner_id, class_id)

    index_file(
        conn,
        file_id=file_id,
        filename="lecture.pdf",
        owner_id=owner_id,
        class_id=class_id,
        chunks=[_chunk(owner_id, class_id, "the new passage", 2)],
        publish_guard=lambda _conn: True,
    )

    status, text = db_other.execute(
        """
        select f.status, c.text from files f join chunks c using (file_id)
        where f.file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert status == "ready"
    assert text == "the new passage"


def test_the_guard_is_asked_once_before_any_write_on_the_index_connection(db):
    """WHEN, HOW OFTEN and ON WHAT — the three properties the veto's meaning rests on (#92).

    ON WHAT is the load-bearing one and the least obvious. `index_file` passes its own `conn`, and
    a guard that consulted any other connection would be answering a question about the world
    OUTSIDE this transaction: whatever it locks would not be locked by the transaction doing the
    writing, and the answer could go stale between being given and being relied on. That is the
    entire reason `worker._publish_guard` can close #92 with `FOR UPDATE` rather than merely
    narrow it — see its docstring.

    WHEN is asserted from inside the guard rather than after the fact: the `files` row must still
    read `failed` at the moment the guard is asked. HOW OFTEN pins it at one call, so a retry loop
    or a second evaluation cannot make the check and the write disagree.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    file_id = _seed_failed_file_with_chunks(conn, owner_id, class_id)
    seen: list[tuple[psycopg.Connection, str]] = []

    def guard(guard_conn: psycopg.Connection) -> bool:
        (status,) = guard_conn.execute(
            "select status from files where file_id = %s::uuid", (file_id,)
        ).fetchone()
        seen.append((guard_conn, status))
        return True

    index_file(
        conn,
        file_id=file_id,
        filename="lecture.pdf",
        owner_id=owner_id,
        class_id=class_id,
        chunks=[_chunk(owner_id, class_id, "the new passage", 2)],
        publish_guard=guard,
    )

    assert len(seen) == 1, f"the guard was asked {len(seen)} times, not once"
    guard_conn, status_when_asked = seen[0]
    assert guard_conn is conn, (
        "the guard was handed a connection other than the one doing the writing - anything it "
        "locks would not be held by the index transaction"
    )
    assert status_when_asked == "failed", (
        "the guard was asked AFTER the `files` upsert - by then the publish it is meant to veto "
        "has already happened"
    )


def test_no_guard_leaves_the_pre_92_behavior_untouched(db, db_other):
    """Omitting the guard publishes over a failure reason exactly as it always did (#92).

    ADR 0030's opt-in bound, stated as a test rather than only as prose. Slice 1's direct callers
    pass no guard - they hold no lease and race no bury - and must not have acquired a new way to
    fail. It is also the exact behavior `test_index_file_does_not_clear_failed_reason` above pins
    from the other side: the reason survives, and the `ready` is written.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    file_id = _seed_failed_file_with_chunks(conn, owner_id, class_id)

    index_file(
        conn,
        file_id=file_id,
        filename="lecture.pdf",
        owner_id=owner_id,
        class_id=class_id,
        chunks=[_chunk(owner_id, class_id, "the new passage", 2)],
    )

    assert db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone() == ("ready", "transient_exhausted")


def test_a_raising_guard_propagates_and_publishes_nothing(db, db_other):
    """A guard that BLOWS UP is not a guard that said yes (#92).

    The failure mode this forbids is `except Exception: pass` around the call, or any default
    that treats an unusable answer as permission. The exception must reach the caller and the
    transaction must roll back, which is what `conn.transaction()` does with any raise - the same
    mechanism `PublishRefused` rides.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    file_id = _seed_failed_file_with_chunks(conn, owner_id, class_id)

    def guard(_conn: psycopg.Connection) -> bool:
        raise RuntimeError("the guard could not answer")

    with pytest.raises(RuntimeError, match="could not answer"):
        index_file(
            conn,
            file_id=file_id,
            filename="lecture.pdf",
            owner_id=owner_id,
            class_id=class_id,
            chunks=[_chunk(owner_id, class_id, "the zombie's passage", 2)],
            publish_guard=guard,
        )

    assert db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone() == ("failed", "transient_exhausted")


# --- The id boundary (#126) -----------------------------------------------------------------
#
# `index_file` bound three `%(...)s::uuid` casts from caller-supplied ids: `file_id` (twice) and
# `class_id` (the argument, and again per chunk in the executemany). The guards it now runs are
# two DIFFERENT decisions, so they get two sets of tests: `class_id` is LENIENT (canonicalise) and
# `file_id` is STRICT (refuse anything non-canonical), by the criterion `enqueue`'s docstring
# states - where the id comes from, not reader vs writer.

# Python's `uuid.UUID` parses four spellings of one id; Postgres's `::uuid` cast parses three.
# Pinning the split keeps the accept test below from being trivially green.
_POSTGRES_ACCEPTS_THE_CAST = {"plain": True, "braced": True, "hex32": True, "urn": False}

# WHAT `uuid.UUID` RAISES DEPENDS ON WHAT IT WAS HANDED, and the three cases are not
# interchangeable - which is why the guard catches three types rather than one.
_NOT_A_USABLE_ID = {
    "malformed": "intro-to-religion",  # ValueError from the parser
    "none": None,  # TypeError: no hex/bytes/fields/int argument was given
    "int": 12345,  # AttributeError: int has no `.replace`
    "already_parsed": uuid.UUID("6f1e1b4a-0f0e-4a3e-9a7d-2f0a1b2c3d4e"),  # AttributeError
}


@pytest.mark.parametrize("spelling", ["plain", "braced", "hex32", "urn"])
def test_every_class_id_spelling_python_accepts_publishes_one_canonical_scope(
    db, db_other, spelling
):
    """`index_file` is LENIENT on `class_id`, and the whole row set lands under ONE spelling.

    The spelling is used TWICE on purpose - as the argument and on every `PreparedChunk` - because
    the two reach different casts. Guarding only the argument leaves the per-chunk bind raw, and a
    `urn` chunk then clears the `files` upsert and the chunk delete before aborting at the insert:
    a half-guard that trades an early refusal for a late one.

    Only `urn` is sensitive to the canonicalisation; Postgres normalises braced and bare-hex
    itself. The other three pin the LENIENCY decision - make this site strict like `file_id` and
    braced, hex32 and urn go red together.

    Read back through `db_other`, a SECOND connection: `db`'s own connection sees its uncommitted
    work, so the canonical value would read back correctly whether or not it was published.
    """
    conn, owner_id, class_id = db
    canonical = uuid.UUID(class_id)
    written = {
        "plain": str(canonical),
        "braced": f"{{{canonical}}}",
        "hex32": canonical.hex,
        "urn": canonical.urn,
    }[spelling]
    assert uuid.UUID(written) == canonical, "the spelling under test names a different class"

    try:
        db_other.execute("select %s::uuid", (written,)).fetchone()
        postgres_accepts = True
    except psycopg.errors.InvalidTextRepresentation:
        postgres_accepts = False
    assert postgres_accepts is _POSTGRES_ACCEPTS_THE_CAST[spelling], (
        f"Postgres's own handling of the {spelling} spelling changed; the premise is stale"
    )

    file_id = str(uuid.uuid4())
    index_file(
        conn,
        file_id=file_id,
        filename="lecture.pdf",
        owner_id=owner_id,
        class_id=written,
        chunks=[_chunk(owner_id, written, "alpha", 1), _chunk(owner_id, written, "beta", 2)],
    )

    assert db_other.execute(
        "select class_id::text, status from files where file_id = %s::uuid", (file_id,)
    ).fetchone() == (str(canonical), "ready")
    assert db_other.execute(
        "select distinct class_id::text from chunks where file_id = %s::uuid", (file_id,)
    ).fetchall() == [(str(canonical),)], "the chunks landed under a different class than the file"
    assert (
        db_other.execute(
            "select count(*) from chunks where file_id = %s::uuid", (file_id,)
        ).fetchone()[0]
        == 2
    )


@pytest.mark.parametrize("kind", sorted(_NOT_A_USABLE_ID))
@pytest.mark.parametrize("where", ["argument", "chunk"])
def test_index_file_refuses_a_non_uuid_class_id_and_publishes_nothing(db, db_other, kind, where):
    """Both class_id binds refuse, with a remedy, before the transaction opens.

    `where` is the arm that would be missing from a guard on the argument alone: the per-chunk
    `class_id` reaches its own cast in step 3, so a chunk carrying a bad id has to be refused
    before `BEGIN` too, or the refusal arrives after two statements have already run inside the
    transaction.

    `already_parsed` is the case that will actually happen - psycopg hands uuid columns back as
    `uuid.UUID` instances, so a caller reassembling chunks from a query holds exactly that.

    `db_other` is what makes "publishes nothing" a real assertion: `db`'s own connection would
    show the same zero whether the guard ran before the writes or the transaction rolled back
    after them, and only the first is what this guard promises.
    """
    conn, owner_id, class_id = db
    bad = _NOT_A_USABLE_ID[kind]
    file_id = str(uuid.uuid4())
    argument = bad if where == "argument" else class_id
    on_chunk = bad if where == "chunk" else class_id
    expected = (
        r"index_file\(\) requires a uuid class_id"
        if where == "argument"
        else (r"index_file\(\) requires a uuid chunk class_id")
    )

    with pytest.raises(ValueError, match=expected) as caught:
        index_file(
            conn,
            file_id=file_id,
            filename="lecture.pdf",
            owner_id=owner_id,
            class_id=argument,
            chunks=[_chunk(owner_id, on_chunk, "alpha", 1)],
        )

    assert "class" in str(caught.value), "the refusal does not name what to pass instead"
    # Refused BEFORE `conn.transaction()`, so the connection is exactly as it was found.
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert (
        db_other.execute("select count(*) from files where owner_id = %s", (owner_id,)).fetchone()[
            0
        ]
        == 0
    ), "a refused index_file published a files row"


@pytest.mark.parametrize("spelling", ["braced", "hex32", "urn", "already_parsed"])
def test_index_file_refuses_a_non_canonical_file_id_and_publishes_nothing(db, db_other, spelling):
    """`file_id` is STRICT - the opposite decision to `class_id`, and this is what pins it apart.

    A `file_id` reaches this function from the database: either `ingest_file` minted it with
    `uuid4()` or a worker read it off the `jobs` row it claimed, canonical in both cases. So any
    other spelling means the caller built it somewhere it should not have, and converting it
    quietly would hide that. Note `braced` and `hex32` are spellings Postgres itself WOULD accept -
    they are refused on the source-of-the-id argument, not because the cast would fail, which is
    exactly what makes this a strict guard rather than a canonicalising one.

    The accepting direction is `test_index_file_lands_full_set_and_flips_ready` and every other
    test in this module: they all pass `str(uuid.uuid4())` and publish. CASE is the one axis this
    guard does not treat as non-canonical - see the test below for why.
    """
    conn, owner_id, class_id = db
    canonical = uuid.uuid4()
    written = {
        "braced": f"{{{canonical}}}",
        "hex32": canonical.hex,
        "urn": canonical.urn,
        "upper": str(canonical).upper(),
        "already_parsed": canonical,
    }[spelling]

    with pytest.raises(ValueError, match=r"index_file\(\) requires a canonical uuid file_id"):
        index_file(
            conn,
            file_id=written,
            filename="lecture.pdf",
            owner_id=owner_id,
            class_id=class_id,
            chunks=[_chunk(owner_id, class_id, "alpha", 1)],
        )

    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert (
        db_other.execute(
            "select count(*) from files where file_id = %s::uuid", (str(canonical),)
        ).fetchone()[0]
        == 0
    ), "a refused index_file published a files row"


def test_index_file_accepts_an_uppercase_file_id_exactly_as_ingest_file_does(db, db_other):
    """The one spelling the strict guard lets through, pinned so nobody "tightens" it by accident.

    `require_canonical_uuid` compares against `value.lower()`, which is `ingest_file`'s shipped
    criterion copied rather than re-decided: `uuid.UUID` renders lowercase, so an all-caps
    hyphenated id differs from the canonical form in case ALONE and Postgres stores it as the same
    value. Refusing it would make the two guards disagree about the same id, which is the class of
    bug this whole ticket is about.

    The publish is read back through `db_other`, since the claim is that the row SURVIVED, and it
    is asserted lowercase - proof the column normalised, not just that the guard let it pass.
    """
    conn, owner_id, class_id = db
    file_id = str(uuid.uuid4())

    index_file(
        conn,
        file_id=file_id.upper(),
        filename="lecture.pdf",
        owner_id=owner_id,
        class_id=class_id,
        chunks=[_chunk(owner_id, class_id, "alpha", 1)],
    )

    assert db_other.execute(
        "select file_id::text, status from files where owner_id = %s", (owner_id,)
    ).fetchall() == [(file_id, "ready")]


def test_each_chunk_keeps_its_own_class_id_when_it_disagrees_with_the_file(db, db_other):
    """A chunk's `class_id` is written from THE CHUNK, never from the file-level argument (#126).

    This pins a decision the guards above made and nothing else could catch. `index_file`
    canonicalises the argument and every chunk's own `class_id` separately, and binds the
    per-chunk value; overwriting the chunk rows with the file's id would be one character's
    difference in the same statement and passes every other test in this module, because `compose`
    never produces a chunk whose scope differs from its file's. `index_file` is a supported direct
    entry point (the PM-4 seam, ADR 0020), so a direct caller can, and this is the only test that
    hands it such a set.

    WHY NOT SILENTLY OVERWRITE, which is the tempting alternative and would make the rows
    self-consistent: retrieval filters chunks on `owner_id AND class_id` (F6/F12), so overwriting
    would move a caller's chunks into a class it never named and answer another class's questions
    from them - a scope violation with no trace, since the rows would look correct. Divergence is a
    caller bug, and this box surfaces it as data rather than papering over it.

    BOTH DIRECTIONS, because a one-sided pin is exactly what the overwrite survives: the chunk rows
    carry the CHUNK's class, and they do NOT carry the file's. The `files` row is asserted too -
    the two values diverging is the whole scenario, so a fix that made them agree by moving the
    FILE instead would otherwise slip through.

    Read back through `db_other`, a SECOND connection: the subject is what was PUBLISHED, and
    `db`'s own connection sees its uncommitted work either way.
    """
    conn, owner_id, file_class = db
    chunk_class = _seed_class(conn, owner_id)
    conn.commit()  # back to IDLE, which `require_idle` demands of every writer
    assert chunk_class != file_class, "the two classes are the same; nothing would be proven"
    file_id = str(uuid.uuid4())

    index_file(
        conn,
        file_id=file_id,
        filename="lecture.pdf",
        owner_id=owner_id,
        class_id=file_class,
        chunks=[
            _chunk(owner_id, chunk_class, "alpha", 1),
            _chunk(owner_id, chunk_class, "beta", 2),
        ],
    )
    conn.commit()

    assert db_other.execute(
        "select class_id::text from files where file_id = %s::uuid", (file_id,)
    ).fetchone() == (file_class,), "the files row did not keep the class_id it was called with"

    published = db_other.execute(
        "select distinct class_id::text from chunks where file_id = %s::uuid", (file_id,)
    ).fetchall()
    assert published == [(chunk_class,)], (
        f"the chunks were written under {published!r}, not their own class"
    )
    assert (
        db_other.execute(
            "select count(*) from chunks where file_id = %s::uuid and class_id = %s::uuid",
            (file_id, file_class),
        ).fetchone()[0]
        == 0
    ), "a chunk took the file-level class_id instead of its own"
