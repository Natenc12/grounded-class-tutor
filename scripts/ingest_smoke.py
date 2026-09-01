"""Slice 2 exit test — the write-path ceremony (issue #72).

Proves the ASYNC write path end to end, over REAL course materials and REAL embeddings, the way
`scripts/ask_smoke.py` proved the read path at Slice 1 exit (#8). Nothing here is a new mechanism:
it drives `enqueue` (#70) and `gct.jobs.worker.run` (#71) and asserts that the lifecycle ACTUALLY
HAPPENED, rather than that the code paths exist.

Three phases, each an acceptance claim from the issue, each run against the real queue:

  1. LIFECYCLE — enqueue every corpus file, run the worker, and watch `queued -> processing ->
     ready` happen. The gate is on the TRANSITIONS, not the end state: a file that reached `ready`
     without anyone ever sighting `processing` would pass a naive end-state check while proving
     nothing about the worker, so `processing` must have been SEEN on the observer's connection.
  2. TERMINAL FAILURE — a corrupt PDF lands `failed(unparseable)` with ZERO retries spent
     (ADR 0020 §1: bad input is exactly as bad on the next attempt, so the budget is not for it).
  3. REAPER — a worker genuinely OUTRUNS a short lease while embedding a real file, a second
     worker's reaper reclaims the job mid-flight, and the redelivered run leaves the file with ONE
     full chunk set. At-least-once is safe by `index_file`'s idempotent replace, not by dedup
     logic (ADR 0011/0020).

Running through all three, sampled on a SECOND connection: no partial index is ever visible — no
sample ever reads `ready` against a chunk count that disagrees with the run that published it.

A THIN peer caller (ADR 0009): this script wires providers, connections, threads and a poll loop,
and prints. Every DECISION it observes is made in `gct` — the claim/lease/settle verbs are
`gct.jobs.queue`'s, the retryable/terminal split and the reaper cadence are `gct.jobs.worker`'s,
the all-or-nothing publish is `gct.ingest.index`'s. The one deliberate exception is the exit GATE,
and it is the same exception `ask_smoke.py` argues for: a one-time slice-acceptance ceremony, which
V3 will never re-execute, correctly lives in the script rather than being enshrined in the library.
Setup VALIDITY rules (is this run stageable at all?) likewise stay here — they are facts about a
corpus directory and a connection, which the core has no access to and no opinion about.

THE WORKER RUNS ON A THREAD, NOT AS A SUBPROCESS, and that is what buys phase 3 (decided
2026-09-01). `scripts/worker.py` has no CLI surface, so a subprocess is pinned to
`DEFAULT_LEASE_SECONDS` and the reaper case could only be staged by this script backdating
`jobs.leased_until` itself — a forced expiry, and forcing it is precisely what would make the
demonstration hollow. On a thread the lease is a parameter, so a worker really does overrun its own
lease while embedding a real file, and the reaper really does reclaim a job that is still being
worked. `scripts/worker.py` is deliberately NOT imported (ADR 0009: the scripts are peers of the
library, not of each other) — this file does its own wiring, exactly as that one does.

ONE WORKER RUNS THE WHOLE CEREMONY, ON A SHORT LEASE, AND THAT IS SAFE — the fact the phase layout
rests on. `reclaim_expired` is called only from `run`, between ticks (`gct/jobs/worker.py`, the
reaper is in the loop and never in `process_one`), so a worker is never reaping while it is inside
an ingest. A LONE worker therefore cannot reap its own in-flight job however short its lease is: by
the time it next reaps, its own job is settled and `reclaim_expired` filters on `state =
'processing'`. Phases 1 and 2 are consequently unaffected by the short lease — a file that outruns
it still completes, because the settle verbs guard on state and token and never read the clock
(ADR 0028 §Consequences). Phase 3 then only has to start a SECOND worker, after the first has
claimed, to make the reaper real. This reasoning is a property of THAT loop being single-threaded —
the same property ADR 0028 §5's safety argument rests on — so an edit that makes a worker
concurrent invalidates this layout, not just this paragraph.

Run (needs a migrated DB, `.env` secrets, and the corpus files present — this SPENDS MONEY):

    uv run python scripts/ingest_smoke.py
    uv run python scripts/ingest_smoke.py --max-files 2        # narrow the lifecycle phase
    uv run python scripts/ingest_smoke.py --verbose            # print the worker's own log lines
    uv run python scripts/ingest_smoke.py --lease 30           # a lease nothing overruns: phase 3
                                                               # then FAILS, which is what proves
                                                               # its verdict is about the run

Every run works in a FRESH class (`ingest-smoke <utc timestamp>`), so every run genuinely enqueues
and ingests. There is no convergence step and no skip: `ask_smoke.py` converges because it re-asks
questions against a corpus it does not want to re-buy, while the thing THIS script measures is the
ingest itself, and a run that reused an earlier one's `ready` rows would observe no transitions at
all. The class id is printed so the rows stay inspectable afterwards; nothing is deleted.

Exit codes, the `ask_smoke.py` idiom: 0 the gate passed · 1 the gate failed · 2 setup failed (no
corpus, no API key, no reachable database).
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg
from openai import OpenAIError

from gct.config import load_settings
from gct.db import connect
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.ingest.parse import ParseError, parse_file
from gct.jobs.queue import enqueue
from gct.jobs.worker import DEFAULT_LEASE_SECONDS, run
from gct.providers.base import Embeddings
from gct.providers.openai_provider import OpenAIEmbeddings

DEFAULT_OWNER = "nate-ingest-smoke"
DEFAULT_CORPUS_DIR = "data/dogfood/religion"

# The extensions `gct.ingest.parse` handles — the same tuple, and the same reason, as
# `ask_smoke.CORPUS_GLOBS`: a corpus file this script counted but never enqueued would read as a
# silent gap in the ceremony.
CORPUS_GLOBS = ("*.pdf", "*.pptx")

# How often the OBSERVER reads `files`/`jobs` on its own connection. Chosen against a real timed
# run, not from theory (2026-09-01): on this corpus the fastest file ingests in ~0.6s and the
# slowest in ~1.3s, so at 40ms every file's `processing` window is sampled fifteen times or more
# even at the floor. It is not a correctness knob — see `Observer.sample`, which never INVENTS a
# state — but a MISSED `processing` fails the gate by design (that is the issue's central
# assertion), so the interval is what makes "the transitions were observed" a claim about the run
# rather than about luck. Named here rather than buried in a default argument for that reason.
OBSERVE_SECONDS = 0.04

# The worker's own poll interval, well under `DEFAULT_POLL_SECONDS`. Two reasons, both about the
# ceremony rather than about production: a 2s tick would put ~2s of dead air between every file in
# phase 1, and in phase 3 the reaper's tick is what closes the window between the lease expiring
# and the redelivery starting — a slow tick spends the overrun the phase is built on.
WORKER_POLL_SECONDS = 0.05

# The lease phase 3 forces a REAL overrun against, and the tightest number in this file. One second
# is the practical floor: `claim` types `lease_seconds` as an int, so there is nothing between this
# and a zero-second lease that expires the instant it is granted — which would be forcing the
# expiry by another name, the thing the thread design exists to avoid.
#
# THE MARGIN IS MEASURED AND IT IS NARROW. On this corpus the wordiest file (12,300 words, 62
# chunks) ingests in 1.2-1.3s warm against this 1s lease — reproduced on three consecutive full
# runs, and ~3.3s on a cold first call where the provider client is still connecting. So the
# overrun is real but only ~0.25s of it, and the whole of it is one embedding round trip: a
# markedly faster provider day is what would end it. That is a fact about THIS corpus, not a
# property of the design, which is why phase 3 fails loudly and names the remedy (a corpus with a
# wordier file) rather than quietly reporting a reaper that never ran.
DEFAULT_SHORT_LEASE_SECONDS = 1

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

    `job_state`/`attempts`/`last_error` ride along because the reaper case is only legible on the
    JOB axis: a redelivery is an `attempts` bump, and which requeue path caused it is `last_error`
    (see `reaper_evidence`).

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

    WHERE A MISS CAN STILL MANUFACTURE A FAULT, said plainly rather than left for someone to
    discover: a missed INTERMEDIATE state turns a legal path into an illegal-looking pair. The only
    such pair this map admits is `failed -> ready`, which is really `failed -> processing -> ready`
    with the middle one unsampled — a retry after a bury. Nothing in this ceremony produces one
    (its only bury is terminal input, which is never retried by construction, ADR 0020 §1), so the
    check is sound for the histories it is actually run over, and would need revisiting before
    being pointed at a run that retries.

    An unknown status is reported here too, as a pair leaving it: `files.status` is CHECK-
    constrained, so a value outside the map means the schema moved and this map did not.
    """
    bad: list[tuple[str, str]] = []
    for before, after in zip(path, path[1:], strict=False):
        if after not in LEGAL_STATUS_TRANSITIONS.get(before, frozenset()):
            bad.append((before, after))
    return bad


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


