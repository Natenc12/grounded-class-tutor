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
  3. REAPER — a worker OUTRUNS a short lease while embedding a real file, a second worker's
     reaper reclaims the job mid-flight, and the redelivered run leaves the file with ONE full
     chunk set. At-least-once is safe by `index_file`'s idempotent replace, not by dedup logic
     (ADR 0011/0020). The overrun is INDUCED, by a delay this script wraps around the real embedder
     — see `_DelayedEmbeddings` for the decision and, more importantly, for the claim it gives up.

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
`DEFAULT_LEASE_SECONDS` and the only way to stage a reclaim would be to write `jobs.leased_until`
from outside — reaching into the queue's own state to fake the one fact the phase is about. On a
thread the lease is a PARAMETER, so the expiry is the queue's own: `claim` stamps it, the reaper
compares it against the server clock, and nothing here touches the column. What this script
supplies is the other side of the race — an embed slow enough to still be running when that lease
elapses (`_DelayedEmbeddings`). Read the two together: the reclaim, the redelivery and the replace
are real; the certainty that A is still working when the lease expires is arranged.

`scripts/worker.py` is deliberately NOT imported (ADR 0009: the scripts are peers of the library,
not of each other) — this file does its own wiring, exactly as that one does.

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
    uv run python scripts/ingest_smoke.py --lease 30           # a lease longer than the induced
                                                               # delay: phase 3 then FAILS, which
                                                               # is what proves its verdict is
                                                               # about the run and not the code
                                                               # path merely existing

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
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
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

# The lease phase 3 overruns. One second is the floor `claim` can express (`lease_seconds` is an
# int, and 0 would already have expired when the lease was granted — forcing the reclaim rather
# than earning it), and `_validate_args` now enforces that floor rather than leaving it in prose.
DEFAULT_SHORT_LEASE_SECONDS = 1

# How long phase 3 pauses after each real embed call, to guarantee the overrun (`_DelayedEmbeddings`
# carries the decision and what it gives up). Sized against the FASTEST file the ceremony can be
# pointed at rather than the slowest, because `--max-files` may narrow the corpus to one small deck:
# the quickest real file ingests in ~0.6s, so 3s puts even that run ~2.6s past the 1s lease, and the
# reaper fires ~1.05s in with most of the ingest still to go. Wall cost is ~2x this, since phase 3
# embeds the file twice — once in the run that loses the lease and once in the redelivery — which is
# a few seconds on a ceremony that already spends longer than that on the corpus.
PHASE_THREE_EMBED_DELAY_SECONDS = 3.0

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


