"""Index - the atomic index transaction: the all-or-nothing write where the ingest pipeline's
in-memory row set becomes the queryable, provenance-carrying `chunks` set the Retriever reads
(ADR 0020; ingestion-worker.md §Internal-approach 6). The one place the write path touches Postgres.

The invariant this box exists to hold: a file's chunks are ALL-from-one-run or ABSENT, and
`files.status='ready'` flips inside the SAME transaction as the chunk insert - so `status=ready`
⟺ the full chunk set is committed & queryable (ADR 0020 §2-3). The transaction wraps only the write;
the slow parse/chunk/embed work already ran, with no transaction open.

Idempotent by construction: processing a `file_id` is `DELETE FROM chunks WHERE file_id` then
insert the full set, so re-running the same `file_id` replaces cleanly with no dedup keys. The
`files` row is written via upsert (`ON CONFLICT (file_id) DO UPDATE`): first call inserts the
minimal row (Slice 1), a repeat lands on UPDATE (drives the re-index test, and is what Slice 2
needs when the row pre-exists - wrap, not rewrite; PM-4 seam).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg

if TYPE_CHECKING:
    # Type-only import: keeps the pipeline -> index runtime edge one-directional (no import cycle).
    from gct.ingest.pipeline import PreparedChunk


def index_file(
    conn: psycopg.Connection,
    *,
    file_id: str,
    filename: str,
    owner_id: str,
    class_id: str,
    chunks: list[PreparedChunk],
) -> None:
    """Atomically write `chunks` for `file_id` and publish the file `ready`, in one transaction.

    ONE transaction (ADR 0020 §3), in order:
      1. Upsert the `files` row -> `status='ready'`
         (`INSERT ... ON CONFLICT (file_id) DO UPDATE SET status='ready', updated_at=now()`) - must
         precede the chunk insert to satisfy the `chunks.file_id -> files.file_id` FK. A `classes`
         row for `class_id` must already exist (caller/fixture seeds it; this box never creates
         classes).
      2. `DELETE FROM chunks WHERE file_id = :file_id` - drops the old set (idempotent re-index).
      3. `INSERT` the full new chunk set from `chunks`.
    Commit as one unit; on any error nothing is committed (all-or-nothing).

    PRECONDITION ON `conn` - the publication half of that guarantee is CONDITIONAL (ADR 0025):
    `conn` MUST NOT already be inside a transaction. psycopg opens an implicit transaction on a
    connection's first statement, and `Connection.transaction()` checks the state - already
    `INTRANS` means the block below issues a SAVEPOINT rather than BEGIN, and releasing a savepoint
    commits NOTHING. This function then returns successfully having published nothing: the rows are
    visible only to this connection, and any later rollback destroys them. Atomicity survives (a
    savepoint rollback still discards exactly its own writes); PUBLICATION does not.
    Satisfy it with a fresh connection, `conn.autocommit = True`, or an explicit commit since the
    last statement. `scripts/ask_smoke.py` uses autocommit; a Slice 2 worker MUST COMMIT ITS CLAIM
    before ingesting on the same connection (`components/ingestion-worker.md`, step 1) - leasing a
    job is a write, so the lease alone is enough to trigger this.
    Deliberately not guarded here: a runtime check would depend on statement order inside the
    caller rather than on caller intent, so it would fire on benign code (ADR 0025, Alternatives).

    SQL-boundary conversions (schema quirks, migrations/0001_init.sql):
      - `page_or_slide` int -> text (`chunks.page_or_slide` is `text`).
      - uuid columns (`file_id`, `class_id`) -> cast `%s::uuid`; `owner_id` is `text`.
      - `embedding` (`list[float]`) adapts to `vector(1536)` only if the connection registered the
        pgvector type - `conn` MUST come from `gct.db.connect()`.

    Raises `ValueError` if `chunks` is empty: publishing `ready` with no chunks would break
    `status=ready` ⟺ full chunk set committed & queryable (ADR 0020). The guard runs BEFORE the
    transaction opens, so nothing is written at all - not even the `files` row.
    """
    # Empty-set guard: `executemany` over an empty sequence is a no-op, so steps 1-2 below would
    # commit alone and publish a `ready` file with zero chunks. Raise (not assert): this guard must
    # hold even under `python -O`, which strips assert statements.
    if not chunks:
        raise ValueError(
            f"index_file called with zero chunks for file_id={file_id}; "
            "publishing 'ready' with no chunks violates ADR 0020"
        )

    with conn.transaction():
        # 1. Publish the files row as `ready` (upsert). Must precede the chunk insert: chunks FK to
        #    files.file_id. A repeat file_id lands on the UPDATE branch (idempotent re-index).
        conn.execute(
            """
            insert into files (file_id, owner_id, class_id, filename, status)
            values (%(file_id)s::uuid, %(owner_id)s, %(class_id)s::uuid, %(filename)s, 'ready')
            on conflict (file_id) do update set status = 'ready', updated_at = now()
            """,
            {"file_id": file_id, "owner_id": owner_id, "class_id": class_id, "filename": filename},
        )
        # 2. Drop the old set for this file - re-running replaces cleanly, no dedup keys (ADR 0020).
        conn.execute(
            "delete from chunks where file_id = %(file_id)s::uuid",
            {"file_id": file_id},
        )
        # 3. Insert the full new set. page_or_slide int -> text; ids cast ::uuid at the boundary;
        #    embedding (list[float]) adapts to vector(1536) via the registered pgvector type.
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into chunks
                    (file_id, owner_id, class_id, text, file, page_or_slide,
                     embedding, embedding_model_id)
                values
                    (%(file_id)s::uuid, %(owner_id)s, %(class_id)s::uuid, %(text)s, %(file)s,
                     %(page_or_slide)s, %(embedding)s, %(embedding_model_id)s)
                """,
                [
                    {
                        "file_id": file_id,
                        "owner_id": chunk.owner_id,
                        "class_id": chunk.class_id,
                        "text": chunk.text,
                        "file": chunk.file,
                        "page_or_slide": str(chunk.page_or_slide),
                        "embedding": chunk.embedding,
                        "embedding_model_id": chunk.embedding_model_id,
                    }
                    for chunk in chunks
                ],
            )
