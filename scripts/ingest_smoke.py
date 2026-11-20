"""Slice 2 exit test — the write-path ceremony (issue #72), with exactly one worker (issue #187).

Proves the ASYNC write path end to end over real course materials, driving `enqueue` and real
`scripts/worker.py` processes, and asserts that the lifecycle ACTUALLY HAPPENED:

  1. LIFECYCLE — enqueue every corpus file, start the worker, and watch `queued -> processing ->
     ready`. `processing` must be SEEN on the observer's connection, not inferred from the end.
  2. TERMINAL FAILURE — a corrupt PDF lands `failed(unparseable)` with no retry spent (ADR 0020 §1).
  3. CRASH — a worker is SIGKILLed mid-ingest and a fresh worker finishes the job: the file ends
     `ready` once, with one chunk set the size of a clean ingest, and `attempts == 2`. A second
     worker started while the first is alive waits for the worker lock and claims nothing.

Throughout, a SECOND connection samples the rows: no partial index may ever be visible.

Phase 3 holds the worker mid-ingest with `LOCK TABLE chunks IN SHARE MODE`, taken before the worker
starts. Nothing before `index_file` writes `chunks`, so the worker claims, parses and embeds, opens
the index transaction, writes `ready` uncommitted, and blocks on its chunk delete. It is killed
there. The lock only decides WHEN; the claim, crash, requeue and redelivery are the real ones.

Run (needs a migrated DB and the corpus files; costs money unless GCT_PROVIDERS=standin):

    GCT_PROVIDERS=standin uv run python scripts/ingest_smoke.py
    uv run python scripts/ingest_smoke.py --max-files 2
    uv run python scripts/ingest_smoke.py --verbose            # print every worker's log

Every run works in a FRESH class (`ingest-smoke <utc timestamp>`), so every run really ingests, and
nothing is deleted. Worker processes inherit this environment (DATABASE_URL, GCT_PROVIDERS).

Exit codes: 0 the gate passed · 1 the gate failed · 2 setup failed (no corpus, no API key, no
reachable database).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg

from gct.db import connect
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.jobs.queue import WORKER_LOCK_KEY, enqueue
from gct.providers.active import require_usable, selected

DEFAULT_OWNER = "nate-ingest-smoke"
DEFAULT_CORPUS_DIR = "data/dogfood/religion"

# The extensions `gct.ingest.parse` handles — the same tuple, and the same reason, as
# `ask_smoke.CORPUS_GLOBS`: a corpus file this script counted but never enqueued would read as a
# silent gap in the ceremony.
CORPUS_GLOBS = ("*.pdf", "*.pptx")

# How often the OBSERVER reads `files`/`jobs` on its own connection. A MISSED `processing` fails
# the gate by design, so this must be well under the shortest `processing` window. With the free
# stand-in embedder the smallest deck ingests in ~20ms (measured 2026-11-20); with OpenAI, ~0.6s.
OBSERVE_SECONDS = 0.005

WORKER_SCRIPT = Path(__file__).resolve().parent / "worker.py"

# Well under `DEFAULT_POLL_SECONDS`, so phase 1 has no dead air between files.
WORKER_POLL_SECONDS = 0.05

# Lines `gct.jobs.worker` logs that phase 3 reads (pinned against its source by the tests).
LOCK_WAIT_LINE = "another worker holds the worker lock"
CLAIM_LINE = ": claimed file"
REQUEUE_LINE = "requeued"

# How long a phase may take before the ceremony gives up waiting. Generous by an order of
# magnitude against the timings above: this is a runaway guard so a wedged worker fails the gate
# with a readable message instead of hanging, never a performance assertion.
PHASE_TIMEOUT_SECONDS = 300.0

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_SETUP = 2

# The `files.status` moves this ceremony accepts as legal. The map is the SHAPE of the state
# machine, not a second copy of anyone's policy — every entry traces to a writer that exists:
#   queued -> processing    `process_one`'s claim write (the only writer of `processing`)
#   processing -> ready     `index_file`, inside the index transaction (ADR 0020 §3)
#   processing -> failed    `_bury` — terminal input, or the retry budget already spent
#   failed -> processing    a later claim over a buried file, which `process_one`'s guard
#                           deliberately PERMITS: `_bury` writes `files` before `jobs`, so a crash
#                           between the two leaves a `failed` file under a still-claimable job, and
#                           this is the transition that recovers it
# What is absent is the assertion: `ready` is ABSORBING. Both writers that could leave it —
# `process_one`'s claim write and `_bury`'s files write — carry the same `status <> 'ready'` guard,
# because `ready` is the only status that is a promise already made to the student.
LEGAL_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"processing"}),
    "processing": frozenset({"ready", "failed"}),
    "failed": frozenset({"processing"}),
    "ready": frozenset(),
}

# The bytes phase 2 enqueues. A real PDF header followed by nothing that can be parsed: pypdf gets
# far enough to try and fails, which is what makes this the `unparseable` case rather than the
# `unsupported` one (a wrong EXTENSION never reaches a parser at all and would prove less).
# Measured, not assumed — `tests/scripts/test_ingest_smoke.py` runs `parse_file` over exactly these
# bytes and pins the reason, so a pypdf upgrade that reclassified them goes red in CI instead of
# silently making phase 2 assert against a reason nothing produces any more.
CORRUPT_PDF_BYTES = b"%PDF-1.7\n" + b"this file has a header and nothing else that parses\n" * 8


class SetupError(RuntimeError):
    """The ceremony cannot honestly run: no corpus, no key, no database.

    Distinct from a failed gate, and the distinction is `ask_smoke.SetupError`'s: a gate failure is
    a RESULT (the write path behaved wrongly); this is the run never having been stageable, so
    reporting on it would describe a lifecycle that was never driven. Exits 2 so a caller can tell
    "it ran and lost" from "it never ran".
    """


# --- observation ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """One reading of a file's two axes plus its chunk count, taken in ONE statement.

    One statement matters: `files.status` and `count(chunks)` read in two round trips could
    straddle the index transaction's commit and manufacture the very `ready`-with-a-wrong-count
    sighting phase 1's invariant check is looking for. Read together they are one snapshot of one
    server-side state, so a violation reported below is a violation of the system and not of the
    observer.

    `job_state`/`attempts`/`last_error` ride along because a redelivery is only legible on the JOB
    axis: it is an `attempts` bump.

    Frozen and compared by value, which is what lets `Observer.sample` collapse an unchanged
    reading — the history is then a list of transitions rather than a few thousand duplicates.
    """

    file_status: str
    failed_reason: str | None
    job_state: str
    attempts: int
    last_error: str | None
    chunks: int


class Observer:
    """Polls `files`/`jobs`/`chunks` on its OWN connection and keeps each file's history.

    A SEPARATE connection from every worker's, and that is the whole point rather than tidiness: a
    connection sees its own uncommitted work, so a status read back through a worker's connection
    would be true whether or not anything was published (ADR 0025, and the `db_other` rule in
    CLAUDE.md). Everything this class reports is therefore a fact about what SURVIVED, which is
    what `ready` has to mean.

    Autocommit for the same reason `tests/conftest.py`'s `db_other` is: every statement becomes its
    own transaction, so the observer can never hold an old snapshot open across a phase and report
    a stale answer.

    It never INVENTS a state. Sampling can MISS one — a transition that opens and closes between
    two reads is simply absent from the history — so an assertion that a state was SEEN is sound
    (nothing can fake a sighting), while an assertion that one was NEVER visited is not, and this
    file makes none.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        self._history: dict[str, list[Snapshot]] = {}

    def watch(self, file_id: str) -> None:
        """Start tracking `file_id`, and take its first reading immediately.

        Called before any worker is started, which is what makes `queued` a guaranteed sighting
        rather than a race: `enqueue` committed the row, and nothing can move it until a worker
        exists to claim it.
        """
        self._history.setdefault(file_id, [])
        self.sample()

    def sample(self) -> dict[str, Snapshot]:
        """Read every watched file once; append to a history only where the reading CHANGED."""
        current = _read(self._conn, list(self._history))
        for file_id, snapshot in current.items():
            history = self._history[file_id]
            if not history or history[-1] != snapshot:
                history.append(snapshot)
        return current

    def history(self, file_id: str) -> list[Snapshot]:
        return list(self._history[file_id])

    def wait_until(
        self, predicate: Callable[[dict[str, Snapshot]], bool], *, timeout: float
    ) -> bool:
        """Sample every `OBSERVE_SECONDS` until `predicate` holds; False on timeout.

        The sampling and the waiting are the same loop on purpose. A `sleep(timeout)` followed by
        one read would observe an end state and no transitions — the exact check the issue calls
        naive — so the ceremony's only way to wait is a way that also watches.
        """
        deadline = time.monotonic() + timeout
        while True:
            if predicate(self.sample()):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(OBSERVE_SECONDS)


