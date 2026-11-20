"""DB-backed tests for the single worker (issues #71, #187).

Every assertion about what the worker PUBLISHED goes through `db_other`, a second connection: a
connection sees its own uncommitted work (ADR 0025). Tests that call `process_one` use `worker_db`,
whose connection already holds the worker lock. `time.sleep` is stubbed wherever a backoff runs.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from gct.config import EMBEDDING_DIM
from gct.db import connect
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.ingest.parse import ParseError
from gct.ingest.pipeline import ingest_file
from gct.jobs import worker
from gct.jobs.queue import (
    claim,
    enqueue,
    holds_worker_lock,
    requeue_orphans,
    try_acquire_worker_lock,
)
from gct.jobs.worker import backoff_seconds, process_one
from gct.providers.base import TransientEmbeddingError


class FakeEmbeddings:
    """Records every batch (before raising), and raises `TransientEmbeddingError` N times."""

    model_id = "fake-embed-3"

    def __init__(self, transient_failures: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._failures_left = transient_failures

    def embed(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        if self._failures_left > 0:
            self._failures_left -= 1
            raise TransientEmbeddingError("simulated provider 429 - slow down")
        return [[float((hash(t) % 97) + 1)] + [0.0] * (EMBEDDING_DIM - 1) for t in texts]


class Stop(Exception):
    """Sentinel - the only way out of `run`'s `while True`."""


def write_corrupt_pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.7\nthis is not a PDF at all\n")
    return path


def write_pdf(path: Path, page_texts: list[str]) -> Path:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    for text in page_texts:
        c.drawString(72, 700, text)
        c.showPage()
    c.save()
    path.write_bytes(buf.getvalue())
    return path


def tick(conn, embedder, **overrides):
    return process_one(
        conn,
        embedder=embedder,
        chunk_size=CHUNK_SIZE_WORDS,
        chunk_overlap=CHUNK_OVERLAP_WORDS,
        **overrides,
    )


def _file_row(db_other, file_id: str) -> tuple:
    return db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()


def _job_row(db_other, file_id: str) -> tuple:
    return db_other.execute(
        "select state, attempts from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()


def _chunk_count(db_other, file_id: str) -> int:
    return db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]


def _another_connection():
    # Through `connect()`, not `conn.info.dsn`: psycopg redacts the password from `info.dsn`.
    conn = connect()
    conn.autocommit = True
    return conn


# --- The happy path -----------------------------------------------------------------------------


