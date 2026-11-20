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
from gct.ids import canonical_uuid, require_canonical_uuid

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
    before ingesting on the same connection (`components/ingestion-worker.md`, step 1). Claiming a
    job is a write, so the claim alone is enough to trigger this.
    ENFORCED, not merely documented: `require_idle` below raises rather than letting the call
    look like it worked. ADR 0025 declined this guard predicting it would fire on benign code;
    ADR 0027 measured that prediction (every firing was a caller already in the hazardous state,
    zero false alarms) and reversed it - accepted via #75.

    Raises `ValueError` before the transaction opens on an id that cannot reach those casts
    intact (#126): `file_id` must already be canonical, `class_id` - the argument and the one on
    every chunk - is canonicalised. The guards below carry which is which and why.

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
    # Connection guard first: a wiring error outranks a payload error, and every guard here must
    # fire before the transaction opens so a refused call writes nothing at all.
    require_idle(conn, "index_file")

    # ID GUARDS (#126). FIVE `%(...)s::uuid` binds across the three statements below took the
    # caller's spelling raw - `file_id` in the `files` upsert, the chunk delete and the chunk
    # insert, `class_id` in the upsert and again on every chunk in that insert - and
    # `uuid.UUID` accepts spellings the cast refuses (`urn:uuid:<id>`), so a caller that had
    # already checked the id with Python still failed inside Postgres - naming a Postgres type
    # and no remedy. They run BEFORE `conn.transaction()`, so a refused call is indistinguishable
    # from one that never happened.
    #
    # STRICT ON `file_id`, LENIENT ON `class_id`, and the split is not this function's invention:
    # it is the criterion `enqueue`'s docstring states - where the id comes from, not reader vs
    # writer. `file_id` is the id `ingest_file` either minted (`uuid4`) or read back out of the
    # `jobs` row a worker claimed, canonical by construction in both cases, so any other spelling
    # is an upstream bug and converting it quietly would hide it; `ingest_file`'s own `file_id`
    # guard already refuses on exactly this reasoning, and this is the same rule enforced at the
    # boundary a direct caller of `index_file` actually crosses. `class_id` travels down from a
    # person - a script, the upload route's form field - and the sibling boundaries that take it
    # (`enqueue`, `class_exists`, `retrieve`) all convert, so refusing here would make this the
    # one place in the library that disagrees about the same id.
    #
    # THE COST OF NOT HAVING THIS WAS PAID EARLIER THAN THE CAST. `index_file` is reached from
    # `ingest_file`, which parses, chunks and EMBEDS first, so a mis-spelled `class_id` used to
    # buy a whole embedding run before Postgres rejected it. That is why `ingest_file` now
    # canonicalises before `compose` as well: this guard is the boundary's, not the saving.
    file_id = require_canonical_uuid(
        file_id,
        fn="index_file",
        param="file_id",
        remedy=(
            "Pass the id `ingest_file` returned or the one on the claimed job row; a file_id "
            "reaches this function from the database, where it is already canonical, so any "
            "other spelling means the caller built it somewhere it should not have."
        ),
    )
    class_id = canonical_uuid(
        class_id,
        fn="index_file",
        param="class_id",
        remedy=(
            "Pass the id `create_class` returned for the class this file belongs to - an id "
            "that is not a uuid cannot name any class, so the file and its chunks would have "
            "nowhere to be scoped to."
        ),
    )
    # EVERY CHUNK'S OWN `class_id` TOO, not just the file-level one. Step 3 binds `chunk.class_id`
    # into its own cast, so guarding only the argument would let a urn-spelled chunk clear steps 1
    # and 2 and abort at the insert - a half-guard that trades an early refusal for a late one.
    # Canonicalised into a parallel list rather than in the comprehension below so the refusal
    # still lands before `BEGIN`; the per-chunk value is kept (not overwritten with `class_id`)
    # because a chunk disagreeing with its file about scope is a caller bug this box must not
    # silently paper over - it would break the F6/F12 filter with no trace.
    chunk_class_ids = [
        canonical_uuid(
            chunk.class_id,
            fn="index_file",
            param="chunk class_id",
            remedy=(
                "Every PreparedChunk carries the scope its rows are written under (F6/F12); "
                "build them with the same canonical class_id the file is indexed under."
            ),
        )
        for chunk in chunks
    ]

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
                        "class_id": chunk_class_id,
                        "text": chunk.text,
                        "file": chunk.file,
                        "page_or_slide": str(chunk.page_or_slide),
                        "embedding": chunk.embedding,
                        "embedding_model_id": chunk.embedding_model_id,
                    }
                    for chunk, chunk_class_id in zip(chunks, chunk_class_ids, strict=True)
                ],
            )
