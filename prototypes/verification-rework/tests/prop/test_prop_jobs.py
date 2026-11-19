"""PROTOTYPE (v3) model-based test of the job queue + worker: random uploads, worker ticks, provider
faults, crashes, lease expiries and a second worker, run against the REAL functions and a REAL
Postgres, and compared after every step with a small model of what the two tables should say.
It TRUNCATEs, so it needs a database of its own (CI's is born empty; locally use a scratch one)."""
import io
import os
import tempfile
import time
import types
import uuid
from pathlib import Path

from hypothesis import HealthCheck, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from reportlab.pdfgen import canvas

from gct.config import EMBEDDING_DIM
from gct.db import connect
from gct.jobs import queue, worker
from gct.providers.base import TransientEmbeddingError

assert "gct_agent_tests" in os.environ["DATABASE_URL"], "this test TRUNCATEs; scratch database only"
BUDGET, TMP, SLEEPS = 3, Path(tempfile.mkdtemp()), []
FAULTS = ["none", "none", "transient", "interrupt", "interrupt_in_bury", "reaped", "stolen",
          "stolen_and_finished", "lost_after_publish"]
worker.time = types.SimpleNamespace(sleep=SLEEPS.append, perf_counter=time.perf_counter, monotonic=time.monotonic)
REAL = types.SimpleNamespace(ingest=worker.ingest_file, complete=worker.complete, fail=worker.fail)
SETTLE = {"complete": {}, "fail": {"error": "x"}, "release": {"error": "x"}, "renew_lease": {"lease_seconds": 900}}


def staged(good: bool) -> Path:
    path, buf = TMP / f"{uuid.uuid4()}.pdf", io.BytesIO()
    page = canvas.Canvas(buf)
    page.drawString(72, 700, "some lecture text")
    page.save()
    path.write_bytes(buf.getvalue() if good else b"%PDF-1.7\nnot a pdf\n")
    return path


class Embedder:
    model_id = "fake"

    def embed(self, texts):
        return [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts]


