"""Run the ingestion worker - a separate OS process polling the job queue (ADR 0011 addendum).

Thin peer caller (ADR 0009): every decision lives in `gct.jobs.worker`; this file only wires
the real dependencies - the connection, the real embedder, the chunk window - and starts the
loop.

THE FLAGS ARE WIRING, NOT POLICY, and that is what keeps them on the right side of ADR 0009.
Every LIBRARY-SOURCED default below is the library's own constant, read by name and never
retyped as a literal - the same one-writer rule ADR 0018 states for the model id, applied to the
queue's numbers. So a run with no flags calls `run` with the values `run` would have defaulted to
anyway, and retuning a library number moves this script with it instead of leaving a stale copy
behind. Two flags are deliberately not library-sourced and `_build_parser` says why: `--log-level`
defaults to a script-local constant because the level is the application's call (ADR 0009), and
`--heartbeat-max` defaults to `None` because `run` must derive the cap from the lease in use
(ADR 0031 5).

WHY A CLI AT ALL. ADR 0011's PM-3 addendum mandates a separate OS process, but until #109 this
`main()` took no arguments, so a subprocess was pinned to the module defaults - which is exactly
why `scripts/ingest_smoke.py` runs its worker on a THREAD rather than as the process the ADR
describes. A caller that has to fake the topology to configure it does not get to demonstrate
the topology. `--heartbeat-max` is here for the same reason and no other: since ADR 0031 a live
worker renews its own lease, so `--lease` alone can no longer stage an expiry (`ingest_smoke`'s
`PHASE_THREE_HEARTBEAT_CAP_SECONDS` carries that argument).

Usage:
    uv run python scripts/worker.py
    uv run python scripts/worker.py --lease 30 --poll 0.5
    uv run python scripts/worker.py --log-level WARNING     # quiet, for a harness that owns
                                                            # the terminal (#109 PR 2)
"""

import argparse
import logging
import math
import signal
from collections.abc import Sequence
from types import FrameType

from gct.db import connect
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.jobs.worker import DEFAULT_LEASE_SECONDS, DEFAULT_POLL_SECONDS, run
from gct.providers.openai_provider import OpenAIEmbeddings

# The level `main` configures when nothing asks for another one. Named rather than inlined
# because two places have to agree on it - the parser's default and the help string that
# announces it - and because it is the value the "no flags behaves exactly as before" test
# compares against.
DEFAULT_LOG_LEVEL = "INFO"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _interrupt(signum: int, _frame: FrameType | None) -> None:
    """Turn SIGTERM into the unwind Ctrl-C already produces (issue #82).

    ROUTING, NOT DECIDING - the ADR 0009 line this file has to stay on. What a stopped worker
    does about its in-flight job is `gct.jobs.worker`'s call, and it has to be: the `Job` and
    its `lease_token` are locals of `process_one`, invisible from here. All this does is make
    SIGTERM arrive as an exception on the same stack SIGINT already does, so
    `_release_on_shutdown` runs for both.

    Python's DEFAULT SIGTERM disposition is what makes this necessary: the interpreter dies in
    the C handler, no frame unwinds, and no `except`/`finally` anywhere in the process runs.
    SIGINT is the special case - the interpreter already raises `KeyboardInterrupt` for it -
    and this makes the two signals equal rather than inventing a second shutdown path.

    `KeyboardInterrupt` rather than a bespoke class or `SystemExit`, for two reasons: the guard
    in `gct.jobs.worker` catches `BaseException` so any of them would reach it, and the `except`
    below already says what this process does about an operator-requested stop - exits quietly,
    no traceback, status 0. A separate type would need that statement written twice.

    Signal delivery is not preemptive: CPython runs the handler between bytecodes in the main
    thread, so one blocked in a C call (psycopg waiting on a socket, `time.sleep` serving a
    backoff) unwinds when that call returns rather than instantly. Late is fine - `release` only
    needs the process to still be alive to make its one write.

    SIGKILL is NOT in scope and cannot be: `kill -9`, an OOM kill and a power cut run no handler
    at all. Those still cost the full lease, and the reaper is still the only thing that
    collects them.
    """
    raise KeyboardInterrupt(f"signal {signum}")


