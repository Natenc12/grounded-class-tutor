"""Slice 3 exit smoke — the two processes, the socket between them, and the teardown (#109).

Brings up the pair the Slice 3 exit gate has to be driven against — `uvicorn gct.api.app:app`
and `scripts/worker.py`, as two REAL child processes — proves each one is up, hands the caller an
HTTP base URL, and guarantees both are gone afterwards. It asks the API nothing. The ceremony
attaches inside `with launched(...)`; everything here is the part that has to be true before the
first request is worth making.

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

IT REFUSES A DATABASE THAT ALREADY HAS WORK IN IT, and that is a cost guard rather than tidiness.
The worker this launches is the real one: it polls `jobs` in whatever `DATABASE_URL` names and
claims anything it finds there, so a run that uploads nothing still ingests — and bills for —
somebody else's queued file. `preflight` refuses that up front (see its docstring for what was
measured). Everything this harness itself enqueues happens after both children are up, so the
guard never sees a file the run is responsible for.

Usage:
    uv run python scripts/http_smoke.py
    uv run python scripts/http_smoke.py --ready-timeout 90
    uv run python scripts/http_smoke.py -- --poll 0.5 --lease 60
"""

from __future__ import annotations

import argparse
import json
import logging
import os
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


def preflight() -> None:
    """Refuse, up front and with the remedy, the conditions that otherwise arrive as a hang — or
    as a bill.

    The first two are checked HERE rather than left to the children because of where they land
    otherwise. A missing key makes uvicorn exit during startup and an unreachable database makes
    the worker die inside `connect()` — with a dead-child check on every tick those are reported
    quickly, but they are reported as a launch failure with somebody else's traceback attached,
    when they are really "this machine is not set up". Naming the remedy costs a line and saves
    the reader a debugging session (the same argument `gct.db.require_idle`'s message makes).

    The key requirement is the API's OWN, imported rather than restated: a second copy of "which
    variable, and what it is for" is a second writer for the fact, and this one would be checked
    from a script nobody edits when the rule changes.

    THE THIRD REFUSAL IS WHAT MAKES THE COST CLAIM STRUCTURAL RATHER THAN CONDITIONAL, and it is
    the one to read before deleting anything here. Neither child issues an API call while starting
    — that is measured, and it is why a launch is free — but the worker is a REAL worker, and
    `gct.jobs.worker.run` polls `jobs` in whatever database `DATABASE_URL` names and claims
    whatever is there. Without this check a harness that uploads nothing still ingests other
    people's files. Measured on a lane database holding one queued PDF:

      - a 25s hold against a recording endpoint drew 12 `POST /v1/embeddings` — billable against a
        real key, from a run that enqueued nothing;
      - a 20s hold against a closed endpoint spent the file's whole retry budget and buried it,
        so the harness alone drove an unrelated upload from `queued` to `failed`;
      - even a bare ~0.1s run moved `files.status` to `processing` and `jobs.attempts` 0 → 1.

    A bare `uv run python scripts/http_smoke.py` reads `.env`, which on a dev machine is the
    dogfood database, so "don't run it there" was the whole protection. Now it refuses.

    ORDERING, FOR WHOEVER ADDS THE CEREMONY (#109 PR 3): this runs ONCE, before either child
    exists, and the ceremony's own upload happens after `launched()` has yielded. A file this run
    enqueues can therefore never be seen by this check — the guard does not block the smoke's own
    work, only work that was already waiting when it started.
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
        waiting, sample = claimable_jobs(conn)
    finally:
        conn.close()

    if waiting:
        rows = "; ".join(
            f"job {job_id} ({state}) for file {file_id}" for job_id, file_id, state in sample
        )
        more = "" if waiting <= len(sample) else f" (and {waiting - len(sample)} more)"
        raise SetupError(
            f"{waiting} job(s) in the database `DATABASE_URL` names are waiting to be claimed: "
            f"{rows}{more}. This smoke launches the REAL worker, which would claim them and "
            "embed their files — money spent, and somebody else's upload moved to `processing` "
            "and on to `ready` or `failed` — for a run that uploaded nothing. Let a worker "
            "finish them (`uv run python scripts/worker.py`), or point `DATABASE_URL` at a "
            "scratch database."
        )


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
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Launch both children, prove they are up, tear them down, and report what was observed.

    Runnable rather than import-only, and that is a claim about this file rather than a
    convenience: "two processes came up over a real socket and both were reaped" is only
    believable if something executes it, and this is the smallest thing that does.

    WHAT IT COSTS, stated as what is true rather than what would be convenient. Neither child
    issues an API call while starting — both construct real OpenAI clients, but construction is
    not a call, and nothing here uploads or asks. That half is measured, against an endpoint that
    records every request it is sent, by
    `test_the_real_pair_comes_up_and_never_reaches_a_paid_endpoint`. It is not the whole claim,
    and an earlier version of this sentence stopped there and was wrong: the worker is a real
    worker and claims whatever `jobs` already holds, so a run against a database with queued work
    embeds — and bills for — files this run never uploaded. `preflight` refuses to start against
    such a database (its docstring carries what that was measured to do), which is what makes
    "this run spends nothing" a property of the script rather than of whatever was in the queue.

    `argv` is taken rather than read from `sys.argv`, for the reason `scripts/worker.py:main`
    records: this module is loaded by path inside a pytest process.
    """
    args = _parse(argv)
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        preflight()
    except SetupError as err:
        print(f"SETUP — {err}", file=sys.stderr)
        return EXIT_SETUP

    try:
        with launched(worker_args=args.worker_args, ready_timeout=args.ready_timeout) as stack:
            print(f"server  pid {stack.server.pid}  ready at {stack.base_url}/health")
            print(f"worker  pid {stack.worker.pid}  past startup ({WORKER_STARTED_TOKEN})")
            port = int(stack.base_url.rsplit(":", 1)[1])
    except LaunchError as err:
        print(f"FAIL — {err}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\nstopped — both children were torn down on the way out", file=sys.stderr)
        return EXIT_FAILED

    faults = [
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
    print(
        f"PASS — both children reaped (server {stack.server.process.returncode}, "
        f"worker {stack.worker.process.returncode}) and port {port} is closed."
    )
    return EXIT_OK


def _accepts(port: int) -> bool:
    """Whether anything is still listening — the half of teardown a `returncode` cannot show."""
    try:
        with socket.create_connection((LOOPBACK, port), timeout=PROBE_HTTP_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
