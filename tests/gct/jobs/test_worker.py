"""DB-backed tests for the worker's happy path (issue #71, PR 1).

Same discipline as `test_queue.py`: real Postgres via the `db` fixture, and every assertion
about what the worker PUBLISHED goes through `db_other` - a second connection. A connection
sees its own uncommitted work, so reading back through `db` would hold whether or not anything
was committed (ADR 0025); the durability assertion IS the point of the happy-path test, not a
flourish on it.

`FakeEmbeddings` and `write_pdf` are LOCAL on purpose: the root conftest records that the
ingest factories "deliberately stayed put" in `tests/gct/ingest/`, pytest does not expose a
conftest sideways, and `test_ask_smoke.py` set the precedent of a suite outside `ingest/`
carrying its own minimal stubs shaped to what IT asserts. Here that shape is: real enough for
`parse_file` to parse (the worker actually opens the file - `test_queue`'s bytes-stub is not
enough), and an embedder whose `calls` list becomes PR 2's assertion surface for "no retries
spent".
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from gct.config import EMBEDDING_DIM
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.jobs import worker
from gct.jobs.queue import enqueue
from gct.jobs.worker import process_one


class FakeEmbeddings:
    """Deterministic, free, and LOUD about how often it was called.

    `calls` records every batch handed to `embed` - unused by PR 1's tests beyond existing,
    but it is the surface PR 2's "terminal failure spends zero retries" assertion reads.
    """

    model_id = "fake-embed-3"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        # Distinct-but-arbitrary vectors; nothing here asserts on ranking (same stance as
        # test_ask_smoke's stub).
        return [[float((hash(t) % 97) + 1)] + [0.0] * (EMBEDDING_DIM - 1) for t in texts]


def write_pdf(path: Path, page_texts: list[str]) -> Path:
    """A real, parseable PDF - one page per entry in `page_texts`."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    for text in page_texts:
        c.drawString(72, 700, text)
        c.showPage()
    c.save()
    path.write_bytes(buf.getvalue())
    return path


def test_happy_path_publishes_through_a_second_connection(db, db_other, tmp_path):
    """queued -> processing -> ready for a real file, proven on a connection the worker never had.

    This is the acceptance item issue #71 bolds - the ADR 0025 durability test. Assert through
    `db_other`, ALL of it:
      - files.status == 'ready' (and failed_reason is null)
      - jobs.state == 'done'
      - a non-zero chunk count for this file_id
    A same-connection read passes even when the savepoint bug is present, so any assertion
    made through `db` here proves nothing about publication.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True  # the worker's wiring contract (ADR 0025) - `db`'s conn is not
    source = write_pdf(tmp_path / "lecture-3.pdf", ["alpha page one words", "beta page two words"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    processed = process_one(
        conn,
        embedder=FakeEmbeddings(),
        chunk_size=CHUNK_SIZE_WORDS,
        chunk_overlap=CHUNK_OVERLAP_WORDS,
    )

    assert processed is True

    # All three reads go through `db_other`. Through `conn` they would pass even if the
    # savepoint bug (ADR 0025) meant nothing was ever published - see the fixture's docstring.
    status, failed_reason = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (status, failed_reason) == ("ready", None)

    (state,) = db_other.execute(
        "select state from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert state == "done"

    # Both status columns above are strings a bug could set with no content behind them; this
    # is the one assertion that the ingest actually produced rows.
    (chunk_count,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert chunk_count > 0


def test_empty_queue_is_a_normal_tick(db):
    """Nothing enqueued: process_one returns False and raises nothing - the loop's idle case."""
    conn, _owner_id, _class_id = db
    conn.autocommit = True

    processed = process_one(
        conn,
        embedder=FakeEmbeddings(),
        chunk_size=CHUNK_SIZE_WORDS,
        chunk_overlap=CHUNK_OVERLAP_WORDS,
    )

    assert processed is False


def test_chunk_window_reaches_ingest(db, db_other, tmp_path):
    """The chunk window is forwarded verbatim, never hardcoded (ADR 0019; issue #71 bolds this).

    Same proof shape as test_pipeline / test_ask_smoke: process two files under two DIFFERENT
    windows - one at the module defaults, one chosen so the same text must chunk differently
    (small `chunk_size`, page text long enough to split) - and assert their chunk counts
    differ through `db_other`. A worker that silently ignored the parameters goes red here
    because both runs would land on the default count.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    # IDENTICAL text in both files - it is the control. Differing chunk counts prove the window
    # reached the chunker only if nothing else about the input differed.
    many_words = " ".join(f"word{i}" for i in range(60))
    first = write_pdf(tmp_path / "default-window.pdf", [many_words])
    second = write_pdf(tmp_path / "small-window.pdf", [many_words])

    # Enqueued and processed ONE AT A TIME rather than both up front. `process_one` takes no
    # file - it claims whatever `claim` picks, `order by created_at`. Enqueuing both first would
    # leave the pairing of file to window resting on two `now()` stamps landing in the intended
    # order; with one claimable job at a time it rests on nothing.
    first_id = enqueue(conn, path=first, owner_id=owner_id, class_id=class_id)
    assert process_one(
        conn,
        embedder=FakeEmbeddings(),
        chunk_size=CHUNK_SIZE_WORDS,
        chunk_overlap=CHUNK_OVERLAP_WORDS,
    )

    # 60 words fits whole inside the 250-word default, so the run above is one chunk; a 20-word
    # window has to split the same text. Any pair that forces different counts would do.
    second_id = enqueue(conn, path=second, owner_id=owner_id, class_id=class_id)
    assert process_one(
        conn,
        embedder=FakeEmbeddings(),
        chunk_size=20,
        chunk_overlap=5,
    )

    (default_chunks,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (first_id,)
    ).fetchone()
    (small_chunks,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (second_id,)
    ).fetchone()

    # Both non-zero first: a worker that ingested nothing at all would satisfy `!=` on 0 vs 0
    # only by accident, but it would satisfy plenty of other broken shapes, and "0 != 1" is not
    # the fact this test is about.
    assert default_chunks > 0
    assert small_chunks > 0
    assert default_chunks != small_chunks


def test_run_sleeps_only_on_empty_ticks(monkeypatch):
    """The loop naps on an idle tick and polls straight through a productive one.

    No DB and no `run` parameter added for testability: `while True` is exited by making the
    stubbed `process_one` raise once its script runs out. The alternative - a `max_ticks` knob
    on `run` - would put a branch in production code that only tests ever take.

    This pins the bug the loop shipped with in review: forwarding nothing and sleeping after
    EVERY tick, which adds `poll_seconds` of dead time to every job in a busy queue.
    """

    class Stop(Exception):
        """Sentinel - the only way out of the loop."""

    ticks = iter([True, True, False, True])
    slept: list[float] = []

    def fake_process_one(conn, **kwargs):
        try:
            return next(ticks)
        except StopIteration:
            raise Stop from None

    monkeypatch.setattr(worker, "process_one", fake_process_one)
    monkeypatch.setattr(worker.time, "sleep", slept.append)

    with pytest.raises(Stop):
        worker.run(None, embedder=None, chunk_size=1, chunk_overlap=0, poll_seconds=0.25)

    # Four ticks, exactly one of them empty - so exactly one sleep, at the configured interval.
    assert slept == [0.25]
