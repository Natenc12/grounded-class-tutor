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

import ast
import importlib.util
import inspect
import logging
import signal
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

    WHAT IT DOES NOT CATCH, measured rather than assumed: a default retyped as a literal that
    still AGREES with the library. Replacing `default=DEFAULT_LEASE_SECONDS` with `default=900`
    leaves this whole file green; `default=901` turns it red. So this comparison catches a
    default that has DRIFTED - which is the retyped literal's eventual symptom, one library
    retune later - and not the retyping itself. The retyping is caught by
    `test_every_flag_default_is_a_name_and_never_a_retyped_literal`, which reads the source.

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


def test_every_flag_default_is_a_name_and_never_a_retyped_literal():
    """The PR's headline promise, enforced on the SOURCE rather than on the value.

    Every `default=` in `_build_parser` must be an imported name, so the library stays the one
    writer of these numbers (ADR 0018's one-writer rule, applied to the queue's numbers rather
    than to a model id). A value comparison cannot enforce that:
    `test_no_flags_runs_exactly_what_it_ran_before_the_cli_existed` reads both sides and both
    sides are the same number today, so `default=900` passes there and only goes red one library
    retune later - after a student's worker has already run on the stale number.

    So this reads the text instead. `ast` and not a regex: `default=` appears inside help strings
    in this file, and a regex would either match those or need to know how they are quoted.

    `None` is the one allowed literal, and it is allowed for a REASON rather than as an
    exception: `--heartbeat-max` must reach `run` as `None` so the cap is derived from the lease
    actually in use (ADR 0031 5). Writing `DEFAULT_HEARTBEAT_MAX_SECONDS` there would be the
    imported name this test asks for and would still be the bug - which is why
    `test_a_retuned_lease_leaves_the_heartbeat_cap_for_the_library_to_derive` exists as well.
    Neither test subsumes the other.
    """
    parser_source = next(
        node
        for node in ast.parse(_SCRIPT.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "_build_parser"
    )
    literal_defaults = [
        (call.args[0].value if call.args else "?", ast.unparse(keyword.value))
        for call in ast.walk(parser_source)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "add_argument"
        for keyword in call.keywords
        if keyword.arg == "default"
        and not isinstance(keyword.value, ast.Name)
        and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)
    ]

    assert literal_defaults == [], (
        f"these flags spell their default out instead of importing it: {literal_defaults}. "
        "A literal that agrees with the library today is invisible until the library moves, "
        "and then the worker silently keeps the old number"
    )

    # The other direction, so this cannot pass by finding no flags at all: the flags that DO
    # carry a name must still be there. A `_build_parser` that stopped calling `add_argument`
    # would satisfy the assertion above trivially.
    named_defaults = [
        ast.unparse(keyword.value)
        for call in ast.walk(parser_source)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "add_argument"
        for keyword in call.keywords
        if keyword.arg == "default" and isinstance(keyword.value, ast.Name)
    ]
    assert sorted(named_defaults) == [
        "CHUNK_OVERLAP_WORDS",
        "CHUNK_SIZE_WORDS",
        "DEFAULT_LEASE_SECONDS",
        "DEFAULT_LOG_LEVEL",
        "DEFAULT_POLL_SECONDS",
    ]


def test_a_heartbeat_cap_under_one_poll_interval_is_accepted_on_purpose(
    monkeypatch, restore_sigterm
):
    """`_validate` calls this combination deliberately legal; nothing else holds it open.

    It is the only configuration that can force a lease to lapse INSIDE a run, which is what a
    reclaim has to be staged from - `ingest_smoke` runs a 1.0s cap under the default 2.0s poll
    for exactly that reason (`PHASE_THREE_HEARTBEAT_CAP_SECONDS`). Without this test a later
    tightening of the validator can refuse the combination with the whole suite still green,
    quietly removing the reason `--heartbeat-max` was added at all.

    Asserted as an ACCEPTANCE - the value reaching `run` - rather than as "no SystemExit": a
    parser that swallowed the flag would also not raise.
    """
    seen = _spy_on_run(monkeypatch)

    worker_script.main(["--heartbeat-max", "1.0"])

    assert seen["poll_seconds"] == worker_lib.DEFAULT_POLL_SECONDS, (
        "this test is only meaningful while the cap it passes is UNDER the default poll"
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
        (["--poll", "0"], "--poll must be greater than 0"),
        (["--poll", "-1"], "--poll must be greater than 0"),
        (["--chunk-overlap", "250", "--chunk-size", "250"], "--chunk-overlap must be at least 0"),
        (["--chunk-overlap", "-1"], "--chunk-overlap must be at least 0"),
        (["--heartbeat-max", "0"], "--heartbeat-max must be greater than 0"),
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

    THREE ASSERTIONS, and they fail differently: the process exits non-zero (a supervisor or a
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

    `basicConfig` is spied rather than allowed to run: it mutates the pytest process's root
    logger, and what is under test is the level this script CHOOSES, not `logging`'s behaviour.
    """
    levels: list[object] = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: levels.append(kwargs.get("level")))
    _spy_on_run(monkeypatch)

    worker_script.main([])
    worker_script.main(["--log-level", "WARNING"])

    assert levels == [worker_script.DEFAULT_LOG_LEVEL, "WARNING"]
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