def _read(conn: psycopg.Connection, file_ids: Sequence[str]) -> dict[str, Snapshot]:
    """One statement, one snapshot per file. Empty in, empty out."""
    if not file_ids:
        return {}
    rows = conn.execute(
        """
        select f.file_id::text, f.status, f.failed_reason,
               j.state, j.attempts, j.last_error,
               (select count(*) from chunks c where c.file_id = f.file_id)
        from files f
        join jobs j using (file_id)
        where f.file_id = any(%(ids)s::uuid[])
        """,
        {"ids": list(file_ids)},
    ).fetchall()
    return {
        row[0]: Snapshot(
            file_status=row[1],
            failed_reason=row[2],
            job_state=row[3],
            attempts=int(row[4]),
            last_error=row[5],
            chunks=int(row[6]),
        )
        for row in rows
    }


# --- what a history is allowed to say (pure) ----------------------------------------------------


def status_path(history: Iterable[Snapshot]) -> list[str]:
    """The `files.status` values in order, consecutive duplicates collapsed."""
    path: list[str] = []
    for snapshot in history:
        if not path or path[-1] != snapshot.file_status:
            path.append(snapshot.file_status)
    return path


def illegal_transitions(path: Sequence[str]) -> list[tuple[str, str]]:
    """Every consecutive pair in `path` that `LEGAL_STATUS_TRANSITIONS` does not permit.

    A SUBSEQUENCE check, not an equality check against one expected sequence, and the choice is
    forced by what sampling can know (decided 2026-09-01, against a real timed run rather than from
    theory). The observer can miss a state; it cannot invent one. So "every pair I saw is a legal
    move" is a claim the sampling supports, while "the sequence was exactly queued/processing/ready"
    is a claim about the SAMPLER as much as about the system, and one that a slow tick would fail
    for no defect. The states that MUST have been sighted are asserted separately and positively —
    `_lifecycle_faults` requires `processing`, which is the issue's actual demand.

    WHERE A MISS CAN STILL MANUFACTURE A FAULT. A missed INTERMEDIATE state turns a legal path into
    an illegal-looking pair, and there are THREE such pairs in this map, not one — enumerate them
    from the map rather than reasoning about the interesting case:

        queued -> ready     (really queued -> processing -> ready)
        queued -> failed    (really queued -> processing -> failed)
        failed -> ready     (really failed -> processing -> ready, a retry after a bury)

    Three, over DISTINCT states: `status_path` collapses consecutive duplicates, so no observed path
    can contain `X -> X` at all.

    An earlier version of this docstring named only the third and called it "the only such pair".
    That was the rarest of the three and the least relevant: the FIRST is a missed `processing` on
    the happy path, which is exactly the miss `OBSERVE_SECONDS` is tuned against and the only one
    this ceremony is at any real risk of. Getting that backwards mattered, because on a sampling
    miss the run printed the true fault (`processing` was never observed) alongside a false claim
    that the state machine had been violated.

    So the pairs are CLASSIFIED rather than all reported the same way — `transition_fault` asks
    `_bridgeable`, which walks this map, whether a missed sighting could explain the pair. Nothing
    is downgraded: a bridgeable pair is still a fault, because a missed `processing` still fails the
    gate by design. Only the sentence changes, from "the state machine was violated" to "either a
    sighting was missed or it was".

    An unknown status is reported here too, as a pair leaving it: `files.status` is CHECK-
    constrained, so a value outside the map means the schema moved and this map did not. Those are
    never bridgeable — nothing leads out of a state the map does not know.
    """
    bad: list[tuple[str, str]] = []
    for before, after in zip(path, path[1:], strict=False):
        if after not in LEGAL_STATUS_TRANSITIONS.get(before, frozenset()):
            bad.append((before, after))
    return bad