class QueueAndWorker(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.conn, self.peer = connect(), connect()
        self.conn.autocommit = self.peer.autocommit = True
        self.peer.execute("truncate chunks, jobs, files, classes cascade")
        self.class_id = str(self.peer.execute("insert into classes (owner_id, name) values ('p', 'c') returning class_id").fetchone()[0])
        self.model, self.stolen, self.stale = {}, [], []  # expected rows; jobs the other worker holds; lost leases

    def teardown(self):
        self.peer.execute("truncate chunks, jobs, files, classes cascade")  # leave no queued job behind
        self.conn.close(), self.peer.close()

    def work(self, conn):
        return worker.process_one(conn, embedder=Embedder(), chunk_size=250, chunk_overlap=40, max_attempts=BUDGET)

    def lapse(self, file_id):  # the clock passes this job's lease and the reaper runs; its holder is now a zombie
        job_id, token = self.peer.execute("select job_id::text, lease_token::text from jobs where file_id = %s::uuid", (file_id,)).fetchone()
        self.stale.append(types.SimpleNamespace(job_id=job_id, lease_token=token))
        self.peer.execute("update jobs set leased_until = now() - interval '1 hour' where file_id = %s::uuid", (file_id,))
        assert queue.reclaim_expired(self.peer) == 1

    @rule(good=st.booleans())
    def upload(self, good):
        file_id = queue.enqueue(self.conn, path=staged(good), owner_id="p", class_id=self.class_id)
        self.model[file_id] = dict(good=good, job="queued", attempts=0, status="queued", reason=None)

    @rule(fault=st.sampled_from(FAULTS))
    def tick(self, fault):
        file_id = next((f for f, m in self.model.items() if m["job"] == "queued"), None)  # oldest queued
        m, armed = self.model.get(file_id), [True]

        def ingest(*args, **kwargs):  # whatever the scenario says happens while this worker is busy
            if armed[0]:
                armed[0] = False
                if fault == "transient":
                    raise TransientEmbeddingError("429")
                if fault == "interrupt":
                    raise KeyboardInterrupt
                if fault in ("reaped", "stolen", "stolen_and_finished"):
                    self.lapse(file_id)
                if fault == "stolen":
                    self.stolen.append(queue.claim(self.peer, lease_seconds=900))
                    assert self.stolen[-1].file_id == file_id
                if fault == "stolen_and_finished":
                    assert self.work(self.peer) is True
            return REAL.ingest(*args, **kwargs)

        def complete(conn, **ids):  # published, then the lease lapses before the worker can say so
            if fault == "lost_after_publish":
                self.lapse(file_id)
            return REAL.complete(conn, **ids)

        def fail(conn, **kwargs):
            if fault == "interrupt_in_bury":
                raise KeyboardInterrupt
            return REAL.fail(conn, **kwargs)

        worker.ingest_file, worker.complete, worker.fail = ingest, complete, fail
        SLEEPS.clear()
        try:
            worked, raised = self.work(self.conn), False
        except KeyboardInterrupt:
            worked, raised = True, True
        finally:
            worker.ingest_file, worker.complete, worker.fail = REAL.ingest, REAL.complete, REAL.fail
        assert worked is (m is not None), "a tick reports work exactly when there was a job"
        if m is None:
            return

        # ---- the model: what one claim of this file, with this fault, must leave behind
        m["attempts"] += 1
        published, interrupted = m["status"] == "ready", False

        def bury(reason):  # a terminal failure never unpublishes a ready file
            if not published:
                m.update(status="failed", reason=reason)

        if m["attempts"] > BUDGET:
            bury("transient_exhausted")
            interrupted = fault == "interrupt_in_bury"
            m["job"] = "queued" if interrupted else "failed"
        else:
            if not published:
                m.update(status="processing", reason=None)
            if fault in ("transient", "interrupt", "reaped"):
                m["job"], interrupted = "queued", fault == "interrupt"
            elif fault == "stolen":
                m.update(job="processing", attempts=m["attempts"] + 1)
            elif fault == "stolen_and_finished":  # the other worker's own claim, start to finish
                m["attempts"] += 1
                if m["attempts"] > BUDGET or not m["good"]:
                    bury("transient_exhausted" if m["attempts"] > BUDGET else "unparseable")
                    m["job"] = "failed"
                else:
                    m.update(job="done", status="ready", reason=None)
            elif not m["good"]:
                bury("unparseable")
                interrupted = fault == "interrupt_in_bury"
                m["job"] = "queued" if interrupted else "failed"
            else:
                m.update(job="queued" if fault == "lost_after_publish" else "done", status="ready", reason=None)
        assert raised is interrupted, "Ctrl-C swallowed, or raised from nowhere"
        backs_off = fault == "transient" and m["attempts"] < BUDGET
        assert SLEEPS == ([min(2.0 * 2 ** (m["attempts"] - 1), 60.0)] if backs_off else []), "ADR 0020 backoff"

    @precondition(lambda self: self.stolen)
    @rule(how=st.sampled_from(["release", "crash", "renew"]))
    def the_other_worker(self, how):
        job = self.stolen[0]
        ids = dict(job_id=job.job_id, lease_token=job.lease_token)
        if how == "renew":
            assert queue.renew_lease(self.peer, lease_seconds=900, **ids) is True
            return
        self.stolen.pop(0)
        if how == "release":
            assert queue.release(self.peer, error="shutdown", **ids) is True
            self.stale.append(job)
        else:
            self.lapse(job.file_id)
        self.model[job.file_id]["job"] = "queued"

    @precondition(lambda self: self.stale)
    @rule(data=st.data(), verb=st.sampled_from(sorted(SETTLE)))
    def a_worker_that_lost_its_lease_tries_to_settle(self, data, verb):
        job = data.draw(st.sampled_from(self.stale))
        settled = getattr(queue, verb)(self.peer, job_id=job.job_id, lease_token=job.lease_token, **SETTLE[verb])
        assert settled is False, f"{verb}() by a worker that no longer holds the lease was accepted"

    @invariant()
    def the_database_says_what_the_model_says(self):
        rows = self.peer.execute(
            """select f.file_id::text, f.status, f.failed_reason, j.state, j.attempts, j.leased_until is not null,
                      (select count(*) from chunks c where c.file_id = f.file_id)
               from files f join jobs j using (file_id)""").fetchall()
        assert len(rows) == len(self.model)
        for file_id, status, reason, job, attempts, leased, chunks in rows:
            m = self.model[file_id]
            assert (status, reason, job, attempts) == (m["status"], m["reason"], m["job"], m["attempts"]), m
            # true whatever the model thinks:
            assert (status == "failed") == (reason is not None), "a status and a reason that contradict each other"
            assert chunks == (1 if status == "ready" else 0), "ready with nothing behind it, duplicates, or orphans"
            assert leased if job == "processing" else not (leased and job == "queued"), "lease/state mismatch"
            assert job != "done" or status == "ready"
            assert job != "failed" or status in ("failed", "ready")


QueueAndWorker.TestCase.settings = settings(max_examples=100, stateful_step_count=30, deadline=None,
                                            suppress_health_check=list(HealthCheck))
test_queue_and_worker = QueueAndWorker.TestCase
