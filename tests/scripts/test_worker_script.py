"""Tests for `scripts/worker.py` - the SIGTERM wiring, and nothing else (issue #82).

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
"""

from __future__ import annotations

import importlib.util
import signal
from pathlib import Path

import pytest

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

    worker_script.main()

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

    worker_script.main()

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
        worker_script.main()
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

    worker_script.main()
    assert registrations == [int(signal.SIGTERM)], "one start must register exactly one handler"

    previous = signal.getsignal(signal.SIGTERM)
    worker_script.main()

    assert registrations == [int(signal.SIGTERM)] * 2
    assert previous is worker_script._interrupt
    assert signal.getsignal(signal.SIGTERM) is worker_script._interrupt, (
        "a restart left something other than `_interrupt` installed - a chained or wrapped "
        "handler means the next Ctrl-C unwinds once per start the process has ever made"
    )
