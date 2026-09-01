"""Tests for `scripts/ingest_smoke.py` — the parts of the ceremony that can go wrong SILENTLY.

WHY A CEREMONY SCRIPT HAS TESTS AT ALL, when ADR 0009 calls the scripts thin peer callers that
usually earn none: `ingest_smoke.py` is the thing that decides whether Slice 2 shipped, and almost
every way for it to be wrong is a way for it to go GREEN. A `partial_index_sightings` that returns
`[]` for the wrong reason, a `reaper_evidence` that says yes to a plain retry, an
`illegal_transitions` whose table quietly permits everything — each one still prints PASS, and the
run that would have caught it costs real money and cannot be run in CI. That is the same shape of
defect `test_ask_smoke.py` and `test_worker_script.py` were written for: invisible from reading,
visible only from running, and only if you already knew the answer.

Everything here is offline and free except the one `db`-marked test at the bottom: no provider
client is constructed anywhere in this file, deliberately, so nothing here takes a `live_*` fixture
and nothing here can spend money. The ceremony's PAID half — that a real worker really does move a
real file — is not simulated here; it is what running the script itself proves.

`parse_file` is driven for real over the ceremony's own corrupt bytes, because the reason phase 2
asserts (`unparseable`) is a fact about pypdf, not about this repo, and pinning it here is what
turns a pypdf upgrade that reclassified those bytes into a red CI run rather than a ceremony
asserting against a reason nothing produces any more.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from gct.config import EMBEDDING_DIM
from gct.ingest.parse import ParseError, parse_file

# Imported by PATH, the same way `test_worker_script.py` and `test_ask_smoke.py` do it and for the
# same reason: `scripts/` is deliberately not a package (ADR 0009 — the scripts are peers of the
# library, not part of it), so there is nothing to `import` and bending `sys.path` would blur the
# boundary.
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ingest_smoke.py"
_spec = importlib.util.spec_from_file_location("ingest_smoke_under_test", _SCRIPT)
smoke = importlib.util.module_from_spec(_spec)
# Registered in `sys.modules` BEFORE it is executed, which the other two script tests do not need
# to do. `@dataclass` resolves its own module out of `sys.modules` while processing the class body
# (to recognise `KW_ONLY`), so a module executed without being registered raises `AttributeError`
# on `Snapshot` — not a missing import, and nothing about the script is wrong. Registering first is
# the documented shape of the from-a-path import; the two-line version only works for a file with
# no dataclass in it.
sys.modules[_spec.name] = smoke
_spec.loader.exec_module(smoke)


def snapshot(status: str, chunks: int, **overrides) -> smoke.Snapshot:
    """A `Snapshot` with only the fields a given test cares about spelled out.

    The two axes almost every test here is about are `files.status` and the chunk count; the job
    columns default to a plausible in-flight reading so a test about the partial-index rule does
    not have to state an opinion about `attempts` to say what it means.
    """
    return smoke.Snapshot(
        file_status=status,
        failed_reason=overrides.get("failed_reason"),
        job_state=overrides.get("job_state", "processing"),
        attempts=overrides.get("attempts", 1),
        last_error=overrides.get("last_error"),
        chunks=chunks,
    )


# --- the terminal-failure input -----------------------------------------------------------------


def test_the_ceremonys_corrupt_bytes_really_parse_to_unparseable(tmp_path):
    """Phase 2 asserts `failed_reason == 'unparseable'`; this is what makes that assertion true.

    The reason is not this repo's decision — it is what pypdf does with a file that has a PDF
    header and no body, routed through `parse.py`'s taxonomy. Read off a real run rather than
    guessed, and pinned here so it stays read off one: a pypdf release that started rejecting these
    bytes at the EXTENSION check, or parsing them into zero pages (`empty`), would leave phase 2
    quietly asserting a reason nothing produces, and the ceremony would fail for a reason that has
    nothing to do with the write path.
    """
    corrupt = smoke.write_corrupt_pdf(tmp_path)
    with pytest.raises(ParseError) as caught:
        parse_file(corrupt)
    assert caught.value.reason == "unparseable"


def test_the_corrupt_file_is_written_with_a_pdf_suffix(tmp_path):
    """The suffix is what routes it to the PDF parser at all.

    Rename it `.txt` and `parse_file` raises `unsupported` without opening anything — a terminal
    failure too, but a much weaker demonstration: it proves the extension whitelist works, not that
    a parser met bad bytes and classified them. Phase 2 is written for the second claim.
    """
    assert smoke.write_corrupt_pdf(tmp_path).suffix == ".pdf"


# --- what a history is allowed to say -----------------------------------------------------------


def test_status_path_collapses_repeats_and_keeps_order():
    """The observer samples far faster than the worker moves, so most readings are duplicates."""
    history = [
        snapshot("queued", 0),
        snapshot("processing", 0),
        snapshot("processing", 0, attempts=2),
        snapshot("ready", 12, job_state="done"),
    ]
    assert smoke.status_path(history) == ["queued", "processing", "ready"]


def test_the_happy_path_has_no_illegal_transitions():
    assert smoke.illegal_transitions(["queued", "processing", "ready"]) == []


def test_a_bury_followed_by_a_later_claim_is_legal():
    """`failed -> processing` is PERMITTED, and deliberately so.

    `_bury` writes `files` before `jobs`, so a crash between the two leaves a `failed` file under a
    still-claimable job; the reaper requeues it and the next claim's `processing` write is what
    recovers the row. A table that forbade this transition would report the recovery path as a
    defect.
    """
    assert smoke.illegal_transitions(["failed", "processing", "ready"]) == []


@pytest.mark.parametrize("after", ["processing", "failed", "queued"])
def test_ready_is_absorbing(after):
    """Nothing may follow `ready` — the one status that is a promise already made to the student.

    Both writers that could leave it carry the same `status <> 'ready'` guard (`process_one`'s
    claim write and `_bury`'s files write), so a sighting of anything after `ready` means one of
    those guards stopped holding. This is the check that would notice.
    """
    assert smoke.illegal_transitions(["ready", after]) == [("ready", after)]


def test_a_status_outside_the_schemas_check_set_is_reported():
    """`files.status` is CHECK-constrained, so an unknown value means the schema moved."""
    assert smoke.illegal_transitions(["queued", "quarantined"]) == [("queued", "quarantined")]


# --- no partial index ever visible --------------------------------------------------------------


def test_a_clean_first_ingest_shows_no_partial_index():
    history = [
        snapshot("queued", 0),
        snapshot("processing", 0),
        snapshot("ready", 62, job_state="done"),
    ]
    assert smoke.partial_index_sightings(history) == []


def test_a_redelivery_that_replaced_the_set_shows_no_partial_index():
    """Two publishes of the same file under the same window are the same size — that is idempotence.

    This is phase 3's shape: the zombie publishes, then the winner republishes over it. Both
    sightings carry the full count, and the count in between never dips.
    """
    history = [
        snapshot("queued", 0),
        snapshot("processing", 0),
        snapshot("ready", 62),
        snapshot("ready", 62, job_state="done"),
    ]
    assert smoke.partial_index_sightings(history) == []


def test_ready_over_zero_chunks_is_a_violation():
    """`status = 'ready'` ⟺ a full chunk set is committed and queryable (ADR 0020).

    A `ready` row with nothing behind it is the trust failure in its purest form: the file is
    advertised as answerable and grounds nothing.
    """
    published = snapshot("ready", 0, job_state="done")
    assert smoke.partial_index_sightings([snapshot("processing", 0), published]) == [published]


def test_a_count_between_zero_and_full_is_a_violation():
    """A chunk set caught mid-write — literally partial.

    Not reachable through `index_file`, whose delete-and-insert is one transaction, which is the
    guarantee rather than a reason not to look: a publish that escaped that transaction would show
    up exactly here, on a real second connection.
    """
    caught = snapshot("processing", 30)
    history = [snapshot("processing", 0), caught, snapshot("ready", 62, job_state="done")]
    assert smoke.partial_index_sightings(history) == [caught]


def test_a_ready_sighting_that_disagrees_with_the_publishing_run_is_a_violation():
    """The issue's own wording: `ready` against a count that disagrees with the run that published.

    Here an earlier publish carried fewer chunks than the file ever ends up with — a shrunken set
    advertised as complete. Self-consistent, and still a lie.
    """
    stale = snapshot("ready", 40)
    history = [snapshot("processing", 0), stale, snapshot("ready", 62, job_state="done")]
    assert smoke.partial_index_sightings(history) == [stale]


def test_a_file_that_never_published_has_nothing_partial_about_it():
    """Phase 2's file: zero chunks throughout, `failed` at the end. Zero is not partial."""
    history = [snapshot("queued", 0), snapshot("failed", 0, job_state="failed", attempts=1)]
    assert smoke.partial_index_sightings(history) == []


# --- the reaper's fingerprint -------------------------------------------------------------------


def test_one_delivery_is_not_a_reclaim():
    reaped, why = smoke.reaper_evidence([snapshot("ready", 62, job_state="done")])
    assert not reaped
    assert "delivered once" in why


def test_a_redelivery_with_a_clean_error_column_is_the_reaper():
    """`attempts >= 2` says a redelivery happened; a null `last_error` says the reaper caused it.

    Every other route back to `queued` writes that column — `release` on a transient failure, `fail`
    on a bury — so the reaper is the only requeue that leaves it untouched. Together the two
    columns identify the writer without reading a single log line.
    """
    reaped, why = smoke.reaper_evidence([snapshot("ready", 62, job_state="done", attempts=2)])
    assert reaped
    assert "attempts=2" in why


def test_a_retry_after_a_transient_failure_is_not_the_reaper():
    """The test that stops phase 3 from passing on the wrong mechanism.

    A 429 mid-embed also produces `attempts == 2`, and a check that only counted deliveries would
    call that a reclaim and print PASS for a run in which no lease ever expired. `release` writes
    `last_error`, so the two are distinguishable — but only if something looks.
    """
    history = [snapshot("ready", 62, job_state="done", attempts=2, last_error="transient: 429")]
    reaped, why = smoke.reaper_evidence(history)
    assert not reaped
    assert "not from the reaper" in why


# --- the lifecycle gate itself ------------------------------------------------------------------


def test_a_file_that_reached_ready_without_being_seen_processing_fails():
    """THE assertion issue #72 is built around, tested against the history that would fake it.

    An end-state check reads this history as a clean pass: it starts `queued`, it ends `ready`,
    the chunks are there. Nothing about the worker was demonstrated — `processing` is the only
    status the worker itself writes, and it was never seen.
    """
    history = [snapshot("queued", 0), snapshot("ready", 62, job_state="done")]
    faults = smoke._lifecycle_faults(smoke.status_path(history), history)
    assert any("'processing' was never observed" in fault for fault in faults)


def test_a_fully_observed_lifecycle_has_no_faults():
    history = [
        snapshot("queued", 0, job_state="queued", attempts=0),
        snapshot("processing", 0),
        snapshot("ready", 62, job_state="done"),
    ]
    assert smoke._lifecycle_faults(smoke.status_path(history), history) == []


def test_a_ready_file_whose_job_never_settled_is_a_fault():
    """`ready` on the file axis and `processing` on the job axis is a job nobody completed.

    It is a legal INTERMEDIATE reading under at-least-once — phase 3 sees it, while the winner is
    still working — but as a FINAL reading it means the lease was lost and nothing ever settled the
    job, which the happy path must not do.
    """
    history = [
        snapshot("queued", 0, job_state="queued", attempts=0),
        snapshot("processing", 0),
        snapshot("ready", 62, job_state="processing"),
    ]
    faults = smoke._lifecycle_faults(smoke.status_path(history), history)
    assert any("jobs.state='processing'" in fault for fault in faults)


# --- setup validity -----------------------------------------------------------------------------


def test_a_missing_corpus_directory_is_a_setup_error(tmp_path):
    with pytest.raises(smoke.SetupError, match="does not exist"):
        smoke._corpus_files(tmp_path / "nope", None)


def test_an_empty_corpus_directory_is_a_setup_error(tmp_path):
    """Exit 2, not exit 1: a run with nothing to ingest never validly started."""
    (tmp_path / "notes.txt").write_text("not a corpus file")
    with pytest.raises(smoke.SetupError, match="nothing to ingest"):
        smoke._corpus_files(tmp_path, None)


def test_max_files_narrows_a_sorted_corpus(tmp_path):
    """Sorted, so `--max-files 2` means the same two files on every run and two runs compare."""
    for name in ("c.pdf", "a.pptx", "b.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-1.7\n")
    assert [p.name for p in smoke._corpus_files(tmp_path, None)] == ["a.pptx", "b.pdf", "c.pdf"]
    assert [p.name for p in smoke._corpus_files(tmp_path, 2)] == ["a.pptx", "b.pdf"]


# --- the observer's one statement ---------------------------------------------------------------


def test_the_snapshot_query_reads_both_axes_and_the_chunk_count(db, db_other):
    """The observer's whole view of the world, driven against real rows on a REAL second connection.

    Read back through `db_other` and not through `db`, which is the point rather than a formality:
    a connection sees its own uncommitted work, so this query run on the writing connection would
    return the same six columns whether or not a single row was ever published (ADR 0025). The
    ceremony's every claim — `ready` happened, the chunks are there, the job settled — is a claim
    about what SURVIVED, so the query behind it is verified the same way.

    One statement, six columns, both axes plus the count: `files.status` and `count(chunks)` fetched
    in two round trips could straddle the index transaction's commit and manufacture the very
    `ready`-with-a-disagreeing-count sighting the ceremony reports as a violation.
    """
    conn, owner_id, class_id = db
    file_id = str(uuid.uuid4())
    conn.execute(
        """
        insert into files (file_id, owner_id, class_id, filename, staging_ref, status,
                           failed_reason)
        values (%s::uuid, %s, %s::uuid, %s, %s, 'failed', 'unparseable')
        """,
        (file_id, owner_id, class_id, "corrupt-lecture.pdf", "/tmp/corrupt-lecture.pdf"),
    )
    conn.execute(
        """
        insert into jobs (file_id, owner_id, class_id, state, attempts, last_error)
        values (%s::uuid, %s, %s::uuid, 'failed', 1, 'unparseable: could not read PDF')
        """,
        (file_id, owner_id, class_id),
    )
    for n in range(3):
        conn.execute(
            """
            insert into chunks (file_id, owner_id, class_id, text, file, page_or_slide,
                                embedding, embedding_model_id)
            values (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s)
            """,
            (
                file_id,
                owner_id,
                class_id,
                f"chunk {n}",
                "corrupt-lecture.pdf",
                str(n + 1),
                [0.0] * EMBEDDING_DIM,
                "test-embedder",
            ),
        )
    conn.commit()

    assert smoke._read(db_other, [file_id]) == {
        file_id: smoke.Snapshot(
            file_status="failed",
            failed_reason="unparseable",
            job_state="failed",
            attempts=1,
            last_error="unparseable: could not read PDF",
            chunks=3,
        )
    }


class ExplodingConnection:
    """A connection double that fails the test if anything asks it to run a statement."""

    def execute(self, *args, **kwargs):
        raise AssertionError("_read issued a statement with nothing watched")


def test_the_snapshot_query_is_not_asked_at_all_when_nothing_is_watched():
    """A test that could not fail for the thing it claimed, replaced with one that can.

    It used to assert only `_read(conn, []) == {}` — but the SQL returns no rows for an empty id
    array anyway, so deleting the short circuit left the assertion green and the round trip
    happening. It measured the RESULT of a behaviour whose whole point is that it does not happen.

    A connection that raises on `execute` measures the behaviour itself. That matters because the
    observer samples every `OBSERVE_SECONDS` for the length of the run: a phase that has watched
    nothing yet should cost zero statements, not a few thousand empty ones.
    """
    assert smoke._read(ExplodingConnection(), []) == {}


# --- the ceremony's own verdict -----------------------------------------------------------------
#
# EVERYTHING ABOVE TESTS THE CEREMONY'S JUDGEMENT; THIS SECTION TESTS THAT IT IS ACTED ON. The two
# are not the same guarantee, and the gap between them was measured: a mutation pass replaced
# `_print_gate`'s verdict with a bare `return EXIT_OK` and the whole suite stayed green. Four
# independent edits — one per phase, plus the exit code — could each make the Slice 2 acceptance
# ceremony report success unconditionally, and nothing noticed. A gate whose own verdict is pinned
# by nothing is not a gate; it is a print statement.
#
# These drive the real `_phase_*` functions with a scripted observer, a stub worker and a stub
# `enqueue`. No database, no provider, no thread, no money — the phases' DECISIONS are pure
# functions of the history they are handed, and that is exactly the half that can be wrong while
# every paid run still passes.


class ScriptedObserver:
    """A faithful miniature of `Observer` that plays a pre-written history back to a phase.

    Faithful in the two ways the phases can tell the difference, and no further:

      - it REVEALS ONE SNAPSHOT PER SAMPLE, so a file whose history is longer settles later than
        one whose history is short. A phase that stopped waiting as soon as ANY file settled would
        then judge the others mid-flight, which is precisely the bug that shape of stub exists to
        catch — a stub that handed every phase the final state up front would call that mutant
        correct.
      - `history()` returns only what has been REVEALED, never the whole script, for the same
        reason: a phase that judged a truncated history must be seen to judge a truncated history.

    `wait_until` gives up after `max_samples` instead of after a wall-clock timeout, so the
    timeout branch is reachable in a unit test without anything sleeping.
    """

    def __init__(self, *histories, max_samples=40):
        self._pending = [list(history) for history in histories]
        self._history: dict[str, list] = {}
        self._cursor: dict[str, int] = {}
        self.max_samples = max_samples
        self.samples = 0

    def watch(self, file_id):
        # Cursor starts at 1: the real `Observer.watch` takes a reading immediately, which is what
        # makes the `queued` sighting a fact rather than a race (see `_phase_lifecycle`).
        self._history[file_id] = self._pending.pop(0)
        self._cursor[file_id] = 1

    def sample(self):
        self.samples += 1
        current = {}
        for file_id, history in self._history.items():
            self._cursor[file_id] = min(self._cursor[file_id] + 1, len(history))
            current[file_id] = history[self._cursor[file_id] - 1]
        return current

    def history(self, file_id):
        return self._history[file_id][: self._cursor[file_id]]

    def wait_until(self, predicate, *, timeout):
        for _ in range(self.max_samples):
            if predicate(self.sample()):
                return True
        return False


class StubWorker:
    """A worker that records that it was started and never runs anything."""

    def __init__(self, *args, lease_seconds=1, **kwargs):
        self.lease_seconds = lease_seconds
        self.error = None
        self.started = False
        self.started_after_samples = None

    def start(self):
        self.started = True


@pytest.fixture
def stub_enqueue(monkeypatch):
    """Replace `enqueue` with a counter that mints ids without a database.

    Returned so a test can assert HOW MANY files were enqueued: a phase that silently enqueued only
    the first of five would otherwise pass every assertion about the one file it did handle.
    """
    calls: list[Path] = []

    def fake(conn, *, path, owner_id, class_id):
        calls.append(path)
        return f"file-{len(calls)}"

    monkeypatch.setattr(smoke, "enqueue", fake)
    return calls


def happy_lifecycle(chunks=62, slow=False):
    """A history that passes phase 1. `slow` adds the real extra sighting seen on live runs — the
    gap between `claim` committing and the `processing` write landing — which makes this file
    settle one sample later than a `slow=False` one."""
    seen = [snapshot("queued", 0, job_state="queued", attempts=0)]
    if slow:
        seen.append(snapshot("queued", 0))
    seen += [snapshot("processing", 0), snapshot("ready", chunks, job_state="done")]
    return seen


def buried_history():
    """A history that passes phase 2: corrupt input, one attempt, no chunks, an actionable error."""
    return [
        snapshot("queued", 0, job_state="queued", attempts=0),
        snapshot(
            "failed",
            0,
            failed_reason="unparseable",
            job_state="failed",
            last_error="unparseable: could not read PDF",
        ),
    ]


def reclaimed_history(chunks=62):
    """A history that passes phase 3: two deliveries, one full chunk set, no error recorded."""
    return [
        snapshot("queued", 0, job_state="queued", attempts=0),
        snapshot("processing", 0),
        snapshot("processing", 0, attempts=2),
        snapshot("ready", chunks, attempts=2),
        snapshot("ready", chunks, job_state="done", attempts=2),
    ]


class FakeDelayGate:
    """Stands in for `_DelayedEmbeddings`: records what phase 3 armed, and that it disarmed."""

    def __init__(self):
        self.armed_with = None
        self.delay_seconds = 0.0

    @contextmanager
    def delaying(self, seconds):
        self.armed_with = seconds
        self.delay_seconds = seconds
        try:
            yield
        finally:
            self.delay_seconds = 0.0


def run_reaper_phase(
    observer, monkeypatch, *, target="Livingston Cosmogony.pdf", reaps=0, worker=None, gate=None
):
    """Drive `_phase_reaper` with worker B and the delay gate stubbed out."""
    monkeypatch.setattr(smoke, "WorkerThread", StubWorker)
    log = smoke.ReaperLog()
    log.reaps = reaps
    return smoke._phase_reaper(
        None,
        observer,
        log,
        worker or StubWorker(),
        owner_id="o",
        class_id="c",
        target=Path(target),
        embedder=gate or FakeDelayGate(),
        lease_seconds=1,
        chunk_size=250,
        chunk_overlap=40,
    )


# --- phase 1's verdict --------------------------------------------------------------------------


def test_phase_one_passes_a_clean_lifecycle(stub_enqueue, capsys):
    observer = ScriptedObserver(happy_lifecycle(), happy_lifecycle(chunks=7))
    worker = StubWorker()
    assert (
        smoke._phase_lifecycle(
            None,
            observer,
            worker,
            owner_id="o",
            class_id="c",
            corpus=[Path("a.pdf"), Path("b.pptx")],
        )
        is True
    )
    assert worker.started
    # Both files, not just the first: a loop that quietly enqueued one of them would satisfy every
    # other assertion here.
    assert [p.name for p in stub_enqueue] == ["a.pdf", "b.pptx"]
    assert "lifecycle over 2 real corpus file(s)" in capsys.readouterr().out


def test_phase_one_fails_when_any_file_never_showed_processing(stub_enqueue, capsys):
    """The issue's central assertion, reached through the phase rather than through the helper.

    File two is the one that jumped straight to `ready`. A phase that judged only the first file's
    history — or that computed its faults and dropped them — reports PASS on this run.
    """
    straight_to_ready = [
        snapshot("queued", 0, job_state="queued", attempts=0),
        snapshot("ready", 62, job_state="done"),
    ]
    observer = ScriptedObserver(happy_lifecycle(), straight_to_ready)
    assert (
        smoke._phase_lifecycle(
            None,
            observer,
            StubWorker(),
            owner_id="o",
            class_id="c",
            corpus=[Path("a.pdf"), Path("b.pptx")],
        )
        is False
    )
    assert "'processing' was never observed" in capsys.readouterr().out


def test_phase_one_waits_for_EVERY_file_not_just_the_first_to_settle(stub_enqueue):
    """A phase that stops waiting on the first settled file judges the rest mid-flight.

    The two files here are both healthy and both end `ready`; they differ only in how many samples
    they take to get there. Waiting for ALL of them passes. Waiting for ANY of them returns while
    the slow one is still `processing`, and its truncated history then fails its own end-state
    check — a red run with no defect behind it.
    """
    observer = ScriptedObserver(happy_lifecycle(slow=True), happy_lifecycle(chunks=7))
    assert (
        smoke._phase_lifecycle(
            None,
            observer,
            StubWorker(),
            owner_id="o",
            class_id="c",
            corpus=[Path("slow.pdf"), Path("quick.pptx")],
        )
        is True
    )


def test_phase_one_reports_a_timeout_and_the_history_it_got_to(stub_enqueue, capsys):
    """A file wedged in `processing` never satisfies the predicate; the phase must say so and fail.

    The history is printed on this path deliberately: a timeout with no evidence tells a reader
    that something hung and nothing about where.
    """
    wedged = [snapshot("queued", 0, job_state="queued", attempts=0), snapshot("processing", 0)]
    observer = ScriptedObserver(wedged)
    assert (
        smoke._phase_lifecycle(
            None, observer, StubWorker(), owner_id="o", class_id="c", corpus=[Path("a.pdf")]
        )
        is False
    )
    out = capsys.readouterr().out
    assert "TIMED OUT" in out
    assert "files.status=processing" in out


# --- phase 2's verdict --------------------------------------------------------------------------


def test_phase_two_passes_a_clean_terminal_failure(stub_enqueue, capsys):
    observer = ScriptedObserver(buried_history())
    assert (
        smoke._phase_terminal_failure(
            None,
            observer,
            StubWorker(),
            owner_id="o",
            class_id="c",
            corrupt=Path("corrupt-lecture.pdf"),
        )
        is True
    )
    out = capsys.readouterr().out
    assert "no retry spent" in out
    assert f"({len(smoke.CORRUPT_PDF_BYTES)} bytes" in out


def test_phase_two_fails_when_a_retry_was_spent_on_terminal_input(stub_enqueue, capsys):
    """ADR 0020 §1's actual claim: bad input costs ZERO retries, i.e. `attempts == 1`.

    A check that only refused `attempts < 1` would pass this run — the file was handed back for a
    second go at a corrupt PDF, which is the one thing the terminal class exists to prevent.
    """
    retried = buried_history()
    retried[-1] = snapshot(
        "failed",
        0,
        failed_reason="unparseable",
        job_state="failed",
        attempts=2,
        last_error="unparseable: could not read PDF",
    )
    observer = ScriptedObserver(retried)
    assert (
        smoke._phase_terminal_failure(
            None,
            observer,
            StubWorker(),
            owner_id="o",
            class_id="c",
            corrupt=Path("corrupt-lecture.pdf"),
        )
        is False
    )
    assert "a retry was spent on terminal input" in capsys.readouterr().out


def test_phase_two_fails_when_the_reason_is_not_the_one_it_asserts(stub_enqueue, capsys):
    wrong = buried_history()
    wrong[-1] = snapshot(
        "failed", 0, failed_reason="transient_exhausted", job_state="failed", last_error="gave up"
    )
    observer = ScriptedObserver(wrong)
    assert (
        smoke._phase_terminal_failure(
            None,
            observer,
            StubWorker(),
            owner_id="o",
            class_id="c",
            corrupt=Path("corrupt-lecture.pdf"),
        )
        is False
    )
    assert "not 'unparseable'" in capsys.readouterr().out


def test_phase_two_fails_when_an_unparseable_file_somehow_indexed_chunks(stub_enqueue, capsys):
    indexed = buried_history()
    indexed[-1] = snapshot(
        "failed",
        9,
        failed_reason="unparseable",
        job_state="failed",
        last_error="unparseable: could not read PDF",
    )
    observer = ScriptedObserver(indexed)
    assert (
        smoke._phase_terminal_failure(
            None,
            observer,
            StubWorker(),
            owner_id="o",
            class_id="c",
            corrupt=Path("corrupt-lecture.pdf"),
        )
        is False
    )
    assert "chunk(s) indexed for a file that failed to parse" in capsys.readouterr().out


def test_phase_two_fails_when_nothing_actionable_was_recorded(stub_enqueue, capsys):
    """`failed_reason` is the closed set; `last_error` is the detail a human needs. Both or bust."""
    silent = buried_history()
    silent[-1] = snapshot(
        "failed", 0, failed_reason="unparseable", job_state="failed", last_error=None
    )
    observer = ScriptedObserver(silent)
    assert (
        smoke._phase_terminal_failure(
            None,
            observer,
            StubWorker(),
            owner_id="o",
            class_id="c",
            corrupt=Path("corrupt-lecture.pdf"),
        )
        is False
    )
    assert "nothing actionable was recorded" in capsys.readouterr().out


# --- phase 3's verdict --------------------------------------------------------------------------


def test_phase_three_passes_a_real_reclaim_and_one_full_chunk_set(
    monkeypatch, stub_enqueue, capsys
):
    observer = ScriptedObserver(reclaimed_history())
    assert run_reaper_phase(observer, monkeypatch) is True
    out = capsys.readouterr().out
    assert "reclaimed and redelivered: attempts=2" in out
    assert "one full chunk set after two deliveries: chunks=62" in out


def test_phase_three_fails_when_no_lease_was_ever_reclaimed(monkeypatch, stub_enqueue, capsys):
    """THE phase-3 claim. A run that ingested cleanly on the first delivery proves nothing about
    the reaper, and must not be able to report PASS."""
    once = [
        snapshot("queued", 0, job_state="queued", attempts=0),
        snapshot("processing", 0),
        snapshot("ready", 62, job_state="done"),
    ]
    assert run_reaper_phase(ScriptedObserver(once), monkeypatch) is False
    assert "no reclaim happened" in capsys.readouterr().out


def test_phase_three_fails_when_the_redelivery_left_a_disagreeing_index(
    monkeypatch, stub_enqueue, capsys
):
    """At-least-once is safe by IDEMPOTENT REPLACE, not by dedup — this is where that is checked.

    Two deliveries that published different-sized sets means the second appended rather than
    replaced. Delete the invariant check and this run reports PASS with a doubled corpus.
    """
    doubled = reclaimed_history()
    doubled[-1] = snapshot("ready", 124, job_state="done", attempts=2)
    assert run_reaper_phase(ScriptedObserver(doubled), monkeypatch) is False
    assert "partial or disagreeing index" in capsys.readouterr().out


def test_phase_three_fails_when_the_redelivered_job_was_buried(monkeypatch, stub_enqueue, capsys):
    """A redelivery that ends `failed` has settled — the phase must judge it, not wait for `done`.

    A predicate that only accepts `done` sits out the full timeout on this history and reports a
    hang, which tells a reader the opposite of what happened.
    """
    buried = reclaimed_history()
    buried[-1] = snapshot(
        "failed", 0, failed_reason="transient_exhausted", job_state="failed", attempts=2
    )
    assert run_reaper_phase(ScriptedObserver(buried), monkeypatch) is False
    out = capsys.readouterr().out
    assert "TIMED OUT" not in out
    assert "not 'ready'" in out


def test_phase_three_reports_only_the_reaps_from_its_own_phase(monkeypatch, stub_enqueue, capsys):
    """The corroboration line is phase-scoped. Reported as a running total it silently credits
    phase 3 with every reap the run ever logged."""
    assert run_reaper_phase(ScriptedObserver(reclaimed_history()), monkeypatch, reaps=5) is True
    assert "logged 0 'reaper:' warning(s)" in capsys.readouterr().out


# --- the exit code itself -----------------------------------------------------------------------


def test_the_gate_exits_zero_only_when_every_phase_passed():
    assert smoke._print_gate({"1": True, "2": True, "3": True}) == smoke.EXIT_OK


@pytest.mark.parametrize(
    "results",
    [
        {"1": False, "2": True, "3": True},
        {"1": True, "2": False, "3": True},
        {"1": True, "2": True, "3": False},
        {"1": False, "2": False, "3": False},
    ],
)
def test_one_failed_phase_is_enough_to_fail_the_gate(results):
    """No partial credit, and no phase that can be lost in the fold.

    Parametrized over WHICH phase failed rather than asserting once, because a gate that read only
    the first result — or only the last — passes a single-case test and ships a ceremony that
    cannot see two thirds of its own evidence.
    """
    assert smoke._print_gate(results) == smoke.EXIT_GATE_FAILED


def test_the_gate_prints_the_verdict_it_returns(capsys):
    """The exit code is for the caller; the last line is for the human. They must not disagree."""
    smoke._print_gate({"1 lifecycle": True, "2 terminal failure": False})
    out = capsys.readouterr().out
    assert "1 lifecycle: PASS" in out
    assert "2 terminal failure: FAIL" in out
    assert out.splitlines()[-1].startswith("FAIL —")


def test_a_passing_gate_says_so_on_its_last_line(capsys):
    smoke._print_gate({"1 lifecycle": True})
    assert capsys.readouterr().out.splitlines()[-1].startswith("PASS —")


# --- a dead worker must be diagnosed where it died ------------------------------------------------


class DeadWorker(StubWorker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.error = FileNotFoundError("no such file: 'Livingston Cosmogony.pdf'")


def test_a_dangling_corpus_symlink_is_refused_at_setup(tmp_path):
    """The shape this repo's own corpus has: `data/dogfood/**` is gitignored and those files are
    symlinks. A broken one still globs as `*.pdf`, and `parse_file` then raises `FileNotFoundError`
    — neither `ParseError` nor `TransientEmbeddingError`, so the worker dies unclassified.

    Refusing it here costs a millisecond. Letting it through costs three phase timeouts and reports
    a lifecycle failure whose cause was a symlink.
    """
    for healthy in ("a.pdf", "b.pptx", "c.pptx", "d.pdf"):
        (tmp_path / healthy).write_bytes(b"%PDF-1.7\n")
    (tmp_path / "ghost.pdf").symlink_to(tmp_path / "nothing-here.pdf")

    with pytest.raises(smoke.SetupError) as caught:
        smoke._corpus_files(tmp_path, None)

    # FOUR HEALTHY FILES, ON PURPOSE. Matching only on "cannot be opened" left the message free to
    # name every entry in the corpus — "5 corpus entry/entries ... cannot be opened: a.pdf, b.pptx,
    # c.pptx, d.pdf, ghost.pdf" — which sends a reader to audit four files that are fine and is
    # exactly as unhelpful as no message. The count and the names are the message; assert them.
    message = str(caught.value)
    assert "cannot be opened" in message
    assert "ghost.pdf" in message
    assert "1 corpus entry" in message
    for healthy in ("a.pdf", "b.pptx", "c.pptx", "d.pdf"):
        assert healthy not in message


def test_a_directory_named_like_a_corpus_file_is_refused_too(tmp_path):
    (tmp_path / "real.pdf").write_bytes(b"%PDF-1.7\n")
    (tmp_path / "notes.pptx").mkdir()
    with pytest.raises(smoke.SetupError, match="notes.pptx"):
        smoke._corpus_files(tmp_path, None)


def test_waiting_stops_on_the_FIRST_sample_after_a_worker_dies():
    """Promptness is the whole fix, so it is what gets asserted.

    The old code caught the crash and read it once, in `main`, after all three phases had run — so
    the diagnosis existed and arrived three timeouts too late to be one. Folding the check into the
    predicate means the sampling loop that is already running notices it on the next tick.
    """
    observer = ScriptedObserver([snapshot("processing", 0)] * 3, max_samples=40)
    reason = smoke._await(
        observer, lambda current: False, workers=(DeadWorker(),), stalled="never mind"
    )
    assert reason is not None
    assert "a worker died" in reason
    assert observer.samples == 1


def test_a_death_is_reported_ahead_of_the_timeout_it_also_caused():
    """Both are true when a worker dies during a wait that would have expired anyway. Only one is
    the cause, and a timeout message would send the reader looking for a slow worker."""
    observer = ScriptedObserver([snapshot("processing", 0)], max_samples=1)
    reason = smoke._await(
        observer, lambda current: False, workers=(DeadWorker(),), stalled="the queue never drained"
    )
    assert "TIMED OUT" not in reason


def test_phase_one_fails_fast_when_its_worker_died(stub_enqueue, capsys):
    observer = ScriptedObserver([snapshot("queued", 0, job_state="queued", attempts=0)] * 3)
    assert (
        smoke._phase_lifecycle(
            None, observer, DeadWorker(), owner_id="o", class_id="c", corpus=[Path("a.pdf")]
        )
        is False
    )
    out = capsys.readouterr().out
    assert "a worker died" in out
    assert "TIMED OUT" not in out


def test_phase_two_fails_fast_when_its_worker_died(stub_enqueue, capsys):
    observer = ScriptedObserver([snapshot("queued", 0, job_state="queued", attempts=0)] * 3)
    assert (
        smoke._phase_terminal_failure(
            None,
            observer,
            DeadWorker(),
            owner_id="o",
            class_id="c",
            corrupt=Path("corrupt-lecture.pdf"),
        )
        is False
    )
    assert "a worker died" in capsys.readouterr().out


def test_phase_three_does_not_start_worker_B_until_A_has_claimed(monkeypatch, stub_enqueue):
    """The ordering IS the mechanism. Start B first and B wins the claim on its long lease, A never
    holds the job, nothing ever expires, and the reaper case quietly stops being demonstrated."""
    observer = ScriptedObserver(reclaimed_history())
    started_at: list[int] = []

    class RecordingWorker(StubWorker):
        def start(self):
            started_at.append(observer.samples)
            super().start()

    monkeypatch.setattr(smoke, "WorkerThread", RecordingWorker)
    log = smoke.ReaperLog()
    assert (
        smoke._phase_reaper(
            None,
            observer,
            log,
            StubWorker(),
            owner_id="o",
            class_id="c",
            target=Path("Livingston Cosmogony.pdf"),
            embedder=FakeDelayGate(),
            lease_seconds=1,
            chunk_size=250,
            chunk_overlap=40,
        )
        is True
    )
    # B was constructed and started only after the observer had actually seen A holding the job.
    assert started_at == [1]


def test_phase_three_fails_when_the_history_shows_an_impossible_transition(
    monkeypatch, stub_enqueue, capsys
):
    """`ready` is absorbing. A redelivery that unpublished a `ready` file is a real violation, and
    phase 3 checks legality for the same reason phase 1 does."""
    unpublished = [
        snapshot("queued", 0, job_state="queued", attempts=0),
        snapshot("processing", 0),
        snapshot("ready", 62, attempts=2),
        snapshot("failed", 0, job_state="failed", attempts=2),
    ]
    assert run_reaper_phase(ScriptedObserver(unpublished), monkeypatch) is False
    assert "no legal path reaches" in capsys.readouterr().out


# --- which illegal pairs a MISSED sighting could explain ------------------------------------------


def test_exactly_three_illegal_pairs_are_bridgeable_by_a_missed_sighting():
    """The enumeration the docstring used to get wrong, done from the map instead of from memory.

    It named `failed -> ready` as "the only such pair". There are three, and the one that actually
    matters here is `queued -> ready` — a missed `processing` on the happy path, which is the exact
    miss `OBSERVE_SECONDS` is tuned against. Enumerated from `LEGAL_STATUS_TRANSITIONS` so the claim
    cannot drift from the table it is about.
    """
    states = list(smoke.LEGAL_STATUS_TRANSITIONS)
    # DISTINCT states only. `status_path` collapses consecutive duplicates, so no observed path can
    # ever contain `X -> X` — `processing -> processing` and `failed -> failed` are bridgeable in
    # the abstract and unobservable in fact, and counting them would make this assertion about the
    # map rather than about anything the ceremony can see.
    bridgeable = {
        (before, after)
        for before in states
        for after in states
        if before != after
        and smoke.illegal_transitions([before, after])
        and smoke._bridgeable(before, after)
    }
    assert bridgeable == {("queued", "ready"), ("queued", "failed"), ("failed", "ready")}


def test_nothing_after_ready_is_bridgeable():
    """`ready` is absorbing, so no missed sighting can explain a pair that leaves it — those are the
    real violations and must not be softened."""
    assert smoke._bridgeable("ready", "processing") is False
    assert smoke._bridgeable("ready", "failed") is False


def test_a_status_the_schema_does_not_have_is_never_bridgeable():
    assert smoke._bridgeable("quarantined", "ready") is False


def test_the_two_kinds_of_illegal_pair_do_not_get_the_same_sentence():
    """A missed `processing` printed as "the state machine was violated" sends a reader to audit
    guards that are working. Both still fail the gate; only the diagnosis differs."""
    missed = smoke.transition_fault("queued", "ready")
    assert "a sighting was missed" in missed

    real = smoke.transition_fault("ready", "processing")
    assert "no legal path reaches" in real
    assert "missed" not in real


# --- flag values the ceremony argues are impossible -----------------------------------------------


def args(**overrides):
    from argparse import Namespace

    return Namespace(**{"max_files": None, "lease": smoke.DEFAULT_SHORT_LEASE_SECONDS, **overrides})


def test_max_files_zero_is_refused_rather_than_read_as_unset():
    """`files[:0] if 0 else files` is the whole corpus. A run that asked for no files silently got
    every one of them, and paid for them."""
    with pytest.raises(smoke.SetupError, match="reads as unset"):
        smoke._validate_args(args(max_files=0))


def test_a_negative_max_files_is_refused():
    """`files[:-1]` silently drops the last file, so a run asks for a corpus and quietly gets a
    different one — and pays for it. Phase 3 is no longer the victim (it takes `corpus[0]` and its
    overrun is induced), which makes this purely about a flag meaning something other than it says:
    a narrowed run and an unnarrowed one stop being comparable, for no error anyone sees."""
    with pytest.raises(smoke.SetupError, match="at least 1"):
        smoke._validate_args(args(max_files=-1))


@pytest.mark.parametrize("lease", [0, -5])
def test_a_lease_at_or_below_zero_is_refused(lease):
    """Three places in this repo argue 1s is the floor and that 0 would be "forcing the expiry by
    another name". Argparse enforced none of them, so phase 3 would have passed on a lease that had
    already expired when `claim` granted it."""
    with pytest.raises(smoke.SetupError, match="forced rather than earned"):
        smoke._validate_args(args(lease=lease))


def test_the_documented_defaults_and_the_negative_control_both_validate():
    smoke._validate_args(args())
    smoke._validate_args(args(max_files=1, lease=30))


# --- claims made in prose that the code has to keep -----------------------------------------------


def test_the_amended_adr_pair_is_never_cited_in_the_slash_form():
    """CLAUDE.md bans `ADR 0025/0027` for an amended pair: the slash says nothing about which ADR
    owns what. ADR 0027 amends ADR 0025, and the repo's form is "(ADR 0025, guarded per ADR 0027)".

    Asserted against the source because a citation-form rule is exactly the kind that is obeyed on
    the day it is written and eroded afterwards. `ADR 0011/0020` is deliberately not covered — those
    two are not an amendment pair.
    """
    source = _SCRIPT.read_text()
    assert "ADR 0025/0027" not in source
    assert "ADR 0025, guarded per ADR 0027" in source


def _help_for(flag):
    """The help string argparse actually holds for a flag, not the one the source appears to set."""
    for action in smoke._build_parser()._actions:
        if flag in action.option_strings:
            return action.help
    raise AssertionError(f"no such flag: {flag}")


def test_the_max_files_help_describes_the_file_phase_three_actually_takes():
    """The help said phase 3 runs on "the largest file of that subset" when the code picked by a
    different axis entirely, and it now takes the FIRST — a third answer over two revisions. A help
    string is read by someone choosing a flag value; it has to name what the code does."""
    help_text = _help_for("--max-files")
    assert "FIRST file" in help_text
    assert "largest" not in help_text


def test_the_lease_help_names_the_constraint_that_is_actually_live():
    """The retired rule, and a test that could not catch it keeping the flag.

    The help said `--lease` "must be shorter than the slowest file's ingest". That rule died when
    the overrun became induced: the live constraint is `PHASE_THREE_EMBED_DELAY_SECONDS`. On a
    corpus whose slowest file takes 5s a reader would follow the printed instruction, pick
    `--lease 4`, and watch phase 3 fail with a remedy contradicting the help they had just read.

    The old assertion was `str(DEFAULT_SHORT_LEASE_SECONDS) in help` — and the help f-string
    INTERPOLATES that constant, so it held for every value of it and for every word around it. A
    test whose expectation is computed from the same source as the thing under test cannot fail.
    What can fail is a NEGATIVE assertion on the retired wording and a positive one on a literal
    the author has to type, which is the shape its `--max-files` sibling already had.

    This is the drift `_build_parser` was split out to catch, catching it.
    """
    help_text = _help_for("--lease")
    assert "embed delay" in help_text
    assert "floor" in help_text
    assert "slowest file" not in help_text


# --- the induced delay ----------------------------------------------------------------------------
#
# `_DelayedEmbeddings` is the one place this ceremony ARRANGES something rather than observing it,
# which makes it the one place a quiet mistake would be least visible: every phase would still pass,
# and phase 3's verdict would be about a wrapper instead of about the write path. The tests below
# are what keep the wrapper honest about being a pass-through with a pause and nothing else.


class RecordingEmbedder:
    """A stand-in for the real embedder: deterministic vectors, and it counts its calls.

    `model_id` is present and NOT the wrapper's business — the trap the proxy test is about.
    """

    model_id = "text-embedding-3-small"

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[float(len(text))] * 3 for text in texts]


def test_the_delay_wrapper_proxies_model_id_to_the_real_embedder():
    """ADR 0018's stamp runs through this attribute, so the wrapper must not own it.

    `chunks.embedding_model_id` is stamped from `embedder.model_id` — "the model that ACTUALLY
    produced the stored vectors" — and the Retriever asserts that stamp against the active
    embedder's. Three ways a wrapper breaks that, all silent until they are not: SHADOW the
    attribute and every chunk the ceremony writes is stamped with a lie; DROP it and the ingest
    raises mid-run; HARDCODE it and the Retriever's guard compares a constant to itself and can
    never fire (config.py says so in as many words). Delegation is the only one of the four that
    keeps the stamp the real model's by construction, so it is asserted rather than assumed.
    """
    inner = RecordingEmbedder()
    assert smoke._DelayedEmbeddings(inner).model_id == inner.model_id


def test_the_delay_wrapper_proxies_an_attribute_it_has_never_heard_of():
    """The proxy is general, not a special case for `model_id`.

    A wrapper that forwarded exactly the one attribute someone remembered would break the next
    thing the pipeline reads off an embedder, and it would break it inside a paid run.
    """
    inner = RecordingEmbedder()
    inner.dimensions = 1536
    assert smoke._DelayedEmbeddings(inner).dimensions == 1536


def test_an_attribute_neither_side_has_still_raises_attribute_error():
    """The proxy must not turn a typo into something that looks like a value."""
    wrapped = smoke._DelayedEmbeddings(RecordingEmbedder())
    with pytest.raises(AttributeError):
        # Bound to a name only to keep ruff's B018 quiet; the access is the whole test.
        _ = wrapped.no_such_thing


def test_the_delay_wrapper_changes_nothing_about_what_is_embedded():
    """Same texts in, the real client's vectors back, ONE inner call per outer call.

    The call count is half the assertion and the easier half to lose: a wrapper that retried, or
    that embedded once to time it and once for real, would double the ceremony's bill and still
    return correct-looking vectors.
    """
    inner = RecordingEmbedder()
    wrapped = smoke._DelayedEmbeddings(inner)
    texts = ["alpha beta", "gamma"]

    assert wrapped.embed(texts) == inner.embed(texts)
    assert inner.calls == [texts, texts]  # one per embed() call, ours and the control's


def test_the_delay_is_not_applied_until_it_is_armed():
    """Phases 1 and 2 run through this object disarmed, so disarmed has to cost nothing.

    Worker A holds ONE embedder for the whole run — there is no seam at which to hand it a different
    object mid-run — so "phases 1 and 2 are unaffected" is a claim about this branch being false,
    not about them holding a different embedder.
    """
    wrapped = smoke._DelayedEmbeddings(RecordingEmbedder())
    started = time.perf_counter()
    wrapped.embed(["alpha"])
    assert time.perf_counter() - started < 0.05


def test_arming_the_delay_actually_pauses_the_embed():
    """The whole point of the wrapper, measured rather than assumed.

    A short delay, because what is under test is that the sleep happens at all — the real
    `PHASE_THREE_EMBED_DELAY_SECONDS` is sized against the lease, and asserting on it here would buy
    nothing but three seconds of test time.
    """
    wrapped = smoke._DelayedEmbeddings(RecordingEmbedder())
    with wrapped.delaying(0.05):
        started = time.perf_counter()
        wrapped.embed(["alpha"])
        elapsed = time.perf_counter() - started
    assert elapsed >= 0.05


def test_the_delay_is_disarmed_even_when_the_block_raises():
    """Phase 3 is the only phase that should pay for the delay.

    If an exception could leave it armed, a failing phase 3 would slow every later embed in the
    process — and the ceremony would look like it had a performance problem rather than a fault.
    """
    wrapped = smoke._DelayedEmbeddings(RecordingEmbedder())
    with pytest.raises(ZeroDivisionError):
        with wrapped.delaying(3.0):
            raise ZeroDivisionError
    assert wrapped.delay_seconds == 0.0


def test_phase_three_arms_the_delay_it_advertises(monkeypatch, stub_enqueue):
    """The wiring assertion: the phase must arm the constant it names in its own output.

    Armed with the wrong value — or not armed at all — phase 3 goes back to depending on the corpus
    being slow enough, which is the flake this change exists to remove, and nothing else in the
    suite would notice.
    """
    gate = FakeDelayGate()
    assert run_reaper_phase(ScriptedObserver(reclaimed_history()), monkeypatch, gate=gate) is True
    assert gate.armed_with == smoke.PHASE_THREE_EMBED_DELAY_SECONDS
    assert gate.delay_seconds == 0.0  # and disarmed on the way out


def test_the_induced_delay_is_longer_than_the_lease_it_has_to_outrun():
    """The one arithmetic relationship phase 3 rests on, stated where it can go red.

    The delay is what guarantees the overrun; a default lease raised past it, or a delay trimmed
    below it, silently returns the phase to hoping the corpus is slow. `_overrun_remedy` says the
    same thing to a human at runtime — this says it to CI.
    """
    assert smoke.PHASE_THREE_EMBED_DELAY_SECONDS > smoke.DEFAULT_SHORT_LEASE_SECONDS


# --- the small guards that only fire on an input nothing normally produces ----------------------
#
# Every function below is one line of defence against an EMPTY or unusual history, which is exactly
# the input no passing run ever supplies — so these are invisible to the ceremony itself and can
# only be pinned here. A ceremony that raised `ValueError` out of `max()` while diagnosing a failure
# would replace a readable verdict with a traceback, at the worst possible moment.


def test_an_unsampled_file_has_no_partial_index_rather_than_an_exception():
    """`max()` over an empty history raises, and the caller is already on a failure path."""
    assert smoke.partial_index_sightings([]) == []


def test_an_unsampled_file_is_not_evidence_of_a_reclaim():
    """`history[-1]` on an empty list raises, and "no samples" is the honest answer anyway."""
    reaped, why = smoke.reaper_evidence([])
    assert not reaped
    assert why == "no samples"


def test_the_reaper_log_counts_the_reaper_and_nothing_else():
    """It is corroboration, so a counter that counted the WRONG warnings would print a plausible
    number beside a verdict that never depended on it — the least likely thing anyone re-derives."""
    log = smoke.ReaperLog()
    reaper = logging.LogRecord(
        "gct.jobs.worker", logging.WARNING, __file__, 1, "reaper: 1 job", (), None
    )
    other = logging.LogRecord(
        "gct.jobs.worker", logging.WARNING, __file__, 1, "job x: lease lost", (), None
    )
    log.emit(reaper)
    log.emit(other)
    log.emit(reaper)
    assert log.reaps == 2


def test_the_printed_history_shows_a_failure_reason_when_there_is_one(capsys):
    """The history block IS the evidence a reader gets on a failing phase; a reason printed only
    when absent would drop the one field that says WHY."""
    smoke._print_history(
        "corrupt-lecture.pdf",
        [snapshot("failed", 0, failed_reason="unparseable", job_state="failed")],
    )
    out = capsys.readouterr().out
    assert "reason=unparseable" in out
    assert "files.status=failed" in out


def test_the_printed_history_does_not_invent_a_reason_when_there_is_none(capsys):
    smoke._print_history("a.pdf", [snapshot("ready", 62, job_state="done")])
    assert "reason=" not in capsys.readouterr().out


def test_the_terminal_input_is_a_header_followed_by_something_that_fails_to_parse():
    """Both halves are load-bearing, and only one of them is asserted by the parse test.

    A bare header with no body still raises `unparseable`, so the parse test alone cannot tell the
    intended fixture from an empty one — and an empty file proves a weaker thing: that pypdf
    rejects nothing, rather than that it met content and could not read it.
    """
    assert smoke.CORRUPT_PDF_BYTES.startswith(b"%PDF")
    assert len(smoke.CORRUPT_PDF_BYTES) > len(b"%PDF-1.7\n") + 64


# --- the faults phase 1 reports -------------------------------------------------------------------


def test_a_file_that_ended_failed_is_told_so_and_told_which(capsys):
    """A fault that says a file failed without saying HOW is a fault a reader cannot act on."""
    history = [
        snapshot("queued", 0, job_state="queued", attempts=0),
        snapshot("processing", 0),
        snapshot("failed", 0, failed_reason="unparseable", job_state="failed"),
    ]
    faults = smoke._lifecycle_faults(smoke.status_path(history), history)
    assert any("ended 'failed', not 'ready'" in fault for fault in faults)


def test_phase_one_checks_transition_legality_and_not_only_the_end_states():
    """`ready` is absorbing. A history that leaves it is a real violation, and the end-state check
    alone would pass this run — it starts `queued`, sees `processing`, and ends `ready`."""
    history = [
        snapshot("queued", 0, job_state="queued", attempts=0),
        snapshot("processing", 0),
        snapshot("ready", 62),
        snapshot("processing", 62),
        snapshot("ready", 62, job_state="done"),
    ]
    faults = smoke._lifecycle_faults(smoke.status_path(history), history)
    assert any("no legal path reaches" in fault for fault in faults)


def test_phase_one_checks_the_no_partial_index_invariant():
    """The invariant the whole ceremony is named for, asserted where phase 1 applies it: a `ready`
    sighting carrying a count that disagrees with the run that published it."""
    history = [
        snapshot("queued", 0, job_state="queued", attempts=0),
        snapshot("processing", 0),
        snapshot("ready", 40),
        snapshot("ready", 62, job_state="done"),
    ]
    faults = smoke._lifecycle_faults(smoke.status_path(history), history)
    assert any("partial index visible" in fault for fault in faults)


# --- the remedy phase 3 offers --------------------------------------------------------------------


def test_the_remedy_tells_a_long_lease_to_come_down():
    """`--lease 30` is the documented negative control, so its remedy has to name the flag."""
    remedy = smoke._overrun_remedy(30)
    assert "Lower --lease" in remedy
    assert "not reaching the worker" not in remedy


def test_the_remedy_for_a_lease_already_under_the_delay_names_the_wiring_and_not_a_knob():
    """Below the induced delay there is no knob left: an ingest that still fit inside the lease
    means worker A is not embedding through the wrapper phase 3 armed. Telling the reader to change
    a flag here would be a confident instruction to change something that cannot help."""
    remedy = smoke._overrun_remedy(smoke.DEFAULT_SHORT_LEASE_SECONDS)
    assert "not reaching the worker" in remedy
    assert "Lower --lease" not in remedy


# --- the real Observer ----------------------------------------------------------------------------
#
# Every phase test above swaps in `ScriptedObserver`, which is what makes those tests about the
# phases' JUDGEMENT. The consequence is that the real observer — the thing that actually produces
# every history the gate judges — is stubbed out of all of them, so nothing there would notice if it
# dropped the first reading, collapsed transitions it should have kept, or returned from a wait that
# had not happened. These are the tests that watch the watcher.


class ScriptedConnection:
    """Hands `_read` a pre-written row set per statement, and counts the statements it was asked."""

    def __init__(self, *row_sets):
        self._row_sets = list(row_sets)
        self.statements = 0

    def execute(self, sql, params=None):
        self.statements += 1
        rows = self._row_sets.pop(0) if self._row_sets else []
        return SimpleNamespace(fetchall=lambda: rows)


def row(file_id="f1", status="queued", job_state="queued", attempts=0, chunks=0):
    """One row in the shape `_read`'s SELECT returns: both axes, then the chunk count."""
    return (file_id, status, None, job_state, attempts, None, chunks)


def test_watching_a_file_takes_its_first_reading_immediately():
    """`queued` is a guaranteed sighting because `watch` reads before any worker exists to move the
    row (`_phase_lifecycle`). A `watch` that only registered the id would make that a race."""
    observer = smoke.Observer(ScriptedConnection([row(status="queued")]))
    observer.watch("f1")
    assert [s.file_status for s in observer.history("f1")] == ["queued"]


def test_the_first_reading_survives_the_second():
    """Drop the empty-history arm of the dedup and every file loses its `queued` sighting — the one
    the gate requires to be first, and the one nothing else can re-derive."""
    conn = ScriptedConnection([row(status="queued")], [row(status="processing")])
    observer = smoke.Observer(conn)
    observer.watch("f1")
    observer.sample()
    assert [s.file_status for s in observer.history("f1")] == ["queued", "processing"]


def test_an_unchanged_reading_is_not_recorded_twice():
    """At 40ms over a multi-second ingest, most readings are duplicates. Without the collapse the
    history is a few thousand identical lines and the transitions are unreadable inside it."""
    conn = ScriptedConnection(*[[row(status="processing")] for _ in range(4)])
    observer = smoke.Observer(conn)
    observer.watch("f1")
    for _ in range(3):
        observer.sample()
    assert len(observer.history("f1")) == 1


def test_a_wait_whose_predicate_already_holds_returns_true_at_once():
    """Two row sets, not one: `watch` consumes the first, and `wait_until`'s own sample needs the
    second. That is the real sequence: the observer reads once at watch time, and again on the
    wait's first tick."""
    settled = [row(status="ready", job_state="done", chunks=62)]
    conn = ScriptedConnection(settled, settled)
    observer = smoke.Observer(conn)
    observer.watch("f1")
    assert (
        observer.wait_until(lambda current: current["f1"].file_status == "ready", timeout=5) is True
    )


def test_a_wait_keeps_sampling_until_the_predicate_holds():
    """The deadline has to be computed from NOW and compared the right way round. Get either wrong
    and every wait gives up on its second sample — which the phases would report as a timeout on a
    write path that was working perfectly."""
    conn = ScriptedConnection(
        [row(status="queued")],
        [row(status="processing")],
        [row(status="ready", job_state="done", chunks=62)],
    )
    observer = smoke.Observer(conn)
    observer.watch("f1")
    assert (
        observer.wait_until(lambda current: current["f1"].file_status == "ready", timeout=5) is True
    )
    assert [s.file_status for s in observer.history("f1")] == ["queued", "processing", "ready"]


def test_a_wait_that_never_settles_reports_a_timeout_rather_than_a_success():
    """A NON-ZERO timeout, and elapsed time asserted — so the mutant fails instead of hanging.

    At `timeout=0` this test caught the inverted deadline comparison by never returning: the loop
    spins forever and CI reports a hung job rather than a red one, which is a worse signal than no
    test (the verifier had to cap its own harness at 180s because of this).

    With a real timeout the two implementations separate on DURATION rather than on liveness. The
    correct one returns only once `monotonic() >= deadline`, so it cannot come back early; the
    inverted one satisfies its check immediately and returns almost at once. Asserting the floor on
    elapsed time is what turns "hangs forever" into "returned in 0.001s, expected >= 0.15".
    """
    conn = ScriptedConnection(*[[row(status="processing")] for _ in range(3)])
    observer = smoke.Observer(conn)
    observer.watch("f1")

    started = time.perf_counter()
    settled = observer.wait_until(lambda current: False, timeout=0.2)
    elapsed = time.perf_counter() - started

    assert settled is False
    assert elapsed >= 0.15


def test_wait_until_requires_an_explicit_timeout():
    """`timeout` is keyword-only AND has no default, and the second half is the load-bearing one.

    Dropping the bare `*` alone is signature hygiene — no correct caller can tell, which is why the
    other keyword-only barriers in this file are deliberately left unpinned. Giving `timeout` a
    default of `0.0` is a different thing: it makes "never wait" the behaviour a caller gets by
    FORGETTING, and a forgotten wait returns before the worker has done anything, so the phase
    judges a history that has not happened yet.

    Latent rather than live — every caller passes it today — and pinned for exactly that reason: a
    fourth phase added later is the caller that would omit it.
    """
    observer = smoke.Observer(ScriptedConnection([row()]))
    observer.watch("f1")
    with pytest.raises(TypeError):
        observer.wait_until(lambda current: True)


def test_the_snapshot_query_ignores_a_file_that_has_no_job_row(db, db_other):
    """The join is INNER on purpose, and the ceremony only ever watches files it enqueued.

    `enqueue` writes both rows in one transaction, so a file with no job is not a state this write
    path produces — which is exactly why a join weakened to LEFT would go unnoticed: every ceremony
    row still comes back, and the null job axis it would now admit only appears for a row nothing
    watches. Asserted through `db_other`, a second connection, so this is a claim about rows that
    were actually published rather than about the writer's own uncommitted view.
    """
    conn, owner_id, class_id = db
    orphan = str(uuid.uuid4())
    conn.execute(
        """
        insert into files (file_id, owner_id, class_id, filename, staging_ref, status)
        values (%s::uuid, %s, %s::uuid, %s, %s, 'queued')
        """,
        (orphan, owner_id, class_id, "never-enqueued.pdf", "/tmp/never-enqueued.pdf"),
    )
    conn.commit()

    assert smoke._read(db_other, [orphan]) == {}


def test_a_missing_api_key_exits_setup_before_anything_is_wired(monkeypatch, capsys):
    """Exit 2, and BEFORE a connection or a thread exists — the ordering is the point.

    `load_settings()` defaults a missing key to `""`, which the OpenAI client accepts at
    construction, so an absent key does not announce itself: unchecked, it surfaces as an
    `AuthenticationError` raised inside a worker THREAD, mid-phase, where `WorkerThread` catches it
    and the ceremony reports a failed lifecycle. That is exit 1 — "the write path misbehaved" —
    for a run that was never stageable. The check converts it to what it is.

    Driven through `main` rather than through the branch, because what is asserted is that the check
    is reached and that its sense is the right way round: a check that aborted on a PRESENT key
    would satisfy any test written against the branch alone.
    """
    monkeypatch.setattr(sys, "argv", ["ingest_smoke.py"])
    monkeypatch.setattr(smoke, "_corpus_files", lambda corpus_dir, max_files: [Path("a.pdf")])
    monkeypatch.setattr(
        smoke, "load_settings", lambda: SimpleNamespace(openai_api_key="", database_url="")
    )
    # Nothing may reach Postgres: if the key check is skipped or inverted, this is what it hits.
    monkeypatch.setattr(
        smoke, "connect", lambda: pytest.fail("main() reached connect() with no API key")
    )

    assert smoke.main() == smoke.EXIT_SETUP
    out = capsys.readouterr().out
    assert "SETUP FAILED" in out
    assert "OPENAI_API_KEY" in out


class FakeConn:
    """Supports the two things `main` does to a connection: `with` it, and set autocommit."""

    def __init__(self):
        self.autocommit = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def quiet_worker_logger():
    """Put `gct.jobs.worker`'s handlers back after a test that drives `main`.

    `main` attaches a `ReaperLog` to that logger and never detaches it — correct for a script that
    exits afterwards, and a leak inside a test process where several tests call `main`. Restoring
    keeps a handler from one test counting records emitted during another.
    """
    logger = logging.getLogger(smoke.run.__module__)
    previous = list(logger.handlers)
    try:
        yield
    finally:
        logger.handlers = previous


@pytest.fixture
def drivable_main(monkeypatch):
    """Stub `main`'s wiring down to nothing, leaving its own control flow real.

    Everything replaced here is something `main` merely CONNECTS — the corpus listing, the settings,
    the provider client, the two connections, the class row, the worker, the three phases. What is
    left running is the part that is `main`'s alone and is reachable no other way: the order it does
    things in, and what it does with the results afterwards.
    """
    monkeypatch.setattr(sys, "argv", ["ingest_smoke.py"])
    monkeypatch.setattr(smoke, "_corpus_files", lambda corpus_dir, max_files: [Path("a.pdf")])
    monkeypatch.setattr(
        smoke, "load_settings", lambda: SimpleNamespace(openai_api_key="sk-test", database_url="")
    )
    monkeypatch.setattr(smoke, "OpenAIEmbeddings", RecordingEmbedder)
    monkeypatch.setattr(smoke, "connect", FakeConn)
    monkeypatch.setattr(smoke, "_create_class", lambda conn, owner_id, name: "class-1")
    for phase in ("_phase_lifecycle", "_phase_terminal_failure", "_phase_reaper"):
        monkeypatch.setattr(smoke, phase, lambda *a, **kw: True)
    return monkeypatch


def test_a_worker_that_died_between_phases_still_fails_the_gate(
    drivable_main, quiet_worker_logger, capsys
):
    """The last remaining reader of `worker_a.error`, and the only one nothing else covers.

    `_await` catches a death DURING a wait, in the phase it broke, and that path is well pinned. The
    sliver this line keeps honest is a death BETWEEN phases, when nothing is waiting on anything —
    no predicate is being evaluated, so the in-wait check never runs. Every phase here reports PASS
    and the worker is dead: the gate must still come back non-zero, or a ceremony whose worker died
    somewhere in the gaps reports success.

    Invert this line and the suite was green — which is to say the backstop's own comment was the
    only thing asserting it existed.
    """
    drivable_main.setattr(smoke, "WorkerThread", DeadWorker)

    assert smoke.main() == smoke.EXIT_GATE_FAILED
    out = capsys.readouterr().out
    assert "worker A died" in out
    assert "worker A survived: FAIL" in out


def test_a_run_whose_worker_survived_every_phase_exits_zero(
    drivable_main, quiet_worker_logger, capsys
):
    """The control for the test above: same wiring, a live worker, and the gate must pass.

    Without it, a backstop that reported EVERY worker as dead would satisfy the assertion above and
    fail every real run — the inverted mutant caught in only one direction.
    """
    drivable_main.setattr(smoke, "WorkerThread", StubWorker)

    assert smoke.main() == smoke.EXIT_OK
    out = capsys.readouterr().out
    assert "worker A died" not in out
    assert out.splitlines()[-1].startswith("PASS —")