class _DelayedEmbeddings:
    """The REAL embedder, with a switchable pause after each call. Phase 3's overrun, made certain.

    WHY THIS EXISTS, AND WHAT IT GIVES UP (Nate's call, 2026-09-01). Phase 3 needs worker A to still
    be embedding when its lease expires. Left to the corpus that was a 0.25s margin on a 1.25s
    ingest against a 1s lease — reproducible on the day it was measured and one fast provider
    response away from silently not happening. What phase 3 is FOR is the idempotent replace
    absorbing a duplicate delivery; the overrun is the setup, not the claim. So the setup is made
    certain and the claim is left alone: the lease expiry, the reaper's reclaim, the redelivery and
    the all-or-nothing replace are all exactly as real as before, and only the timing stops being
    luck.

    Say the cost out loud, because it is a claim this ceremony can no longer make: the run no longer
    demonstrates that a worker overran its lease UNPROMPTED. It demonstrates that when one does, the
    reaper reclaims the job and the redelivery leaves one full chunk set. The first was never the
    acceptance criterion; the second is.

    IT LIVES HERE, NOT IN `gct` (ADR 0009). A delay knob on `gct.providers` or on the worker would
    be library behaviour existing only to serve a ceremony — the wrong side of the seam. This is
    wiring, and wiring is what a script is for.

    IT PROXIES EVERYTHING IT DOES NOT OVERRIDE, and `model_id` is why that is a rule and not a
    courtesy: `chunks.embedding_model_id` is stamped from `embedder.model_id` (ADR 0018), and the
    Retriever asserts that stamp against the active embedder's. A wrapper that shadowed the
    attribute would stamp the wrong value; one that dropped it would raise mid-ingest; one that
    hardcoded it would make the guard compare a constant to itself. Delegation keeps the stamp the
    real model's by construction.

    IT CHANGES NOTHING ABOUT WHAT IS EMBEDDED. Same texts in, the real client's vectors back, one
    inner call per outer call. The only difference is when `embed` returns.

    SWITCHABLE RATHER THAN ALWAYS-ON, because worker A is a single long-lived thread holding ONE
    embedder for all three phases (see the module docstring) — there is no seam at which to hand it
    a different object mid-run. Disarmed, `embed` is a delegation and an `if`; phases 1 and 2 are
    therefore unaffected in every way they could observe, and their timing remains part of no claim.
    `delaying()` is what arms it, scoped to a block so it cannot be left on.
    """

    def __init__(self, inner: Embeddings) -> None:
        self._inner = inner
        self.delay_seconds = 0.0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._inner.embed(texts)
        # AFTER the real call, so the provider round trip still happens as promptly as it would
        # unwrapped and the pause is purely additional. Per CALL, not per run: a file large enough
        # to sub-batch pauses once per sub-batch, which only ever lengthens the overrun.
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return vectors

    @contextmanager
    def delaying(self, seconds: float) -> Iterator[None]:
        """Arm the delay for the duration of a block, and disarm it however the block leaves."""
        self.delay_seconds = seconds
        try:
            yield
        finally:
            self.delay_seconds = 0.0

    def __getattr__(self, name: str):
        # Reached only for attributes this class does not define — `model_id` above all. Goes
        # through `__dict__` rather than `self._inner`, which would recurse into this method if it
        # ever fired before `__init__` bound that name.
        #
        # KNOWN RESIDUAL, LEFT DELIBERATELY: this collapses a BROKEN attribute into a MISSING one.
        # If a property on the real embedder raised `AttributeError`, the exception would propagate
        # out of here, and Python's attribute protocol reads any `AttributeError` from `__getattr__`
        # as "no such attribute" — so `hasattr(wrapper, name)` would be False for something that
        # exists and is failing. Unreachable today: the attributes anything reads off an embedder
        # (`model_id`, `dim`) are plain returns on `OpenAIEmbeddings`.
        #
        # Not fixed, because no fix is small. Telling the two apart means probing the inner TYPE
        # before the call and re-raising the broken case as a non-`AttributeError` — a second
        # exception type, a `vars()` fallback that `__slots__` breaks, and a change to what
        # `hasattr` means on this object, all bought on speculation about a property that does not
        # exist. The residual is written down instead, which is what makes it a decision rather than
        # an oversight.
        try:
            inner = self.__dict__["_inner"]
        except KeyError:  # pragma: no cover - only reachable mid-construction
            raise AttributeError(name) from None
        return getattr(inner, name)


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
    """What to change when phase 3's ingest finished inside its lease.

    THE ARGUMENT HERE CHANGED WHEN THE DELAY DID (2026-09-01), and the old one is gone rather than
    left standing beside the new: it told the reader to point `--corpus-dir` at a wordier corpus,
    because back then the overrun depended on the file being slow enough to embed. It no longer
    does. `_DelayedEmbeddings` puts `PHASE_THREE_EMBED_DELAY_SECONDS` between the embed and the
    return, so any file overruns any lease shorter than that and corpus size stopped being a lever.
    A remedy naming the wrong variable is worse than none — it is a confident instruction to change
    something that cannot help.

    Two things can still produce this fault, and they have different fixes:
      - the lease was raised above the induced delay (`--lease 30` does this on purpose — it is the
        documented negative control that proves the phase's verdict is about the run);
      - the delay never reached the worker, i.e. the embedder worker A is holding is not the wrapper
        this phase armed. That is a wiring bug in this script, not a knob.
    """
    if lease_seconds >= PHASE_THREE_EMBED_DELAY_SECONDS:
        return (
            f"--lease is {lease_seconds}s and the induced embed delay is only "
            f"{PHASE_THREE_EMBED_DELAY_SECONDS:.0f}s, so the ingest fits inside the lease. Lower "
            f"--lease below the delay (the default {DEFAULT_SHORT_LEASE_SECONDS}s is what the "
            "ceremony is built around)"
        )
    return (
        f"--lease ({lease_seconds}s) is already under the {PHASE_THREE_EMBED_DELAY_SECONDS:.0f}s "
        "induced delay, so the ingest should not have fit inside it: the delay is not reaching the "
        "worker. Check that worker A was handed the same `_DelayedEmbeddings` this phase arms"
    )


