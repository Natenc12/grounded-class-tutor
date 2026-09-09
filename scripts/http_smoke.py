"""Slice 3 exit smoke — the whole loop over HTTP, against two real processes (#109).

Brings up the pair the Slice 3 exit gate has to be driven against — `uvicorn gct.api.app:app`
and `scripts/worker.py`, as two REAL child processes — proves each one is up, guarantees both are
gone afterwards, and in between drives the student's whole loop over the socket between them:
create a class, upload a file, poll it to `ready`, ask a question the file answers and get a
CITED answer, ask one it does not and get an honest REFUSAL, then upload a file no parser can
read and watch the reason surface through `GET /files/{id}`. That is the slice's Exit criterion —
"the full loop is drivable over HTTP; the status surface exposes actionable terminal reasons" —
and `run_gate` is the one function that asserts it.

THE HALVES ARE SEPARABLE AND THE SPLIT IS ABOUT COST, not about phases. Everything down to
`launched()` is processes, sockets and signals, and `--launch-only` runs exactly that and spends
nothing; the ceremony from `run_gate` down is HTTP, and it spends real money on real models. The
launch half is what PR 2 of this issue built and its decisions are recorded below unchanged.

TWO PROCESSES, NOT ONE, AND NOT AN IN-PROCESS `TestClient`. ADR 0011's PM-3 addendum mandates the
worker be a separate OS process rather than a task in uvicorn's loop, so a harness that staged the
exit gate any other way would be demonstrating a topology we do not ship. `scripts/ingest_smoke.py`
runs its worker on a thread; its own docstring is the single writer of why THAT one stays that way,
and it is not a precedent for this file.

A thin peer caller (ADR 0009): every decision this file makes is about processes, sockets and
signals — nothing about ingestion, retrieval or grounding. It imports `gct` for exactly two things,
both of which are refusals it would otherwise have to duplicate: the API's own key requirement and
the database URL.

THE SERVER IS HANDED A SOCKET THAT IS ALREADY LISTENING, rather than a port number to bind.
`_reserve_listener` binds `127.0.0.1:0`, listens, and the fd is inherited by uvicorn through
`--fd` (`pass_fds`), so between allocation and service there is no instant at which the port is
free for anyone else to take. The alternative — bind port 0, read the number, close, pass `--port`
— leaves a window as wide as the child's interpreter startup: hundreds of milliseconds here (the
measurement is beside `READY_TIMEOUT_SECONDS`, which is its one writer) and unbounded on a loaded
machine. Several ship lanes and a dev server run concurrently here, and the cost when that race
fires is not a clean failure: the loser binds nothing, uvicorn exits, and the smoke reports a
launch failure that has nothing to do with the code under test. Two further properties fall out
and are relied on below: the socket is bound to LOOPBACK by us, so a lane's smoke server is never
reachable off-box whatever uvicorn's own `--host` default is; and the parent closes its copy
immediately after spawning, so once the child dies the port refuses connections instead of
accepting into a backlog nobody serves.

READINESS IS A 200 FROM `GET /health`, WITH THE BODY CHECKED. A TCP connect proves nothing here
by construction — the socket was listening before the child was even forked. Uvicorn's own
"Application startup complete" line would prove the lifespan finished, but it is a third party's
log format and it says nothing about Postgres. `/health` is the one probe that establishes what
the first real request needs: a request connection was built, it reached the database, and it was
closed (`src/gct/api/routers/health.py`).

THE WORKER HAS NO SUCH SURFACE, AND THIS FILE DOES NOT PRETEND OTHERWISE. There is nothing for it
to claim yet and no endpoint to ask, so "ready" is not available and is not claimed. What is
observable is that it got through its own startup — `connect()` returned, the embedder was
constructed, and `gct.jobs.worker.run` entered its loop — because `run` logs one INFO line at
exactly that point and this harness waits for it (`WORKER_STARTED_TOKEN`). That is a second reader
of a message the library writes, which is a coupling, and it is pinned by a test rather than left
to drift: `test_the_token_the_harness_waits_for_is_the_one_the_library_actually_logs`.

A DEAD CHILD IS NEVER WAITED OUT. Every readiness tick asks `poll()` first, so a process that died
at startup — no key, Postgres down, a flag the CLI refused — is reported in the time it took to
die, with its exit status and the tail of its own log, instead of consuming the whole timeout and
reporting a hang. That is the difference between a harness that tells you what broke and one that
tells you it waited.

IT REFUSES A DATABASE IT DOES NOT OWN, and that is a cost guard rather than tidiness. The worker
this launches is the real one: it polls `jobs` in whatever `DATABASE_URL` names and claims
anything it finds there, so a run that uploads nothing still ingests — and bills for — somebody
else's file. `preflight` refuses up front unless `files` is empty (`--dedicated-database` is the
operator's word in place of that evidence), because a queue that is merely empty AT THE INSTANT OF
THE CHECK is not the same promise: a lease lapsing a second later hands that job straight to this
harness's worker, which is what #138 measured. Its docstring is the writer of what the check does
and does not cover. Everything this harness itself enqueues happens after both children are up, so
the guard never sees a file the run is responsible for.

A THIN PEER CALLER STILL (ADR 0009), and the ceremony is where that is worth restating. It makes
no product judgement of its own: what a cited answer IS, what a refusal is, and what a terminal
reason means are all decided one layer down and simply asserted here. What is legitimately
script-local is the CEREMONY — the order of the steps and what counts as a pass — for the reason
`scripts/ask_smoke.py`'s docstring gives about the Slice 1 gate.

Usage:
    uv run python scripts/http_smoke.py
    uv run python scripts/http_smoke.py --launch-only
    uv run python scripts/http_smoke.py --dedicated-database
    uv run python scripts/http_smoke.py --ready-timeout 90 --ingest-timeout 300
    uv run python scripts/http_smoke.py --corpus my-lecture.pdf --question "What is a watershed?"
    uv run python scripts/http_smoke.py -- --poll 0.5 --lease 60
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # `gct` and its dependencies are imported only where a real run needs them
    import psycopg

# The interface the smoke serves and probes on. Ours, not uvicorn's `--host` default: we own the
# socket, so the bind address is decided here and a smoke server cannot be reached off-box.
LOOPBACK = "127.0.0.1"
LISTEN_BACKLOG = 128

# How long either child may take to become observable. Generous on purpose and cheap to be wrong
# about: a child that DIED is reported at once (see the module docstring), so this only bounds a
# child that is genuinely still starting.
#
# THE ONE WRITER OF THAT MEASUREMENT, which the module docstring points at rather than repeats.
# Warm, over five back-to-back launches on this machine: a median 0.62s from spawn to a served
# `/health` (max 0.82) and 0.67s from spawn to the worker's start-of-loop line (max 0.70), nearly
# all of it interpreter and import time — `python scripts/worker.py --help`, which does the
# imports and nothing else, is 0.53s of the 0.67. So the default is ~90x the observed cost, which
# is the headroom a cold cache or a loaded CI runner is allowed to eat into before this number is
# the thing that failed.
READY_TIMEOUT_SECONDS = 60.0
# Gap between readiness probes. Small enough that a fast start is not rounded up to a tenth of a
# second of dead air, large enough that the loop is not a spin.
PROBE_INTERVAL_SECONDS = 0.1
# Per-probe HTTP timeout, distinct from the deadline above: a server that accepts and then never
# answers must not consume the whole readiness budget in one call.
PROBE_HTTP_TIMEOUT_SECONDS = 2.0

# How long a child gets between SIGTERM and SIGKILL. Signal delivery is not preemptive, and the
# worker's stop path is the one that needs the room: `scripts/worker.py`'s handler raises on the
# main thread, so a process blocked in a C call (an embedding request in flight) unwinds only when
# that call returns. Past this the harness stops waiting and kills, which costs the lease — the
# reaper collects it (ADR 0011; SIGKILL runs no handler, as `_interrupt` records).
TERMINATE_GRACE_SECONDS = 10.0

# What the harness waits for in the worker's log to call its startup done. `gct.jobs.worker.run`
# logs this at INFO immediately after the lease/heartbeat numbers are resolved and before the
# first tick, so seeing it means connect() returned and the embedder was constructed.
WORKER_STARTED_TOKEN = "worker started"

# How much of a child's log a failure message carries. The whole file stays on disk (the path is
# in the message); this is the part that gets read.
LOG_TAIL_LINES = 25
LOG_TAIL_BYTES = 64 * 1024

# Exit statuses a stopped child may legitimately have, spelled as measured rather than assumed.
# `scripts/worker.py` catches the interrupt its SIGTERM handler raises and exits 0 (#82);
# uvicorn re-raises the signal after shutting down, so it exits as killed-by-SIGTERM. A child
# that stops any other way had something go wrong on the way out and the harness says so.
CLEAN_EXIT_STATUSES = (0, -int(signal.SIGTERM))

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_SETUP = 2

# The rows the worker this harness launches would take if it found them: `claim` selects
# `state = 'queued'` (`src/gct/jobs/queue.py`), and its own first tick calls `reclaim_expired`,
# which turns a `processing` row whose lease has lapsed back into a queued one. So both belong in
# the same question — "is there work here that is not ours?" — and asking only about `queued`
# would miss a row this harness itself is about to requeue. `count(*) over ()` is the total before
# `limit`, so one statement answers how many there are and names the first few.
_WORK_THE_WORKER_WOULD_CLAIM = """
    select count(*) over () as waiting, job_id::text, file_id::text, state
    from jobs
    where state = 'queued'
       or (state = 'processing' and leased_until < now())
    order by created_at
    limit 3