def _build_parser() -> argparse.ArgumentParser:
    """The CLI surface, separated from parsing so a test can read what the flags actually SAY.

    Split out for the reason `ingest_smoke._build_parser` gives: help text is documentation that
    ships inside the program, it drifts exactly like a comment, and unlike a comment a user acts
    on it. Every LIBRARY-SOURCED `default=` here is an imported name, so a help string that
    interpolates one cannot go stale. The two that are not - `--log-level`, whose level is the
    application's call under ADR 0009 and has no library constant to import, and
    `--heartbeat-max`, which must stay `None` so `run` derives the cap from the lease in use
    (ADR 0031 §5) - are the two exceptions, and they are the ones listed in the module docstring
    above. `--heartbeat-max` states its derivation in its own `help=`; `--log-level`'s reason is
    in `main`'s comment beside `basicConfig`, not in its help, because an operator choosing a
    level does not need the ADR.

    WHAT IS DELIBERATELY NOT A FLAG. `run` also takes `max_attempts` and `heartbeat_fraction`,
    and both are left off:

      - `max_attempts` is the retry BUDGET, a ratified policy number (ADR 0028 §1) rather than a
        deployment knob. A flag would let an operator quietly run a worker that gives up sooner
        or later than the ADR specifies, and the resulting `transient_exhausted` on a student's
        file would be unattributable to anything in the row.
      - `heartbeat_fraction` is the beat CADENCE, and the cadence is deliberately derived from
        the lease so the two cannot drift (`heartbeat_interval`, ADR 0031 §5). A caller that
        wants a shorter beat already gets one by shortening `--lease`. The cap is different -
        it is the number `ingest_smoke` had to thread by hand to reach an expiry - so the cap
        gets a flag and the fraction does not.

    Neither has ever been threaded by any SCRIPT in the tree - the tests thread both, which is
    what ADR 0031 5 made them parameters for. The bar for a flag here is a demonstrated caller
    outside the suite, not the existence of a parameter.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--lease",
        type=int,
        default=DEFAULT_LEASE_SECONDS,
        metavar="SECONDS",
        help=f"how long a claim holds a job before the reaper may take it (default "
        f"{DEFAULT_LEASE_SECONDS}, the library's own constant). Shortening it also shortens the "
        "heartbeat beat and cap, which derive from it",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        metavar="SECONDS",
        help=f"sleep after an EMPTY tick (default {DEFAULT_POLL_SECONDS}). A tick that processed "
        "a job polls again immediately, so this never delays real work",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=CHUNK_SIZE_WORDS,
        metavar="WORDS",
        help=f"chunk window in words (default {CHUNK_SIZE_WORDS}, the chunker's own constant)",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=CHUNK_OVERLAP_WORDS,
        metavar="WORDS",
        help=f"chunk overlap in words (default {CHUNK_OVERLAP_WORDS}, the chunker's own "
        "constant). Must be less than --chunk-size",
    )
    parser.add_argument(
        "--heartbeat-max",
        type=float,
        default=None,
        metavar="SECONDS",
        help="cap on how long ONE job may keep renewing its lease (default: derived from "
        "--lease by the library, HEARTBEAT_CAP_LEASES leases). Pass a value only to make a "
        "lease lapse inside a run's lifetime - that is what staging a reclaim needs since "
        "ADR 0031",
    )
    parser.add_argument(
        "--log-level",
        choices=_LOG_LEVELS,
        default=DEFAULT_LOG_LEVEL,
        help=f"level for the root logger (default {DEFAULT_LOG_LEVEL}). WARNING leaves only "
        "reaps and failures, for a harness that owns the terminal; DEBUG adds the client "
        "libraries' own lines, since `gct` itself emits nothing below INFO",
    )
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Refuse values argparse accepts and the run cannot survive. Exits 2 with the remedy.

    ARGPARSE ENFORCES TYPES, NOT RANGES - the same gap `ingest_smoke._validate_args` was written
    for. What makes this worth doing at the BOUNDARY rather than leaving to the library is where
    the failure lands otherwise: a worker is a long-lived process holding other people's files,
    so a bad flag does not fail the operator who typed it, it fails the next student's upload.

    `--chunk-overlap` is the sharpest case and it is measured, not hypothetical. `chunk_units`
    raises a plain `ValueError` on `overlap >= size`, and `process_one` classifies an
    unclassified exception as TRANSIENT (ADR 0020 §1, DB-blip class amended per ADR 0028): the
    job is retried the full budget with backoff and then buried `transient_exhausted`, and
    `files.status` becomes `failed`. So a typo in a flag reaches the student as "your file could
    not be ingested", for every file the worker claims, with nothing in the row pointing back at
    the flag. Refusing at startup costs one line and cannot reach a student at all.

    `--lease 0` or less has already expired when `claim` grants it, so the reaper may hand the
    job to a second worker while the first is still embedding - a double embed, which is money,
    and the exact race the lease exists to prevent.

    `--poll 0` spins a hot loop against Postgres; a negative value raises inside `time.sleep`
    on the first empty tick, i.e. after the worker has reported itself started.

    WHAT IS NOT REFUSED, deliberately: a `--heartbeat-max` below `--lease`, or below one poll
    interval. Both are legitimate and one of them is the only way to stage a reclaim -
    `ingest_smoke` runs a 1.0s cap on purpose, under a default poll of 2.0s. A rule refusing
    those would refuse the single configuration in this tree that needs them.
    """
    if args.lease < 1:
        parser.error(
            f"--lease must be at least 1 second (got {args.lease}); omit it for the library "
            "default. A lease of 0 or less has already expired when `claim` grants it, so the "
            "reaper can hand the job to a second worker mid-embed"
        )
    if not math.isfinite(args.poll) or args.poll <= 0:
        parser.error(
            f"--poll must be a finite number greater than 0 (got {args.poll}); omit it for the "
            "library default. 0 spins a hot loop against Postgres, a negative value raises "
            "inside `time.sleep` on the first empty tick, and `nan`/`inf` reach that same sleep "
            "- `nan` because no comparison with it is ever true, so a bare `<= 0` lets it past"
        )
    if not 0 <= args.chunk_overlap < args.chunk_size:
        parser.error(
            f"--chunk-overlap must be at least 0 and less than --chunk-size (got overlap="
            f"{args.chunk_overlap}, size={args.chunk_size}); omit both for the chunker's own "
            "constants. The chunker refuses this window, and the worker reads that refusal as a "
            "transient fault: every file it claims is retried the whole budget and then marked "
            "`failed` for the student"
        )
    if args.heartbeat_max is not None and (
        not math.isfinite(args.heartbeat_max) or args.heartbeat_max <= 0
    ):
        parser.error(
            f"--heartbeat-max must be a finite number greater than 0 (got {args.heartbeat_max}); "
            "omit it to let "
            "the library derive the cap from --lease. A cap of 0 or less means the lease is "
            "never renewed at all, which is the pre-ADR-0031 behaviour and not a configuration"
        )