def _terminal(snapshot: Snapshot) -> bool:
    """A file the worker is finished with, on the FILE axis the student watches."""
    return snapshot.file_status in ("ready", "failed")


def _first_death(workers: Sequence[WorkerThread]) -> BaseException | None:
    """The exception that killed the first dead worker, or None while they are all alive."""
    for worker in workers:
        if worker.error is not None:
            return worker.error
    return None


def _await(
    observer: Observer,
    predicate: Callable[[dict[str, Snapshot]], bool],
    *,
    workers: Sequence[WorkerThread],
    stalled: str,
) -> str | None:
    """Wait for `predicate` — but stop the moment a worker dies. None on success, else why not.

    ONE WRITER FOR "STOP WAITING", used by all three phases, because the failure it exists to
    prevent was a phase-shaped one and would have come back per phase. `WorkerThread` catches a
    crash so the ceremony can diagnose it, but catching it is only half the mechanism: the caught
    exception has to be READ somewhere that can act on it. It was read once, in `main`, AFTER every
    phase had run — so a worker that died on the first file left all three phases waiting out
    `PHASE_TIMEOUT_SECONDS` against a queue nothing was serving, and the ceremony reported a
    lifecycle failure fifteen minutes later with the wrong cause on top of the right one. The
    diagnosis existed and arrived too late to be a diagnosis.

    Folding the check into the PREDICATE rather than polling it separately is what makes it prompt:
    the observer is already sampling every `OBSERVE_SECONDS`, so a death is noticed on the next
    tick, using the loop that is already running. Phase 3 already did this for worker B, one wait
    too late; this is that move made general and made early.

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

    stalled = _await(
        observer,
        lambda current: all(_terminal(current[file_id]) for file_id in file_ids),
        workers=(worker,),
        stalled="not every file reached a terminal",
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
    worker: WorkerThread,
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
        lambda current: _terminal(current[file_id]),
        workers=(worker,),
        stalled="the corrupt file never settled",
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


def _phase_reaper(
    conn: psycopg.Connection,
    observer: Observer,
    reaper_log: ReaperLog,
    worker: WorkerThread,
    *,
    owner_id: str,
    class_id: str,
    target: Path,
    embedder: _DelayedEmbeddings,
    lease_seconds: int,
    chunk_size: int,
    chunk_overlap: int,
) -> bool:
    """An induced lease overrun, a real reclaim, and one full chunk set at the end.

    WHAT IS ARRANGED AND WHAT IS NOT, in one place, because this is the phase where the difference
    matters. `jobs.leased_until` is never written by this script: worker A takes a real
    `lease_seconds` lease from `claim`, and the reaper compares it against the server's clock like
    any other. What is arranged is that A is STILL EMBEDDING when that moment arrives — the embed
    is padded by `PHASE_THREE_EMBED_DELAY_SECONDS` (`_DelayedEmbeddings`), so the overrun happens
    every run instead of most runs. Worker B is started only AFTER A has been observed to claim, so
    the ordering is a fact and not a race, and B's first reaping tick past the expiry reclaims a job
    that is genuinely still being worked.

    So the phase no longer demonstrates that a worker overran its lease UNPROMPTED. It demonstrates
    the thing the issue actually asks for: that when a lease expires under a running worker, the
    reaper reclaims it, the job is redelivered, and `index_file`'s all-or-nothing replace absorbs
    the duplicate into ONE full chunk set.

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
    print(
        f"\nPhase 3 — reaper: a {PHASE_THREE_EMBED_DELAY_SECONDS:.0f}s induced embed delay "
        f"against a real {lease_seconds}s lease:"
    )
    # ARMED AROUND THE WHOLE PHASE, not just the enqueue. Worker A is already running and holds this
    # very object (there is no seam at which to swap its embedder mid-run), so arming has to happen
    # before A can claim and stay on until the redelivery has finished — worker B embeds through the
    # same wrapper. The block disarms it however this phase leaves, so nothing after phase 3 pays
    # for it.
    with embedder.delaying(PHASE_THREE_EMBED_DELAY_SECONDS):
        return _reaper_body(
            conn,
            observer,
            reaper_log,
            worker,
            owner_id=owner_id,
            class_id=class_id,
            target=target,
            embedder=embedder,
            lease_seconds=lease_seconds,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )


