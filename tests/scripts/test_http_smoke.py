"""Tests for `scripts/http_smoke.py` - the launch, the readiness poll, and the teardown (#109).

WHY A SCRIPT HAS TESTS AT ALL, when ADR 0009 calls the scripts thin peer callers that usually
earn none: the same reason `test_worker_script.py` gives for the SIGTERM wiring, one level up.
Everything this file guards fails SILENTLY when it breaks. A readiness probe that answers "up"
too early makes PR 3's first request fail for a reason PR 3 did not cause. A teardown that
misses one child leaves a uvicorn holding a port for the rest of the session - measured, not
imagined: with `_interrupt` NOT installed, a SIGTERM to the harness left both children running
(the counterfactual is in this PR's ship record). Neither shows up as a red anything.

WHAT RUNS WHERE, and it is a two-way split rather than the usual one:

  - OFFLINE AND FREE - everything below except the last test. The children are STAND-INS: a
    dozen lines of `socket` that serve one canned `/health`, a `print` that emits the worker's
    token, a `sleep` that never becomes observable. That is not a weaker version of the real
    launch; it is the honest scope. This file's subject is processes, sockets and signals, and a
    stand-in child exercises every one of those mechanisms while removing the two things that
    would make the test skip - Postgres and a key.
  - REAL, AND MARKED `db` - `test_the_real_pair_comes_up_and_never_reaches_a_paid_endpoint`
    alone. Only a run of the ACTUAL pair can show that uvicorn's `--fd` spelling serves our ASGI
    app, that the real worker's own logging config puts `WORKER_STARTED_TOKEN` where
    `log_contains` looks, and that neither child buys anything on the way up.

The token the harness greps for is pinned from BOTH sides, and both halves are needed. The
library half (`test_the_token_the_harness_waits_for_is_the_one_the_library_actually_logs`) reads
the log record `gct.jobs.worker.run` emits and is free; it cannot see the script's logging
config. The real-pair test sees the whole path and needs Postgres. Either one alone leaves a way
for the harness to wait forever on a line nobody writes.

`scripts/` is deliberately not a package (ADR 0009), so the module is loaded BY PATH, the way
`test_worker_script.py` and `test_ask_smoke.py` do it. One thing they do not need and this file
does: the module is registered in `sys.modules` BEFORE `exec_module`. `@dataclass` resolves its
annotations through `sys.modules[cls.__module__]` under `from __future__ import annotations`, so
a by-path load of a module containing a dataclass raises `AttributeError: 'NoneType' object has
no attribute '__dict__'` at import - measured on the first attempt to load this script.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shlex
import signal
import socket
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from gct.jobs import worker as worker_lib

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "http_smoke.py"
_spec = importlib.util.spec_from_file_location("http_smoke_script_under_test", _SCRIPT)
http_smoke = importlib.util.module_from_spec(_spec)
# Registered before execution - see the module docstring; without this the dataclasses below the
# script's imports cannot resolve their own annotations and the load raises.
sys.modules[_spec.name] = http_smoke
_spec.loader.exec_module(http_smoke)


# A stand-in server: accepts on the fd it inherits and answers every request with the one body
# `health_ok` requires. Raw sockets rather than `http.server` because the whole point is that
# this child received an ALREADY-LISTENING socket - there is nothing to bind, and every stdlib
# server class wants to do the binding itself.
_STUB_SERVER = """
import socket, sys
sock = socket.socket(fileno=int(sys.argv[1]))
print("PORT", sock.getsockname()[1], flush=True)
body = b'{"status": "ok"}'
head = (
    b"HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\n"
    b"Content-Length: %d\\r\\nConnection: close\\r\\n\\r\\n" % len(body)
)
while True:
    conn, _ = sock.accept()
    conn.recv(65536)
    conn.sendall(head + body)
    conn.close()