def reaper_evidence(history: Sequence[Snapshot]) -> tuple[bool, str]:
    """Did the reaper redeliver this job? `(verdict, what the DB says)`.

    PURELY FROM THE DATABASE, deliberately, rather than from the worker's log line. Two facts
    together identify the reaper as the requeuer, and neither is a message anyone can reword:

      - `attempts` reached 2 or more. `claim` is the only writer that bumps it, so the job was
        handed out twice — a redelivery happened.
      - `last_error` is still null. It is written by every OTHER path back to `queued`: `release`
        sets it on a transient failure, `fail` sets it on a bury. `reclaim_expired` is the one
        requeue that writes no error at all, so a second delivery with a clean error column can
        only have come from the reaper.

    The observed status path is deliberately NOT part of this. The requeue and the re-claim happen
    in the same `run` tick — reap, then claim, microseconds apart — so the `queued` window between
    them is far below any honest poll interval, and gating on sighting it would make the phase a
    coin flip. The worker's own `reaper:` warning is printed alongside as corroboration and is not
    gated on; a log string is not evidence this script should hang a verdict on.
    """
    if not history:
        return False, "no samples"
    last = history[-1]
    if last.attempts < 2:
        return False, f"attempts={last.attempts} — the job was only ever delivered once"
    if last.last_error is not None:
        return False, (
            f"attempts={last.attempts} but last_error={last.last_error!r} — the requeue came from "
            "release/fail, not from the reaper"
        )
    return True, f"attempts={last.attempts}, last_error is null"


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