def _reaper_body(
    conn: psycopg.Connection,
    observer: Observer,
    reaper_log: ReaperLog,
    worker: WorkerThread,
    *,
    owner_id: str,
    class_id: str,
    target: Path,
    embedder: _DelayedEmbeddings,
    lease_seconds: int,
    chunk_size: int,
    chunk_overlap: int,
) -> bool:
    """Phase 3 with the delay already armed — split out so the arming is one unmissable line."""
    file_id = enqueue(conn, path=target, owner_id=owner_id, class_id=class_id)
    observer.watch(file_id)
    print(f"  enqueued {target.name} -> file_id={file_id}")

    reaps_before = reaper_log.reaps
    stalled = _await(
        observer,
        lambda current: current[file_id].job_state == "processing",
        workers=(worker,),
        stalled="worker A never claimed the job; nothing to overrun",
    )
    if stalled is not None:
        print(f"  {stalled}")
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

    stalled = _await(
        observer,
        lambda current: current[file_id].job_state in ("done", "failed"),
        workers=(worker, worker_b),
        stalled="the job never settled",
    )
    history = observer.history(file_id)
    _print_history(f"{target.name}: {' -> '.join(status_path(history))}", history)
    if stalled is not None:
        print(f"  {stalled}")
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
        faults.append(transition_fault(before, after))
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
    """Refuse flag values the ceremony argues elsewhere are impossible. Raises `SetupError`.

    ARGPARSE ENFORCES TYPES, NOT RANGES, and every range this ceremony depends on was argued in
    prose and checked nowhere — which is the same defect twice:

    `--max-files 0` reads as UNSET, because `files[:max_files] if max_files else files` treats 0 as
    falsy. A run that asked for no files silently got the whole corpus. `--max-files -1` is worse
    than useless: it drops the LAST file, which after sorting is the one with the most words, so it
    quietly removes the file phase 3 is most likely to need and then fails phase 3 for a reason the
    output does not contain.

    `--lease 0` and below are refused for the reason `_overrun_remedy` and this module's docstring
    both already give: a zero-second lease expires at the instant it is granted, so phase 3 would
    "pass" on an expiry nobody earned — forcing the reclaim by another name, and the exact thing the
    thread design exists to avoid. The argument was written in three places and enforced in none,
    which made it a comment rather than a rule.

    A SETUP error, not a gate failure: a run configured this way was never stageable, and calling it
    a bad RESULT would report the write path misbehaving for a typo in a flag.
    """
    if args.max_files is not None and args.max_files < 1:
        raise SetupError(
            f"--max-files must be at least 1 (got {args.max_files}); omit it to use the whole "
            "corpus. 0 is not 'no files' — it reads as unset and silently ingests all of them"
        )
    if args.lease < DEFAULT_SHORT_LEASE_SECONDS:
        raise SetupError(
            f"--lease must be at least {DEFAULT_SHORT_LEASE_SECONDS}s (got {args.lease}). A lease "
            "of 0 or less has already expired when `claim` grants it, so phase 3's reclaim would "
            "be forced rather than earned — which is precisely what the thread design exists to "
            "avoid"
        )


