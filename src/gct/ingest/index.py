"""Index - the atomic index transaction: the all-or-nothing write where the ingest pipeline's
in-memory row set becomes the queryable, provenance-carrying `chunks` set the Retriever reads
(ADR 0020; ingestion-worker.md §Internal-approach 6). The one place the write path touches Postgres.

The invariant this box exists to hold: a file's chunks are ALL-from-one-run or ABSENT, and
`files.status='ready'` flips inside the SAME transaction as the chunk insert - so `status=ready`
⟺ the full chunk set is committed & queryable (ADR 0020 §2-3), GIVEN the caller precondition in
`index_file`'s docstring. Atomicity is unconditional; PUBLICATION is not (ADR 0025 amends 0020's
unconditional claim). The transaction wraps only the write; the slow parse/chunk/embed work
already ran, with no transaction open.

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

    ONE transaction (ADR 0020 §3): upsert `files` -> `ready`, drop this file's old chunks, insert
    the new set. Commit as one unit; on any error nothing is committed. A `classes` row for
    `class_id` must already exist - this box never creates classes.

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

    Raises `ValueError` if `file_id` already names a row under a different `owner_id`/`class_id`.
    The raise happens inside the transaction, so nothing is written; it is terminal, never
    retryable (ADR 0011). Callers that mint their own id never hit it - only a supplied one can
    collide with a row someone else owns.

    Raises `ValueError` if `chunks` is empty: publishing `ready` with no chunks would break
    `status=ready` ⟺ full chunk set committed & queryable (ADR 0020, publication conditional per
    ADR 0025). The guard runs BEFORE the
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
        # 0. Scope guard: if `file_id` already names a row, it must be THIS caller's row.
        #    The upsert below sets only `status`/`updated_at`, so on its UPDATE branch the existing
        #    row's `owner_id`/`class_id` survive while step 3 writes chunks under the ones passed
        #    in. Nothing reconciles the two, so a caller supplying someone else's `file_id` leaves
        #    a `files` row scoped to one tenant owning chunks scoped to another - and retrieval
        #    filters on `owner_id AND class_id` (F6/F12), so those chunks answer for a student who
        #    never uploaded them. Refusing the write is the only outcome that keeps F6/F12 true.
        #    Widening the upsert to overwrite owner/class instead would make the mismatch vanish by
        #    letting any caller reassign a file - the same violation with no error to notice.
        #    Compared in SQL so Postgres normalizes the uuid: `select` returns NULL for no row,
        #    else the boolean.
        scope_ok = conn.execute(
            """
            select owner_id = %(owner_id)s and class_id = %(class_id)s::uuid
            from files where file_id = %(file_id)s::uuid
            """,
            {"file_id": file_id, "owner_id": owner_id, "class_id": class_id},
        ).fetchone()
        if scope_ok is not None and not scope_ok[0]:
            raise ValueError(
                f"file_id={file_id} exists under a different owner/class; "
                f"refusing to index chunks for owner_id={owner_id} class_id={class_id} onto it"
            )
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
        # 3. Insert the full new set (SQL-boundary conversions per the docstring).
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