class WorkerThread:
    """One `gct.jobs.worker.run` loop on a daemon thread, with its OWN connection.

    ITS OWN CONNECTION, always: the worker writes on it constantly and the observer reads on
    another, so sharing one would both serialise them and — worse — let the observer see a worker's
    uncommitted work, which is the one thing the observer exists not to do.

    DAEMON, and never stopped. `run` is an unbounded loop with no stop condition, and this ceremony
    deliberately does not invent one: every phase waits for its files to reach a terminal state
    before moving on, so a worker still alive at the next phase is an IDLE worker, and the phase
    layout (see the module docstring) is built so an idle worker doing the next phase's work is
    correct rather than interference. At exit the interpreter drops the thread.

    A crash inside `run` is CAUGHT AND KEPT, not printed and forgotten. `threading`'s default
    excepthook writes a traceback to stderr and the thread dies silently as far as the ceremony is
    concerned — a worker that died on file 2 would then look exactly like a worker that is being
    slow, and the phase would fail on a timeout with the wrong diagnosis. `BaseException` for the
    same reason `process_one`'s guard uses it: `SystemExit` and `KeyboardInterrupt` are not
    `Exception`, and a worker that took one still stopped working.
    """

    def __init__(
        self,
        name: str,
        *,
        embedder: Embeddings,
        lease_seconds: int,
        chunk_size: int,
        chunk_overlap: int,
    ) -> None:
        self.name = name
        self.lease_seconds = lease_seconds
        self.error: BaseException | None = None
        self._embedder = embedder
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._thread = threading.Thread(target=self._body, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _body(self) -> None:
        conn = connect()
        # Autocommit, the same wiring and the same argument as `scripts/worker.py`: every writer on
        # the worker path commits inside its own `conn.transaction()`, so this is defence in depth
        # rather than load-bearing — it keeps a future statement added outside a transaction block
        # from sitting unpublished in psycopg's implicit one (ADR 0025).
        conn.autocommit = True
        try:
            run(
                conn,
                # The embedder constructs itself from config (ADR 0018: never hardcode a model id)
                # and is SHARED with every other worker here, which is safe because it holds a
                # stateless client. The chunk window is threaded explicitly because the names are
                # provisional spike parameters (ADR 0019).
                embedder=self._embedder,
                chunk_size=self._chunk_size,
                chunk_overlap=self._chunk_overlap,
                lease_seconds=self.lease_seconds,
                poll_seconds=WORKER_POLL_SECONDS,
            )
        except BaseException as exc:
            self.error = exc
        finally:
            conn.close()


class ReaperLog(logging.Handler):
    """Counts the worker module's own `reaper:` warnings, for the printout only.

    Deciding where a library's events go is the application's call and this script is the
    application (ADR 0009), so a handler here is the wiring, not a hook into the library. It is
    CORROBORATION and nothing more: `reaper_evidence` is what the gate reads, and it reads the
    database. Counting a log message is the kind of assertion that goes green forever after
    somebody rewords the message, which is why the verdict does not rest on it.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.reaps = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith("reaper:"):
            self.reaps += 1


# --- phases -------------------------------------------------------------------------------------


def _overrun_remedy(lease_seconds: int) -> str:
    """What to change when phase 3's ingest finished inside its lease. Depends on which knob is at
    fault, because the two situations have opposite fixes and one message for both would send the
    reader the wrong way.

    Above the default, the run asked for a lease nothing could outrun and the fix is the flag.
    AT the default the flag is spent — `claim` types `lease_seconds` as an int, so 1s is the floor
    and 0 would expire the lease at the instant it was granted, which is forcing the expiry rather
    than earning it — and the only remaining lever is the WORK.
    """
    if lease_seconds > DEFAULT_SHORT_LEASE_SECONDS:
        return (
            f"Lower --lease: it is {lease_seconds}s, and the default "
            f"{DEFAULT_SHORT_LEASE_SECONDS}s is what this corpus is measured to overrun"
        )
    return (
        "--lease is already at its floor of 1s (`claim` takes an int, and 0 would expire the lease "
        "at the moment it was granted), so the lever is the WORK: point --corpus-dir at a corpus "
        "whose wordiest file takes longer than a second to embed"
    )


def _terminal(snapshot: Snapshot) -> bool:
    """A file the worker is finished with, on the FILE axis the student watches."""
    return snapshot.file_status in ("ready", "failed")


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
    worker: WorkerThread,
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

    The worker is NOT stopped afterwards; it serves phases 2 and 3 too. The module docstring
    carries why one long-lived worker on a short lease is the right shape for all three.
    """
    print(f"\nPhase 1 — lifecycle over {len(corpus)} real corpus file(s):")
    file_ids: dict[str, Path] = {}
    for path in corpus:
        file_id = enqueue(conn, path=path, owner_id=owner_id, class_id=class_id)
        observer.watch(file_id)
        file_ids[file_id] = path
        print(f"  enqueued {path.name} -> file_id={file_id}")

    worker.start()
    print(f"  worker A started: lease {worker.lease_seconds}s, poll {WORKER_POLL_SECONDS}s")

    if not observer.wait_until(
        lambda current: all(_terminal(current[file_id]) for file_id in file_ids),
        timeout=PHASE_TIMEOUT_SECONDS,
    ):
        print(f"  TIMED OUT after {PHASE_TIMEOUT_SECONDS:.0f}s — not every file reached a terminal")
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
        faults.append(f"illegal transition {before!r} -> {after!r}")
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
    *,
    owner_id: str,
    class_id: str,
    corrupt: Path,
) -> bool:
    """A corrupt PDF must land `failed(unparseable)` with the retry budget untouched."""
    print("\nPhase 2 — terminal failure, zero retries (ADR 0020 §1):")
    file_id = enqueue(conn, path=corrupt, owner_id=owner_id, class_id=class_id)
    observer.watch(file_id)
    print(f"  enqueued {corrupt.name} ({len(CORRUPT_PDF_BYTES)} bytes of header and noise)")

    if not observer.wait_until(
        lambda current: _terminal(current[file_id]), timeout=PHASE_TIMEOUT_SECONDS
    ):
        print(f"  TIMED OUT after {PHASE_TIMEOUT_SECONDS:.0f}s — the corrupt file never settled")
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