"""

# Whether this database holds anything at all, which is a different question from the one above
# and the reason #138 exists. The predicate above asks what is claimable NOW; this one asks
# whether there is anything here that could BECOME claimable, and `jobs.file_id references
# files(file_id)` (0001_init.sql, no cascade) makes an empty `files` the whole answer: no file,
# no job, nothing for the reaper or a release to hand the launched worker.
#
# THE ORDER IS PART OF THE MESSAGE, not a tidiness choice. The refusal names three rows and counts
# the rest, so which three it names is what an operator actually reads. Sorting by age alone put
# three finished `ready` files in front of the one `processing` row that could still cost money,
# hiding the hazard behind "(and 1 more)" — measured in this ticket's verification round.
# `status in ('ready', 'failed')` is FALSE for the rows that can still move, and false sorts
# first, so a live row is always named ahead of a settled one. `file_id` last so the sample is
# deterministic under equal timestamps instead of order-undefined, which no test could pin.
_FILES_THIS_RUN_DID_NOT_PUT_HERE = """
    select count(*) over () as present, file_id::text, filename, status
    from files
    order by (status in ('ready', 'failed')), created_at, file_id
    limit 3
"""


class SetupError(RuntimeError):
    """The run was never stageable — refused before anything was launched.

    Same distinction `scripts/ingest_smoke.py` and `scripts/ask_smoke.py` draw, and for the same
    reason: a missing key or an unmigrated database is a fact about this machine, not a verdict
    about the code, so it exits 2 and must never be read as the smoke failing.
    """


class LaunchError(RuntimeError):
    """A child was launched and did not become observable — died, or never answered in time."""


@dataclass(frozen=True)
class Child:
    """One launched process, its log file, and the two questions the harness asks about it."""

    name: str
    process: subprocess.Popen[bytes]
    log_path: Path

    @property
    def pid(self) -> int:
        return self.process.pid

    def tail(self, lines: int = LOG_TAIL_LINES) -> str:
        """The last few lines the child wrote, for a message that has to explain a failure.

        Seeks rather than reading the file whole: a long run's worker log is unbounded, and the
        harness reads it while the process is still appending to it.
        """
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - LOG_TAIL_BYTES))
                text = handle.read().decode("utf-8", "replace")
        except OSError as err:
            # Reported inline rather than raised: this runs INSIDE a failure message, so a raise
            # here would replace the launch failure with an error from the code explaining it.
            return f"(could not read {self.log_path}: {err})"
        kept = text.splitlines()[-lines:]
        return "\n".join(kept) if kept else "(the child wrote nothing)"

    def log_contains(self, token: str) -> bool:
        try:
            return token in self.log_path.read_text("utf-8", "replace")
        except OSError:
            # False, and the direction matters: this IS the worker's readiness probe, so a log
            # that cannot be read must never answer "started" about a child that wrote nothing.
            return False


@dataclass(frozen=True)
class Stack:
    """Both children plus the URL the API is reachable at, for the duration of a `with` block."""

    base_url: str
    server: Child
    worker: Child


def repo_root() -> Path:
    """The tree this script belongs to. `scripts/` is not a package, so this is the anchor."""
    return Path(__file__).resolve().parent.parent


def preflight(*, dedicated_database: bool = False) -> None:
    """Refuse, up front and with the remedy, the conditions that otherwise arrive as a hang — or
    as a bill.

    WHAT A CLEAN RETURN GUARANTEES, EXACTLY — and the list is here because the version #138 was
    filed against did not have one, so a reader took a point-in-time queue check for a promise
    about the whole run. When this returns:

      - the API's own key requirement is satisfied, Postgres is reachable, and `files` and `jobs`
        exist, so neither child dies at startup for a reason about this machine;
      - `files` HELD NOTHING when it looked, unless `--dedicated-database` said not to ask;
      - nothing was claimable at that instant.

    WHY AN EMPTY `files` IS THE STRONGER FACT — stated as the two mechanisms it rests on rather
    than as an absolute, because the absolute was written first and falsified in this ticket's own
    verification round. `jobs.file_id references files(file_id)` with no cascade
    (`0001_init.sql`) normally makes an empty `files` an empty `jobs`, and that is the everyday
    argument. It is not a proof: `set session_replication_role = replica` — what
    `pg_restore --disable-triggers` does — suspends the constraint, and a `jobs` row can outlive
    its file. What holds even then is the second mechanism, inside the claim itself:
    `gct.jobs.queue.claim` inner-joins `files` (`join files f using (file_id)`), so a job whose
    file is gone is reclaimable but never claimable. Between them the conclusion survives — an
    empty `files` leaves the launched worker nothing it can take — but a reader deleting this
    check needs the mechanism, not the slogan, because the slogan is what fails the moment
    somebody restores a dump.

    WHAT IT DOES NOT GUARANTEE, and no version of this check can. It cannot stop a row that
    ARRIVES after it looks: a second API process, another `enqueue`, or a person at a psql prompt
    can put a `queued` job here a second later, and the launched worker will take it, because
    that worker claims from the whole table by design (ADR 0011) and cannot be told which files
    are this run's. "This run costs only its own work" is therefore a property of the database
    being nobody else's. An empty `files` is EVIDENCE of that; `--dedicated-database` is only the
    operator's word for it, and passing it drops the evidence and leaves the queue check alone —
    which #138 measured to be blind to exactly the case that matters, a `processing` row whose
    lease lapses one second after the check passed.

    The first two are checked HERE rather than left to the children because of where they land
    otherwise. A missing key makes uvicorn exit during startup and an unreachable database makes
    the worker die inside `connect()` — with a dead-child check on every tick those are reported
    quickly, but they are reported as a launch failure with somebody else's traceback attached,
    when they are really "this machine is not set up". Naming the remedy costs a line and saves
    the reader a debugging session (the same argument `gct.db.require_idle`'s message makes).

    The key requirement is the API's OWN, imported rather than restated: a second copy of "which
    variable, and what it is for" is a second writer for the fact, and this one would be checked
    from a script nobody edits when the rule changes.

    WHAT THE LAST TWO REFUSALS COST IF THEY ARE DELETED, and it is a cost guard rather than
    tidiness. Neither child issues an API call while starting — that is measured, and it is why a
    launch is free — but the worker is a REAL worker, and `gct.jobs.worker.run` polls `jobs` in
    whatever database `DATABASE_URL` names and claims whatever is there. Without these checks a
    harness that uploads nothing still ingests other people's files. Measured on a lane database
    holding one queued PDF:

      - a 25s hold against a recording endpoint drew 12 `POST /v1/embeddings` — billable against a
        real key, from a run that enqueued nothing;
      - a 20s hold against a closed endpoint spent the file's whole retry budget and buried it,
        so the harness alone drove an unrelated upload from `queued` to `failed`;
      - even a bare ~0.1s run moved `files.status` to `processing` and `jobs.attempts` 0 → 1.

    A bare `uv run python scripts/http_smoke.py` reads `.env`, which on a dev machine is the
    dogfood database, so "don't run it there" was the whole protection. Now it refuses.

    EMPTINESS RATHER THAN A SHARPER PREDICATE, deliberately. "No file is in a state that could
    still move" would let the smoke run against a database of finished work, and it would be one
    more predicate to keep in step with the job state machine — the queue check is already a
    predicate that got that wrong, which is the whole of #138. "Nothing is here" needs no
    maintenance and cannot be subtly incomplete. The price is real and is paid on purpose: a full
    run uploads a file and leaves it behind, so a SECOND full run against the same database is
    refused until it is re-created or declared. `--launch-only` uploads nothing and so repeats
    freely against a database this check accepts — which on a dev machine is NOT the one `.env`
    names, and the remedy there is a scratch `DATABASE_URL`, not the flag.

    `--dedicated-database` IS NOT THE WAY PAST A REFUSAL ON A DATABASE YOU DO NOT OWN, and the
    temptation is worth naming because it is the shortest thing to type when a launch is refused.
    Measured in this ticket's verification round, with the flag and a foreign `processing` job
    whose lease lapsed inside the ~2.3s launch window: the launched worker reaped it, claimed it,
    and left it `queued` with `attempts` 0 → 1 — one retry of the ADR 0011 budget spent on
    somebody else's file, by a `--launch-only` run that uploaded nothing. The flag is a statement
    that nothing else writes here. Where that statement is false it buys back exactly the hazard
    #138 is about.

    ORDERING, FOR THE CEREMONY (#109 PR 3): this runs ONCE, before either child exists, and the
    ceremony's own upload happens after `launched()` has yielded. A file this run enqueues can
    therefore never be seen by this check — the guard does not block the smoke's own work, only
    work that was here before it started.
    """
    from gct.api.app import require_openai_key  # imported here: only a real run needs fastapi
    from gct.db import connect

    try:
        require_openai_key()
    except RuntimeError as err:
        raise SetupError(f"{err} The smoke launches the real API, which refuses to start.") from err

    try:
        conn = connect()
    except Exception as err:
        raise SetupError(
            f"cannot reach Postgres: {err}. Both children open their own connection at startup "
            "and neither can run without one — check `DATABASE_URL` in `.env` and that the "
            "server is up"
        ) from err
    try:
        files, jobs = conn.execute("select to_regclass('files'), to_regclass('jobs')").fetchone()
        if files is None or jobs is None:
            raise SetupError(
                "the schema is not applied — `files` and/or `jobs` is missing from the database "
                "in `DATABASE_URL`. Run `uv run python scripts/migrate.py` first. (Left unchecked "
                "this surfaces as a worker that starts cleanly and dies on its first claim.)"
            )
        present, files_sample = (0, []) if dedicated_database else existing_files(conn)
        waiting, jobs_sample = claimable_jobs(conn)
    finally:
        conn.close()

    # Ownership first, and the order is the point rather than an accident. A database holding
    # somebody's files is refused whether or not any of them happens to be claimable at this
    # instant, so the message a reader gets names the real condition — "this database is not
    # yours" — instead of a symptom that may not even be showing yet.
    if present:
        rows = "; ".join(
            f"{file_id} ({status}) {filename!r}" for file_id, filename, status in files_sample
        )
        more = "" if present <= len(files_sample) else f" (and {present - len(files_sample)} more)"
        raise SetupError(
            f"the database `DATABASE_URL` names is not this run's to use: `files` already holds "
            f"{present} row(s) — {rows}{more}. This smoke launches the REAL worker, which claims "
            "from the whole `jobs` table (ADR 0011) and cannot be told which files belong to this "
            "run, so any of those rows that becomes claimable while the gate runs — a lease "
            "lapsing under the reaper, a job released back to `queued`, a retry — is ingested and "
            "billed to a run that uploaded nothing. Point `DATABASE_URL` at an empty database, or "
            "re-create this one (`dropdb` + `createdb`, then `uv run python scripts/migrate.py`); "
            "a previous full run of this smoke leaves its own rows behind and is refused here too. "
            "Pass `--dedicated-database` only if nothing but this run writes to it."
        )

    if waiting:
        rows = "; ".join(
            f"job {job_id} ({state}) for file {file_id}" for job_id, file_id, state in jobs_sample
        )
        more = "" if waiting <= len(jobs_sample) else f" (and {waiting - len(jobs_sample)} more)"
        raise SetupError(
            f"{waiting} job(s) in the database `DATABASE_URL` names are waiting to be claimed: "
            f"{rows}{more}. This smoke launches the REAL worker, which would claim them and "
            "embed their files — money spent, and somebody else's upload moved to `processing` "
            "and on to `ready` or `failed` — for a run that uploaded nothing. Let a worker "
            "finish them (`uv run python scripts/worker.py`), or point `DATABASE_URL` at a "
            "scratch database."
        )


def existing_files(conn: psycopg.Connection) -> tuple[int, list[tuple[str, str, str]]]:
    """How many files this database already holds, and up to three of them, oldest first.

    Split out for the same reason `claimable_jobs` is: SQL that is only ever executed behind a
    fake connection is SQL nobody has run against the real tables, and `filename` is a column
    name this file would otherwise never spell out loud.
    """
    rows = conn.execute(_FILES_THIS_RUN_DID_NOT_PUT_HERE).fetchall()
    present = rows[0][0] if rows else 0
    return present, [(file_id, filename, status) for _, file_id, filename, status in rows]


def claimable_jobs(conn: psycopg.Connection) -> tuple[int, list[tuple[str, str, str]]]:
    """How many jobs the launched worker could take, and up to three of them, oldest first.

    Split out from `preflight` so the predicate can be executed against a real schema in a test
    without a launch: the whole guard is one SQL statement, and a statement that is only ever run
    behind a fake connection is a statement nobody has checked against the real tables.
    """
    rows = conn.execute(_WORK_THE_WORKER_WOULD_CLAIM).fetchall()
    waiting = rows[0][0] if rows else 0
    return waiting, [(job_id, file_id, state) for _, job_id, file_id, state in rows]


def _reserve_listener() -> socket.socket:
    """A listening loopback socket on a kernel-chosen port, handed to the child as an fd.

    No `SO_REUSEADDR`: the point of the exercise is that this port is exclusively ours from the
    moment it is allocated, and the option exists to relax exactly that.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((LOOPBACK, 0))
        sock.listen(LISTEN_BACKLOG)
    except OSError:
        sock.close()
        raise
    return sock


def server_argv(fd: int) -> list[str]:
    """`uvicorn gct.api.app:app` on the inherited socket. `app` is the ASGI callable (#104)."""
    return [sys.executable, "-m", "uvicorn", "gct.api.app:app", "--fd", str(fd)]


def worker_argv(extra: Sequence[str] = ()) -> list[str]:
    """`scripts/worker.py`, plus whatever the caller wants to configure.

    Pass-through rather than a fixed command line: the CLI exists so a subprocess is no longer
    pinned to the module defaults (#109 PR 1), and a harness that could not reach those flags
    would leave that CLI with no caller outside its own tests. Nothing is passed by default —
    a flag this file supplied would be this file deciding a queue number, which is not its call
    (ADR 0009) — and `--log-level` in particular is never supplied here. It is also the one flag
    a caller cannot freely choose: above INFO it silences the line readiness waits for, so the
    CLI refuses it rather than hanging (`_refuse_a_worker_level_the_probe_cannot_see`).
    """
    return [sys.executable, str(repo_root() / "scripts" / "worker.py"), *extra]


def _child_env() -> dict[str, str]:
    """The parent's environment, plus unbuffered output.

    Pass-through is deliberate: `DATABASE_URL`, `OPENAI_API_KEY` and `GCT_STAGING_DIR` reach the
    children the same way they reach this process, so there is no second place where a lane's
    settings could be assembled differently. `PYTHONUNBUFFERED` is the one addition and it is
    load-bearing rather than tidy: stdout redirected to a FILE is block-buffered, so without it a
    child that hangs before filling 8KB has written nothing, and every failure message the harness
    builds out of the log tail would be blank in exactly the case it exists for.
    """
    return {**os.environ, "PYTHONUNBUFFERED": "1"}


@contextmanager
def _spawned(
    name: str, argv: Sequence[str], log_dir: Path, *, pass_fds: Sequence[int] = ()
) -> Iterator[Child]:
    """Launch one child, guarantee it is stopped, whatever happens in the block.

    `start_new_session` puts the child in its own process group, which buys two things. The
    harness becomes the single writer of "stop": a Ctrl-C at the terminal is delivered to the
    foreground group, and without this both children would start dying while the parent was still
    mid-report, interleaving output and racing the exit statuses it wants to check. And stopping
    is a `killpg`, so anything the child itself spawned goes with it rather than being orphaned.

    Both streams go to a FILE, not a pipe and not the terminal. A pipe deadlocks a child that
    fills it while the parent is blocked in a readiness poll; inheriting the terminal makes the
    child's output interleave with the ceremony's and leaves the harness with nothing to quote
    when it has to explain a failure.
    """
    log_path = log_dir / f"{name}.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            list(argv),
            cwd=repo_root(),
            env=_child_env(),
            stdout=log,
            stderr=subprocess.STDOUT,
            pass_fds=tuple(pass_fds),
            start_new_session=True,
        )
    child = Child(name=name, process=process, log_path=log_path)
    try:
        yield child
    finally:
        stop(child)


def stop(child: Child, grace: float = TERMINATE_GRACE_SECONDS) -> int:
    """SIGTERM the child's group, then SIGKILL it if it is still there. Returns the exit status.

    SIGTERM first because both children do something useful with it — `scripts/worker.py` hands
    its in-flight job back rather than stranding it for a lease (#82), and uvicorn finishes its
    shutdown — and SIGKILL after `grace` because a harness whose teardown can hang is not a
    teardown. Signalling the GROUP rather than the pid is what makes the second half total.
    """
    if child.process.poll() is None:
        _signal_group(child, signal.SIGTERM)
        try:
            child.process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            _signal_group(child, signal.SIGKILL)
    return child.process.wait()


def _signal_group(child: Child, signum: int) -> None:
    """Signal the child's whole process group; a child that just exited is not an error.

    The pid IS the process group id because `_spawned` starts a new session, so this needs no
    `getpgid` lookup — which would itself race the child's exit.
    """
    try:
        os.killpg(child.pid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def _await(child: Child, probe: Callable[[], bool], what: str, timeout: float) -> None:
    """Wait for `probe()` while the child is still alive and the deadline has not passed.

    The order inside the loop is the whole point of the function. `poll()` is asked FIRST, so a
    child that died at startup is reported as dead — with its status and its own last lines —
    rather than waited out and reported as a timeout. The probe is asked before the deadline is
    checked so a probe that comes good on the last tick still counts.

    Takes the TIMEOUT and derives the deadline, rather than taking a deadline, so the number in
    the timeout message is necessarily the one this wait was actually given. Measured on the
    draft that took a deadline: it interpolated `READY_TIMEOUT_SECONDS` into the message, so
    `--ready-timeout 0.05` failed after 0.05s reporting "within 60s" — a harness lying about the
    one number the operator had just typed.
    """
    deadline = time.monotonic() + timeout
    while True:
        status = child.process.poll()
        if status is not None:
            raise LaunchError(
                f"the {child.name} exited with status {status} before {what}. Its last lines:\n"
                f"{child.tail()}\n(full log: {child.log_path})"
            )
        if probe():
            return
        if time.monotonic() >= deadline:
            raise LaunchError(
                f"the {child.name} is still running but never {what} within {timeout:g}s. "
                f"Its last lines:\n{child.tail()}\n(full log: {child.log_path})"
            )
        time.sleep(PROBE_INTERVAL_SECONDS)


def health_ok(base_url: str) -> bool:
    """One `GET /health`: 200 AND the body the route promises, or not ready.

    The body is checked, not just the status, because the status alone would accept any 200 the
    app might grow at that path; `{"status": "ok"}` is what `health.py` states it returns once
    Postgres has answered.
    """
    try:
        with urllib.request.urlopen(  # noqa: S310 - loopback, and the URL is built here
            f"{base_url}/health", timeout=PROBE_HTTP_TIMEOUT_SECONDS
        ) as response:
            if response.status != 200:
                return False
            return json.loads(response.read().decode("utf-8")).get("status") == "ok"
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False


@contextmanager
def launched(
    *,
    worker_args: Sequence[str] = (),
    ready_timeout: float = READY_TIMEOUT_SECONDS,
    log_dir: Path | None = None,
) -> Iterator[Stack]:
    """Both children, both proven up, both guaranteed gone afterwards.

    The two launches are composed on an `ExitStack` so that a failure in the SECOND one tears down
    the first. That is the orphan this shape exists to prevent: a server that came up fine, a
    worker that died on a bad flag, and a uvicorn left holding a port after the harness has already
    reported failure.

    Logs live in a temp directory that is removed on success and KEPT on failure, with the path in
    the message. Removing them always would delete the evidence at the moment it is wanted; keeping
    them always would leave a directory per run on the operator's disk.
    """
    own_logs = log_dir is None
    logs = Path(tempfile.mkdtemp(prefix="gct-http-smoke-")) if own_logs else log_dir
    logs.mkdir(parents=True, exist_ok=True)
    finished = False
    try:
        with ExitStack() as stack:
            listener = stack.enter_context(_reserve_listener())
            port = listener.getsockname()[1]
            base_url = f"http://{LOOPBACK}:{port}"
            fd = listener.fileno()
            server = stack.enter_context(_spawned("server", server_argv(fd), logs, pass_fds=(fd,)))
            # The parent's copy goes now, and only now — after the fork that duplicated it, so
            # the child still has one. While the parent holds a copy the socket outlives the
            # server: a connect is accepted into the kernel's backlog with nothing behind it, so
            # "is anything serving that port" answers yes for a process that is gone. The
            # readiness probe is immune to that by construction (it is a REQUEST, not a
            # connect, and an unserved connection just times out), but `_accepts` is not, and
            # `_accepts` is how both this file and PR 3's ceremony ask whether teardown was
            # real. Deleting this line leaves the ExitStack to close the socket eventually and
            # the answer wrong for the whole window in between.
            listener.close()
            _await(
                server,
                lambda: health_ok(base_url),
                'answered GET /health with 200 {"status": "ok"}',
                ready_timeout,
            )
            worker = stack.enter_context(_spawned("worker", worker_argv(worker_args), logs))
            _await(
                worker,
                lambda: worker.log_contains(WORKER_STARTED_TOKEN),
                f"logged {WORKER_STARTED_TOKEN!r} (its own start-of-loop line)",
                ready_timeout,
            )
            yield Stack(base_url=base_url, server=server, worker=worker)
            finished = True
    finally:
        if own_logs:
            if finished:
                shutil.rmtree(logs, ignore_errors=True)
            else:
                print(f"child logs kept for inspection: {logs}", file=sys.stderr)


# ---------------------------------------------------------------------------------------------
# The ceremony — one class, one file, one cited answer, one refusal, one terminal failure
# ---------------------------------------------------------------------------------------------

# WHAT THE GATE UPLOADS WHEN `--corpus` IS NOT GIVEN, and why it is the generated corpus rather
# than the dogfood one. `ask_smoke.py` (the Slice 1 gate) is anchored to `data/dogfood/` by file
# and page and is therefore local-only; the Slice 2 gate uses `scripts/ci_corpus.py` because the
# WRITE path does not care what the documents say. This gate is neither: it has to GROUND on what
# it uploaded, so Slice 2's precedent does not carry on its own — it had to be measured. It was,
# twice over: five rounds against two independent ingests of this corpus gave GROUNDED 20/20 on
# four candidate in-corpus questions and REFUSAL 10/10 on two out-of-corpus ones, and the two
# questions kept below then went GROUNDED and REFUSAL on three consecutive runs of this whole
# gate over HTTP, citing the same page each time. No flip in either direction, and a gate that
# flips is worse than no gate (both runs are in this PR's ship record). So the generated corpus
# is the default — which also makes this the only paid gate that COULD run in CI and does not; two
# already do, and `live-gates.yml` is the writer of which. Wiring this one in is not this issue's
# to decide; what is decided here is that nothing in the SCRIPT prevents it.
#
# One PDF, four pages, no deck. `ci_corpus.page_text` walks its paragraph list two per page, so
# four pages carry all eight paragraphs exactly once: the smallest corpus that is self-contained
# rather than repetitive, which is what makes one citation attributable to one page.
GENERATED_CORPUS_ARGV = ("--pdfs", "1", "--pptx", "0", "--pages", "4")

# The two questions, anchored to the corpus above and to nothing else — which is exactly why
# `--corpus` cannot be given without `--question`
# (`_refuse_a_corpus_with_no_question_anchored_to_it`). The in-corpus one names a fact that
# appears in one paragraph and nowhere else ("about nine days" in the atmosphere), so an answer
# that grounds has to have retrieved that page. The out-of-corpus one is a real question about a
# subject the corpus does not mention: `retrieve` has NO relevance floor in V1, so it still
# returns k chunks and the REFUSAL is the grounder's decision about what those chunks support
# (ADR 0016) — which is the half of the product this gate exists to prove.
DEFAULT_QUESTION = (
    "What is residence time in the water cycle, and how long is it in the atmosphere?"
)
DEFAULT_OUT_OF_CORPUS_QUESTION = "What were the main causes of the French Revolution?"

# The states that count as "a cited answer" for the in-corpus question. GROUNDED is what was
# measured, 20 times out of 20 — PARTIAL is accepted anyway because it is a CITED answer that
# named a gap (ADR 0014), and a gate that reddened on it would be asserting a determinism the
# product does not promise and does not need. INTEGRITY_FLAGGED is deliberately NOT here: the
# answer is shown but not presented as verified (ADR 0015), which is not a passing exit.
CITED_STATES = ("GROUNDED", "PARTIAL")
REFUSAL_STATE = "REFUSAL"

# The file whose failure the gate reads back through `GET /files/{id}`. A PDF header with nothing
# a reader can follow past it: `parse_file` dispatches on the SUFFIX, so `.pdf` is what sends this
# down the PDF path at all, and pypdf then fails on the structure — which is what makes the reason
# `unparseable` rather than `unsupported` (a suffix refused before any parsing) or `empty` (a file
# that opened and held no text). Generated here rather than committed for the reason
# `scripts/ingest_smoke.py:write_corrupt_pdf` gives about its own copy: a corrupt binary in the
# tree is a fixture no reviewer can read. That the bytes really do produce `unparseable` is not
# assumed from that precedent — it is what this gate asserts, and it is pinned offline too
# (`test_the_unparseable_fixture_is_unparseable_to_the_real_parser`).
UNPARSEABLE_PDF_BYTES = b"%PDF-1.7\n" + b"a header, and past it nothing a reader can follow\n" * 8
UNPARSEABLE_FILENAME = "unreadable-lecture.pdf"
UNPARSEABLE_REASON = "unparseable"

# How long the gate waits for one upload to leave `queued`/`processing`. Measured warm on this
# machine, over three consecutive real runs of this gate: 2.6/2.7/2.8s from `POST /files` to
# `status=ready` for the four-page PDF (an inline `ingest_file` of the same file is 2.3s of that,
# and the worker's poll tick is most of the rest), ~0.1-2.6s to `status=failed` for the unparseable
# one — a `worker.DEFAULT_POLL_SECONDS` tick, a poll, two round trips. The default is ~43x the READY
# maximum, not this one; headroom for a cold cache, a loaded machine, and a bigger corpus, and
# a file that is genuinely stuck is reported with the last status it held rather than waited out.
INGEST_TIMEOUT_SECONDS = 120.0
# Gap between `GET /files/{id}` polls. The status surface is a single indexed row read, so this
# costs nothing but a round trip; it is not a paid call and there is no retry anywhere near it.
STATUS_POLL_SECONDS = 0.5
# Per-request timeout for the ceremony's own calls, distinct from the readiness probe's: `POST
# /ask` is a real generation call and is the one request here that can take seconds. A whole run,
# launch to teardown, was 9.2-9.5s over the three runs above, so this is a bound on a request that
# has HUNG rather than a budget anything is expected to approach.
CEREMONY_HTTP_TIMEOUT_SECONDS = 120.0

# The statuses `files.status` can hold, split by whether the worker is still going to touch the
# row. Terminal is where a poll STOPS — reaching `failed` when `ready` was wanted is reported at
# once with the reason, rather than waited out to the deadline and reported as a stall. Same
# ordering `_await` uses for a dead child, and for the same reason: a harness that tells you what
# broke beats one that tells you it waited. The values themselves are the database's (`files`
# CHECK, `migrations/0001_init.sql`); the API passes them through untouched.
TERMINAL_STATUSES = ("ready", "failed")


class GateError(RuntimeError):
    """A ceremony step could not continue — the loop stopped short of a verdict.

    Distinct from a fault. A FAULT is the gate's answer: the loop ran and the product did the
    wrong thing, and every fault found is reported together. A GateError is the loop failing to
    reach the next step at all — a class that could not be created, an upload the API refused —
    where continuing would only produce cascading noise about ids that were never handed out.
    Both exit 1: neither is a statement about this machine, which is what `SetupError` is for.
    """


@dataclass(frozen=True)
class Reply:
    """One HTTP response: the status and the decoded body, never an exception.

    A 4xx or 5xx is a FINDING for this script rather than a crash. `urllib` raises `HTTPError`
    for both, and letting that propagate would replace "the upload was refused, and here is the
    sentence the API sent" with a traceback out of the standard library — the wrong half of the
    story, in the one situation the reader needs the other half.
    """

    status: int
    body: object

    def describe(self) -> str:
        """The one line a fault message carries about this response.

        Renders the shared error envelope (`{"error": {kind, message, detail}}`) as its kind and
        message, because that pair is what the API wrote to be read by a human with a problem;
        anything else is JSON, clipped, because a fault message that dumps a whole answer body
        buries itself.
        """
        if isinstance(self.body, dict) and isinstance(self.body.get("error"), dict):
            error = self.body["error"]
            return f"{self.status} {error.get('kind')}: {error.get('message')}"
        rendered = json.dumps(self.body, default=str)
        clipped = rendered if len(rendered) <= 300 else rendered[:300] + "…"
        return f"{self.status} {clipped}"

    def field(self, name: str) -> object:
        """One field out of a JSON object body, or a `GateError` naming what came back instead.

        The alternative — `self.body["x"]` at every call site — raises `TypeError: string indices
        must be integers` when a proxy or a stack trace page arrives where JSON was expected,
        which says nothing about which request it was.
        """
        if not isinstance(self.body, dict):
            raise GateError(f"expected a JSON object, got {self.describe()}")
        if name not in self.body:
            raise GateError(f"no {name!r} in the response body: {self.describe()}")
        return self.body[name]


def _call(
    method: str,
    url: str,
    *,
    payload: bytes | None = None,
    content_type: str | None = None,
    timeout: float = CEREMONY_HTTP_TIMEOUT_SECONDS,
) -> Reply:
    """One request to the launched server, over the loopback socket `launched()` handed out.

    `urllib` rather than `httpx`: this file already depends on it for the readiness probe, and
    `httpx2` is in `[project.optional-dependencies].dev` — a gate that a bare `uv sync` cannot
    run is a gate that fails for a reason that is not the product's.
    """
    request = urllib.request.Request(url, data=payload, method=method)  # noqa: S310 - loopback
    if content_type is not None:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return Reply(response.status, _decode(response.read()))
    except urllib.error.HTTPError as err:
        return Reply(err.code, _decode(err.read()))
    except (urllib.error.URLError, OSError) as err:
        raise GateError(f"{method} {url} never completed: {err}") from err


def _decode(raw: bytes) -> object:
    """The body as JSON, or as text when it is not JSON — so a fault message can quote either."""
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def multipart(
    fields: dict[str, str], *, part: str, filename: str, content: bytes
) -> tuple[str, bytes]:
    """Encode a file plus some plain fields as `multipart/form-data`: (content-type, body).

    Hand-rolled because the standard library has no encoder for it: `email.mime` builds MIME
    DOCUMENTS — it folds long headers and may apply a transfer encoding — and starlette's parser
    expects neither, so a body from that path is a body the real route reads differently from the
    browser it was written for.

    The boundary is random per call rather than a constant. A boundary that appears anywhere in
    the payload ends the part early, and this function is handed arbitrary file bytes; 16 random
    bytes make that a non-event instead of a rare, silent, content-dependent corruption.

    A filename containing a quote or a newline is REFUSED rather than escaped or stripped.
    `gct.staging.validate_filename` forbids `/`, `\\` and NUL and permits both of these, so such
    a name really can arrive here through `--corpus`; and the header this builds is the one place
    in the loop where those characters stop being data. Refusing at the boundary is what this
    codebase does — converting would upload the file under a name the student never chose, and
    that name is the citation label every answer shows.
    """
    for bad, what in (('"', "a quote"), ("\r", "a carriage return"), ("\n", "a newline")):
        if bad in filename:
            raise GateError(
                f"the filename {filename!r} contains {what}, which cannot be put in a "
                "Content-Disposition header without changing what the request means. Rename the "
                "file and run again."
            )
    boundary = f"----gct-http-smoke-{secrets.token_hex(16)}"
    lines: list[bytes] = []
    for key, value in fields.items():
        lines += [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{key}"'.encode(),
            b"",
            value.encode(),
        ]
    lines += [
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="{part}"; filename="{filename}"'.encode(),
        # Generic on purpose: `upload_file` judges no content type — a `.txt`, a corrupt PDF and
        # a zero-byte file all stage and enqueue and fail terminally at parse, which keeps
        # `parse_file` the one writer of "is this file usable". Claiming `application/pdf` here
        # would be this script asserting something it has not checked.
        b"Content-Type: application/octet-stream",
        b"",
        content,
        f"--{boundary}--".encode(),
        b"",
    ]
    return f"multipart/form-data; boundary={boundary}", b"\r\n".join(lines)


def create_class(base_url: str, name: str) -> str:
    """`POST /classes` — returns the id the rest of the loop is scoped by.

    A FRESH class per run, never a reused one. Every later assertion is about what THIS run
    uploaded: a class carrying an earlier run's files would still refuse the out-of-corpus
    question and would still cite a file with the right name, so the gate would keep passing over
    a broken upload path. It also means a run leaves rows behind, which is the other half of why
    `preflight` wants a scratch database.
    """
    reply = _call(
        "POST",
        f"{base_url}/classes",
        payload=json.dumps({"name": name}).encode(),
        content_type="application/json",
    )
    if reply.status != 201:
        raise GateError(f"POST /classes did not create the class: {reply.describe()}")
    return str(reply.field("class_id"))


@dataclass(frozen=True)
class Uploaded:
    """What `POST /files` handed back: the id to poll, and the name the file was accepted under.

    The NAME is carried alongside the id because it is the one a citation will show. `stage`
    stores a name it neither trims nor rewrites today, so it currently equals the local file's —
    but the gate compares citations against THIS value rather than the path it read, so the
    comparison stays right if that ever stops being true, instead of reddening a working product.
    """

    file_id: str
    filename: str


def upload(base_url: str, *, class_id: str, filename: str, content: bytes) -> Uploaded:
    """`POST /files` — stage and enqueue; returns the id `GET /files/{id}` takes.

    202 rather than 201 is the API's own contract and is asserted rather than tolerated: it is
    the statement that nothing has been ingested yet, which is the premise of every poll below.
    """
    content_type, body = multipart(
        {"class_id": class_id}, part="file", filename=filename, content=content
    )
    reply = _call("POST", f"{base_url}/files", payload=body, content_type=content_type)
    if reply.status != 202:
        raise GateError(f"POST /files did not accept {filename}: {reply.describe()}")
    return Uploaded(file_id=str(reply.field("file_id")), filename=str(reply.field("filename")))


@dataclass(frozen=True)
class Settled:
    """What one file's polling observed: where it ended, how it got there, and how long it took."""

    status: str
    failed_reason: str | None
    message: str
    path: tuple[str, ...]
    seconds: float

    def render(self) -> str:
        """`queued -> processing -> ready in 3.0s`, with the reason when there is one."""
        end = self.status if self.failed_reason is None else f"{self.status}({self.failed_reason})"
        return f"{' -> '.join(self.path[:-1] + (end,))} in {self.seconds:.1f}s"


def await_settled(
    base_url: str, file_id: str, *, timeout: float, interval: float = STATUS_POLL_SECONDS
) -> Settled:
    """Poll `GET /files/{id}` until the file is terminal, recording every status it passed through.

    STOPS AT EITHER TERMINAL STATUS, and leaves the verdict to the caller. A file that lands
    `failed` when the caller wanted `ready` is a fact available immediately, and waiting it out to
    the deadline would turn a reason the student can act on into "it never became ready" two
    minutes later. Same ordering `_await` applies to a dead child.

    The status PATH is kept, not just the final value, because `queued -> processing -> ready` is
    the state machine Slice 2 shipped (`files.status` is the domain truth, distinct from
    `jobs.state`) and a run that jumped straight to `ready` would mean the poll was too slow to
    see the middle — worth showing rather than hiding, in a gate whose output is read when it
    fails. It is printed, not asserted: at `--ingest-timeout`-scale intervals a fast ingest can
    legitimately be observed only once, and a gate that reddened on that would be timing the
    poll, not the product.
    """
    started = time.monotonic()
    path: list[str] = []
    while True:
        reply = _call("GET", f"{base_url}/files/{file_id}")
        if reply.status != 200:
            raise GateError(
                f"GET /files/{file_id} did not answer with a status: {reply.describe()}"
            )
        status = str(reply.field("status"))
        if not path or path[-1] != status:
            path.append(status)
        if status in TERMINAL_STATUSES:
            failed_reason = (
                reply.body.get("failed_reason") if isinstance(reply.body, dict) else None
            )
            return Settled(
                status=status,
                failed_reason=None if failed_reason is None else str(failed_reason),
                message=str(reply.body.get("message", "")) if isinstance(reply.body, dict) else "",
                path=tuple(path),
                seconds=time.monotonic() - started,
            )
        if time.monotonic() - started >= timeout:
            raise GateError(
                f"the file is still {status!r} after {timeout:g}s (it went {' -> '.join(path)}). "
                "Nothing moved it to `ready` or `failed`: the worker child is up but may not have "
                "claimed it — its log is in the kept child-log directory named above, and "
                "`jobs.attempts` for this file says whether it was tried. Raise --ingest-timeout "
                "if the machine is merely slow."
            )
        time.sleep(interval)


def ask(base_url: str, *, class_id: str, question: str) -> Reply:
    """`POST /ask` — one question, scoped to one class. The reply is returned, not judged.

    Judged by the caller because BOTH of this gate's asks are successful requests: a refusal is a
    200 with `state=REFUSAL` (ADR 0016), and rendering it as an error here would be this script
    disagreeing with the product about what its own answer means.
    """
    return _call(
        "POST",
        f"{base_url}/ask",
        payload=json.dumps({"class_id": class_id, "question": question}).encode(),
        content_type="application/json",
    )


def _answer_line(reply: Reply) -> str:
    """One printed line about an answer: its status, its state, and what it cited."""
    if reply.status != 200 or not isinstance(reply.body, dict):
        return reply.describe()
    citations = reply.body.get("citations") or []
    rendered = ", ".join(
        f"[{c.get('label')}] {c.get('file')} p{c.get('page_or_slide')}"
        for c in citations
        if isinstance(c, dict)
    )
    return f"{reply.status} {reply.body.get('state')}  {len(citations)} citation(s)" + (
        f": {rendered}" if rendered else ""
    )


def cited_answer_faults(reply: Reply, *, filename: str) -> list[str]:
    """Everything wrong with the answer to the IN-CORPUS question, or an empty list.

    Every fault, not the first one: a reader of a failing run wants to know whether the model
    refused a question the corpus covers, or answered it and cited nothing, or cited a file
    nobody uploaded. Those are three different defects and stopping at the first hides the rest.

    `filename` is the name the UPLOAD echoed back, never the name on disk. `POST /files` returns
    the name as stored and that is the name that becomes the citation label, so comparing against
    the local path would make this assertion wrong the day staging starts disambiguating a
    collision — and wrong in the direction that reddens a working product.
    """
    if reply.status != 200:
        return [
            f"POST /ask (in-corpus) answered {reply.describe()}, and a question this corpus "
            "covers has an answer"
        ]
    if not isinstance(reply.body, dict):
        return [f"POST /ask (in-corpus) did not answer with a JSON object: {reply.describe()}"]
    faults: list[str] = []
    state = reply.body.get("state")
    if state not in CITED_STATES:
        faults.append(
            f"the in-corpus question came back {state!r}; a cited answer is "
            f"{' or '.join(CITED_STATES)}, "
            "and this question names a fact that is on a page of the file just uploaded"
        )
    citations = reply.body.get("citations") or []
    if not citations:
        faults.append(
            "the in-corpus answer cited nothing — an uncited answer is the failure this "
            "product exists to not have"
        )
    for citation in citations:
        if not isinstance(citation, dict):
            faults.append(f"a citation was not an object: {citation!r}")
            continue
        if citation.get("file") != filename:
            faults.append(
                f"a citation names {citation.get('file')!r}, which is not the file this run "
                f"uploaded ({filename!r})"
            )
        page = citation.get("page_or_slide")
        if not isinstance(page, int) or page < 1:
            faults.append(
                f"a citation's page_or_slide is {page!r}, which points at no page of anything"
            )
    if not str(reply.body.get("answer_prose") or "").strip():
        faults.append(
            f"the in-corpus answer came back {state!r} with no prose, so there is nothing to "
            "show the student"
        )
    return faults


def refusal_faults(reply: Reply) -> list[str]:
    """Everything wrong with the answer to the OUT-OF-CORPUS question, or an empty list.

    The status is checked first and separately because it is the assertion this whole phase
    exists for: a refusal is a SUCCESSFUL outcome and leaves as 200 with `state=REFUSAL` (ADR
    0016). A 4xx here would mean the API had started calling "your materials do not cover this"
    a client error, which is a lie about the student's corpus — and it is exactly the shape a
    future edit would reach for, so the gate says it out loud.
    """
    faults: list[str] = []
    if reply.status != 200:
        faults.append(
            f"POST /ask (out-of-corpus) answered {reply.describe()}; a refusal is a successful "
            "outcome and must be a 200 carrying the refusal state (ADR 0016)"
        )
    if not isinstance(reply.body, dict):
        return [
            *faults,
            f"POST /ask (out-of-corpus) did not answer with a JSON object: {reply.describe()}",
        ]
    state = reply.body.get("state")
    if state != REFUSAL_STATE:
        faults.append(
            f"the out-of-corpus question came back {state!r}, not {REFUSAL_STATE!r} — an answer "
            "to a question these materials do not cover is the fabrication this product refuses"
        )
    citations = reply.body.get("citations") or []
    if citations:
        faults.append(
            f"the out-of-corpus answer carried {len(citations)} citation(s), which cite the "
            "corpus for something it does not say"
        )
    return faults


def terminal_failure_faults(settled: Settled) -> list[str]:
    """Everything wrong with how the unreadable file's failure reached the status surface.

    The Exit criterion is that the surface exposes ACTIONABLE terminal reasons, and this checks
    the two halves a client actually consumes: the machine-readable `failed_reason` a caller
    switches on, and a non-empty `message` to put in front of a student. It does NOT check the
    sentence's wording. That map is `src/gct/api/routers/files.py:_FAILED_MESSAGE`, and a test
    there already reads both member sets off `pg_constraint` and fails the moment a migration
    widens either without a sentence — so a keyword match here would be a second writer for a
    fact that is already guarded, and the copy is the one that would drift.
    """
    faults: list[str] = []
    if settled.status != "failed":
        faults.append(
            f"the unreadable file settled {settled.render()}, and a file no parser can read "
            "must fail"
        )
    if settled.failed_reason != UNPARSEABLE_REASON:
        faults.append(
            f"the unreadable file failed with reason {settled.failed_reason!r}, not "
            f"{UNPARSEABLE_REASON!r} — the reason is what the student is told to act on"
        )
    if not settled.message.strip():
        faults.append(
            f"the status surface carried reason {settled.failed_reason!r} with no message, "
            "which is not actionable"
        )
    return faults


def run_gate(
    base_url: str,
    *,
    corpus: Path,
    question: str,
    out_of_corpus_question: str,
    ingest_timeout: float = INGEST_TIMEOUT_SECONDS,
) -> list[str]:
    """Drive the whole Slice 3 loop over HTTP and return every fault found; [] is a pass.

    THE ORDER IS A DECISION. The unreadable file is uploaded LAST, after both asks, on one
    single-lane worker. Uploading it first or alongside would interleave two files' status lines
    in the output a reader only ever reads when something failed, and would save only the
    terminal path's own duration — which never reaches a model. That duration is measured where
    `INGEST_TIMEOUT_SECONDS` is defined and is not restated here: one measurement, one writer.
    Both files go to the SAME class, because that is what a student's class looks like and
    because it puts one more thing under test: a failed file must not change what the ready one
    answers.

    A file that never reaches `ready` skips the asks rather than failing them. `answer()` renders
    an empty retrieval as the canned refusal with no generation call (ADR 0016), so asking anyway
    would report two more faults that are both restatements of the upload that did not land.
    """
    faults: list[str] = []
    name = f"slice-3 exit smoke {time.strftime('%Y-%m-%d %H:%M:%S')}"
    class_id = create_class(base_url, name)
    print(f"class   {class_id}  {name!r}")

    content = corpus.read_bytes()
    uploaded = upload(base_url, class_id=class_id, filename=corpus.name, content=content)
    print(f"upload  {uploaded.file_id}  {uploaded.filename} ({len(content)} bytes)")
    settled = await_settled(base_url, uploaded.file_id, timeout=ingest_timeout)
    print(f"status  {settled.render()}")

    if settled.status == "ready":
        answer = ask(base_url, class_id=class_id, question=question)
        print(f"ask     {_answer_line(answer)}\n        in-corpus: {question!r}")
        faults += cited_answer_faults(answer, filename=uploaded.filename)

        refusal = ask(base_url, class_id=class_id, question=out_of_corpus_question)
        print(f"ask     {_answer_line(refusal)}\n        out-of-corpus: {out_of_corpus_question!r}")
        faults += refusal_faults(refusal)
    else:
        faults.append(
            f"{uploaded.filename} settled {settled.render()} instead of `ready`: {settled.message}"
        )
        print(
            "ask     skipped — nothing was indexed, so both asks would only restate the fault above"
        )

    broken = upload(
        base_url, class_id=class_id, filename=UNPARSEABLE_FILENAME, content=UNPARSEABLE_PDF_BYTES
    )
    print(
        f"upload  {broken.file_id}  {broken.filename} "
        f"({len(UNPARSEABLE_PDF_BYTES)} bytes, unreadable on purpose)"
    )
    broken_settled = await_settled(base_url, broken.file_id, timeout=ingest_timeout)
    print(f"status  {broken_settled.render()}")
    print(f"        {broken_settled.message}")
    faults += terminal_failure_faults(broken_settled)
    return faults


@contextmanager
def generated_corpus() -> Iterator[Path]:
    """The default corpus: one four-page PDF, built by `scripts/ci_corpus.py` in a temp directory.

    Run as a SUBPROCESS rather than imported. `scripts/` is deliberately not a package (ADR
    0009), so importing it means a path-based load with a `sys.modules` dance for one function;
    running it costs one interpreter startup, once, before either child exists — and it is the
    only thing outside CI that would notice the Slice 2 gate's corpus generator breaking.
    """
    with tempfile.TemporaryDirectory(prefix="gct-http-smoke-corpus-") as into:
        finished = subprocess.run(
            [
                sys.executable,
                str(repo_root() / "scripts" / "ci_corpus.py"),
                into,
                *GENERATED_CORPUS_ARGV,
            ],
            cwd=repo_root(),
            capture_output=True,
            check=False,
        )
        if finished.returncode != 0:
            raise SetupError(
                "could not generate the corpus this gate uploads. `scripts/ci_corpus.py` needs "
                "`reportlab`, which lives in the dev extra — run `uv sync --extra dev`, or point "
                "`--corpus` at a .pdf/.pptx of your own and give `--question` a question it "
                f"answers. It exited {finished.returncode}:\n"
                f"{finished.stderr.decode('utf-8', 'replace')[-1000:]}"
            )
        written = sorted(Path(into).glob("*.pdf"))
        if len(written) != 1:
            raise SetupError(
                f"`scripts/ci_corpus.py {' '.join(GENERATED_CORPUS_ARGV)}` wrote {len(written)} "
                "PDF(s) where this gate expects exactly one; the corpus arguments and that "
                "script have drifted apart"
            )
        yield written[0]


def _interrupt(signum: int, _frame: FrameType | None) -> None:
    """Route SIGTERM onto the stack Ctrl-C already unwinds, so the `with` blocks run.

    Same mechanism `scripts/worker.py` installs and for the reason recorded there (#82); what is
    specific here is the stake: this process is holding two children, and under Python's default
    disposition a `kill` of the harness runs no `finally` and leaves both of them running.
    """
    raise KeyboardInterrupt(f"signal {signum}")


def _build_parser() -> argparse.ArgumentParser:
    """The CLI surface, separated from parsing so a test can read what the flags actually SAY.

    Split out for the reason `scripts/worker.py:_build_parser` gives, and the advertised
    pass-through spelling is why it earns the split here specifically: help text is
    documentation that ships inside the program, and a user acts on it.

    EVERYTHING AFTER `--` GOES TO THE WORKER, rather than a repeatable `--worker-arg FLAG`. The
    repeatable form was written first and its own help string was untypeable: argparse reads
    `--worker-arg --poll` as two options and refuses with "expected one argument", so the only
    spelling that works is `--worker-arg=--poll --worker-arg=0.5` — one `=` per token, silently
    required. `--` is the idiom `uv` and `cargo` already teach, it passes a flag exactly as the
    worker's own CLI spells it, and it cannot be typed in a form that parses into something else.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=READY_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"how long either child may take to become observable (default "
        f"{READY_TIMEOUT_SECONDS:.0f}). A child that DIES is reported at once regardless",
    )
    parser.add_argument(
        "--ingest-timeout",
        type=float,
        default=INGEST_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"how long one upload may take to reach `ready` or `failed` (default "
        f"{INGEST_TIMEOUT_SECONDS:.0f}); a file that settles is never waited on",
    )
    parser.add_argument(
        "--corpus",
        metavar="PATH",
        help="a .pdf/.pptx to upload instead of the generated one. Requires --question: the "
        "questions this gate asks are anchored to the corpus it uploads and to nothing else",
    )
    parser.add_argument(
        "--question",
        metavar="TEXT",
        help="the in-corpus question, which must have a cited answer in the uploaded file "
        "(default: one anchored to the generated corpus)",
    )
    parser.add_argument(
        "--out-of-corpus-question",
        metavar="TEXT",
        help="the question the corpus must NOT be able to answer (default: one about a subject "
        "the generated corpus does not mention)",
    )
    parser.add_argument(
        "--dedicated-database",
        action="store_true",
        help="declare that nothing but this run writes to the database `DATABASE_URL` names, and "
        "skip the empty-`files` requirement. A FLAG rather than an env var on purpose: an "
        "exported variable defeats the guard on every later run silently, and this is a promise "
        "that has to be made again each time it is true. The queue check still runs",
    )
    parser.add_argument(
        "--launch-only",
        action="store_true",
        help="bring both children up, prove they are up, tear them down, and run NO ceremony. "
        "Free — nothing is uploaded and nothing is asked — and it is not the exit gate",
    )
    parser.add_argument(
        "worker_args",
        nargs="*",
        metavar="WORKER_ARG",
        help="everything after `--` is handed to scripts/worker.py unread "
        "(e.g. `... -- --poll 0.5 --lease 60`). A worker --log-level above INFO is the one "
        "thing refused here: it silences the line readiness waits for",
    )
    return parser


def _refuse_a_worker_level_the_probe_cannot_see(
    parser: argparse.ArgumentParser, worker_args: Sequence[str]
) -> None:
    """Refuse a worker `--log-level` above INFO, at the boundary, naming the remedy.

    The one worker flag this harness cannot stay out of, and the reason is a coupling this file
    already has: readiness is `log_contains(WORKER_STARTED_TOKEN)`, and `gct.jobs.worker.run`
    logs that line at INFO. Run the child at WARNING and the probe becomes unsatisfiable, so a
    healthy worker is waited out for the whole `--ready-timeout` and reported as a hang —
    measured at `--ready-timeout 8`: "the worker is still running but never logged 'worker
    started' … (the child wrote nothing)", which names neither the cause nor the fix, and at the
    default takes sixty seconds to say it.

    Refusing rather than rewriting the flag: the harness has no business overriding what an
    operator typed, and it is the same strict-refusal boundary `scripts/worker.py:_validate`
    draws for a range argparse accepts (#109 PR 1) — usage to stderr, exit 2, in the time it
    takes to parse.

    A level argparse would not accept at all (`--log-level LOUD`) is deliberately NOT refused
    here: the worker's own CLI owns that message, and answering it twice would put a second
    writer on the list of valid levels.
    """
    level = _worker_log_level(worker_args)
    if level is None:
        return
    numeric = logging.getLevelName(level.upper())
    if isinstance(numeric, int) and numeric > logging.INFO:
        parser.error(
            f"a worker --log-level of {level} silences the line this harness waits for: "
            f"`gct.jobs.worker.run` logs {WORKER_STARTED_TOKEN!r} at INFO, so anything above "
            "INFO makes readiness unsatisfiable and the launch fails after --ready-timeout with "
            "an empty log to show for it. Pass DEBUG or INFO, or leave the flag off."
        )


def _worker_log_level(worker_args: Sequence[str]) -> str | None:
    """The level the worker would end up running at, or None if the pass-through never sets one.

    Reads the tokens the way the worker's own parser will. Two behaviours are copied on purpose
    and neither is decorative: argparse accepts any UNAMBIGUOUS PREFIX of a long option, so
    `--lo WARNING` reaches `--log-level` on that CLI (`--l` is ambiguous with `--lease` and is
    refused there), and a repeated option takes its LAST value. A scan that matched only the full
    spelling, or only the first occurrence, would wave through exactly the spellings that still
    silence the probe.
    """
    found: str | None = None
    for index, token in enumerate(worker_args):
        name, sets_inline, inline = token.partition("=")
        if not (name.startswith("--") and len(name) >= 4 and "--log-level".startswith(name)):
            continue
        if sets_inline:
            found = inline
        elif index + 1 < len(worker_args):
            found = worker_args[index + 1]
    return found


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    """The whole CLI boundary — the parser AND every refusal — behind one name.

    One writer, because the usage examples in this module's docstring are checked by running
    them through THIS function: a test that only called `parse_args` would ratify an example
    that parses cleanly and then fails at launch, which is precisely the defect that put
    `-- --poll 0.5 --log-level WARNING` in the docstring in the first place.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    _refuse_a_worker_level_the_probe_cannot_see(parser, args.worker_args)
    _refuse_a_corpus_with_no_question_anchored_to_it(parser, args)
    args.question = args.question or DEFAULT_QUESTION
    args.out_of_corpus_question = args.out_of_corpus_question or DEFAULT_OUT_OF_CORPUS_QUESTION
    return args


def _refuse_a_corpus_with_no_question_anchored_to_it(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """Refuse `--corpus` without `--question`, at the boundary, naming the remedy.

    The defaults are not a corpus-independent fallback, and treating them as one is the mistake
    this refusal exists to make impossible. `DEFAULT_QUESTION` asks about a fact that appears on
    one page of the generated hydrology PDF; point `--corpus` at anything else and the gate asks
    a question that corpus cannot answer, gets the REFUSAL the product is right to give, and
    reports the whole loop as broken. That failure names the answer rather than the cause, and it
    would arrive after an upload, an ingest and two paid calls — so it is refused in the
    milliseconds it takes to parse, the way a worker `--log-level` above INFO is.

    Only the IN-CORPUS question is required. The out-of-corpus one keeps its default because
    "what caused the French Revolution" is out of corpus for any plausible corpus a reader points
    this at, and a wrong guess there fails in the safe direction: the corpus answers it, the
    refusal does not happen, and the gate goes red rather than green.
    """
    if args.corpus is not None and args.question is None:
        parser.error(
            "--corpus needs --question. The default question asks about a fact in the generated "
            "corpus, so against your own materials it would be answered with the honest refusal "
            "this gate reads as a failure. Pass a question your corpus has a cited answer for."
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Launch both children, drive the whole loop over HTTP, tear them down, and report.

    THE SLICE 3 EXIT GATE, by default and without a flag: the ceremony is what a bare run does,
    and `--launch-only` is what buys the launch alone. That direction is deliberate — a gate you
    have to remember to switch on is one a tired operator reports green without having run.

    ONE `main`, NOT A PHASE SELECTOR. The two phases are not independently meaningful: the asks
    need a file the upload put there, and both need the pair `launched()` brings up. What
    `--launch-only` selects is not a phase but a COST — it uploads nothing and asks nothing, so
    it needs no key that spends and no corpus, which is what makes it the thing to run when the
    question is whether this machine can host the gate at all. Its PASS line says so in words,
    because a `PASS` a reader mistakes for the gate's is worse than no output.

    WHAT IT COSTS. `--launch-only` is free, and that is measured rather than assumed: both
    children construct real OpenAI clients at startup, but construction is not a call, and an
    endpoint that records every request it is sent saw none
    (`test_the_real_pair_comes_up_and_never_reaches_a_paid_endpoint`). A full run is NOT free —
    it embeds one file and generates two answers, on the real models — which is the same money
    `scripts/ask_smoke.py` and `scripts/ingest_smoke.py` spend and the reason all three are gates
    rather than tests. Nothing on this path retries a paid call and no paid call sits inside a
    loop: the two asks are issued once each and their verdicts are read, never re-requested.

    WHAT IT MAY ALSO COST, AND WHAT `preflight` NOW CLOSES. The worker this launches is the real
    one and claims whatever `jobs` holds, so somebody else's file can be ingested — and billed
    for — by a run that uploaded nothing. `preflight` refuses a database whose `files` table is
    not empty, which is not a narrower version of the old queue check but a different question:
    an empty `files` means an empty `jobs` (foreign key, no cascade), so nothing that exists can
    be reaped, released or retried into this worker's reach for the length of the gate. What no
    check closes is a row that ARRIVES mid-run — a second API process, another `enqueue` — and
    `--dedicated-database` swaps the evidence for the operator's word that there is no such
    writer. So a run costing only its own work is still a property of the database being nobody
    else's; the difference is that the harness now refuses rather than assumes it (#138).

    `argv` is taken rather than read from `sys.argv`, for the reason `scripts/worker.py:main`
    records: this module is loaded by path inside a pytest process.
    """
    args = _parse(argv)
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        preflight(dedicated_database=args.dedicated_database)
    except SetupError as err:
        print(f"SETUP — {err}", file=sys.stderr)
        return EXIT_SETUP

    faults: list[str] = []
    try:
        with ExitStack() as resources:
            corpus = None if args.launch_only else _corpus(args, resources)
            with launched(worker_args=args.worker_args, ready_timeout=args.ready_timeout) as stack:
                print(f"server  pid {stack.server.pid}  ready at {stack.base_url}/health")
                print(f"worker  pid {stack.worker.pid}  past startup ({WORKER_STARTED_TOKEN})")
                if corpus is not None:
                    faults = run_gate(
                        stack.base_url,
                        corpus=corpus,
                        question=args.question,
                        out_of_corpus_question=args.out_of_corpus_question,
                        ingest_timeout=args.ingest_timeout,
                    )
                port = int(stack.base_url.rsplit(":", 1)[1])
    except SetupError as err:
        print(f"SETUP — {err}", file=sys.stderr)
        return EXIT_SETUP
    except (LaunchError, GateError) as err:
        print(f"FAIL — {err}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\nstopped — both children were torn down on the way out", file=sys.stderr)
        return EXIT_FAILED

    faults += [
        f"{child.name} exited with status {child.process.returncode}, which is not a clean stop"
        for child in (stack.server, stack.worker)
        if child.process.returncode not in CLEAN_EXIT_STATUSES
    ]
    if _accepts(port):
        faults.append(f"port {port} is still accepting connections after teardown")
    if faults:
        for fault in faults:
            print(f"FAIL — {fault}", file=sys.stderr)
        return EXIT_FAILED
    reaped = (
        f"both children reaped (server {stack.server.process.returncode}, "
        f"worker {stack.worker.process.returncode}) and port {port} is closed"
    )
    if args.launch_only:
        print(f"PASS — launch only: {reaped}. NO ceremony ran; this is not the Slice 3 exit gate.")
    else:
        print(
            "PASS — the whole loop is drivable over HTTP: a cited answer for an in-corpus "
            "question, an honest refusal for one the corpus does not cover, and an actionable "
            f"terminal reason for a file no parser can read. And {reaped}."
        )
    return EXIT_OK


def _corpus(args: argparse.Namespace, resources: ExitStack) -> Path:
    """The file the gate uploads: the caller's `--corpus`, or one generated for the run.

    The caller's path is checked HERE rather than at parse time, because argparse cannot see the
    difference between a typo and a file that exists: this refusal is a `SetupError` — a fact
    about the machine, exit 2 — and it happens before either child is launched, so a mistyped
    path costs a message rather than a launch.
    """
    if args.corpus is None:
        return resources.enter_context(generated_corpus())
    corpus = Path(args.corpus)
    if not corpus.is_file():
        raise SetupError(
            f"--corpus {corpus} is not a file. Point it at one .pdf or .pptx — this gate uploads "
            "a single file — or leave it off to use the generated corpus."
        )
    return corpus


def _accepts(port: int) -> bool:
    """Whether anything is still listening — the half of teardown a `returncode` cannot show."""
    try:
        with socket.create_connection((LOOPBACK, port), timeout=PROBE_HTTP_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
