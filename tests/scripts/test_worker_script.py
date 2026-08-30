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