"""

# A stand-in worker that becomes observable, and one that never does. The first emits the token
# INTERPOLATED from the harness's own constant rather than typed out: a stub carrying a copy of
# the string would keep passing after a rename that broke the real launch, which is the one
# failure this file's stand-ins could plausibly hide.
_STUB_WORKER_READY = (
    f"import time; print({http_smoke.WORKER_STARTED_TOKEN!r} + ': stub', flush=True); "
    "time.sleep(60)"
)
_STUB_WORKER_SILENT = "import time; time.sleep(60)"


def _await_line(log_path: Path, pattern: str, timeout: float = 15.0) -> str:
    """Poll a child's log for a line matching `pattern`. Fails the test rather than hanging."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            for line in log_path.read_text("utf-8", "replace").splitlines():
                if re.search(pattern, line):
                    return line
        time.sleep(0.05)
    raise AssertionError(f"no line matching {pattern!r} in {log_path} within {timeout}s")


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextmanager
def _stub_health(payload: bytes, *, status: int = 200):
    """A one-route HTTP server in this process, for the probe's own decision table."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - the stdlib's spelling
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer((http_smoke.LOOPBACK, 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{http_smoke.LOOPBACK}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def restore_sigterm():
    """Put the process's real SIGTERM disposition back after a test that registers one.

    The registration test drives the REAL `signal.signal`, for the reason the identical fixture
    in `test_worker_script.py` records: `signal.getsignal` is the only reader that sees what the
    interpreter actually installed, so the test genuinely mutates the pytest process.
    """
    previous = signal.getsignal(signal.SIGTERM)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@pytest.fixture
def stub_children(monkeypatch):
    """Point the harness at stand-in children and hand back what it launched.

    Returns a namespace with `use(server=..., worker=...)` to choose the two programs, and a
    `spawned` list the harness fills in as it goes - which is what lets a test ask whether the
    FIRST child was still reaped when the SECOND one failed.
    """
    programs = {"server": _STUB_SERVER, "worker": _STUB_WORKER_READY}
    spawned: list = []

    monkeypatch.setattr(
        http_smoke, "server_argv", lambda fd: [sys.executable, "-c", programs["server"], str(fd)]
    )
    monkeypatch.setattr(
        http_smoke,
        "worker_argv",
        lambda extra=(): [sys.executable, "-c", programs["worker"], *extra],
    )

    real_spawned = http_smoke._spawned

    @contextmanager
    def recording(name, argv, log_dir, **kwargs):
        with real_spawned(name, argv, log_dir, **kwargs) as child:
            spawned.append(child)
            yield child

    monkeypatch.setattr(http_smoke, "_spawned", recording)

    def use(*, server: str | None = None, worker: str | None = None) -> None:
        if server is not None:
            programs["server"] = server
        if worker is not None:
            programs["worker"] = worker

    return SimpleNamespace(use=use, spawned=spawned)


# --------------------------------------------------------------------------------------------
# The coupling to the library's own log line
# --------------------------------------------------------------------------------------------


class _StopBeforeTheFirstTick(Exception):
    """Raised out of the reaper so `run` cannot enter its `while True` against a real database."""


def test_the_token_the_harness_waits_for_is_the_one_the_library_actually_logs(monkeypatch, caplog):
    """`WORKER_STARTED_TOKEN` is a SECOND READER of a message `gct.jobs.worker` writes.

    Nothing in the library knows this harness greps for it, so a reword of that line - a
    reasonable thing to do while retuning what the startup banner reports - would leave the
    smoke waiting out its whole timeout on a worker that is perfectly healthy, and reporting a
    hang. That is the drift this pins, and it is the test `scripts/http_smoke.py`'s docstring
    names.

    Three properties, all of them load-bearing for the harness and none of them checkable from
    the script's side:

      - the token appears in the FORMATTED message, since the child's log is text;
      - the record is INFO, which is what `scripts/worker.py`'s default level emits;
      - it is emitted BEFORE the first tick - the reaper raises here, and the record already
        exists - which is why "the worker logged it" means startup finished rather than "a job
        was processed". A line logged after the first `claim` would make readiness wait on the
        queue having work in it.

    `reclaim_expired` is stubbed rather than the connection: `run`'s first statement through
    that name is the reaper, and stubbing it makes the stop deterministic instead of depending
    on which attribute a fake connection happens to fail on first.
    """

    def _reaper_that_stops_the_loop(_conn):
        raise _StopBeforeTheFirstTick

    monkeypatch.setattr(worker_lib, "reclaim_expired", _reaper_that_stops_the_loop)

    with caplog.at_level(logging.INFO, logger="gct.jobs.worker"):
        with pytest.raises(_StopBeforeTheFirstTick):
            worker_lib.run(object(), embedder=object(), chunk_size=10, chunk_overlap=0)

    records = [r for r in caplog.records if r.name == "gct.jobs.worker"]
    assert records, (
        "`gct.jobs.worker.run` logged NOTHING before its first tick, so the harness's readiness "
        f"probe can never come true. It waits for {http_smoke.WORKER_STARTED_TOKEN!r}"
    )
    first = records[0]
    assert http_smoke.WORKER_STARTED_TOKEN in first.getMessage(), (
        f"the harness greps the worker's log for {http_smoke.WORKER_STARTED_TOKEN!r}; the first "
        f"line `run` actually emits is {first.getMessage()!r}. Change one and the smoke hangs "
        "until its ready-timeout on a worker that started fine."
    )
    assert first.levelno == logging.INFO, (
        "the harness runs the worker at its default level (INFO). A start-of-loop line below "
        f"that never reaches the log file the probe reads; this one is {first.levelname}."
    )


# --------------------------------------------------------------------------------------------
# The socket: why readiness is a request, and what teardown has to leave behind
# --------------------------------------------------------------------------------------------


def test_the_reserved_socket_is_loopback_and_accepts_before_anything_serves_it():
    """The two facts the whole readiness design rests on, neither of them obvious.

    The listener is bound by US, to loopback, so a lane's smoke server is unreachable off-box
    whatever uvicorn's `--host` default is - and a kernel-chosen port is exclusively ours from
    the moment it is allocated rather than from the moment a child binds it.

    And it ACCEPTS with nothing behind it. That is the measured reason a TCP connect is not a
    readiness probe here: the socket is listening before the child is forked, so a connect-based
    probe would report "ready" against a uvicorn that had not finished importing. It is equally
    the reason `_accepts` is a fair teardown check - it answers False only once the listening
    socket is actually gone.
    """
    sock = http_smoke._reserve_listener()
    try:
        host, port = sock.getsockname()
        assert host == http_smoke.LOOPBACK
        assert port != 0
        assert http_smoke._accepts(port), (
            "a listening socket with no server behind it refused a connection, which would make "
            "a TCP-connect readiness probe honest and this file's whole argument wrong"
        )
    finally:
        sock.close()
    assert not http_smoke._accepts(port), "the port still accepts after its listener was closed"


def test_the_reserved_socket_does_not_relax_the_exclusivity_it_was_taken_for():
    """No `SO_REUSEADDR`, and its absence is a decision rather than an omission.

    That option exists to relax exactly the property this design buys - that the port is ours
    from the instant it is allocated - so setting it would quietly undo the reason the socket is
    reserved before the child exists at all.
    """
    sock = http_smoke._reserve_listener()
    try:
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
    finally:
        sock.close()


@pytest.mark.parametrize(
    ("payload", "status", "expected", "why"),
    [
        (b'{"status": "ok"}', 200, True, "the contract the route states"),
        (b'{"status": "degraded"}', 200, False, "a 200 whose body says it is NOT ok"),
        (b'{"ok": true}', 200, False, "a 200 with some other shape entirely"),
        (b"<html>up</html>", 200, False, "a 200 that is not even JSON"),
        (b'{"status": "ok"}', 503, False, "the right body behind the wrong status"),
    ],
)
def test_health_ok_reads_the_body_and_not_only_the_status(payload, status, expected, why):
    """A 200 alone is not readiness, and the degraded case is why.

    `/health` reaches Postgres before it answers. Accepting any 200 at that path would make the
    probe pass against an app that grew a different route there, or a future health route that
    reports a problem in its body with a perfectly successful status - the case an operator most
    needs the smoke to catch.
    """
    with _stub_health(payload, status=status) as base_url:
        assert http_smoke.health_ok(base_url) is expected, why


def test_health_ok_is_false_rather_than_raising_when_nothing_is_listening():
    """Every probe runs against a port that is usually not answering yet; it must return, not raise.

    A `health_ok` that propagated `URLError` would turn the first tick of the readiness loop into
    a crash, and the dead-child check that runs before it would never get a second chance to fire.
    """
    sock = http_smoke._reserve_listener()
    port = sock.getsockname()[1]
    sock.close()
    assert http_smoke.health_ok(f"http://{http_smoke.LOOPBACK}:{port}") is False


# --------------------------------------------------------------------------------------------
# The readiness loop: dead child vs slow child
# --------------------------------------------------------------------------------------------


def test_a_child_that_died_is_reported_with_its_own_output_instead_of_being_waited_out(tmp_path):
    """The distinction the loop's statement order exists for, measured against a real corpse.

    A child that died at startup - no key, Postgres down, a flag its CLI refused - has already
    told you why, in its own words, on its own stderr. Waiting out the remaining timeout and then
    reporting "never became ready" throws that away and blames the wrong thing. The deadline here
    is 30s and the assertion is that the failure arrives in under 5: what is being pinned is that
    `poll()` is asked BEFORE the probe, not merely that the message is nice.
    """
    argv = [sys.executable, "-c", "import sys; print('the child said this'); sys.exit(3)"]
    started = time.monotonic()
    with http_smoke._spawned("probe", argv, tmp_path) as child:
        with pytest.raises(http_smoke.LaunchError) as err:
            http_smoke._await(child, lambda: False, "became observable", 30.0)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"a dead child was waited out for {elapsed:.1f}s of a 30s deadline"
    assert "status 3" in str(err.value), str(err.value)
    assert "the child said this" in str(err.value), (
        "the failure message did not carry the child's own output, which is the only place the "
        f"actual reason ever appears. Got: {err.value}"
    )
    assert str(child.log_path) in str(err.value)


def test_a_slow_child_is_reported_against_the_timeout_it_was_actually_given(tmp_path):
    """The number in a timeout message has to be the number the operator typed.

    Measured on the inherited draft, which took a deadline and interpolated the MODULE DEFAULT:
    `--ready-timeout 0.05` failed after 0.05s reporting "within 60s". Nothing was broken except
    the sentence, and the sentence is the whole output of a failed launch.
    """
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    with http_smoke._spawned("probe", argv, tmp_path) as child:
        with pytest.raises(http_smoke.LaunchError) as err:
            http_smoke._await(child, lambda: False, "became observable", 0.25)

    message = str(err.value)
    assert "0.25s" in message, message
    assert f"{http_smoke.READY_TIMEOUT_SECONDS:g}s" not in message, (
        "the timeout message quoted the module default instead of the timeout this wait was "
        f"given. Got: {message}"
    )


# --------------------------------------------------------------------------------------------
# Teardown
# --------------------------------------------------------------------------------------------


def test_stop_kills_a_child_that_ignores_sigterm_once_the_grace_is_up(tmp_path):
    """A teardown that can hang is not a teardown.

    SIGTERM first, because both real children do something useful with it. SIGKILL after the
    grace, because "useful" is not "guaranteed" - a child wedged in a C call, or one that
    installed `SIG_IGN`, would otherwise hold the harness open forever.

    The stub announces itself AFTER installing `SIG_IGN` and the test waits for that line, which
    is not ceremony: without it the teardown lands during the child's interpreter startup, the
    default disposition is still in force, and the test passes against a stubborn child that was
    never actually stubborn - measured, it exited -15 rather than -9.
    """
    argv = [
        sys.executable,
        "-c",
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('IGNORING', flush=True); time.sleep(60)",
    ]
    with http_smoke._spawned("stubborn", argv, tmp_path) as child:
        _await_line(child.log_path, r"^IGNORING$")
        started = time.monotonic()
        status = http_smoke.stop(child, grace=0.5)
        elapsed = time.monotonic() - started

    assert status == -int(signal.SIGKILL), f"expected a SIGKILL exit, got {status}"
    assert 0.5 <= elapsed < 10.0, f"the grace was not served as written ({elapsed:.2f}s)"
    assert not _is_alive(child.pid)


def test_stop_takes_the_whole_process_group_so_a_grandchild_is_not_orphaned(tmp_path):
    """`start_new_session` + `killpg` is what makes teardown TOTAL rather than merely tidy.

    Signalling the pid alone reaps the child and leaves anything it spawned running - a real
    shape here, since uvicorn is a process that may reload or fork. This drives that directly: a
    stand-in child spawns a sleeper and reports its pid, and the sleeper has to be gone after
    `stop` returns.
    """
    argv = [
        sys.executable,
        "-c",
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "print('GRANDCHILD', p.pid, flush=True); time.sleep(60)",
    ]
    with http_smoke._spawned("parent", argv, tmp_path) as child:
        grandchild = int(_await_line(child.log_path, r"^GRANDCHILD ").split()[1])
        assert _is_alive(grandchild)
        http_smoke.stop(child, grace=5.0)

    deadline = time.monotonic() + 5.0
    while _is_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _is_alive(grandchild), (
        f"pid {grandchild}, spawned BY the child, survived the child's teardown - the harness "
        "signalled a process rather than a process group"
    )


def test_stop_of_a_child_that_already_exited_returns_its_status_without_raising(tmp_path):
    """The ordinary case on every failure path: the child is already gone when teardown runs.

    `killpg` on a reaped group raises `ProcessLookupError`, so a teardown that did not absorb it
    would replace a launch failure's real message with a traceback from the cleanup.
    """
    with http_smoke._spawned("gone", [sys.executable, "-c", "raise SystemExit(4)"], tmp_path) as c:
        c.process.wait(timeout=30)
        assert http_smoke.stop(c) == 4
        assert http_smoke.stop(c) == 4, "a second stop of a reaped child must also be harmless"


def test_the_sigterm_handler_raises_the_interrupt_the_context_managers_unwind_on():
    """Under Python's default disposition a `kill` of the harness runs no `finally` at all.

    Measured, with the two children of a real launch: with `_interrupt` installed, a SIGTERM to
    the harness left neither child alive; with it removed, BOTH survived the harness. Raising is
    the entire mechanism - `launched`'s `with` blocks only run their teardown if a frame unwinds.
    """
    with pytest.raises(KeyboardInterrupt):
        http_smoke._interrupt(int(signal.SIGTERM), None)


# --------------------------------------------------------------------------------------------
# The command lines the harness builds
# --------------------------------------------------------------------------------------------


def test_server_argv_serves_the_asgi_callable_on_the_inherited_socket():
    """`--fd`, and NOTHING that would make uvicorn bind for itself.

    A `--port`/`--host` pair here would reopen the window this design exists to close, and would
    hand the bind address to uvicorn's default rather than keeping it on the loopback socket the
    harness owns. The ASGI target is `gct.api.app:app`, the callable `create_app()` builds (#104).
    """
    argv = http_smoke.server_argv(7)
    assert argv == [sys.executable, "-m", "uvicorn", "gct.api.app:app", "--fd", "7"]
    assert "--port" not in argv and "--host" not in argv


def test_worker_argv_runs_the_real_script_and_adds_no_flags_of_its_own():
    """Pass-through, not policy.

    The CLI exists so a subprocess is no longer pinned to the module defaults (#109 PR 1); a
    flag supplied HERE would be this harness deciding a queue number, which is not its call
    (ADR 0009). `--log-level` is the one worth naming: leaving it alone is what keeps the INFO
    line the readiness probe waits for.
    """
    script = http_smoke.repo_root() / "scripts" / "worker.py"
    assert script.is_file()
    assert http_smoke.worker_argv() == [sys.executable, str(script)]
    assert http_smoke.worker_argv(["--poll", "0.5"]) == [
        sys.executable,
        str(script),
        "--poll",
        "0.5",
    ]


def test_the_child_environment_is_inherited_and_output_is_unbuffered(monkeypatch):
    """Two properties, and the second one is load-bearing rather than tidy.

    Inheritance is what keeps `DATABASE_URL`, `OPENAI_API_KEY` and `GCT_STAGING_DIR` from being
    assembled a second way. `PYTHONUNBUFFERED` is what makes a failure message have content:
    stdout redirected to a FILE is block-buffered, so without it a child that dies before
    filling 8KB has written nothing, and every log tail the harness quotes would be empty in
    exactly the case it exists for.
    """
    monkeypatch.setenv("GCT_A_SETTING_THE_LANE_SUPPLIES", "sentinel")
    env = http_smoke._child_env()
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["GCT_A_SETTING_THE_LANE_SUPPLIES"] == "sentinel"


def test_every_usage_line_in_the_docstring_actually_parses():
    """The examples a reader copies are checked against the parser that has to accept them.

    Not hypothetical. The inherited draft advertised `--worker-arg --poll --worker-arg 0.5`, and
    argparse refuses it - it reads `--poll` as an option, not as that flag's value, so the only
    spelling that worked was one `=` per token and nobody would have guessed it. Help text is
    documentation shipped inside the program; unlike a comment, a user types it.
    """
    usage = http_smoke.__doc__.split("Usage:")[1]
    lines = [line.strip() for line in usage.strip().splitlines() if line.strip()]
    assert len(lines) >= 2, "the module docstring's Usage block advertises nothing to check"
    for line in lines:
        tokens = shlex.split(line)
        assert "scripts/http_smoke.py" in tokens, f"not a usage line for this script: {line!r}"
        argv = tokens[tokens.index("scripts/http_smoke.py") + 1 :]
        # A SystemExit out of `parse_args` IS the failure: argparse refused a line the file
        # tells a reader to type.
        http_smoke._build_parser().parse_args(argv)


def test_everything_after_the_double_dash_reaches_the_worker_and_nothing_else_does():
    """The pass-through boundary, from both sides.

    Flags the harness owns stay in front; everything behind `--` is handed to
    `scripts/worker.py` unread, spelled exactly as that script spells it. And a worker flag
    typed WITHOUT the separator is refused rather than silently swallowed - the harness has no
    business guessing which of two CLIs an unknown flag belongs to.
    """
    args = http_smoke._build_parser().parse_args(
        ["--ready-timeout", "5", "--", "--poll", "0.5", "--log-level", "WARNING"]
    )
    assert args.ready_timeout == 5.0
    assert args.worker_args == ["--poll", "0.5", "--log-level", "WARNING"]

    assert http_smoke._build_parser().parse_args([]).worker_args == []

    with pytest.raises(SystemExit) as exit_info:
        http_smoke._build_parser().parse_args(["--poll", "0.5"])
    assert exit_info.value.code == 2


# --------------------------------------------------------------------------------------------
# `launched()` composed, against stand-in children
# --------------------------------------------------------------------------------------------


def test_launched_brings_both_children_up_and_leaves_neither_behind(stub_children):
    """The happy path, end to end, with the two real mechanisms and stand-in programs.

    The server child is handed a socket it did not bind and serves on it; the worker child is
    waited for by its log; both are alive inside the block and both are reaped outside it, with
    the port refusing connections afterwards. What is NOT proven here is that uvicorn and
    `scripts/worker.py` behave this way - that is the `db`-marked test at the bottom.
    """
    with http_smoke.launched(ready_timeout=30.0) as stack:
        port = int(stack.base_url.rsplit(":", 1)[1])
        assert http_smoke.health_ok(stack.base_url)
        assert stack.server.process.poll() is None
        assert stack.worker.process.poll() is None
        assert _await_line(stack.server.log_path, r"^PORT ").split()[1] == str(port), (
            "the server child is serving on a different port than the harness advertises, so "
            "the socket it inherited is not the one that was reserved"
        )

    assert not _is_alive(stack.server.pid)
    assert not _is_alive(stack.worker.pid)
    assert not http_smoke._accepts(port), "the port still accepts connections after teardown"


def test_the_port_stops_accepting_the_moment_the_server_child_is_gone(stub_children):
    """The parent's copy of the listening socket is closed early, and that is why this holds.

    A socket survives as long as ANY fd refers to it, so a parent that kept its copy would leave
    the port accepting connections into a backlog after the server process died - and `_accepts`
    would answer "yes, something is serving" about a corpse. That is the question this file's
    own PASS line asks, and the one PR 3's ceremony asks inside the block.

    Measured: this is the only assertion in the file that sees the early close at all. Deleting
    `listener.close()` and letting the `ExitStack` do it left the other 27 tests green, because
    every one of them looks at the port only after the stack has already unwound.
    """
    with http_smoke.launched(ready_timeout=30.0) as stack:
        port = int(stack.base_url.rsplit(":", 1)[1])
        assert http_smoke._accepts(port)
        http_smoke.stop(stack.server, grace=5.0)
        assert not http_smoke._accepts(port), (
            "the port still accepts with the server process gone - something other than that "
            "child is holding the listening socket open, and the harness's teardown check is "
            "asking a question it cannot get a true answer to"
        )


def test_a_worker_that_never_becomes_observable_does_not_orphan_the_server(stub_children):
    """The orphan this shape exists to prevent, driven rather than argued.

    A server that came up fine, a worker that never does, and - without the `ExitStack` - a
    uvicorn left holding a port after the harness has already printed its failure and exited.
    The failure has to reach the caller AND take the first child with it.
    """
    stub_children.use(worker=_STUB_WORKER_SILENT)

    with pytest.raises(http_smoke.LaunchError) as err:
        with http_smoke.launched(ready_timeout=0.75):
            pytest.fail("launched() yielded despite a worker that never reported itself started")

    assert "worker" in str(err.value)
    assert len(stub_children.spawned) == 2, "the server was never launched, so this proves nothing"
    server, worker = stub_children.spawned
    assert not _is_alive(server.pid), (
        f"the server (pid {server.pid}) outlived a failed launch - the failure was reported and "
        "the process was left holding its port"
    )
    assert not _is_alive(worker.pid)


def test_the_child_logs_are_kept_on_failure_and_removed_on_success(
    stub_children, monkeypatch, tmp_path, capsys
):
    """Evidence survives exactly the run that needs it, and no other.

    Removing the logs always would delete a failed launch's only explanation at the moment it is
    wanted; keeping them always would leave a directory per run on the operator's disk. The
    failure path prints the path it kept, because a directory nobody can find is not evidence.
    """
    kept = tmp_path / "kept"
    kept.mkdir()
    monkeypatch.setattr(http_smoke.tempfile, "mkdtemp", lambda prefix: str(kept))

    stub_children.use(worker=_STUB_WORKER_SILENT)
    with pytest.raises(http_smoke.LaunchError):
        with http_smoke.launched(ready_timeout=0.75):
            pass
    assert kept.is_dir() and (kept / "worker.log").exists()
    assert str(kept) in capsys.readouterr().err

    removed = tmp_path / "removed"
    removed.mkdir()
    monkeypatch.setattr(http_smoke.tempfile, "mkdtemp", lambda prefix: str(removed))
    stub_children.use(worker=_STUB_WORKER_READY)
    with http_smoke.launched(ready_timeout=30.0):
        pass
    assert not removed.exists(), "a successful run left its log directory on disk"


# --------------------------------------------------------------------------------------------
# `main()`
# --------------------------------------------------------------------------------------------


def test_main_reports_an_unstageable_machine_as_setup_and_a_failed_launch_as_failure(
    monkeypatch, restore_sigterm, capsys
):
    """Exit 2 and exit 1 mean different things and a caller branches on them.

    The same distinction `ingest_smoke.py` and `ask_smoke.py` draw: a missing key or an
    unmigrated database is a fact about this machine, not a verdict about the code, and reading
    it as the smoke failing sends someone to debug the wrong thing.
    """

    def _refuse():
        raise http_smoke.SetupError("no Postgres here")

    monkeypatch.setattr(http_smoke, "preflight", _refuse)
    assert http_smoke.main([]) == http_smoke.EXIT_SETUP
    assert "SETUP" in capsys.readouterr().err

    monkeypatch.setattr(http_smoke, "preflight", lambda: None)

    @contextmanager
    def _fails_to_launch(**_kwargs):
        raise http_smoke.LaunchError("the worker exited with status 2")
        yield  # pragma: no cover - unreachable, present so this is a generator

    monkeypatch.setattr(http_smoke, "launched", _fails_to_launch)
    assert http_smoke.main([]) == http_smoke.EXIT_FAILED
    assert "FAIL" in capsys.readouterr().err


def test_main_installs_the_handler_that_makes_a_killed_harness_tear_its_children_down(
    monkeypatch, restore_sigterm, stub_children
):
    """Registration is one line with no return value, and deleting it changes nothing visible.

    The symptom is only ever a `kill` of the harness leaving two processes behind, which no
    other test in this suite can see. `signal.getsignal` is the only reader of what the
    interpreter actually installed, so the real `signal.signal` runs here and the fixture hands
    the disposition back.
    """
    monkeypatch.setattr(http_smoke, "preflight", lambda: None)
    assert http_smoke.main([]) == http_smoke.EXIT_OK
    assert signal.getsignal(signal.SIGTERM) is http_smoke._interrupt


def test_main_reports_a_child_that_stopped_any_other_way_as_a_failure(monkeypatch, restore_sigterm):
    """`CLEAN_EXIT_STATUSES` is a measurement, and the check on it has to actually fire.

    The two statuses are what the real children were observed to exit with - the worker catches
    the interrupt its handler raises and exits 0 (#82), uvicorn re-raises the signal and exits
    as killed-by-SIGTERM. Anything else means something went wrong on the way out, and a smoke
    that printed PASS over it would be reporting a teardown it did not get.
    """
    monkeypatch.setattr(http_smoke, "preflight", lambda: None)
    sock = http_smoke._reserve_listener()
    port = sock.getsockname()[1]
    sock.close()

    def _child(name: str, returncode: int) -> http_smoke.Child:
        return http_smoke.Child(
            name=name,
            process=SimpleNamespace(pid=-1, returncode=returncode),
            log_path=Path("/nonexistent"),
        )

    @contextmanager
    def _yields_a_badly_stopped_worker(**_kwargs):
        yield http_smoke.Stack(
            base_url=f"http://{http_smoke.LOOPBACK}:{port}",
            server=_child("server", -int(signal.SIGTERM)),
            worker=_child("worker", 9),
        )

    monkeypatch.setattr(http_smoke, "launched", _yields_a_badly_stopped_worker)
    assert http_smoke.main([]) == http_smoke.EXIT_FAILED


# --------------------------------------------------------------------------------------------
# The real pair
# --------------------------------------------------------------------------------------------


def test_the_real_pair_comes_up_and_never_reaches_a_paid_endpoint(db, monkeypatch):
    """The only test that launches the ACTUAL `uvicorn gct.api.app:app` and `scripts/worker.py`.

    Everything above uses stand-in children, which cannot see three things this can: that
    uvicorn's `--fd` spelling serves our ASGI app on a socket it did not bind, that `/health`
    answers 200 with the body the probe requires once it has reached Postgres, and that
    `scripts/worker.py`'s own logging config puts `WORKER_STARTED_TOKEN` into the log file
    `log_contains` reads.

    AND THAT THE LAUNCH IS FREE, measured rather than argued. Both children DO construct real
    OpenAI clients at startup - the worker at wiring, the API in its lifespan - and the question
    a cost claim turns on is whether construction is a CALL. So the SDK is pointed at a closed
    loopback port for the duration: if either child issued a request while starting, it would
    get a connection error and die, and this test would fail as a launch failure. It passes, so
    neither did. The key is a placeholder for the same reason - the API refuses to start without
    one, and this proves that requirement is satisfiable without a real one.

    Takes `db` for the Postgres gate it carries, not for the connection: both children open
    their own, and the fixture is what makes this skip locally when Postgres is down and
    HARD-FAIL in CI. No `db_other` - this test writes nothing and claims no publication.

    One thing it does to the database, worth knowing before the next person adds to it: the
    worker is a REAL worker, and it may complete one poll tick (reap, claim) against the test
    database in the moment between reporting itself started and being torn down. It enqueues
    nothing itself. The suite is serial and each ship lane has its own database, so the only
    row it can reach is one an earlier test failed to clean up.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-placeholder-this-test-must-not-spend")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://{http_smoke.LOOPBACK}:1/v1")

    with http_smoke.launched() as stack:
        port = int(stack.base_url.rsplit(":", 1)[1])
        assert stack.server.process.poll() is None
        assert stack.worker.process.poll() is None
        assert http_smoke.health_ok(stack.base_url)
        with urllib.request.urlopen(  # noqa: S310 - loopback, and the URL came from the harness
            f"{stack.base_url}/health", timeout=http_smoke.PROBE_HTTP_TIMEOUT_SECONDS
        ) as response:
            assert json.loads(response.read().decode("utf-8")) == {"status": "ok"}
        assert stack.worker.log_contains(http_smoke.WORKER_STARTED_TOKEN)

    assert stack.server.process.returncode in http_smoke.CLEAN_EXIT_STATUSES, (
        f"uvicorn stopped with {stack.server.process.returncode}, which is not one of the "
        f"statuses a clean teardown was measured to produce ({http_smoke.CLEAN_EXIT_STATUSES})"
    )
    assert stack.worker.process.returncode in http_smoke.CLEAN_EXIT_STATUSES, (
        f"the worker stopped with {stack.worker.process.returncode}; #82's handler is what makes "
        "it exit 0 rather than being killed"
    )
    assert not _is_alive(stack.server.pid)
    assert not _is_alive(stack.worker.pid)
    assert not http_smoke._accepts(port)
