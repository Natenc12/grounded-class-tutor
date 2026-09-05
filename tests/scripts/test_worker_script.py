"""Tests for `scripts/worker.py` - its SIGTERM wiring (issue #82) and its CLI (issue #109).

WHY A SCRIPT HAS TESTS AT ALL, when ADR 0009 calls the scripts thin peer callers that usually
earn none: this one now carries a signal handler, and a handler that is never REGISTERED fails
in total silence. `signal.signal(SIGTERM, ...)` is one line, it has no return value anyone
checks, and deleting it changes nothing any other test can see - the worker keeps working, the
library's shutdown guard keeps passing its own tests, and the only symptom is that a
`kill`ed worker strands its job for fifteen minutes again. That is exactly the shape of defect
`test_ask_smoke.py` was written for: invisible from reading, visible only from running.

Everything here is offline and free - no DB, no provider, no network. `main()` is driven with
`connect`, `run` and `OpenAIEmbeddings` all stubbed, so nothing constructs a real client and
nothing reaches Postgres.

What is deliberately NOT here: a test that the released job is actually claimable afterwards.
That is the library's decision and it is covered where it lives, on a real connection through a
second one - `test_an_interrupt_mid_ingest_leaves_the_job_claimable_at_once`
(tests/gct/jobs/test_worker.py). This file only proves the signal reaches that path.

THE CLI TESTS OBSERVE `run`'s KEYWORD ARGUMENTS, NEVER A POLL LOOP. `run` is a `while True`
against Postgres, so the only honest thing to assert here is what this script HANDED it -
whether the loop then behaves is `tests/gct/jobs/test_worker.py`'s subject and is tested there
on a real connection. Everything stays offline and free.

`main` is called with an EXPLICIT argv everywhere below, including `[]`. That is not style: this
module is loaded by path inside a pytest process, so a bare `main()` would parse pytest's own
flags and exit 2 before reaching a single line under test.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.jobs import worker as worker_lib

# Imported by PATH, the same way `test_ask_smoke.py` does it and for the same reason: `scripts/`
# is deliberately not a package (ADR 0009 - the scripts are peers of the library, not part of
# it), so there is nothing to `import` and bending `sys.path` would blur the boundary.
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "worker.py"
_spec = importlib.util.spec_from_file_location("worker_script_under_test", _SCRIPT)
worker_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker_script)


@pytest.fixture
def restore_sigterm():
    """Put the process's real SIGTERM disposition back after a test that registers one.

    The wiring test drives the REAL `signal.signal` rather than a stub, because a recording stub
    would pass against a script that registered its handler on the wrong signal or under a
    different mechanism entirely - `signal.getsignal` is the only reader that sees what the
    interpreter actually installed. That means the test genuinely mutates the pytest process,
    so it has to be handed back.
    """
    previous = signal.getsignal(signal.SIGTERM)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_the_sigterm_handler_raises_the_interrupt_the_guard_unwinds_on():
    """SIGTERM has to arrive as an EXCEPTION or none of the shutdown path exists.

    Python's default disposition for SIGTERM kills the interpreter inside the C handler: no
    frame unwinds, no `finally` runs, and `process_one`'s guard never sees anything. Raising is
    the whole mechanism, and `KeyboardInterrupt` specifically is what makes SIGTERM and Ctrl-C
    one path rather than two.

    The signal number travels into the message because the two are indistinguishable at the
    catch site otherwise, and an operator reading a log wants to know whether the worker was
    Ctrl-C'd or `kill`ed.
    """
    with pytest.raises(KeyboardInterrupt, match=str(int(signal.SIGTERM))):
        worker_script._interrupt(int(signal.SIGTERM), None)


def test_main_registers_the_handler_for_sigterm(monkeypatch, restore_sigterm):
    """The one line whose absence nothing else can detect - asserted against the interpreter.

    `signal.getsignal` is the reader, not a recording stub: it reports what is actually
    installed, so a script that registered on SIGINT, or wrapped the handler in something that
    swallowed the raise, goes red here.

    `main` is driven to completion with every real dependency stubbed. `run` raises
    `KeyboardInterrupt` because that is the shutdown path this script exists to handle, and it
    also ends the call without a loop - the assertion afterwards is that `main` swallowed it
    (an operator who stopped the worker gets a quiet exit, not a traceback).
    """
    closed: list[bool] = []

    class FakeConn:
        autocommit = False

        def close(self):
            closed.append(True)

    # `run` is stubbed, but its ARGUMENTS are still evaluated - so the embedder is constructed
    # either way, and stubbing the class is what keeps this test free and keyless.
    def fake_embedder():
        return object()

    def stopped(*_args, **_kwargs):
        raise KeyboardInterrupt("signal 15")

    monkeypatch.setattr(worker_script, "connect", FakeConn)
    monkeypatch.setattr(worker_script, "OpenAIEmbeddings", fake_embedder)
    monkeypatch.setattr(worker_script, "run", stopped)

    worker_script.main([])

    assert signal.getsignal(signal.SIGTERM) is worker_script._interrupt, (
        "a SIGTERM'd worker takes Python's default disposition - dead in the C handler, no "
        "unwind, and its job stranded under a live lease for the full 15 minutes"
    )
    assert closed == [True], "the connection must be closed even on the shutdown path"


def _stub_main_dependencies(monkeypatch, *, connect):
    """Wire `main()` up with every real dependency replaced - no DB, no provider, no network.

    `run` is stubbed to raise `KeyboardInterrupt`, which is both the shutdown path this script
    exists to handle and the only way out of a `while True` loop. Its ARGUMENTS are still
    evaluated, so `OpenAIEmbeddings` is constructed either way and stubbing the class is what
    keeps these tests free and keyless.
    """

    def stopped(*_args, **_kwargs):
        raise KeyboardInterrupt("signal 15")

    monkeypatch.setattr(worker_script, "connect", connect)
    monkeypatch.setattr(worker_script, "OpenAIEmbeddings", lambda: object())
    monkeypatch.setattr(worker_script, "run", stopped)


class _FakeConn:
    """Just enough connection for `main()`: an `autocommit` to set and a `close` to record."""

    def __init__(self, closed: list[bool]) -> None:
        self.autocommit = False
        self._closed = closed

    def close(self):
        self._closed.append(True)


def test_the_handler_is_registered_before_the_connection_is_opened(monkeypatch, restore_sigterm):
    """The ORDER of `main`'s first two lines, asserted from inside the gap between them.

    `connect()` is the slowest thing in startup and the likeliest to hang - a Postgres that is
    down, a DNS lookup, a full connection pool - which makes it exactly the window in which an
    operator gives up and sends a SIGTERM. Register after it and that signal takes Python's
    DEFAULT disposition: the interpreter dies in the C handler, no frame unwinds, and the
    `finally` that closes the connection never runs.

    Asserted by reading the interpreter's disposition FROM INSIDE the stubbed `connect`, which
    is the one vantage point that can see the ordering. A test that only checked the disposition
    after `main()` returned would pass with the two lines swapped, because by then both have
    run - the assertion has to be made at the moment that distinguishes them.
    """
    seen: list[object] = []
    closed: list[bool] = []

    def connect_that_looks_around():
        seen.append(signal.getsignal(signal.SIGTERM))
        return _FakeConn(closed)

    _stub_main_dependencies(monkeypatch, connect=connect_that_looks_around)

    worker_script.main([])

    assert seen == [worker_script._interrupt], (
        "the SIGTERM handler is registered AFTER the connection is opened, so a signal arriving "
        "during startup - the slowest, most likely-to-hang part of it - takes Python's default "
        "disposition and kills the interpreter with no unwind at all"
    )
    assert closed == [True]


def test_a_sigterm_arriving_during_startup_exits_as_quietly_as_one_mid_run(
    monkeypatch, restore_sigterm
):
    """A REAL signal, delivered while `connect()` is still running, and the worker exits clean.

    The test above proves the registration happens first; this proves what that buys, by raising
    an actual SIGTERM through the interpreter's actual disposition rather than calling
    `_interrupt` by hand. Without the early registration this is a dead process and an unrunnable
    assertion.

    THREE THINGS, and each fails differently:
      - THE SIGNAL IS NOT LOST. It arrives as the `KeyboardInterrupt` the whole shutdown path is
        built on, rather than as Python's default disposition for SIGTERM - which would kill the
        interpreter in the C handler with no frame unwound at all.
      - IT REACHES `main`'s QUIET `except`, because that `try` covers `connect()`. `main` returns
        normally, so the interpreter exits 0 with no traceback: an operator who stopped a worker
        during startup gets the same silence as one who stopped it mid-run, and there is exactly
        one way a stopped worker exits rather than two that differ by timing.
      - THE `finally` SURVIVES AN UNOPENED CONNECTION. It runs with `conn` still `None`, so an
        unguarded `conn.close()` there raises over the top of the interrupt and the quiet exit
        becomes a cleanup traceback - strictly worse than the escape it replaced. That is what
        `if conn is not None` is for, and this is the test that reddens without it.

    `run` is spied rather than left as the shared stub, and that is load-bearing: the shared stub
    raises `KeyboardInterrupt` itself, so a `main` that somehow reached `run` would swallow that
    one and return quietly too. Asserting `run` was never entered is what separates "the startup
    signal was handled" from "the startup signal was ignored and the run was stopped instead".
    """
    reached_the_end_of_connect: list[bool] = []
    entered_run: list[bool] = []
    closed: list[bool] = []

    def connect_that_gets_signalled():
        # LOOK BEFORE RAISING. A real SIGTERM under Python's default disposition does not fail
        # this test, it TERMINATES THE PYTEST PROCESS - no report, no failure line, no other
        # test in the session gets to run. Measured, by moving the registration below
        # `connect()`: the ordering test above went red as intended and then this one killed the
        # run mid-report. So the precondition is checked rather than assumed, and a broken
        # ordering fails here as an assertion like anything else.
        installed = signal.getsignal(signal.SIGTERM)
        assert installed is worker_script._interrupt, (
            f"nothing safe to raise: SIGTERM is still {installed!r} inside `connect()`, so the "
            "signal would kill the interpreter outright rather than unwind"
        )
        signal.raise_signal(signal.SIGTERM)
        # CPython runs the handler between bytecodes, not inside `raise_signal`, so the
        # exception appears a beat later - here, standing in for the rest of a real `connect`.
        for _ in range(100):
            pass
        reached_the_end_of_connect.append(True)
        return _FakeConn(closed)

    _stub_main_dependencies(monkeypatch, connect=connect_that_gets_signalled)
    monkeypatch.setattr(worker_script, "run", lambda *_a, **_k: entered_run.append(True))

    # CAUGHT AT THE TEST BOUNDARY, not left to propagate. An interrupt escaping `main()` is the
    # exact regression this test is for, and pytest reads a loose `KeyboardInterrupt` as the
    # OPERATOR interrupting the session: it aborts the run where it stands, reports the tests it
    # had already finished as passed, and never reaches the ones after this file. Measured, by
    # narrowing the `try` back: no FAILED line anywhere, just a truncated session. Catching it
    # turns that into one red test with a message.
    escaped: list[BaseException] = []
    try:
        worker_script.main([])
    except BaseException as exc:  # noqa: BLE001 - the escape IS the failure being reported
        escaped.append(exc)

    assert escaped == [], (
        f"a signal during startup propagated out of `main()` as {escaped[0]!r} instead of "
        "reaching its quiet `except` - the operator gets a traceback and a non-zero exit for "
        "stopping a worker that had not even connected yet"
        if escaped
        else ""
    )
    assert reached_the_end_of_connect == [], (
        "the signal was delivered but nothing raised - the handler was not installed yet, or it "
        "was installed as something that does not interrupt"
    )
    assert entered_run == [], (
        "the startup signal did not stop startup - `main` went on to enter the poll loop with a "
        "connection the interrupt was supposed to have unwound past"
    )
    assert closed == [], "nothing was ever opened, so nothing should have been closed"


def test_the_handler_is_registered_once_per_start_and_replaces_rather_than_stacks(
    monkeypatch, restore_sigterm
):
    """Restarting the worker must not leave two handlers, or a handler wrapping a handler.

    A worker is a process an operator stops and starts repeatedly - that is the whole premise of
    issue #82 - so `main()` running again in a process that has already run it (a supervisor
    importing and calling it, a test session, a future in-process restart) must be idempotent.
    `signal.signal` REPLACES a disposition, so it is; what this pins is that nothing in the
    script has grown a "chain the previous handler" pattern, where each start adds a frame and a
    Ctrl-C after N restarts raises N times.

    Two assertions, because they fail differently: exactly ONE registration per `main()` (a
    second call inside one start would be dead code hiding a real second registration
    elsewhere), and the disposition after two starts is still `_interrupt` ITSELF - not a wrapper
    that happens to call it.
    """
    registrations: list[int] = []
    real_signal = signal.signal

    def recording_signal(signum, handler):
        registrations.append(int(signum))
        return real_signal(signum, handler)

    monkeypatch.setattr(signal, "signal", recording_signal)
    _stub_main_dependencies(monkeypatch, connect=lambda: _FakeConn([]))

    worker_script.main([])
    assert registrations == [int(signal.SIGTERM)], "one start must register exactly one handler"

    previous = signal.getsignal(signal.SIGTERM)
    worker_script.main([])

    assert registrations == [int(signal.SIGTERM)] * 2
    assert previous is worker_script._interrupt
    assert signal.getsignal(signal.SIGTERM) is worker_script._interrupt, (
        "a restart left something other than `_interrupt` installed - a chained or wrapped "
        "handler means the next Ctrl-C unwinds once per start the process has ever made"
    )


# ---------------------------------------------------------------------------------------------
# The CLI (issue #109 PR 1)
# ---------------------------------------------------------------------------------------------


def _spy_on_run(monkeypatch, *, connected: list[bool] | None = None) -> dict:
    """Drive `main` with every real dependency stubbed and hand back the kwargs `run` received.

    `run` RECORDS AND RETURNS rather than raising, unlike `_stub_main_dependencies` above: these
    tests are about the arguments, and a stub that raised would leave the recorded dict correct
    but the exit path entangled with the thing being measured.
    """
    seen: dict = {}

    def recording_run(_conn, **kwargs):
        seen.update(kwargs)

    def connect():
        if connected is not None:
            connected.append(True)
        return _FakeConn([])

    monkeypatch.setattr(worker_script, "connect", connect)
    monkeypatch.setattr(worker_script, "OpenAIEmbeddings", lambda: object())
    monkeypatch.setattr(worker_script, "run", recording_run)
    return seen


def test_no_flags_runs_exactly_what_it_ran_before_the_cli_existed(monkeypatch, restore_sigterm):
    """A CLI must not change the no-argument behaviour it was added to, and this reads WHY.

    Compared against `run`'s OWN SIGNATURE DEFAULTS rather than against numbers written here.
    That is the assertion that survives a retune: if someone shortens `DEFAULT_LEASE_SECONDS`,
    a test pinning 900 goes red for a change that was correct, while this one keeps asking the
    real question - does a flagless start still run the library's defaults?

    WHAT THIS TEST DOES NOT CATCH, measured rather than assumed: a default retyped as a literal
    that still AGREES with the library. Replacing `default=DEFAULT_LEASE_SECONDS` with
    `default=900` leaves THIS test green; `default=901` turns it red. So the comparison catches a
    default that has DRIFTED - the retyped literal's eventual symptom, one library retune later -
    and not the retyping itself. The retyping is caught elsewhere in this file, by
    `test_a_retuned_library_constant_reaches_the_flagless_worker`, which retunes the library and
    checks the worker follows - and `test_the_real_script_entry_point_follows_a_retune`, which
    asks the same question of the real subprocess. Either is why `default=900` reddens the FILE
    while leaving this test green.

    THE ABSENT KEYS ARE HALF THE TEST. `max_attempts` and `heartbeat_fraction` are deliberately
    not flags - the retry budget is ratified policy (ADR 0028 §1) and the beat cadence is derived
    from the lease on purpose (ADR 0031 §5) - so this script must not pass them at all. Passing
    them "with the default value" would look identical today and freeze the library's own
    defaults into a script the next retune would not reach.
    """
    seen = _spy_on_run(monkeypatch)

    worker_script.main([])

    defaults = {
        name: p.default
        for name, p in inspect.signature(worker_lib.run).parameters.items()
        if p.default is not inspect.Parameter.empty
    }
    assert seen["lease_seconds"] == defaults["lease_seconds"]
    assert seen["poll_seconds"] == defaults["poll_seconds"]
    assert seen["heartbeat_max_seconds"] == defaults["heartbeat_max_seconds"]
    assert seen["chunk_size"] == CHUNK_SIZE_WORDS
    assert seen["chunk_overlap"] == CHUNK_OVERLAP_WORDS
    assert "max_attempts" not in seen, (
        "the script now passes the retry budget itself - a library retune of "
        "DEFAULT_MAX_ATTEMPTS would no longer reach the worker (ADR 0028 §1)"
    )
    assert "heartbeat_fraction" not in seen, (
        "the script now passes the beat cadence itself - the fraction is derived from the lease "
        "on purpose so the two cannot drift (ADR 0031 §5)"
    )


def test_every_flag_reaches_the_loop_that_the_defect_said_it_could_not(
    monkeypatch, restore_sigterm
):
    """The defect #109 names, inverted: a non-default value must arrive at `run`, not be ignored.

    On the base tree `main()` took no arguments at all, so `--lease 30` was not "rejected" - it
    was accepted by the shell, dropped by Python, and the worker polled on the 15-minute default
    while the caller believed otherwise. Silence is what made it worth a ticket: a flag that
    errors is a typo, a flag that is ignored is a wrong belief about a running process.

    All five at once, with values that are individually valid and share no digits with any
    library default, so a stub that happened to echo a default cannot pass this.
    """
    seen = _spy_on_run(monkeypatch)

    worker_script.main(
        [
            "--lease",
            "37",
            "--poll",
            "0.25",
            "--chunk-size",
            "111",
            "--chunk-overlap",
            "7",
            "--heartbeat-max",
            "1.5",
        ]
    )

    assert seen["lease_seconds"] == 37
    assert seen["poll_seconds"] == 0.25
    assert seen["chunk_size"] == 111
    assert seen["chunk_overlap"] == 7
    assert seen["heartbeat_max_seconds"] == 1.5


def _load_worker_fresh():
    """A SECOND, independent load of `scripts/worker.py`, so its module-level `from` imports rebind.

    `from gct.jobs.worker import DEFAULT_LEASE_SECONDS` copies the value at import time, and this
    file imported the script once at collection. Patching the library constant afterwards cannot
    reach that copy - so a test about a RETUNE has to re-execute the module under the patch.
    """
    spec = importlib.util.spec_from_file_location("worker_script_retuned", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("module_name", "constant", "kwarg", "retuned"),
    [
        ("gct.jobs.worker", "DEFAULT_LEASE_SECONDS", "lease_seconds", 613),
        ("gct.jobs.worker", "DEFAULT_POLL_SECONDS", "poll_seconds", 3.25),
        ("gct.ingest.chunk", "CHUNK_SIZE_WORDS", "chunk_size", 271),
        ("gct.ingest.chunk", "CHUNK_OVERLAP_WORDS", "chunk_overlap", 19),
    ],
)
def test_a_retuned_library_constant_reaches_the_flagless_worker(
    monkeypatch, restore_sigterm, module_name, constant, kwarg, retuned
):
    """The PR's headline promise, asked as a QUESTION ABOUT BEHAVIOUR rather than about source.

    The promise is that the library stays the one writer of these numbers (ADR 0018's one-writer
    rule, applied to the queue's numbers rather than to a model id). Which is to say: retune the
    library and the flagless worker follows. So this retunes the library and checks that it does.

    THREE SOURCE-READING DRAFTS PRECEDED THIS AND ALL THREE WERE BROKEN BY EXECUTION, which is
    why the approach changed rather than the pattern being tightened a fourth time. Draft 1 asked
    that each `default=` be an `ast.Name`; a module-local `DEFAULT_LEASE_SECONDS = 900` satisfied
    it. Draft 2 also required the name to appear in a `from gct...` import; leaving the import in
    place and rebinding the name afterwards satisfied it. Draft 3 also forbade the name from
    being a `Store` target anywhere in the file; `parser.set_defaults(lease=900)` is neither an
    `add_argument` call nor an assignment to that name, and argparse applies it anyway - and
    moving the registration into a module-level helper took it out of the inspected function
    entirely. Each draft closed the previous counterexample and left the next one open, because a
    number can be written in more places than a pattern can enumerate.

    Six distinct routes are measured red against this test: an equal-valued literal default; a
    module-local constant, with the import kept or deleted; a rebind local to `_build_parser`;
    `parser.set_defaults`; and a registration extracted to a helper. They fail for one reason
    rather than six: each ends with the worker running a number the library did not supply, and
    that is the thing asserted. The cost is a failure message that says "the
    worker did not follow the retune" rather than naming the line, which is the right trade for a
    property whose danger is that it is invisible in every OTHER observation - a retyped literal
    agrees with the library until the day it does not.

    WHAT THIS STILL DOES NOT REACH, stated rather than implied, because three earlier drafts each
    claimed a closure they did not have. It observes `main([])`. A number introduced outside that
    call - in the `if __name__ == "__main__":` block, which no other test here executes - is
    invisible to it; `test_the_real_script_entry_point_follows_a_retune` drives that path in a
    subprocess. And no test defends against an author actively hiding a knob: a flag registered
    only when its own spelling appears in `argv` escapes any inventory taken from a normal run.
    That is a limit of testing rather than a hole to patch, and saying so beats a fourth claim
    that gets falsified.

    A SECOND MODULE LOAD is what makes this work at all - see `_load_worker_fresh`. The values
    are deliberately unlike any library default and unlike each other, so a stub echoing the
    wrong one cannot pass.
    """
    monkeypatch.setattr(importlib.import_module(module_name), constant, retuned)
    fresh = _load_worker_fresh()

    seen: dict = {}
    monkeypatch.setattr(fresh, "connect", lambda: _FakeConn([]))
    monkeypatch.setattr(fresh, "OpenAIEmbeddings", lambda: object())
    monkeypatch.setattr(fresh, "run", lambda _conn, **kwargs: seen.update(kwargs))

    fresh.main([])

    assert seen[kwarg] == retuned, (
        f"the library was retuned to {constant}={retuned} and the flagless worker ran "
        f"{seen[kwarg]!r} instead. Something between the library and `run` is holding a number "
        "of its own - a literal, a rebound name, a `set_defaults`, a helper - and it will keep "
        "holding it after every future retune"
    )


def test_the_flag_set_is_closed_on_the_parser_that_actually_parses(monkeypatch, restore_sigterm):
    """Which flags exist, read off the parser that actually parses - not one built to look at.

    An inventory taken from a freshly-called `_build_parser()` sees only what that function
    registers, and `main` holds the returned parser for two more statements before parsing.
    Registering `--max-attempts` there, with `default=None` and threaded to `run` only when
    supplied, left `main([])` byte-identical and the whole suite green while
    `worker.py --max-attempts 2` really did override the retry budget - which ADR 0028 1
    ratifies as policy precisely so that it is not a deployment knob. So the parser is captured
    at the moment it is asked to parse, which is the last point anything can have been added.

    Asserted as an EQUALITY, so a deleted flag fails too; a set of required flags would be
    satisfied by a parser that had grown three more.
    """
    captured: dict = {}
    real_parse_args = argparse.ArgumentParser.parse_args

    def capturing_parse_args(self, args=None, namespace=None):
        captured["parser"] = self
        return real_parse_args(self, args, namespace)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capturing_parse_args)
    _spy_on_run(monkeypatch)

    worker_script.main([])

    exposed = {option for action in captured["parser"]._actions for option in action.option_strings}
    assert exposed == {
        "-h",
        "--help",
        "--lease",
        "--poll",
        "--chunk-size",
        "--chunk-overlap",
        "--heartbeat-max",
        "--log-level",
    }

    # And the refusal, end to end: whatever route a future flag is registered by, one that was
    # never registered has to be rejected rather than ignored.
    with pytest.raises(SystemExit) as refused:
        worker_script.main(["--max-attempts", "2"])
    assert refused.value.code == 2


def test_the_real_script_entry_point_follows_a_retune(tmp_path):
    """The `if __name__ == "__main__":` line, which every OTHER test in this file skips.

    They all import the module under a different name and drive `main` IN-PROCESS - some with
    `[]`, some with flags - so the two lines that run when an operator types
    `python scripts/worker.py` are never executed by any of them. The gap is not
    theoretical: planting `main(sys.argv[1:] or ["--lease", "900"])` in that block leaves the
    whole suite green while the real flagless command ignores a retuned library.

    RUN WITH NO ARGUMENTS, which is the point and was the second draft of this test. A first
    draft used `--help` and that same mutant defeated it: `sys.argv[1:]` is then `["--help"]`,
    which is truthy, so the planted fallback never fires. Only the argument-free invocation
    exercises the path the runbook line actually takes.

    `sitecustomize.py` on `PYTHONPATH` is how the retune and the stubs cross the process
    boundary - Python imports it before the script, so patching `gct.jobs.worker` there is seen
    by the script's own `from gct.jobs.worker import ...`. Monkeypatching cannot cross a process,
    and editing the library to test it would be testing the edit. Stubbing `run`, `connect` and
    the embedder keeps this offline and free: no database, no provider, no poll loop.
    """
    (tmp_path / "sitecustomize.py").write_text(
        """
import json
import types

import gct.db
import gct.jobs.worker as w
import gct.providers.openai_provider as prov

w.DEFAULT_LEASE_SECONDS = 613
gct.db.connect = lambda: types.SimpleNamespace(autocommit=False, close=lambda: None)
prov.OpenAIEmbeddings = lambda: None


def _fake_run(_conn, **kwargs):
    kwargs.pop("embedder", None)
    print("RUN_KWARGS " + json.dumps(kwargs))
    raise SystemExit(0)


w.run = _fake_run
"""
    )
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "OPENAI_API_KEY": "sk-not-used"}

    flagless = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=_SCRIPT.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert flagless.returncode == 0, flagless.stderr
    handed = json.loads(
        next(
            line[len("RUN_KWARGS ") :]
            for line in flagless.stdout.splitlines()
            if line.startswith("RUN_KWARGS ")
        )
    )
    # The WHOLE kwarg set, not just the retuned one. A first draft asserted only the lease, and a
    # mutant appending `--heartbeat-max 3600` in the `__main__` guard sailed past it - resolving
    # in the script the cap ADR 0031 5 says must be derived from the lease in use.
    assert handed == {
        "chunk_size": CHUNK_SIZE_WORDS,
        "chunk_overlap": CHUNK_OVERLAP_WORDS,
        "lease_seconds": 613,
        "poll_seconds": worker_lib.DEFAULT_POLL_SECONDS,
        "heartbeat_max_seconds": None,
    }, (
        "the REAL flagless entry point handed `run` something the library did not supply. Every "
        "other test in this file drives `main` IN-PROCESS and so never executes the `__main__` "
        f"guard this one runs through. Got: {handed}"
    )

    # ...and the number an operator READS. A previous defeat route left `--help` advertising 900
    # while the loop ran something else, so the printed default earns its own assertion.
    helped = subprocess.run(
        [sys.executable, str(_SCRIPT), "--help"],
        cwd=_SCRIPT.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert helped.returncode == 0, helped.stderr
    assert "613" in helped.stdout and "900" not in helped.stdout


def test_the_two_short_cap_shapes_validate_declares_legal_are_accepted(
    monkeypatch, restore_sigterm
):
    """`_validate` names two combinations as deliberately legal; nothing else holds them open.

    THE ONE WITH A CALLER is a cap at or under a very short LEASE, which is what forces the
    heartbeat to stop renewing while a run is still going - the only way to stage a reclaim.
    `ingest_smoke` pairs a 1.0s cap with a 1s lease for exactly that
    (`PHASE_THREE_HEARTBEAT_CAP_SECONDS`, `DEFAULT_SHORT_LEASE_SECONDS`), and it is the shape
    `--heartbeat-max` was added for.

    THE ONE WITHOUT A CALLER is a cap under one poll interval, and it is pinned anyway because
    `_validate`'s docstring declares it legal: a rule refusing it would be invented rather than
    derived, since the poll governs how long an EMPTY tick sleeps and has no bearing on how long
    one job may keep renewing. An earlier draft of this test justified it by claiming
    `ingest_smoke` needed it - it does not, its poll is 0.05s and its cap is twenty times that.
    Measured before it was written down this time.

    Asserted as ACCEPTANCES - the values reaching `run` - rather than as "no SystemExit": a
    parser that swallowed the flag would also not raise.
    """
    seen = _spy_on_run(monkeypatch)
    worker_script.main(["--heartbeat-max", "1.0", "--lease", "1"])
    assert seen["heartbeat_max_seconds"] == 1.0
    assert seen["lease_seconds"] == 1
    assert seen["heartbeat_max_seconds"] <= seen["lease_seconds"], (
        "the first half is only meaningful while the cap it passes is AT OR UNDER the lease - "
        "that is the shape that stops the heartbeat inside a run"
    )

    seen = _spy_on_run(monkeypatch)
    worker_script.main(["--heartbeat-max", "1.0"])
    assert seen["poll_seconds"] == worker_lib.DEFAULT_POLL_SECONDS, (
        "the second half is only meaningful while the cap it passes is UNDER the default poll"
    )
    assert seen["heartbeat_max_seconds"] == 1.0
    assert seen["heartbeat_max_seconds"] < seen["poll_seconds"]


def test_a_retuned_lease_leaves_the_heartbeat_cap_for_the_library_to_derive(
    monkeypatch, restore_sigterm
):
    """`--lease` alone must hand `run` a `None` cap, not the DEFAULT lease's cap.

    This is the one place a flag could reintroduce a bug the library already fixed. The cap is
    specified in units of the lease - `HEARTBEAT_CAP_LEASES` of the lease ACTUALLY IN USE (ADR
    0031 §5) - and `run` resolves it from `None`. A script that instead defaulted the flag to
    `DEFAULT_HEARTBEAT_MAX_SECONDS` would pin the cap to the default lease's four leases, so
    `--lease 60` would beat for an hour: sixty leases, not four. The comment beside
    `HEARTBEAT_CAP_LEASES` records that exact drift as having already happened once, which is
    why it is pinned from the caller's side too.

    A SCRIPT THAT STOPPED PASSING THE ARGUMENT is caught here too, and not by a second
    assertion: `_spy_on_run` records `run`'s kwargs in a dict, so the omitted argument makes the
    lookup below raise `KeyError` before any comparison happens. An earlier draft of this
    docstring claimed the opposite and defended a `!= DEFAULT_HEARTBEAT_MAX_SECONDS` line with
    it; that line could never fail, because `None != 3600.0` is unconditionally true once the
    assertion above it has passed. It is gone rather than reworded - an assertion presented to
    the reader as coverage, which cannot fail, is worse than no assertion.
    """
    seen = _spy_on_run(monkeypatch)

    worker_script.main(["--lease", "60"])

    assert seen["lease_seconds"] == 60
    assert seen["heartbeat_max_seconds"] is None, (
        "the cap was resolved in the script instead of in `run`, so it no longer tracks the "
        "lease actually in use (ADR 0031 §5)"
    )


@pytest.mark.parametrize(
    ("argv", "remedy"),
    [
        (["--lease", "0"], "--lease must be at least 1 second"),
        (["--lease", "-5"], "--lease must be at least 1 second"),
        (["--poll", "0"], "--poll must be a finite number greater than 0"),
        (["--poll", "-1"], "--poll must be a finite number greater than 0"),
        # `nan` and `inf` parse as floats and reach `time.sleep`. `nan` is the sharp one: no
        # comparison with it is ever true, so a bare `<= 0` lets it straight through and the
        # worker dies on its first EMPTY tick - after reporting itself started.
        (["--poll", "nan"], "--poll must be a finite number greater than 0"),
        (["--poll", "inf"], "--poll must be a finite number greater than 0"),
        (["--chunk-overlap", "250", "--chunk-size", "250"], "--chunk-overlap must be at least 0"),
        (["--chunk-overlap", "-1"], "--chunk-overlap must be at least 0"),
        (["--heartbeat-max", "0"], "--heartbeat-max must be a finite number greater than 0"),
        (["--heartbeat-max", "nan"], "--heartbeat-max must be a finite number greater than 0"),
    ],
)
def test_an_unrunnable_value_is_refused_before_anything_is_opened(
    monkeypatch, restore_sigterm, capsys, argv, remedy
):
    """Refuse, do not convert - and refuse EARLY, before a student's file is ever claimed.

    A worker is a long-lived process holding other people's uploads, so a bad flag does not fail
    the operator who typed it. `overlap >= size` is the worst of these and it is the reason the
    check is here rather than left to the library: `chunk_units` raises a plain `ValueError`,
    `process_one` classifies an unclassified exception as TRANSIENT (ADR 0020 §1, DB-blip class
    amended per ADR 0028), so every file the worker claims is retried the whole budget and then
    buried `transient_exhausted` - surfacing to the student as `failed`, with nothing in the row
    pointing at a flag. A startup refusal cannot reach a student at all.

    FOUR ASSERTIONS, and they fail differently: the process exits non-zero (a supervisor or a
    harness has to be able to tell), the message NAMES the flag and its remedy rather than
    echoing a traceback, and `connect` was never called - proof the refusal happened at the
    boundary and not somewhere downstream that had already touched the database.
    """
    connected: list[bool] = []
    _spy_on_run(monkeypatch, connected=connected)

    with pytest.raises(SystemExit) as exit_info:
        worker_script.main(argv)

    assert exit_info.value.code == 2
    message = capsys.readouterr().err
    assert remedy in message, f"the refusal did not name the flag or its remedy: {message!r}"
    assert "omit" in message, "a refusal that does not say what to do instead is a stack trace"
    assert connected == [], "the run reached the database before refusing an unrunnable flag"


def test_the_log_level_is_a_choice_with_the_old_hardcoded_level_as_its_default(
    monkeypatch, restore_sigterm
):
    """INFO by default (unchanged), and settable - which is the direction a harness needs.

    `--log-level` rather than `ingest_smoke`'s `--verbose`, because the boolean would mean the
    wrong thing here: that script's `--verbose` lifts WARNING to INFO, while this one already
    configured INFO, and `gct` emits nothing below it. The demonstrated need is the other
    direction - a launch harness that owns the terminal wanting the worker quiet (#109 PR 2) -
    and a boolean cannot express it.

    `basicConfig` is spied for the first half, because what is under test there is the level the
    script CHOOSES. The second half lets it run and reads the root logger back, because a choice
    is not an outcome - a later `setLevel` could undo it and the spy would never know.
    """
    levels: list[object] = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: levels.append(kwargs.get("level")))
    _spy_on_run(monkeypatch)

    worker_script.main([])
    worker_script.main(["--log-level", "WARNING"])

    assert levels == [worker_script.DEFAULT_LOG_LEVEL, "WARNING"]

    monkeypatch.undo()  # the real `basicConfig` back; the spy above has served its purpose
    _spy_on_run(monkeypatch)
    root = logging.getLogger()
    was_level, was_handlers = root.level, root.handlers[:]
    # `basicConfig` DOES NOTHING when the root logger already has handlers, and under pytest it
    # always does. Clearing them is not a convenience - it reproduces the only state this line
    # ever runs in, a fresh worker process - and without it the assertion below would pass or
    # fail on pytest's logging plugin rather than on the script.
    root.handlers.clear()
    try:
        worker_script.main([])
        assert root.level == logging.INFO, (
            f"the root logger ended at {root.level}, not INFO. A worker started the way every "
            "runbook line starts it would print less than the operator was told to expect"
        )
    finally:
        root.setLevel(was_level)
        root.handlers[:] = was_handlers
    assert worker_script.DEFAULT_LOG_LEVEL == "INFO", (
        "the flagless default log level moved - a worker started the way every runbook line "
        "starts it now says a different amount"
    )


def test_an_unknown_level_is_refused_by_argparse_rather_than_by_logging(
    monkeypatch, restore_sigterm
):
    """`choices` refuses at the boundary; `basicConfig` would have raised after startup began.

    Worth its own test because the failure mode it removes is not a crash but a LATE crash: an
    unrecognised level reaches `logging.basicConfig` as a `ValueError` raised after the parse,
    which reads as a defect in the worker rather than a typo in a flag.
    """
    connected: list[bool] = []
    _spy_on_run(monkeypatch, connected=connected)

    with pytest.raises(SystemExit) as exit_info:
        worker_script.main(["--log-level", "CHATTY"])

    assert exit_info.value.code == 2
    assert connected == []