def _bridgeable(before: str, after: str) -> bool:
    """Could a MISSED sighting explain `before -> after`? True when it is reachable in >= 2 moves.

    DERIVED from `LEGAL_STATUS_TRANSITIONS` by walking it, never a stored list of the three pairs.
    A stored list is a second writer for a fact the map already owns, and it would go stale the
    first time anyone adds a status — which is the drift CLAUDE.md's "cite, don't re-argue" section
    is about, in miniature.

    Only asked about pairs `illegal_transitions` has already rejected, so the one-step case never
    reaches it and does not need excluding.
    """
    seen = {before}
    frontier = set(LEGAL_STATUS_TRANSITIONS.get(before, frozenset()))
    reachable_beyond_one: set[str] = set()
    while frontier:
        nxt: set[str] = set()
        for state in frontier:
            for onward in LEGAL_STATUS_TRANSITIONS.get(state, frozenset()):
                reachable_beyond_one.add(onward)
                if onward not in seen:
                    seen.add(onward)
                    nxt.add(onward)
        frontier = nxt
    return after in reachable_beyond_one


def transition_fault(before: str, after: str) -> str:
    """The fault line for one illegal pair, worded for which of the two things it could be.

    Both are faults and both fail the gate; they are different NEWS. A pair a missed sighting could
    explain is most likely the observer blinking, and telling a reader the state machine was
    violated sends them to read `process_one`'s guards for a defect that is not there. A pair
    nothing could bridge — anything after `ready`, or a status the schema does not have — is the
    real thing, and must not be softened into "maybe we blinked".
    """
    if _bridgeable(before, after):
        return (
            f"observed {before!r} -> {after!r}, which is not a legal single move: either a "
            f"sighting was missed ({before!r} -> 'processing' -> {after!r} is legal) or the state "
            "machine was violated"
        )
    return f"illegal transition {before!r} -> {after!r}, which no legal path reaches"


def partial_index_sightings(history: Sequence[Snapshot]) -> list[Snapshot]:
    """Every sample that would be a partially-visible index. Empty is the invariant holding.

    "Full" is taken as the LARGEST chunk count this file was ever seen carrying, because the run
    that published it is the only definition of full there is and re-ingesting the same file under
    the same window replaces it with an identically-sized set. Two things then count as a
    violation, and they are different failures:

      - any sample carrying a count that is neither 0 nor full — a chunk set caught mid-write,
        which is what "partial" literally means;
      - any `ready` sample not carrying a NON-ZERO count equal to full — `status = 'ready'` ⟺ the
        full chunk set is committed and queryable (ADR 0020, publication conditional per ADR 0025),
        so `ready` over zero or over a stale partial count breaks the promise even if the rows are
        self-consistent.

    THE ZERO IS SPELLED OUT SEPARATELY because "full" is derived from this very history, and a file
    that only ever carried zero chunks makes `full` zero — at which point a `ready` sighting over
    nothing AGREES with the run that published it and slips past the count comparison entirely. It
    is a state `index_file` refuses to create (a zero-chunk write raises before the transaction
    opens, #23), which is exactly why the check has to name it: an invariant enforced elsewhere is
    not a reason for the observer to be unable to see it broken. Found by
    `test_ready_over_zero_chunks_is_a_violation`, which this function's first version passed by
    returning nothing.

    HOW MUCH THIS CAN PROVE, stated rather than implied: the chunk delete-and-insert happens inside
    one transaction, so an intermediate count is not visible to another connection BY
    CONSTRUCTION — which is the guarantee, not a limitation of the check. What sampling adds is
    that the guarantee is not being circumvented in practice: a publish that escaped the
    transaction, a `ready` written before the rows, or a delete committed apart from its insert
    would all surface here as a real sighting on a real second connection.
    """
    if not history:
        return []
    full = max(snapshot.chunks for snapshot in history)

    def violates(snapshot: Snapshot) -> bool:
        if snapshot.chunks not in (0, full):
            return True
        return snapshot.file_status == "ready" and (snapshot.chunks == 0 or snapshot.chunks != full)

    return [snapshot for snapshot in history if violates(snapshot)]


def write_corrupt_pdf(directory: Path) -> Path:
    """Write phase 2's terminal input and return its path.

    GENERATED, not committed (decided 2026-09-01). Three reasons, in order of weight: the corpus
    directory these bytes join is gitignored wholesale (ADR 0021 / C5), so a fixture beside it
    could not be committed anyway; a corrupt binary in the tree is a fixture no reviewer can read,
    while `CORRUPT_PDF_BYTES` is legible at the top of this file; and generating it puts the bytes
    and the reason they produce in one place, where the test that pins the reason can reach them.
    """
    path = directory / "corrupt-lecture.pdf"
    path.write_bytes(CORRUPT_PDF_BYTES)
    return path