def main(argv: Sequence[str] | None = None) -> None:
    """Wire the real dependencies and start the loop. `argv` is `None` for `sys.argv[1:]`.

    THE PARAMETER EXISTS FOR THE TESTS, and it is not optional politeness: this module is loaded
    by path inside a pytest process, so a `main()` that read `sys.argv` unconditionally would
    parse PYTEST's flags - `-q`, `-m`, `not live` - and exit 2 before doing anything. Taking the
    list makes the parse an argument like any other.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate(parser, args)
    # The library only EMITS events (`logging.getLogger(__name__)`); deciding where they go and
    # at what level is the application's call, and this script is the application (ADR 0009).
    # Without this the worker's info lines vanish - Python's default level is WARNING.
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Registered BEFORE the connection is opened, so a SIGTERM arriving during startup takes
    # the quiet path rather than the default kill. `signal.signal` is main-thread-only, which
    # `main()` is by construction. Parsing happens first and that is safe: it touches no I/O and
    # cannot block, so it is not a window an operator waits through and signals into.
    signal.signal(signal.SIGTERM, _interrupt)
    # Bound before the `try` so the `finally` below can ask whether there is anything to close:
    # a signal landing inside `connect()` unwinds through that block with the name unassigned,
    # and a bare `conn.close()` would raise `UnboundLocalError` over the top of the interrupt.
    conn = None
    try:
        # INSIDE the try, so an interrupt during startup exits the same way one during the run
        # does. `connect()` is the slowest step here and the likeliest to hang - a Postgres that
        # is down, a full connection pool - which makes it a window an operator sends a signal
        # into rather than a hypothetical one.
        conn = connect()
        # Autocommit is defense in depth here, not load-bearing: every writer on the worker path
        # commits itself inside its own `conn.transaction()`, so a plain connection works end to
        # end (ADR 0025, guarded per ADR 0027; the plain-connection test in test_worker.py proves
        # it). Kept because it protects a future statement added outside a transaction block from
        # silently opening the implicit transaction. ask_smoke.py's wiring comment carries the
        # original argument for the mode.
        conn.autocommit = True
        run(
            conn,
            # The embedder constructs itself from config (ADR 0018: never hardcode a model id);
            # the chunk window is threaded explicitly because the names are provisional spike
            # parameters (ADR 0019) - see `process_one`'s docstring.
            embedder=OpenAIEmbeddings(),
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
            lease_seconds=args.lease,
            poll_seconds=args.poll,
            # `None` PASSED THROUGH rather than resolved here, and that is the whole point of
            # the flag's default. `run` derives the cap from the lease actually in use; a script
            # that substituted `DEFAULT_HEARTBEAT_MAX_SECONDS` would pin it to the DEFAULT
            # lease's cap, so `--lease 60` would beat for 3600s - sixty leases where ADR 0031 §5
            # specifies four. That exact drift is recorded beside `HEARTBEAT_CAP_LEASES` as
            # having already happened once.
            heartbeat_max_seconds=args.heartbeat_max,
        )
    except KeyboardInterrupt:
        # Ctrl-C, or the SIGTERM `_interrupt` routes onto the same stack. Either way the run
        # committed nothing (ADR 0020), and `process_one`'s shutdown guard has already handed
        # the in-flight job back to `queued` on the way out, so the next start claims it at once
        # instead of waiting out its lease (ADR 0028 §Consequences, shutdown-release bullet).
        # Nothing left to do here but exit quietly: an operator who stopped the worker does not
        # need a traceback, and the exception has already done its work upstream. A signal that
        # arrived during startup reaches here too, with no job claimed and `run` never entered.
        pass
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
