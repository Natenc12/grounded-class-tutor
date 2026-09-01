"""Index - the atomic index transaction: the all-or-nothing write where the ingest pipeline's
in-memory row set becomes the queryable, provenance-carrying `chunks` set the Retriever reads
(ADR 0020; ingestion-worker.md §Internal-approach 6). The one place the write path touches Postgres.

The invariant this box exists to hold: a file's chunks are ALL-from-one-run or ABSENT, and
`files.status='ready'` flips inside the SAME transaction as the chunk insert - so `status=ready`
⟺ the full chunk set is committed & queryable (ADR 0020 §2-3), GIVEN the caller precondition in
`index_file`'s docstring. Atomicity is unconditional; PUBLICATION is not (ADR 0025 amends 0020's
unconditional claim) - and the precondition is now ENFORCED, not just documented: `index_file`
refuses a non-IDLE connection (ADR 0025, guarded per ADR 0027). The transaction wraps only the
write; the slow parse/chunk/embed work already ran, with no transaction open.

Idempotent by construction: processing a `file_id` is `DELETE FROM chunks WHERE file_id` then
insert the full set, so re-running the same `file_id` replaces cleanly with no dedup keys. The
`files` row is written via upsert (`ON CONFLICT (file_id) DO UPDATE`): first call inserts the
minimal row (Slice 1), a repeat lands on UPDATE (drives the re-index test, and is what Slice 2
needs when the row pre-exists - wrap, not rewrite; PM-4 seam).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg

from gct.db import require_idle

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

    PRECONDITION ON `conn` - the publication half of that guarantee is CONDITIONAL (ADR 0025,
    guarded per ADR 0027): `conn` MUST NOT already be inside a transaction. psycopg opens an
    implicit transaction on a connection's first statement, and `Connection.transaction()` checks
    the state - already `INTRANS` means the block below issues a SAVEPOINT rather than BEGIN, and
    releasing a savepoint commits NOTHING. This function would then return successfully having
    published nothing: the rows visible only to this connection, destroyed by any later rollback.
    Atomicity survives (a savepoint rollback still discards exactly its own writes); PUBLICATION
    does not.
    Satisfy it with a fresh connection, `conn.autocommit = True`, or an explicit commit since the
    last statement. `scripts/ask_smoke.py` uses autocommit; a Slice 2 worker MUST COMMIT ITS CLAIM
    before ingesting on the same connection (`components/ingestion-worker.md`, step 1) - leasing a
    job is a write, so the lease alone is enough to trigger this.
    ENFORCED, not merely documented: `require_idle` below raises rather than letting the call
    look like it worked. ADR 0025 declined this guard predicting it would fire on benign code;
    ADR 0027 measured that prediction (every firing was a caller already in the hazardous state,
    zero false alarms) and reversed it - accepted via #75.

    SQL-boundary conversions (schema quirks, migrations/0001_init.sql):
      - `page_or_slide` int -> text (`chunks.page_or_slide` is `text`).
      - uuid columns (`file_id`, `class_id`) -> cast `%s::uuid`; `owner_id` is `text`.
      - `embedding` (`list[float]`) adapts to `vector(1536)` only if the connection registered the
        pgvector type - `conn` MUST come from `gct.db.connect()`.

    Raises `ValueError` if `chunks` is empty: publishing `ready` with no chunks would break
    `status=ready` ⟺ full chunk set committed & queryable (ADR 0020, publication conditional per
    ADR 0025). The guard runs BEFORE the
    transaction opens, so nothing is written at all - not even the `files` row.
    """
    # Connection guard first: a wiring error outranks a payload error, and both must fire before
    # the transaction opens so a refused call writes nothing at all.
    require_idle(conn, "index_file")

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
        #
        #    THE UPDATE BRANCH REFRESHES SCOPE, not just status (issue #24). Step 3 below replaces
        #    the chunk set wholesale from THIS call's arguments, so a `DO UPDATE` that kept the old
        #    `filename`/`owner_id`/`class_id` published a `files` row describing a chunk set that no
        #    longer exists: retrieval filters chunks on `owner_id AND class_id` (F6/F12) and cites
        #    `chunks.file`, so the two rows can disagree about who owns the file, which class it is
        #    in, and what it is called - and the disagreement is silent.
        #
        #    REJECTED ALTERNATIVE - REFUSE a call whose scope differs from the stored row. It keeps
        #    the original assignment intact, which refresh does not: under F6/F12, refresh means any
        #    caller supplying a `file_id` can silently reassign an existing file's owner and class,
        #    and this box has no way to tell a legitimate re-ingest from that. Refresh was taken
        #    because the alternative failure is worse - a row and its own chunks disagreeing is
        #    unrecoverable from the data alone, while a bad reassignment is at least visible in it -
        #    and because Slice 2's only caller derives all three from the job row it claimed.
        #    Revisit when an API adapter starts taking `file_id` from a client (Slice 3).
        #
        #    `status` stays a LITERAL rather than `excluded.status`: this function is the publisher,
        #    so `ready` is its decision, not something the caller's payload gets to supply.
        #
        #    `failed_reason` is DELIBERATELY ABSENT from both halves. `worker._bury` is its only
        #    writer and the worker clears it at the claim, inside its own `processing` write, which
        #    keeps this box pure of job/queue/retry machinery (PM-4 seam, ADR 0020). Adding it here
        #    would make the publisher a second writer for a column it knows nothing about - pinned
        #    by `test_index_file_does_not_clear_failed_reason`.
        #
        #    THE OMISSION IS NOT COST-FREE, AND #92 IS THE COST. This statement is the only writer
        #    of `ready`, reads no lease, and leaves the reason where it is - so a worker whose
        #    lease expired mid-ingest republishes `ready` over a reason a live-leased `_bury`
        #    committed while it was working, and a student queries a file wearing a failure
        #    message. Reachable today, demonstrated by execution. Whether the seam or the
        #    invariant gives way is #92's decision, not this comment's.
        conn.execute(
            """
            insert into files (file_id, owner_id, class_id, filename, status)
            values (%(file_id)s::uuid, %(owner_id)s, %(class_id)s::uuid, %(filename)s, 'ready')
            on conflict (file_id) do update set
                filename   = excluded.filename,
                owner_id   = excluded.owner_id,
                class_id   = excluded.class_id,
                status     = 'ready',
                updated_at = now()
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
