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
    owner_id: str, class_id: str, text: str, page: int, dim: int = EMBEDDING_DIM
) -> PreparedChunk:
    """A PreparedChunk with a dim-1536 vector by default, so the `vector(1536)` INSERT succeeds.

    Pass a `dim` other than `EMBEDDING_DIM` to build a chunk the column will REJECT — that is the
    mid-write failure injection the atomicity tests use (issue #23). The rejection comes from
    Postgres itself (`psycopg.errors.DataException: expected 1536 dimensions, not N`), so it fires
    *during* the `executemany`, with rows on both sides of it already sent — a genuine mid-write
    failure, with no monkeypatching and no fault-injection dependency.
    """
    return PreparedChunk(
        text=text,
        file="lecture.pdf",
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

    index_file(conn, file_id=file_id, filename="lecture.pdf",
               owner_id=owner_id, class_id=class_id, chunks=first)
    index_file(conn, file_id=file_id, filename="lecture.pdf",
               owner_id=owner_id, class_id=class_id, chunks=second)

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
# strictly worse than never re-indexing (ADR 0020 §2-3; ingestion-worker.md §Failure modes).


def test_midwrite_failure_leaves_old_set_intact(db):
    """RE-INDEX path: a write that fails partway leaves the OLD chunk set complete and queryable.

    Index a good 3-chunk set and commit, then re-index with a set whose MIDDLE chunk carries a
    wrong-dimension vector (`_chunk(..., dim=8)`). Assert the raise, then assert all three original
    texts are still present and `status` is still 'ready' — readers see old-full → new-full, never
    empty or partial (ADR 0020 §3).

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
    index_file(conn, file_id=file_id, filename="lecture.pdf",
               owner_id=owner_id, class_id=class_id, chunks=good)

    # Re-index with a doomed set: the MIDDLE chunk's vector is dim 8, not 1536. Rows on BOTH sides
    # of it are in play, so a leaky transaction would show up as a partial set either way.
    doomed = [
        _chunk(owner_id, class_id, "new-A", 1),
        _chunk(owner_id, class_id, "BAD", 2, dim=8),
        _chunk(owner_id, class_id, "new-C", 3),
    ]
    with pytest.raises(psycopg.errors.DataException):
        index_file(conn, file_id=file_id, filename="lecture.pdf",
                   owner_id=owner_id, class_id=class_id, chunks=doomed)

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
    `status=ready` ⟺ full chunk set committed & queryable (ADR 0020 §3).

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
        index_file(conn, file_id=file_id, filename="lecture.pdf",
                   owner_id=owner_id, class_id=class_id, chunks=doomed)

    # Nothing published. No chunks...
    count = conn.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    assert count == 0

    # ...and no `files` row AT ALL: the upsert (step 1) is inside the same transaction as the
    # insert (step 3), so it rolled back too. `ready` was never published for this file.
    row = conn.execute(
        "select status from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
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

    # Nothing published: the guard fires before the transaction opens, so the files upsert never ran.
    row = conn.execute(
        "select status from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert row is None
