"""Issue #86 — a zombie's `_bury` plants a `failed_reason` on the file the winner publishes.

Run it against a scratch database (NEVER `grounded_class_tutor`):

    DATABASE_URL=postgresql://localhost:5432/gct_ship_86 uv run python \
        evidence/issue_86_zombie_bury_repro.py

Costs nothing: the embedder is a local stub, so no provider is ever constructed.

WHY A SCRIPT AND NOT ONLY A TEST. The fix ships with one
(`test_a_zombie_whose_lease_expired_writes_nothing_to_files`, tests/gct/jobs/test_worker.py) and
that test is the thing CI runs; it asserts two rows and an end state. This prints the whole
interleaving, step by step, on a connection none of the writers hold — so the claim "step 4 is the
defect" is readable rather than inferred from one failing assertion, and it stays readable for
someone who arrives after the branch is gone. It runs the
REAL `_bury`, the REAL `claim`/`reclaim_expired`, the REAL `processing` SQL `process_one` issues,
and the REAL `ingest_file`; nothing about the ordering is simulated except the lease expiry, which
is backdated rather than waited out (the same move `_backdate_lease` makes in the suite).

It exits 0 when the run ends `('ready', None)` — i.e. when the guard is present — and 1 when it
reproduces the defect, so it is usable as a check on either side of the fix.
"""

from __future__ import annotations

import io
import os
import sys
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from reportlab.pdfgen import canvas

from gct.config import EMBEDDING_DIM
from gct.db import connect
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.ingest.pipeline import ingest_file
from gct.jobs.queue import claim, enqueue, reclaim_expired
from gct.jobs.worker import _bury

# `process_one`'s own `processing` write, verbatim — the statement the winner issues between its
# claim and its `index_file`. Copied rather than called because calling `process_one` would run
# the ingest too, closing the very window this reproduction needs to step into.
WINNER_PROCESSING_SQL = """
    update files
    set status = 'processing',
        failed_reason = null,
        updated_at = now()
    where file_id = %(file_id)s::uuid
      and status <> 'ready'
"""


class FakeEmbeddings:
    """Deterministic and free — the same stub shape `tests/gct/jobs/test_worker.py` carries."""

    model_id = "fake-embed-3"

    def embed(self, texts):
        return [[float((hash(t) % 97) + 1)] + [0.0] * (EMBEDDING_DIM - 1) for t in texts]


def write_pdf(path: Path, page_texts: list[str]) -> Path:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    for text in page_texts:
        c.drawString(72, 700, text)
        c.showPage()
    c.save()
    path.write_bytes(buf.getvalue())
    return path


def observe(reader, file_id: str, label: str) -> tuple:
    """Read files + jobs on a connection NEITHER writer holds (ADR 0025's `db_other` rule)."""
    status, reason = reader.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    (state,) = reader.execute(
        "select state from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    print(f"{label:<52} files={status!r:<14} failed_reason={reason!r:<16} jobs={state!r}")
    return status, reason


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if url.endswith("/grounded_class_tutor"):
        print("refusing to run against grounded_class_tutor — use a scratch database")
        return 2

    owner_id = f"evidence-86-{uuid.uuid4()}"
    class_id = str(uuid.uuid4())

    winner = connect()  # also does the setup
    zombie = connect()
    reader = connect()
    for conn in (winner, zombie, reader):
        conn.autocommit = True

    try:
        winner.execute(
            "insert into classes (class_id, owner_id, name) values (%s::uuid, %s, %s)",
            (class_id, owner_id, "evidence 86"),
        )
        with TemporaryDirectory() as tmp:
            source = write_pdf(Path(tmp) / "lecture.pdf", ["the real lecture text"])
            file_id = enqueue(winner, path=str(source), owner_id=owner_id, class_id=class_id)

            zombie_job = claim(zombie, lease_seconds=900)
            assert zombie_job is not None
            observe(reader, file_id, "1. zombie claims")

            # Lease expiry is FORCED, not waited out: unambiguous, and no wall-clock cost.
            winner.execute(
                "update jobs set leased_until = now() - interval '1 hour' where file_id = %s::uuid",
                (file_id,),
            )
            assert reclaim_expired(winner) == 1
            winner_job = claim(winner, lease_seconds=900)
            assert winner_job is not None
            assert winner_job.lease_token != zombie_job.lease_token
            observe(reader, file_id, "2. lease expired, reaper requeued, winner claims")

            winner.execute(WINNER_PROCESSING_SQL, {"file_id": file_id})
            observe(reader, file_id, "3. winner writes processing")

            # The zombie is still running its own (doomed) attempt and hits the terminal path.
            # Its `fail()` correctly refuses — the lease is dead — and it logs as much.
            _bury(zombie, zombie_job, reason="unparseable", error="zombie's dead-lease bury")
            observe(reader, file_id, "4. ZOMBIE calls _bury (lease is dead)")

            ingest_file(
                winner_job.staging_ref,
                owner_id,
                class_id,
                file_id=file_id,
                embedder=FakeEmbeddings(),
                conn=winner,
                chunk_size=CHUNK_SIZE_WORDS,
                chunk_overlap=CHUNK_OVERLAP_WORDS,
            )
            status, reason = observe(reader, file_id, "5. winner publishes via index_file")

            (chunks,) = reader.execute(
                "select count(*) from chunks where file_id = %s::uuid", (file_id,)
            ).fetchone()
            print()
            print(f"FINAL: status={status!r} failed_reason={reason!r} chunks={chunks}")

            if (status, reason) == ("ready", None):
                print("PASS — the zombie wrote nothing; the guard is present.")
                return 0
            print(
                "FAIL — a queryable file is carrying a failure reason. Step 4 is the defect: "
                "the jobs half refused the dead lease and the files half wrote anyway."
            )
            return 1
    finally:
        for table in ("chunks", "jobs", "files", "classes"):
            reader.execute(f"delete from {table} where owner_id = %s", (owner_id,))
        for conn in (winner, zombie, reader):
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