def _build_parser() -> argparse.ArgumentParser:
    """The CLI surface, separated from parsing so a test can read what the flags actually SAY.

    Split out after `--max-files`' help described phase 3 as running on "the largest file of that
    subset" when the code picked a different one — a help string and its code disagreeing, with
    nothing able to notice. Help text is documentation that ships inside the program; it drifts
    exactly like a comment and, unlike a comment, a user acts on it.
    """
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
        "still runs, on the FIRST file of that subset — its overrun is induced, so which file it "
        "picks no longer affects whether the lease expires",
    )
    parser.add_argument(
        "--lease",
        type=int,
        default=DEFAULT_SHORT_LEASE_SECONDS,
        help=f"lease worker A holds, in seconds (default and floor {DEFAULT_SHORT_LEASE_SECONDS}). "
        f"Phase 3 induces a {PHASE_THREE_EMBED_DELAY_SECONDS:.0f}s embed delay, so this must be "
        "shorter than that delay. File ingest speed is NOT a constraint here: it stopped "
        "being one when the overrun became induced rather than earned from the corpus",
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
    return parser


def _parse_args() -> argparse.Namespace:
    return _build_parser().parse_args()


def main() -> int:
    args = _parse_args()
    print("Slice 2 exit test — the write path, over real course materials and real embeddings.")

    try:
        _validate_args(args)
        corpus = _corpus_files(Path(args.corpus_dir), args.max_files)

        # The key check is explicit because absence never raises: `load_settings()` defaults a
        # missing key to "", which the client ACCEPTS at construction, so the failure would
        # otherwise surface as an AuthenticationError inside a worker THREAD, mid-phase — where it
        # reads as a gate failure ("the write path misbehaved") for a run that never validly
        # started. Same conversion, same argument, as `ask_smoke.main`.
        if not load_settings().openai_api_key:
            raise SetupError("OPENAI_API_KEY is empty — set it in .env (CLAUDE.md, Local dev)")
        try:
            # Wrapped ONCE, here, and handed to worker A and to phase 3 as the same object — which
            # is what makes phase 3 able to arm it (`_DelayedEmbeddings`). Disarmed it is a
            # delegation and an `if`, so phases 1 and 2 embed exactly as they would unwrapped.
            embedder = _DelayedEmbeddings(OpenAIEmbeddings())
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
            # transaction (`require_idle`; ADR 0025, guarded per ADR 0027), and psycopg opens the
            # implicit transaction on the first statement — so without this the class insert leaves
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
                worker_a,
                owner_id=args.owner,
                class_id=class_id,
                corrupt=write_corrupt_pdf(Path(tmp)),
            )
            results["3 reaper"] = _phase_reaper(
                conn,
                observer,
                reaper_log,
                worker_a,
                owner_id=args.owner,
                class_id=class_id,
                target=corpus[0],
                embedder=embedder,
                lease_seconds=args.lease,
                chunk_size=args.chunk_size,
                chunk_overlap=args.chunk_overlap,
            )
            # A worker that CRASHED is a gate failure, not a setup failure, and the split is
            # `SetupError`'s: the ceremony was staged, the write path ran, and it fell over — a
            # result, however bad.
            #
            # A BACKSTOP NOW, NOT THE DIAGNOSIS. `_await` reads the same flag inside every phase's
            # wait, so a worker that dies while anything is being waited on is caught there, in the
            # phase it broke, within one `OBSERVE_SECONDS`. This line covers the remaining sliver —
            # a death BETWEEN phases, with nothing waiting — and keeps the gate honest about it. It
            # used to be the only reader, which is why the ceremony could spend three full phase
            # timeouts before mentioning that its worker had been dead the whole time.
            death = _first_death((worker_a,))
            if death is not None:
                print(f"\n  worker A died: {death!r}")
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