# --- the worker under ceremony ------------------------------------------------------------------


def worker_command(chunk_size: int, chunk_overlap: int) -> list[str]:
    return [
        sys.executable,
        str(WORKER_SCRIPT),
        "--poll",
        str(WORKER_POLL_SECONDS),
        "--chunk-size",
        str(chunk_size),
        "--chunk-overlap",
        str(chunk_overlap),
        "--log-level",
        "INFO",
    ]


class WorkerProcess:
    """One real `scripts/worker.py` process, with its output in a log file."""

    def __init__(self, name: str, log_dir: Path, *, chunk_size: int, chunk_overlap: int) -> None:
        self.name = name
        self.log_path = log_dir / f"{name}.log"
        self.command = worker_command(chunk_size, chunk_overlap)
        self._proc: subprocess.Popen | None = None
        self._ended_on_purpose = False

    def start(self) -> None:
        with self.log_path.open("wb") as log:
            self._proc = subprocess.Popen(self.command, stdout=log, stderr=subprocess.STDOUT)

    @property
    def pid(self) -> int | None:
        return None if self._proc is None else self._proc.pid

    @property
    def error(self) -> str | None:
        """Why the process is gone, if it exited without being told to; otherwise None."""
        if self._proc is None or self._ended_on_purpose:
            return None
        code = self._proc.poll()
        if code is None:
            return None
        return f"{self.name} exited with code {code}: {' | '.join(self.log().splitlines()[-5:])}"

    def log(self) -> str:
        try:
            return self.log_path.read_text(errors="replace")
        except FileNotFoundError:
            return ""

    def wait_for_log(self, text: str, *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while text not in self.log():
            if self.error is not None or time.monotonic() >= deadline:
                return False
            time.sleep(OBSERVE_SECONDS)
        return True

    def kill(self) -> int:
        """SIGKILL: no handler runs and nothing unwinds. Returns the exit status."""
        self._ended_on_purpose = True
        self._proc.kill()
        return self._proc.wait()

    def stop(self) -> None:
        """SIGTERM and wait (SIGKILL if it hangs). Safe before `start` and safe twice."""
        self._ended_on_purpose = True
        if self._proc is None or self._proc.poll() is not None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()


# --- phases -------------------------------------------------------------------------------------


def _terminal(snapshot: Snapshot) -> bool:
    """A file the worker is finished with, on the FILE axis the student watches."""
    return snapshot.file_status in ("ready", "failed")


def _settled(snapshot: Snapshot) -> bool:
    """Finished on BOTH axes — the file AND the job that was moving it.

    WAIT ON EVERY AXIS YOU LATER ASSERT, or the wait itself manufactures the failure. The file
    axis reaches its terminal FIRST and the job axis follows in a separate transaction, so a
    phase that stops on `_terminal` and then asserts anything about `jobs` is sampling a window
    where the answer is legitimately "not yet":

      - happy path — `index_file` commits `ready`, and `complete` is the NEXT statement. A last
        sample landing between them reads ('ready', 'processing'), and `_lifecycle_faults` calls
        that "file is ready but jobs.state='processing'".
      - terminal failure — wider, and guaranteed rather than incidental: `_bury` writes `files`
        and calls `fail()` in SEPARATE TRANSACTIONS on purpose (`worker._bury` explains what a
        crash between them must leave behind), so ('failed', 'processing') with a null
        `last_error` is a state the design commits to producing.

    Both were reachable: the ceremony reported `1 lifecycle: FAIL` on a healthy run, caught at
    `/land` by running it repeatedly. No rate is claimed — the window is roughly 1ms of publish
    against a 40ms observer tick, so sightings are a few percent per file, and a failure needs the
    sighting to land on the LAST file to settle (nothing samples after the wait returns). An exit
    gate that fails on a correct system is worse than no gate — it teaches the reader to re-run it
    until it is green, which is exactly how a real red gets ignored.
    """
    return _terminal(snapshot) and snapshot.job_state in ("done", "failed")


def _first_death(workers: Sequence[WorkerProcess]) -> object | None:
    """Why the first dead worker died, or None while they are all alive."""
    for worker in workers:
        if worker.error is not None:
            return worker.error
    return None


def _await(
    observer: Observer,
    predicate: Callable[[dict[str, Snapshot]], bool],
    *,
    workers: Sequence[WorkerProcess],
    stalled: str,
) -> str | None:
    """Wait for `predicate` — but stop the moment a worker dies. None on success, else why not.

    The check is folded into the PREDICATE, so a death is noticed on the observer's next tick
    rather than after the whole phase has timed out against a queue nothing is serving.

    A DEATH IS REPORTED AHEAD OF A TIMEOUT, deliberately, because both are true at once when a
    worker dies during a wait that would also have expired, and only one of them is the cause. The
    caller prints the string and fails its phase — this returns the reason rather than printing it
    so the phase keeps control of its own layout.
    """
    settled = observer.wait_until(
        lambda current: _first_death(workers) is not None or predicate(current),
        timeout=PHASE_TIMEOUT_SECONDS,
    )
    death = _first_death(workers)
    if death is not None:
        return (
            f"a worker died, so nothing was ever going to move again: {death!r}. Nothing below "
            "this line is a fact about the write path"
        )
    if not settled:
        return f"TIMED OUT after {PHASE_TIMEOUT_SECONDS:.0f}s — {stalled}"
    return None


def _print_history(label: str, history: Sequence[Snapshot]) -> None:
    print(f"    {label}")
    for snapshot in history:
        reason = f" reason={snapshot.failed_reason}" if snapshot.failed_reason else ""
        print(
            f"      files.status={snapshot.file_status:<10}{reason:<26}"
            f"jobs.state={snapshot.job_state:<10} attempts={snapshot.attempts} "
            f"chunks={snapshot.chunks}"
        )


def _phase_lifecycle(
    conn: psycopg.Connection,
    observer: Observer,
    worker: WorkerProcess,
    *,
    owner_id: str,
    class_id: str,
    corpus: Sequence[Path],
) -> bool:
    """Enqueue the corpus, then start the worker and watch `queued -> processing -> ready`.

    THIS PHASE OWNS THE WORKER'S START, and the ordering is what makes the `queued` sighting a fact
    instead of a race. `enqueue` commits, and only then is there a worker that could move the row —
    so the first reading is `queued` by construction. Start the worker first and the assertion
    becomes a coin toss between the observer's next read and the worker's next poll, which is a
    flake with a 1-in-20 face and no defect behind it.

    The worker is NOT stopped afterwards; it serves phase 2 too, and `main` stops it after that.
    """
    print(f"\nPhase 1 — lifecycle over {len(corpus)} real corpus file(s):")
    file_ids: dict[str, Path] = {}
    for path in corpus:
        file_id = enqueue(conn, path=path, owner_id=owner_id, class_id=class_id)
        observer.watch(file_id)
        file_ids[file_id] = path
        print(f"  enqueued {path.name} -> file_id={file_id}")

    worker.start()
    print(f"  worker A started (pid {worker.pid}), poll {WORKER_POLL_SECONDS}s")

    stalled = _await(
        observer,
        lambda current: all(_settled(current[file_id]) for file_id in file_ids),
        workers=(worker,),
        stalled="not every file settled on both the file and the job axis",
    )
    if stalled is not None:
        print(f"  {stalled}")
        for file_id, path in file_ids.items():
            _print_history(path.name, observer.history(file_id))
        return False

    passed = True
    for file_id, path in file_ids.items():
        history = observer.history(file_id)
        path_seen = status_path(history)
        _print_history(f"{path.name}: {' -> '.join(path_seen)}", history)
        for message in _lifecycle_faults(path_seen, history):
            print(f"      FAIL — {message}")
            passed = False
    return passed


def _lifecycle_faults(path_seen: Sequence[str], history: Sequence[Snapshot]) -> list[str]:
    """Everything wrong with one happy-path file's history. Empty means the file passed.

    `processing` MUST APPEAR, and that single line is the issue's central ask: a file that reached
    `ready` without anyone ever sighting `processing` would satisfy a naive end-state check while
    proving nothing about the worker, and this is the assertion that refuses it.
    """
    faults: list[str] = []
    if path_seen[0] != "queued":
        faults.append(f"first sighting was {path_seen[0]!r}, not 'queued'")
    if "processing" not in path_seen:
        faults.append("'processing' was never observed — the end state proves nothing on its own")
    if path_seen[-1] != "ready":
        faults.append(f"ended {path_seen[-1]!r}, not 'ready'")
    for before, after in illegal_transitions(path_seen):
        faults.append(transition_fault(before, after))
    for sighting in partial_index_sightings(history):
        faults.append(
            f"partial index visible: status={sighting.file_status} chunks={sighting.chunks}"
        )
    if history[-1].chunks == 0:
        faults.append("published 'ready' with no chunks at all")
    if history[-1].job_state != "done":
        faults.append(f"file is ready but jobs.state={history[-1].job_state!r}")
    return faults


def _phase_terminal_failure(
    conn: psycopg.Connection,
    observer: Observer,
    worker: WorkerProcess,
    *,
    owner_id: str,
    class_id: str,
    corrupt: Path,
) -> bool:
    """A corrupt PDF must land `failed(unparseable)` with the retry budget untouched.

    Takes the worker only so it can stop waiting on a dead one (`_await`); it never starts it —
    phase 1 owns the start, and by the time this runs the worker is idle and already serving.
    """
    print("\nPhase 2 — terminal failure, zero retries (ADR 0020 §1):")
    file_id = enqueue(conn, path=corrupt, owner_id=owner_id, class_id=class_id)
    observer.watch(file_id)
    print(f"  enqueued {corrupt.name} ({len(CORRUPT_PDF_BYTES)} bytes of header and noise)")

    stalled = _await(
        observer,
        lambda current: _settled(current[file_id]),
        workers=(worker,),
        stalled="the corrupt file never settled on both axes",
    )
    if stalled is not None:
        print(f"  {stalled}")
        _print_history(corrupt.name, observer.history(file_id))
        return False

    history = observer.history(file_id)
    final = history[-1]
    _print_history(f"{corrupt.name}: {' -> '.join(status_path(history))}", history)

    faults: list[str] = []
    if final.file_status != "failed":
        faults.append(f"ended {final.file_status!r}, not 'failed'")
    if final.failed_reason != "unparseable":
        faults.append(f"failed_reason={final.failed_reason!r}, not 'unparseable'")
    # ONE attempt, not zero. `claim` bumps `attempts` on the way in, so a job that was delivered
    # exactly once and never retried reads 1 — that bump is the trail of what happened, not a retry
    # spent (`process_one`'s ParseError branch says so). "Zero retries" is `attempts == 1`; a 2 here
    # would mean the terminal input had been handed back for another go, which is the whole thing
    # ADR 0020 §1 forbids: a corrupt file is exactly as corrupt on the next attempt.
    if final.attempts != 1:
        faults.append(f"attempts={final.attempts} — a retry was spent on terminal input")
    if final.job_state != "failed":
        faults.append(f"jobs.state={final.job_state!r}, not 'failed'")
    if final.chunks:
        faults.append(f"{final.chunks} chunk(s) indexed for a file that failed to parse")
    if not final.last_error:
        faults.append("jobs.last_error is empty — nothing actionable was recorded")

    for message in faults:
        print(f"      FAIL — {message}")
    if not faults:
        print(f"      reason={final.failed_reason!r} · attempts={final.attempts} (no retry spent)")
        print(f"      last_error: {final.last_error}")
    return not faults


def crash_faults(
    history: Sequence[Snapshot],
    *,
    at_kill: Snapshot,
    chunk_set: tuple[int, int],
    clean: tuple[int, int] | None,
) -> list[str]:
    """Everything wrong with phase 3's file. Empty means it passed.

    `at_kill` is read after the second worker stopped and just before the SIGKILL, so it shows both
    that the first worker was mid-ingest and that the second claimed nothing. `chunk_set` and
    `clean` are `(chunks, distinct chunks)` for this file and for phase 1's ingest of the same file.
    """
    faults: list[str] = []
    seen_at_kill = (at_kill.file_status, at_kill.job_state, at_kill.attempts, at_kill.chunks)
    if seen_at_kill != ("processing", "processing", 1, 0):
        faults.append(
            f"not mid-ingest on its first claim when killed: files.status={at_kill.file_status} "
            f"jobs.state={at_kill.job_state} attempts={at_kill.attempts} chunks={at_kill.chunks}"
        )
    final = history[-1]
    path = status_path(history)
    if final.file_status != "ready":
        faults.append(f"ended {final.file_status!r}, not 'ready'")
    if path.count("ready") != 1:
        faults.append(f"reached 'ready' {path.count('ready')} time(s), not once")
    if final.job_state != "done":
        faults.append(f"jobs.state={final.job_state!r}, not 'done'")
    if final.attempts != 2:
        faults.append(f"attempts={final.attempts}, not 2 (the killed claim and the finished one)")
    for before, after in illegal_transitions(path):
        faults.append(transition_fault(before, after))
    for sighting in partial_index_sightings(history):
        faults.append(
            f"partial index visible: status={sighting.file_status} chunks={sighting.chunks}"
        )
    total, distinct = chunk_set
    if total == 0:
        faults.append("no chunks after recovery")
    if clean is None:
        faults.append("no clean ingest of this file from phase 1 to compare the chunk set against")
    elif chunk_set != clean:
        faults.append(
            f"{total} chunks ({distinct} distinct) after recovery, but a clean ingest of the same "
            f"file has {clean[0]} ({clean[1]} distinct)"
        )
    return faults


def waiter_faults(log: str) -> list[str]:
    """What is wrong with the log of a worker started while another held the lock."""
    faults: list[str] = []
    if LOCK_WAIT_LINE not in log:
        faults.append("the second worker never logged that it was waiting for the worker lock")
    if CLAIM_LINE in log:
        faults.append("the second worker claimed a job while another worker held the lock")
    return faults


def _blocked_lock_holder(conn: psycopg.Connection, blocker_pid: int) -> tuple[int, str] | None:
    """`(backend pid, query)` of the worker-lock holder, if it is waiting on `blocker_pid`."""
    row = conn.execute(
        """
        select a.pid, a.query
        from pg_locks l join pg_stat_activity a using (pid)
        where l.locktype = 'advisory' and l.granted
          and l.classid = 0 and l.objid = %(key)s and l.objsubid = 1
          and %(blocker)s = any(pg_blocking_pids(a.pid))
        """,
        {"key": WORKER_LOCK_KEY, "blocker": blocker_pid},
    ).fetchone()
    return None if row is None else (int(row[0]), row[1])


def _backend_alive(conn: psycopg.Connection, pid: int) -> bool:
    return bool(
        conn.execute(
            "select exists (select 1 from pg_stat_activity where pid = %s)", (pid,)
        ).fetchone()[0]
    )


def _chunk_set(conn: psycopg.Connection, file_id: str) -> tuple[int, int]:
    """`(chunks, distinct chunks)` for one file. A duplicated set shows up in the first number."""
    row = conn.execute(
        """
        select count(*), count(distinct (page_or_slide, text))
        from chunks where file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    return int(row[0]), int(row[1])


def _clean_chunk_set(
    conn: psycopg.Connection, class_id: str, path: Path, *, exclude: str
) -> tuple[int, int] | None:
    """The chunk set an earlier, uninterrupted ingest of `path` in this class published."""
    row = conn.execute(
        """
        select file_id::text from files
        where class_id = %s::uuid and staging_ref = %s and status = 'ready'
          and file_id <> %s::uuid
        order by created_at limit 1
        """,
        (class_id, str(path.resolve()), exclude),
    ).fetchone()
    return None if row is None else _chunk_set(conn, row[0])


def _log_line(log: str, text: str) -> str | None:
    """The last line of a worker log containing `text`, without its timestamp."""
    for line in reversed(log.splitlines()):
        if text in line:
            return line.split(" ", 2)[-1]
    return None


def _wait_for(check: Callable[[], bool], *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= deadline:
            return False
        time.sleep(OBSERVE_SECONDS)
    return True


def _phase_crash(
    conn: psycopg.Connection,
    observer: Observer,
    *,
    owner_id: str,
    class_id: str,
    target: Path,
    log_dir: Path,
    chunk_size: int,
    chunk_overlap: int,
) -> bool:
    """SIGKILL a real worker mid-ingest, start a fresh one, and check the job finished once."""
    print(f"\nPhase 3 — crash recovery with real worker processes, on {target.name}:")
    workers: list[WorkerProcess] = []

    def spawn(name: str) -> WorkerProcess:
        worker = WorkerProcess(name, log_dir, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        workers.append(worker)
        worker.start()
        return worker

    lock_conn = connect()
    try:
        lock_conn.execute("lock table chunks in share mode")
        lock_pid = lock_conn.info.backend_pid
        file_id = enqueue(conn, path=target, owner_id=owner_id, class_id=class_id)
        observer.watch(file_id)
        print(f"  enqueued -> file_id={file_id}; chunks table SHARE-locked by backend {lock_pid}")

        first = spawn("worker-1")
        holder: list[tuple[int, str]] = []

        def mid_index(current: dict[str, Snapshot]) -> bool:
            if current[file_id].file_status != "processing":
                return False
            found = _blocked_lock_holder(conn, lock_pid)
            if found:
                holder.append(found)
            return found is not None

        stalled = _await(
            observer,
            mid_index,
            workers=(first,),
            stalled="worker-1 never reached the index transaction",
        )
        if stalled is not None:
            print(f"  {stalled}")
            return False
        backend_pid, query = holder[-1]
        print(
            f"  worker-1 (pid {first.pid}) is mid-ingest: its backend {backend_pid} holds the "
            f"worker lock and is blocked on `{query.strip().splitlines()[0]}`"
        )

        second = spawn("worker-2")
        second.wait_for_log(LOCK_WAIT_LINE, timeout=PHASE_TIMEOUT_SECONDS)
        second.stop()
        waiter_log = second.log()
        for text in (LOCK_WAIT_LINE, CLAIM_LINE):
            if _log_line(waiter_log, text):
                print(f"  worker-2 (pid {second.pid}): {_log_line(waiter_log, text)}")

        at_kill = observer.sample()[file_id]
        status = first.kill()
        print(
            f"  SIGKILLed worker-1 (exit {status}) at files.status={at_kill.file_status} "
            f"jobs.state={at_kill.job_state} attempts={at_kill.attempts} chunks={at_kill.chunks}"
        )
        lock_conn.rollback()
        if not _wait_for(lambda: not _backend_alive(conn, backend_pid), timeout=30):
            print(f"  backend {backend_pid} outlived its killed worker; the lock was never freed")
            return False
        after = observer.sample()[file_id]
        print(
            f"  backend {backend_pid} ended with it, publishing nothing: files.status="
            f"{after.file_status} jobs.state={after.job_state} chunks={after.chunks}"
        )

        third = spawn("worker-3")
        stalled = _await(
            observer,
            lambda current: _settled(current[file_id]),
            workers=(third,),
            stalled="the orphaned job never settled after the restart",
        )
        history = observer.history(file_id)
        _print_history(f"{target.name}: {' -> '.join(status_path(history))}", history)
        if stalled is not None:
            print(f"  {stalled}")
            return False
        requeued = _log_line(third.log(), REQUEUE_LINE)
        if requeued:
            print(f"  worker-3 (pid {third.pid}): {requeued}")

        chunk_set = _chunk_set(conn, file_id)
        clean = _clean_chunk_set(conn, class_id, target, exclude=file_id)
        faults = waiter_faults(waiter_log) + crash_faults(
            history, at_kill=at_kill, chunk_set=chunk_set, clean=clean
        )
        for message in faults:
            print(f"      FAIL — {message}")
        if not faults:
            print(
                f"      ready once · attempts={history[-1].attempts} · chunks={chunk_set[0]} "
                f"({chunk_set[1]} distinct), same as the clean ingest's {clean[0]}"
            )
        return not faults
    finally:
        for worker in workers:
            worker.stop()
        lock_conn.close()


# --- setup + wiring -----------------------------------------------------------------------------


def _corpus_files(corpus_dir: Path, max_files: int | None) -> list[Path]:
    """The corpus this run enqueues, sorted, narrowed by `--max-files`.

    Sorted so the run is reproducible and so `--max-files 2` means the same two files every time —
    a narrowed run that picked a different subset per invocation would make two runs
    incomparable for no benefit.

    EVERY ENTRY MUST BE A READABLE FILE, and that check is aimed at the exact shape this repo's own
    corpus has rather than at a hypothetical. `data/dogfood/**` is gitignored, so the five religion
    files are SYMLINKS into another checkout, and a symlink whose target has moved still matches
    `*.pdf` and still sorts into this list. `glob` names directory entries; it does not promise
    they open.

    What it costs if it gets through is the whole reason it is refused HERE. `parse_file` opens the
    path and raises `FileNotFoundError` — neither `ParseError` nor `TransientEmbeddingError`, so
    `process_one` does not classify it, `run` propagates it, and worker A dies. Every phase then
    waits out its full timeout against a queue nothing is serving, and the ceremony spends
    `3 x PHASE_TIMEOUT_SECONDS` to report a lifecycle failure whose real cause was a dangling
    symlink. A `SetupError` at this line is the same fact delivered in a millisecond, with the
    right exit code: the run was never stageable, not badly behaved.

    `is_file()` rather than `exists()`, because it follows the link AND refuses a directory named
    `lecture.pdf` — openability is what the worker actually needs.
    """
    if not corpus_dir.is_dir():
        raise SetupError(f"corpus directory {corpus_dir} does not exist")
    files = sorted(path for glob in CORPUS_GLOBS for path in corpus_dir.glob(glob))
    if not files:
        raise SetupError(f"no {'/'.join(CORPUS_GLOBS)} files in {corpus_dir} — nothing to ingest")
    unreadable = [path for path in files if not path.is_file()]
    if unreadable:
        raise SetupError(
            f"{len(unreadable)} corpus entry/entries in {corpus_dir} cannot be opened (a dangling "
            f"symlink, or a directory named like a file): "
            f"{', '.join(path.name for path in unreadable)} — the worker would die on the first "
            "one with an unclassified FileNotFoundError rather than failing the file"
        )
    return files[:max_files] if max_files else files


def _create_class(conn: psycopg.Connection, owner_id: str, name: str) -> str:
    class_id = str(uuid4())
    with conn.transaction():
        conn.execute(
            """
            insert into classes (class_id, owner_id, name)
            values (%(class_id)s::uuid, %(owner_id)s, %(name)s)
            """,
            {"class_id": class_id, "owner_id": owner_id, "name": name},
        )
    return class_id


def _validate_args(args: argparse.Namespace) -> None:
    """Refuse flag values argparse accepts and the ceremony cannot use. Raises `SetupError`.

    `--max-files 0` would read as UNSET (`files[:0] if 0 else files`) and ingest everything, and a
    negative value silently drops files from the end of the sorted corpus.
    """
    if args.max_files is not None and args.max_files < 1:
        raise SetupError(
            f"--max-files must be at least 1 (got {args.max_files}); omit it to use the whole "
            "corpus. 0 is not 'no files' — it reads as unset and silently ingests all of them"
        )


def _build_parser() -> argparse.ArgumentParser:
    """The CLI surface, separated from parsing so a test can read what the flags actually SAY."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--owner", default=DEFAULT_OWNER, help="owner_id every row is scoped to")
    parser.add_argument(
        "--corpus-dir", default=DEFAULT_CORPUS_DIR, help="directory of .pdf/.pptx to enqueue"
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        metavar="N",
        help="enqueue only the first N corpus files in phase 1 (default: all of them). Phase 3 "
        "runs on the FIRST file of that subset and compares its chunks with phase 1's ingest of it",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print every worker process's own log at the end of the run",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=CHUNK_SIZE_WORDS,
        help=f"chunk window in words (default {CHUNK_SIZE_WORDS}, the chunker's own constant)",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=CHUNK_OVERLAP_WORDS,
        help=f"chunk overlap in words (default {CHUNK_OVERLAP_WORDS}, the chunker's own constant)",
    )
    return parser


def _parse_args() -> argparse.Namespace:
    return _build_parser().parse_args()


def main() -> int:
    args = _parse_args()
    print("Slice 2 exit test — the write path, over real course materials, with one worker.")

    try:
        _validate_args(args)
        corpus = _corpus_files(Path(args.corpus_dir), args.max_files)
        # Checked here because the workers are separate processes: a missing key would otherwise
        # surface as a dead worker mid-phase, which reads as a gate failure.
        try:
            require_usable()
            providers = selected()
        except RuntimeError as err:
            raise SetupError(str(err)) from err
        try:
            conn = connect()
            observer_conn = connect()
        except psycopg.OperationalError as err:
            raise SetupError(
                f"cannot connect to Postgres (DATABASE_URL — is the server up?): {err}"
            ) from err

        with conn, observer_conn, TemporaryDirectory(prefix="gct-ingest-smoke-") as tmp:
            # `enqueue` refuses a connection inside a transaction (`require_idle`;
            # ADR 0025, guarded per ADR 0027), and the class insert would otherwise leave one open.
            conn.autocommit = True
            observer = Observer(observer_conn)
            log_dir = Path(tmp)

            name = f"ingest-smoke {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
            class_id = _create_class(conn, args.owner, name)
            print(f"\nSetup — owner {args.owner!r}, class {name!r} (class_id={class_id})")
            print(f"  corpus {args.corpus_dir}: {len(corpus)} file(s)")
            print(f"  chunk window {args.chunk_size}/{args.chunk_overlap} words")
            print(f"  providers: {providers}; workers run {WORKER_SCRIPT}")

            worker_a = WorkerProcess(
                "worker-A", log_dir, chunk_size=args.chunk_size, chunk_overlap=args.chunk_overlap
            )
            # Every phase runs even when an earlier one failed, so the whole picture is printed.
            results: dict[str, bool] = {}
            try:
                results["1 lifecycle"] = _phase_lifecycle(
                    conn,
                    observer,
                    worker_a,
                    owner_id=args.owner,
                    class_id=class_id,
                    corpus=corpus,
                )
                results["2 terminal failure"] = _phase_terminal_failure(
                    conn,
                    observer,
                    worker_a,
                    owner_id=args.owner,
                    class_id=class_id,
                    corrupt=write_corrupt_pdf(log_dir),
                )
                # `_await` catches a death during a wait; this catches one between phases.
                death = _first_death((worker_a,))
                if death is not None:
                    print(f"\n  worker A died: {death!r}")
                    results["worker A survived"] = False
            finally:
                # Phase 3 starts its own workers, and only one may hold the worker lock.
                worker_a.stop()
            results["3 crash recovery"] = _phase_crash(
                conn,
                observer,
                owner_id=args.owner,
                class_id=class_id,
                target=corpus[0],
                log_dir=log_dir,
                chunk_size=args.chunk_size,
                chunk_overlap=args.chunk_overlap,
            )
            if args.verbose:
                for log in sorted(log_dir.glob("*.log")):
                    print(f"\n--- {log.name} ---\n{log.read_text(errors='replace')}", end="")

            return _print_gate(results)

    except SetupError as err:
        print(f"\nSETUP FAILED — {err}")
        return EXIT_SETUP


def _print_gate(results: dict[str, bool]) -> int:
    """Issue #72's acceptance criterion: every phase, no partial credit."""
    line = "  ·  ".join(f"{name}: {'PASS' if ok else 'FAIL'}" for name, ok in results.items())
    print(f"\n  {line}")
    passed = all(results.values())
    verdict = "PASS" if passed else "FAIL"
    print(
        f"{verdict} — exit gate (observed queued->processing->ready · terminal failure with no "
        "retry spent · SIGKILLed worker's job finished once by a fresh worker, attempts=2, one "
        "chunk set · a second worker waited for the lock · no partial index sighted)"
    )
    return EXIT_OK if passed else EXIT_GATE_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
