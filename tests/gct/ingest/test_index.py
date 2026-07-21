"""DB-backed tests for the atomic index transaction (issue #4, ADR 0020).

Runs against the real local Postgres via the `db` fixture (seeds a `classes` row, cleans up by
`owner_id`, skips when the DB is unreachable). Proves the two invariants this box exists to hold:
the full set lands with `status='ready'` in one shot, and a re-index replaces atomically (no dups).
"""
from __future__ import annotations

import uuid

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


def test_reindex_replaces_atomically(db):
    """Calling twice on the same file_id with a different set leaves ONLY the second set - the
    all-or-nothing DELETE-then-insert replace, no duplicates (idempotent by construction)."""
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


# --- Atomicity + write-path guards (issue #23) — STUBS, not yet implemented -------------------
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
    test goes green while proving nothing about atomicity (see plan-23 §Risks).
    """
    pytest.skip("stub — see plan-23-index-hardening.md, build order step 5")


def test_midwrite_failure_on_first_index_publishes_nothing(db):
    """FIRST-INDEX path: a write that fails partway publishes NOTHING — no chunks, no `files` row.

    This is the path on which the status half of the invariant is actually provable. On a re-index
    `status` was already 'ready' before the failure, so asserting it "did not flip" proves nothing;
    on a first index the `files` upsert rolls back with the chunks, so the row is absent entirely.
    `status=ready` ⟺ full chunk set committed & queryable (ADR 0020 §3).
    """
    pytest.skip("stub — see plan-23-index-hardening.md, build order step 6")


def test_index_file_rejects_empty_chunk_set(db):
    """`index_file(chunks=[])` raises rather than publishing 'ready' with zero chunks.

    Today it publishes: `executemany` over an empty sequence is a no-op, so the `files` upsert and
    the DELETE commit alone — contradicting `status=ready` ⟺ full chunk set queryable. Unreachable
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
