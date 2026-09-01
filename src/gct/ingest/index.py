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

A publish can also be REFUSED before it starts. `index_file` takes an optional `publish_guard`
and calls it inside the transaction; a False answer raises `PublishRefused` and nothing is
written (#92, ADR 0030). The predicate is opaque to this module - it is how the write path checks
a caller's entitlement to publish without this box learning that jobs or leases exist.

Idempotent by construction: processing a `file_id` is `DELETE FROM chunks WHERE file_id` then
insert the full set, so re-running the same `file_id` replaces cleanly with no dedup keys. The
`files` row is written via upsert (`ON CONFLICT (file_id) DO UPDATE`): first call inserts the
minimal row (Slice 1), a repeat lands on UPDATE (drives the re-index test, and is what Slice 2
needs when the row pre-exists - wrap, not rewrite; PM-4 seam).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import psycopg

from gct.db import require_idle


class PublishRefused(Exception):
    """The caller's `publish_guard` vetoed this publish; NOTHING was written.

    Raised from inside `index_file`'s transaction, which is what makes the veto atomic: the
    raise propagates out of `conn.transaction()`, so the block rolls back and the `files`
    upsert, the chunk delete and the chunk insert are all discarded together. A caller that
    catches this has the same guarantee a failed publish gives - all or nothing (ADR 0020).

    NOT an error condition. `gct.jobs.worker` raises it on the routine at-least-once outcome
    (ADR 0011): a worker whose lease was reaped mid-ingest reaching the publish after another
    worker took the job. Callers log it and move on; it is not a `ParseError` and buys no retry.
    """

    def __init__(self, file_id: str) -> None:
        super().__init__(
            f"publish of file_id={file_id} refused by the caller's publish_guard; nothing written"
        )
        self.file_id = file_id


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
    publish_guard: Callable[[psycopg.Connection], bool] | None = None,
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

    `publish_guard`, when given, is called ONCE inside the transaction and before any write; a
    False answer raises `PublishRefused` and the block rolls back, so a refused publish writes
    nothing at all (#92, ADR 0030). It is a predicate over `conn` deliberately: this box never
    learns what entitlement is being checked, which is what keeps the PM-4 seam (ADR 0020) intact
    while the check still runs inside the transaction it protects. Omit it and this function
    behaves exactly as it did before #92. Step 0's comment carries the argument.

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
        # 0. THE CALLER'S VETO, INSIDE THE TRANSACTION IT PROTECTS (#92). `publish_guard` answers
        #    one question this box is not allowed to ask: is this caller STILL ENTITLED to publish?
        #    Ingest is slow and the entitlement can expire while it runs - under ADR 0011's
        #    at-least-once queue a worker's lease is reaped mid-ingest and the job re-handed out,
        #    and the reaped worker then arrives here with a complete, correct chunk set and no
        #    right to publish it. Nothing else on this path notices: every other write to `files`
        #    checks the lease and this one did not, which is the whole of #92.
        #
        #    THE SEAM IS KEPT BY NOT KNOWING WHAT THE GUARD CHECKS (PM-4, ADR 0020, extended per
        #    ADR 0030). The parameter is a predicate over `conn`; this module never learns that
        #    `jobs`, leases or workers exist, and Slice 1's direct callers pass nothing and get
        #    the pre-#92 behavior exactly. The alternative - clearing `failed_reason` in the
        #    upsert below - closes the same path but makes this box a second writer for a
        #    job-layer column, which ADR 0030 §3 records as considered and declined.
        #
        #    IT RUNS ON `conn`, NOT ON A CONNECTION OF THE GUARD'S OWN, and that is the entire
        #    point of passing it: checked on any other connection the answer would be true
        #    OUTSIDE this transaction and unenforceable inside it. Here the check and the writes
        #    are one atomic unit, so whatever the guard locks stays locked until this commits.
        #    A guard that opens its own connection re-introduces the check-then-act gap this
        #    argument exists to close - see `worker._publish_guard` for what holding it buys.
        #
        #    RAISE, NOT RETURN FALSE. The transaction must roll back, and a raise out of
        #    `conn.transaction()` is what does that; a bool would leave the caller to remember an
        #    explicit rollback, and a caller who forgot would publish exactly the state the guard
        #    just refused. It is also the load-bearing difference for a caller who ignores the
        #    result entirely - an ignored exception stops the program, an ignored bool does not.
        if publish_guard is not None and not publish_guard(conn):
            raise PublishRefused(file_id)
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
        #    THE OMISSION WAS NOT COST-FREE, AND #92 WAS THE COST - PAID ABOVE, NOT HERE. This
        #    statement is the only writer of `ready` and reads no lease, so a worker whose lease
        #    expired mid-ingest used to republish `ready` over a reason a live-leased `_bury`
        #    committed while it was working. #92 closed that by ENTITLEMENT (step 0's
        #    `publish_guard`) rather than by cleanup: the reaped worker never reaches this
        #    statement, so there is no stale reason here to clear. Clearing it instead was the
        #    considered alternative and ADR 0030 §3 records why it lost - it would move the
        #    column's second writer into this box to fix a problem that is not about the column.
        #
        #    THE GUARD IS OPT-IN, WHICH BOUNDS WHAT THE SENTENCE ABOVE PROMISES (ADR 0030 §4). A
        #    caller that passes no `publish_guard` is unguarded by construction; that is correct
        #    for Slice 1's direct callers, which hold no lease and race no bury, and it is a trap
        #    for any future caller that acquires one. `worker.process_one` is the only guarded
        #    caller today.
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