def _phase_reaper(
    conn: psycopg.Connection,
    observer: Observer,
    reaper_log: ReaperLog,
    *,
    owner_id: str,
    class_id: str,
    target: Path,
    embedder: Embeddings,
    lease_seconds: int,
    chunk_size: int,
    chunk_overlap: int,
) -> bool:
    """A real lease overrun, a real reclaim, and one full chunk set at the end.

    The staging is the point and it forces NOTHING: `jobs.leased_until` is never touched by this
    script. Worker A is already running on a `lease_seconds` lease; it claims this file and spends
    longer embedding it than the lease covers, which is a genuine overrun of a genuine lease.
    Worker B is started only AFTER A has been observed to claim, so the ordering is a fact and not
    a race, and B's first reaping tick past the expiry reclaims a job that is still being worked.

    Bounded to exactly one redelivery, which is what makes this a phase and not a ping-pong: B
    takes the default lease, so when A finishes and reaps in its turn, B's lease is nowhere near
    expiry and there is nothing to take back. A's `complete` then returns False — the lease it
    lost — and the job settles once, under B.

    ON #92, EXPLICITLY. This phase walks the neighbourhood of the open `ready`-carries-no-reason
    interleaving (#92, and the NOT YET HELD invariant in `design/components/ingestion-worker.md`),
    which needs a BURY between the reclaim and the zombie's publish. This phase produces no bury at
    all — the redelivered run succeeds — so it neither reaches that state nor asserts the invariant
    that would forbid it. Nothing below reads `failed_reason` on a `ready` row, on purpose: an
    assertion that happened to pass here would be read as evidence the invariant holds, and it does
    not.
    """
    print(f"\nPhase 3 — reaper: a real overrun of a real {lease_seconds}s lease:")
    file_id = enqueue(conn, path=target, owner_id=owner_id, class_id=class_id)
    observer.watch(file_id)
    print(f"  enqueued {target.name} -> file_id={file_id}")

    reaps_before = reaper_log.reaps
    if not observer.wait_until(
        lambda current: current[file_id].job_state == "processing", timeout=PHASE_TIMEOUT_SECONDS
    ):
        print("  TIMED OUT — worker A never claimed the job; nothing to overrun")
        return False
    print(f"  worker A claimed it — starting worker B (lease {DEFAULT_LEASE_SECONDS}s)")

    worker_b = WorkerThread(
        "worker-B",
        embedder=embedder,
        lease_seconds=DEFAULT_LEASE_SECONDS,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    worker_b.start()

    settled = observer.wait_until(
        lambda current: current[file_id].job_state in ("done", "failed"),
        timeout=PHASE_TIMEOUT_SECONDS,
    )
    history = observer.history(file_id)
    _print_history(f"{target.name}: {' -> '.join(status_path(history))}", history)
    if worker_b.error is not None:
        print(f"      FAIL — worker B died: {worker_b.error!r}")
        return False
    if not settled:
        print(f"  TIMED OUT after {PHASE_TIMEOUT_SECONDS:.0f}s — the job never settled")
        return False

    final = history[-1]
    reaped, evidence = reaper_evidence(history)
    faults: list[str] = []
    if not reaped:
        faults.append(
            f"no reclaim happened ({evidence}) — the ingest finished inside the {lease_seconds}s "
            f"lease, so nothing expired. {_overrun_remedy(lease_seconds)}"
        )
    if final.file_status != "ready":
        faults.append(f"ended {final.file_status!r}, not 'ready'")
    if final.job_state != "done":
        faults.append(f"jobs.state={final.job_state!r}, not 'done'")
    if not final.chunks:
        faults.append("the redelivered run left no chunks at all")
    for before, after in illegal_transitions(status_path(history)):
        faults.append(f"illegal transition {before!r} -> {after!r}")
    # THE at-least-once claim: two deliveries, ONE chunk set. `partial_index_sightings` reads
    # "full" off the largest count this file ever carried, so two runs that published different
    # sized sets — a duplicate appended rather than replaced, a half-written set published — land
    # here as a violation. Idempotence is by `index_file`'s all-or-nothing replace, not by dedup
    # logic (ADR 0011/0020), and this is where the ceremony checks that it actually held.
    for sighting in partial_index_sightings(history):
        faults.append(
            f"redelivery left a partial or disagreeing index: status={sighting.file_status} "
            f"chunks={sighting.chunks}"
        )

    for message in faults:
        print(f"      FAIL — {message}")
    if not faults:
        print(f"      reclaimed and redelivered: {evidence}")
        print(f"      one full chunk set after two deliveries: chunks={final.chunks}")
    print(
        f"      corroboration (not gated on): the worker logged "
        f"{reaper_log.reaps - reaps_before} 'reaper:' warning(s) during this phase"
    )
    return not faults


# --- setup + wiring -----------------------------------------------------------------------------


def _corpus_files(corpus_dir: Path, max_files: int | None) -> list[Path]:
    """The corpus this run enqueues, sorted, narrowed by `--max-files`.

    Sorted so the run is reproducible and so `--max-files 2` means the same two files every time —
    a narrowed run that picked a different subset per invocation would make two runs
    incomparable for no benefit.
    """
    if not corpus_dir.is_dir():
        raise SetupError(f"corpus directory {corpus_dir} does not exist")
    files = sorted(path for glob in CORPUS_GLOBS for path in corpus_dir.glob(glob))
    if not files:
        raise SetupError(f"no {'/'.join(CORPUS_GLOBS)} files in {corpus_dir} — nothing to ingest")
    return files[:max_files] if max_files else files


def _slowest_to_embed(corpus: Sequence[Path]) -> Path:
    """The file phase 3 overruns its lease on: the one with the most extractable WORDS.

    Words, not bytes on disk, and the difference is not academic — it is measured on this very
    corpus. The largest file in `data/dogfood/religion` is a 7.1MB deck carrying 315 words, and the
    slowest to ingest is a 3.7MB PDF carrying 12,300; picking by size would hand phase 3 the file
    that finishes in half a second and the overrun would never happen. Embedding time tracks the
    text, and a zip full of images is mostly images.

    Parsing the corpus twice is the price. It is a fraction of a second per file and costs nothing
    — the paid call is the embed, one step later (ADR 0020 §3) — which is cheap against a phase
    that would otherwise fail for a reason nobody could see from the output.

    A file that will not parse counts as zero rather than raising: it is phase 1's job to report an
    unparseable corpus file, with the reason and the history, and a `SetupError` thrown from a
    picker would pre-empt that with a worse message.
    """

    def words(path: Path) -> int:
        try:
            return sum(len(unit.text.split()) for unit in parse_file(path))
        except ParseError:
            return 0

    return max(corpus, key=words)


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


def _parse_args() -> argparse.Namespace:
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
        "still runs, on the largest file of that subset",
    )
    parser.add_argument(
        "--lease",
        type=int,
        default=DEFAULT_SHORT_LEASE_SECONDS,
        help=f"lease worker A holds, in seconds (default {DEFAULT_SHORT_LEASE_SECONDS}); phase 3 "
        "overruns it for real, so it must be shorter than the slowest file's ingest",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="also print the worker's own INFO log lines (claims, durations, reaps) interleaved "
        "with the ceremony's output",
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
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    print("Slice 2 exit test — the write path, over real course materials and real embeddings.")

    try:
        corpus = _corpus_files(Path(args.corpus_dir), args.max_files)

        # The key check is explicit because absence never raises: `load_settings()` defaults a
        # missing key to "", which the client ACCEPTS at construction, so the failure would
        # otherwise surface as an AuthenticationError inside a worker THREAD, mid-phase — where it
        # reads as a gate failure ("the write path misbehaved") for a run that never validly
        # started. Same conversion, same argument, as `ask_smoke.main`.
        if not load_settings().openai_api_key:
            raise SetupError("OPENAI_API_KEY is empty — set it in .env (CLAUDE.md, Local dev)")
        try:
            embedder = OpenAIEmbeddings()
        except OpenAIError as err:
            raise SetupError(f"cannot construct the OpenAI embedder: {err}") from err
        try:
            conn = connect()
            observer_conn = connect()
        except psycopg.OperationalError as err:
            raise SetupError(
                f"cannot connect to Postgres (DATABASE_URL — is the server up?): {err}"
            ) from err

        # The library only EMITS events; deciding where they go is the application's call, and this
        # script is the application (ADR 0009). Default WARNING so the worker's reaper line and any
        # transient-failure line land in the transcript beside the ceremony's own output — those
        # are the ones that explain a surprising result — while the per-file INFO chatter rides
        # `--verbose`.
        logging.basicConfig(
            level=logging.INFO if args.verbose else logging.WARNING,
            format="      | %(levelname)s %(name)s: %(message)s",
        )
        reaper_log = ReaperLog()
        # `run.__module__` rather than the string "gct.jobs.worker": the module names its logger
        # `getLogger(__name__)`, so deriving it from the function this script already imports keeps
        # the two the same value by construction. A hardcoded name would go quietly deaf the day
        # the module moved — and a handler that receives nothing looks exactly like a reaper that
        # never fired.
        logging.getLogger(run.__module__).addHandler(reaper_log)

        with conn, observer_conn, TemporaryDirectory(prefix="gct-ingest-smoke-") as tmp:
            # Autocommit on the ENQUEUE connection: `enqueue` refuses a connection already inside a
            # transaction (`require_idle`, ADR 0025/0027), and psycopg opens the implicit
            # transaction on the first statement — so without this the class insert would leave
            # this connection INTRANS and the first `enqueue` would raise rather than silently
            # publish nothing. The guard makes it loud; autocommit makes it correct.
            conn.autocommit = True
            observer = Observer(observer_conn)

            name = f"ingest-smoke {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
            class_id = _create_class(conn, args.owner, name)
            print(f"\nSetup — owner {args.owner!r}, class {name!r} (class_id={class_id})")
            print(f"  corpus {args.corpus_dir}: {len(corpus)} file(s)")
            print(f"  chunk window {args.chunk_size}/{args.chunk_overlap} words")

            worker_a = WorkerThread(
                "worker-A",
                embedder=embedder,
                lease_seconds=args.lease,
                chunk_size=args.chunk_size,
                chunk_overlap=args.chunk_overlap,
            )
            # The phases run in order and are written as three statements rather than one dict
            # literal: they SHARE a worker and a queue, so their order is a precondition, not a
            # presentation choice, and burying it in evaluation order would hide that. Every phase
            # runs even when an earlier one failed — a ceremony that stopped at the first fault
            # would report one line where the whole picture is what a reader needs.
            results: dict[str, bool] = {}
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
                owner_id=args.owner,
                class_id=class_id,
                corrupt=write_corrupt_pdf(Path(tmp)),
            )
            results["3 reaper"] = _phase_reaper(
                conn,
                observer,
                reaper_log,
                owner_id=args.owner,
                class_id=class_id,
                target=_slowest_to_embed(corpus),
                embedder=embedder,
                lease_seconds=args.lease,
                chunk_size=args.chunk_size,
                chunk_overlap=args.chunk_overlap,
            )
            # A worker that CRASHED is a gate failure, not a setup failure, and the split is
            # `SetupError`'s: the ceremony was staged, the write path ran, and it fell over — a
            # result, however bad. Reported after the phases so its traceback does not read as the
            # cause of a phase that was already failing for its own reason.
            if worker_a.error is not None:
                print(f"\n  worker A died: {worker_a.error!r}")
                results["worker A survived"] = False

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
        "retry spent · reaper redelivery leaving one full chunk set · no partial index sighted)"
    )
    return EXIT_OK if passed else EXIT_GATE_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
