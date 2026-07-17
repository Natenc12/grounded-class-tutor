"""Index - the atomic index transaction: the all-or-nothing write where the ingest pipeline's
in-memory row set becomes the queryable, provenance-carrying `chunks` set the Retriever reads
(ADR 0020; ingestion-worker.md §Internal-approach 6). The one place the write path touches Postgres.

The invariant this box exists to hold: a file's chunks are ALL-from-one-run or ABSENT, and
`files.status='ready'` flips inside the SAME transaction as the chunk insert - so `status=ready`
⟺ the full chunk set is committed & queryable (ADR 0020 §2-3). The transaction wraps only the write;
the slow parse/chunk/embed work already ran, with no transaction open.

Idempotent by construction: processing a `file_id` is `DELETE FROM chunks WHERE file_id` then insert
the full set, so re-running the same `file_id` replaces cleanly with no dedup keys. The `files` row is
written via upsert (`ON CONFLICT (file_id) DO UPDATE`): first call inserts the minimal row (Slice 1),
a repeat lands on UPDATE (drives the re-index test, and is what Slice 2 needs when the row pre-exists -
wrap, not rewrite; PM-4 seam).
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
    """Atomically write `chunks` for `file_id` and publish the file as `ready`, all in one transaction.

    ONE transaction (ADR 0020 §3), in order:
      1. Upsert the `files` row -> `status='ready'`
         (`INSERT ... ON CONFLICT (file_id) DO UPDATE SET status='ready', updated_at=now()`) - must
         precede the chunk insert to satisfy the `chunks.file_id -> files.file_id` FK. A `classes` row
         for `class_id` must already exist (caller/fixture seeds it; this box never creates classes).
      2. `DELETE FROM chunks WHERE file_id = :file_id` - drops the old set (idempotent re-index).
      3. `INSERT` the full new chunk set from `chunks`.
    Commit as one unit; on any error nothing is committed (all-or-nothing).

    SQL-boundary conversions (schema quirks, migrations/0001_init.sql):
      - `page_or_slide` int -> text (`chunks.page_or_slide` is `text`).
      - uuid columns (`file_id`, `class_id`) -> cast `%s::uuid`; `owner_id` is `text`.
      - `embedding` (`list[float]`) adapts to `vector(1536)` only if the connection registered the
        pgvector type - `conn` MUST come from `gct.db.connect()`.
    """
    raise NotImplementedError