def test_happy_path_publishes_through_a_second_connection(worker_db, db_other, tmp_path):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    source = write_pdf(tmp_path / "lecture-3.pdf", ["alpha page one words", "beta page two words"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    assert tick(conn, FakeEmbeddings()) is True

    assert _file_row(db_other, file_id) == ("ready", None)
    assert _job_row(db_other, file_id) == ("done", 1)
    assert _chunk_count(db_other, file_id) > 0


def test_processing_status_publishes_on_a_plain_connection(worker_db, db_other, tmp_path):
    conn, owner_id, class_id = worker_db  # deliberately NOT autocommit
    source = write_pdf(tmp_path / "plain-conn.pdf", ["plain connection page words"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    assert tick(conn, FakeEmbeddings()) is True

    assert _file_row(db_other, file_id)[0] == "ready"
    assert _job_row(db_other, file_id)[0] == "done"


def test_empty_queue_is_a_normal_tick(worker_db):
    conn, _owner_id, _class_id = worker_db
    conn.autocommit = True

    assert tick(conn, FakeEmbeddings()) is False


def test_process_one_refuses_a_connection_without_the_worker_lock(db, db_other, tmp_path):
    conn, owner_id, class_id = db
    conn.autocommit = True
    file_id = enqueue(
        conn, path=write_pdf(tmp_path / "x.pdf", ["words"]), owner_id=owner_id, class_id=class_id
    )

    with pytest.raises(RuntimeError, match="worker lock"):
        tick(conn, FakeEmbeddings())

    assert _job_row(db_other, file_id) == ("queued", 0)


def test_chunk_window_reaches_ingest(worker_db, db_other, tmp_path):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    many_words = " ".join(f"word{i}" for i in range(60))
    first = write_pdf(tmp_path / "default-window.pdf", [many_words])
    second = write_pdf(tmp_path / "small-window.pdf", [many_words])

    # One claimable job at a time, so the pairing of file to window does not rest on `now()`.
    first_id = enqueue(conn, path=first, owner_id=owner_id, class_id=class_id)
    assert tick(conn, FakeEmbeddings())
    second_id = enqueue(conn, path=second, owner_id=owner_id, class_id=class_id)
    assert process_one(conn, embedder=FakeEmbeddings(), chunk_size=20, chunk_overlap=5)

    default_chunks = _chunk_count(db_other, first_id)
    small_chunks = _chunk_count(db_other, second_id)
    assert default_chunks > 0
    assert small_chunks > 0
    assert default_chunks != small_chunks


# --- The loop -----------------------------------------------------------------------------------


def _script_the_loop(monkeypatch, ticks, *, orphans=0):
    """Stub `run`'s collaborators; return `(events, slept)`. Raises `Stop` when `ticks` run out."""
    events: list[tuple[str, object]] = []
    slept: list[float] = []
    remaining = iter(ticks)

    def fake_lock(conn):
        events.append(("lock", conn))
        return True

    def fake_requeue(conn):
        events.append(("requeue", conn))
        return orphans

    def fake_process_one(conn, **kwargs):
        events.append(("tick", conn))
        try:
            return next(remaining)
        except StopIteration:
            raise Stop from None

    monkeypatch.setattr(worker, "try_acquire_worker_lock", fake_lock)
    monkeypatch.setattr(worker, "requeue_orphans", fake_requeue)
    monkeypatch.setattr(worker, "process_one", fake_process_one)
    monkeypatch.setattr(worker.time, "sleep", slept.append)
    return events, slept


def test_run_sleeps_only_on_empty_ticks(monkeypatch):
    _events, slept = _script_the_loop(monkeypatch, [True, True, False, True])

    with pytest.raises(Stop):
        worker.run(object(), embedder=None, chunk_size=1, chunk_overlap=0, poll_seconds=0.25)

    assert slept == [0.25]


def test_run_takes_the_lock_and_requeues_orphans_once_before_the_first_claim(monkeypatch):
    conn = object()
    events, _slept = _script_the_loop(monkeypatch, [True, False])

    with pytest.raises(Stop):
        worker.run(conn, embedder=None, chunk_size=1, chunk_overlap=0, poll_seconds=0)

    assert events == [
        ("lock", conn),
        ("requeue", conn),
        ("tick", conn),
        ("tick", conn),
        ("tick", conn),
    ]


def test_run_waits_while_another_connection_holds_the_lock_and_starts_once_released(
    db, monkeypatch
):
    conn, _owner_id, _class_id = db
    conn.autocommit = True
    holder = _another_connection()
    started = threading.Event()
    outcome: dict[str, object] = {}

    def first_tick(conn, **kwargs):
        outcome["held_lock"] = holds_worker_lock(conn)
        started.set()
        raise Stop

    monkeypatch.setattr(worker, "process_one", first_tick)

    def run_worker():
        try:
            worker.run(conn, embedder=None, chunk_size=1, chunk_overlap=0, lock_retry_seconds=0.05)
        except BaseException as exc:  # noqa: BLE001 - handed to the main thread
            outcome["exc"] = exc

    try:
        assert try_acquire_worker_lock(holder)
        thread = threading.Thread(target=run_worker, daemon=True)
        thread.start()

        assert not started.wait(0.5), "run claimed work while another connection held the lock"
        assert thread.is_alive()
    finally:
        holder.close()

    thread.join(5)
    assert not thread.is_alive(), "run never started after the lock was released"
    assert isinstance(outcome.get("exc"), Stop)
    assert outcome["held_lock"] is True


def test_run_requeues_an_orphaned_job_at_start_and_then_completes_it(
    db, db_other, tmp_path, monkeypatch
):
    conn, owner_id, class_id = db
    conn.autocommit = True
    source = write_pdf(tmp_path / "orphan.pdf", ["a page the previous worker never finished"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    dead = _another_connection()
    try:
        assert try_acquire_worker_lock(dead)
        assert claim(dead) is not None
    finally:
        dead.close()
    assert _job_row(db_other, file_id) == ("processing", 1)

    def stop_when_idle(_seconds):
        raise Stop

    monkeypatch.setattr(worker.time, "sleep", stop_when_idle)
    with pytest.raises(Stop):
        worker.run(
            conn,
            embedder=FakeEmbeddings(),
            chunk_size=CHUNK_SIZE_WORDS,
            chunk_overlap=CHUNK_OVERLAP_WORDS,
        )

    assert _file_row(db_other, file_id) == ("ready", None)
    assert _job_row(db_other, file_id) == ("done", 2)
    assert _chunk_count(db_other, file_id) > 0


def test_backoff_doubles_and_is_capped():
    base = worker.BACKOFF_BASE_SECONDS
    cap = worker.BACKOFF_MAX_SECONDS

    assert backoff_seconds(1) == base
    assert backoff_seconds(2) == base * 2
    assert backoff_seconds(3) == base * 4
    assert all(backoff_seconds(n) <= cap for n in range(1, 50))
    assert backoff_seconds(49) == cap


# --- The failure split (ADR 0020) ---------------------------------------------------------------


def test_a_corrupt_file_fails_terminally_and_buys_no_embed(
    worker_db, db_other, tmp_path, monkeypatch
):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    slept: list[float] = []
    monkeypatch.setattr(worker.time, "sleep", slept.append)
    embedder = FakeEmbeddings()
    file_id = enqueue(
        conn,
        path=write_corrupt_pdf(tmp_path / "shredded.pdf"),
        owner_id=owner_id,
        class_id=class_id,
    )

    assert tick(conn, embedder) is True

    assert _file_row(db_other, file_id) == ("failed", "unparseable")
    state, attempts, last_error = db_other.execute(
        "select state, attempts, last_error from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (state, attempts) == ("failed", 1)
    assert last_error and "unparseable" in last_error
    assert embedder.calls == []
    assert slept == []
    assert _chunk_count(db_other, file_id) == 0


def test_a_transient_failure_backs_off_then_requeues_the_job(
    worker_db, db_other, tmp_path, monkeypatch
):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    slept: list[float] = []
    monkeypatch.setattr(worker.time, "sleep", slept.append)
    source = write_pdf(tmp_path / "flaky.pdf", ["some words the provider choked on"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    assert tick(conn, FakeEmbeddings(transient_failures=1)) is True

    state, attempts, last_error = db_other.execute(
        "select state, attempts, last_error from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (state, attempts) == ("queued", 1)
    assert last_error and "429" in last_error
    assert _file_row(db_other, file_id) == ("processing", None)
    assert _chunk_count(db_other, file_id) == 0
    assert slept == [backoff_seconds(1)]


def test_a_retry_after_a_transient_failure_publishes_normally(
    worker_db, db_other, tmp_path, monkeypatch
):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    embedder = FakeEmbeddings(transient_failures=1)
    source = write_pdf(tmp_path / "second-time-lucky.pdf", ["alpha words", "beta words"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    for _ in range(2):
        assert tick(conn, embedder) is True

    assert _file_row(db_other, file_id) == ("ready", None)
    assert _job_row(db_other, file_id) == ("done", 2)
    assert len(embedder.calls) == 2
    assert _chunk_count(db_other, file_id) > 0


def test_the_budget_runs_out_and_the_file_fails_transient_exhausted(
    worker_db, db_other, tmp_path, monkeypatch
):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    slept: list[float] = []
    monkeypatch.setattr(worker.time, "sleep", slept.append)
    embedder = FakeEmbeddings(transient_failures=99)
    source = write_pdf(tmp_path / "doomed.pdf", ["words the provider never accepts"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    for _ in range(3):
        assert tick(conn, embedder, max_attempts=2) is True

    assert _file_row(db_other, file_id) == ("failed", "transient_exhausted")
    assert _job_row(db_other, file_id) == ("failed", 3)
    assert len(embedder.calls) == 2, "the budget bounds PAID attempts; the burying claim is free"
    assert slept == [backoff_seconds(1)], "the last permitted attempt must not wait for nothing"
    assert _chunk_count(db_other, file_id) == 0


def test_a_terminal_failure_cannot_unpublish_a_file_that_is_already_ready(
    worker_db, db_other, tmp_path, monkeypatch
):
    # Reachable with one worker: it crashed after `index_file` committed, the job was requeued
    # as an orphan, and the bytes at `staging_ref` no longer parse.
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_corrupt_pdf(tmp_path / "already-published.pdf")
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    conn.execute("update files set status = 'ready' where file_id = %s::uuid", (file_id,))

    assert tick(conn, FakeEmbeddings()) is True

    assert _file_row(db_other, file_id) == ("ready", None)
    assert _job_row(db_other, file_id)[0] == "failed"


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, RuntimeError])
def test_an_unexpected_exception_propagates_and_leaves_the_job_processing_until_requeued(
    worker_db, db_other, tmp_path, monkeypatch, exc_type
):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    real_ingest = worker.ingest_file
    armed = {"on": True}

    def crash_once(*args, **kwargs):
        if armed["on"]:
            armed["on"] = False
            raise exc_type("unclassified")
        return real_ingest(*args, **kwargs)

    monkeypatch.setattr(worker, "ingest_file", crash_once)
    source = write_pdf(tmp_path / "crashes-once.pdf", ["a page the first attempt never finished"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    with pytest.raises(exc_type):
        tick(conn, FakeEmbeddings())

    assert _job_row(db_other, file_id) == ("processing", 1)
    assert _file_row(db_other, file_id) == ("processing", None)
    assert _chunk_count(db_other, file_id) == 0
    assert tick(conn, FakeEmbeddings()) is False, "an orphan is not claimable until requeued"

    assert requeue_orphans(conn) == 1
    assert tick(conn, FakeEmbeddings()) is True

    assert _job_row(db_other, file_id) == ("done", 2)
    assert _file_row(db_other, file_id) == ("ready", None)


def test_an_orphan_that_had_already_published_reruns_and_leaves_one_chunk_set(
    worker_db, db_other, tmp_path
):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    embedder = FakeEmbeddings()
    pdf = write_pdf(tmp_path / "lecture-4.pdf", ["rerun page one", "rerun page two"])
    file_id = enqueue(conn, path=pdf, owner_id=owner_id, class_id=class_id)

    # The worker published and died before `complete`.
    job = claim(conn)
    assert job is not None
    ingest_file(
        job.staging_ref,
        job.owner_id,
        job.class_id,
        file_id=job.file_id,
        embedder=embedder,
        conn=conn,
        chunk_size=CHUNK_SIZE_WORDS,
        chunk_overlap=CHUNK_OVERLAP_WORDS,
    )
    published = _chunk_count(db_other, file_id)
    assert published > 0

    assert requeue_orphans(conn) == 1
    assert tick(conn, embedder) is True

    assert len(embedder.calls) == 2, "the rerun re-ingests rather than inheriting the chunks"
    assert _chunk_count(db_other, file_id) == published, "replaced, not doubled"
    assert _file_row(db_other, file_id) == ("ready", None)
    assert _job_row(db_other, file_id) == ("done", 2)


# --- The claim clears the previous attempt's `failed_reason` (issue #24) ------------------------
#
# A half-bury is a crash between `_bury`'s `files` write and its `jobs` write: the file reads
# `failed` while the job is still `processing`. The next start requeues it (`requeue_orphans`),
# and the next claim's `processing` write must clear the stale reason.


def _half_bury(conn, monkeypatch, *, source, owner_id, class_id) -> str:
    """Leave `source` half-buried (file `failed`, job `processing`), then requeue it."""
    real_fail = worker.fail
    armed = {"on": True}

    def fail_or_crash(*args, **kwargs):
        if armed["on"]:
            raise KeyboardInterrupt("crash inside `_bury`, between its files and jobs writes")
        return real_fail(*args, **kwargs)

    monkeypatch.setattr(worker, "fail", fail_or_crash)
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    with pytest.raises(KeyboardInterrupt):
        tick(conn, FakeEmbeddings())
    armed["on"] = False
    assert requeue_orphans(conn) == 1
    return file_id


def test_a_crash_inside_bury_self_heals_on_the_next_attempt(
    worker_db, db_other, tmp_path, monkeypatch
):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    source = write_corrupt_pdf(tmp_path / "half-buried.pdf")
    file_id = _half_bury(conn, monkeypatch, source=source, owner_id=owner_id, class_id=class_id)
    assert _file_row(db_other, file_id) == ("failed", "unparseable")
    assert _job_row(db_other, file_id) == ("queued", 1)

    assert tick(conn, FakeEmbeddings()) is True

    assert _file_row(db_other, file_id) == ("failed", "unparseable")
    assert _job_row(db_other, file_id) == ("failed", 2)


def test_the_claim_clears_the_previous_attempts_failed_reason(
    worker_db, db_other, tmp_path, monkeypatch
):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_corrupt_pdf(tmp_path / "half-buried.pdf")
    file_id = _half_bury(conn, monkeypatch, source=source, owner_id=owner_id, class_id=class_id)
    assert _file_row(db_other, file_id) == ("failed", "unparseable")
    assert _job_row(db_other, file_id)[0] == "queued"

    def transient(*_args, **_kwargs):
        raise TransientEmbeddingError("simulated provider 429 - this attempt settles nothing")

    monkeypatch.setattr(worker, "ingest_file", transient)
    assert tick(conn, FakeEmbeddings()) is True

    assert _file_row(db_other, file_id) == ("processing", None)


def test_a_retry_that_succeeds_leaves_no_failed_reason_on_the_ready_file(
    worker_db, db_other, tmp_path, monkeypatch
):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_corrupt_pdf(tmp_path / "arrived-late.pdf")
    file_id = _half_bury(conn, monkeypatch, source=source, owner_id=owner_id, class_id=class_id)
    assert _file_row(db_other, file_id) == ("failed", "unparseable")

    write_pdf(source, ["the real lecture text finally arrived at the staging path"])
    assert tick(conn, FakeEmbeddings()) is True

    assert _file_row(db_other, file_id) == ("ready", None)
    assert _job_row(db_other, file_id)[0] == "done"
    assert _chunk_count(db_other, file_id) > 0


def test_the_claim_does_not_clear_a_reason_on_a_file_that_is_already_ready(
    worker_db, db_other, tmp_path, monkeypatch
):
    # The clear must ride the `status <> 'ready'` UPDATE, not a separate unguarded one.
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_corrupt_pdf(tmp_path / "already-published.pdf")
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    conn.execute(
        "update files set status = 'ready', failed_reason = 'unparseable' where file_id = %s::uuid",
        (file_id,),
    )

    assert tick(conn, FakeEmbeddings()) is True

    assert _file_row(db_other, file_id) == ("ready", "unparseable")
    assert _job_row(db_other, file_id)[0] == "failed"


def test_a_terminal_failure_after_a_cleared_reason_writes_the_new_reason_not_the_old(
    worker_db, db_other, tmp_path, monkeypatch
):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_corrupt_pdf(tmp_path / "fails-twice.pdf")
    file_id = _half_bury(conn, monkeypatch, source=source, owner_id=owner_id, class_id=class_id)
    assert _file_row(db_other, file_id) == ("failed", "unparseable")

    seen: list[tuple] = []

    def empty_after_looking(*_args, **_kwargs):
        seen.append(_file_row(db_other, file_id))
        raise ParseError("empty", "the file parsed but contained no extractable text")

    monkeypatch.setattr(worker, "ingest_file", empty_after_looking)
    assert tick(conn, FakeEmbeddings()) is True

    assert seen == [("processing", None)]
    assert _file_row(db_other, file_id) == ("failed", "empty")


def test_a_transient_failure_after_a_half_bury_leaves_processing_with_no_reason(
    worker_db, db_other, tmp_path, monkeypatch
):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    slept: list[float] = []
    monkeypatch.setattr(worker.time, "sleep", slept.append)
    source = write_corrupt_pdf(tmp_path / "then-a-429.pdf")
    file_id = _half_bury(conn, monkeypatch, source=source, owner_id=owner_id, class_id=class_id)
    assert _file_row(db_other, file_id) == ("failed", "unparseable")

    write_pdf(source, ["a page the provider refuses exactly once"])
    embedder = FakeEmbeddings(transient_failures=1)
    assert tick(conn, embedder) is True

    assert _file_row(db_other, file_id) == ("processing", None)
    assert _job_row(db_other, file_id) == ("queued", 2)
    assert len(embedder.calls) == 1
    assert slept == [backoff_seconds(2)]


def test_the_diagnostic_trail_survives_the_clear(worker_db, db_other, tmp_path, monkeypatch):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_corrupt_pdf(tmp_path / "keeps-its-trail.pdf")
    file_id = _half_bury(conn, monkeypatch, source=source, owner_id=owner_id, class_id=class_id)
    first_error = "transient: an earlier attempt's 429"
    conn.execute("update jobs set last_error = %s where file_id = %s::uuid", (first_error, file_id))

    write_pdf(source, ["the real lecture text"])
    assert tick(conn, FakeEmbeddings()) is True

    status, state, last_error = db_other.execute(
        """
        select f.status, j.state, j.last_error
        from files f join jobs j using (file_id) where f.file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert (status, state) == ("ready", "done")
    assert last_error == first_error


def test_a_budget_exhausted_bury_is_not_undone_by_a_later_claim(
    worker_db, db_other, tmp_path, monkeypatch
):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_corrupt_pdf(tmp_path / "out-of-budget.pdf")
    file_id = _half_bury(conn, monkeypatch, source=source, owner_id=owner_id, class_id=class_id)
    assert _file_row(db_other, file_id) == ("failed", "unparseable")

    statements: list[str] = []
    real_execute = conn.execute

    def recording_execute(query, *args, **kwargs):
        statements.append(str(query))
        return real_execute(query, *args, **kwargs)

    monkeypatch.setattr(conn, "execute", recording_execute)
    assert tick(conn, FakeEmbeddings(), max_attempts=1) is True

    assert not any("set status = 'processing'" in sql for sql in statements), (
        "the exhausted claim ran the `processing` write - the budget check is no longer first"
    )
    assert _file_row(db_other, file_id) == ("failed", "transient_exhausted")
    assert _job_row(db_other, file_id)[0] == "failed"


def test_the_worker_publishes_a_files_row_that_agrees_with_its_chunks(
    worker_db, db_other, tmp_path, monkeypatch
):
    conn, owner_id, class_id = worker_db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_pdf(tmp_path / "week-3-lecture.pdf", ["page one text", "page two text"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    assert tick(conn, FakeEmbeddings()) is True

    status, reason, f_owner, f_class, f_name = db_other.execute(
        """
        select status, failed_reason, owner_id, class_id::text, filename
        from files where file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert (status, reason) == ("ready", None)
    scopes = db_other.execute(
        "select distinct owner_id, class_id::text, file from chunks where file_id = %s::uuid",
        (file_id,),
    ).fetchall()
    assert scopes == [(f_owner, f_class, f_name)]
    assert f_name == source.name
