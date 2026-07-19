"""DB-backed tests for the atomic index transaction (issue #4, ADR 0020).

Runs against the real local Postgres via the `db` fixture (seeds a `classes` row, cleans up by
`owner_id`, skips when the DB is unreachable). Proves the two invariants this box exists to hold:
the full set lands with `status='ready'` in one shot, and a re-index replaces atomically (no dups).
"""
from __future__ import annotations

import uuid

from gct.config import EMBEDDING_DIM
from gct.ingest.index import index_file
from gct.ingest.pipeline import PreparedChunk

MODEL_ID = "fake-embed-3"


def _chunk(owner_id: str, class_id: str, text: str, page: int) -> PreparedChunk:
    """A PreparedChunk with a valid dim-1536 vector so the `vector(1536)` INSERT succeeds."""
    return PreparedChunk(
        text=text,
        file="lecture.pdf",
        page_or_slide=page,
        embedding=[0.0] * EMBEDDING_DIM,
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
        _chunk(owner_id, class_id, "third passage", 3),
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
        from chunks where file_id = %s::uuid order by page_or_slide
        """,
        (file_id,),
    ).fetchall()
    assert len(rows) == 3
    for row_owner, row_class, row_model, row_page in rows:
        assert row_owner == owner_id
        assert str(row_class) == class_id
        assert row_model == MODEL_ID
        assert isinstance(row_page, str)  # int -> text at the SQL boundary
    assert [r[3] for r in rows] == ["1", "2", "3"]


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
