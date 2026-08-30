"""Run the ingestion worker - a separate OS process polling the job queue (ADR 0011 addendum).

Thin peer caller (ADR 0009): every decision lives in `gct.jobs.worker`; this file only wires
the real dependencies - the connection, the real embedder, the chunk window - and starts the
loop.

Usage:  uv run python scripts/worker.py
"""

import logging
import signal
from types import FrameType

from gct.db import connect
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.jobs.worker import run
from gct.providers.openai_provider import OpenAIEmbeddings


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


def main() -> None:
    # The library only EMITS events (`logging.getLogger(__name__)`); deciding where they go and
    # at what level is the application's call, and this script is the application (ADR 0009).
    # Without this the worker's info lines vanish - Python's default level is WARNING.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Registered BEFORE the connection is opened, so a SIGTERM arriving during startup still
    # takes the quiet path rather than the default kill. `signal.signal` is main-thread-only,
    # which `main()` is by construction.
    signal.signal(signal.SIGTERM, _interrupt)
    conn = connect()
    # Autocommit is defense in depth here, not load-bearing: every writer on the worker path
    # commits itself inside its own `conn.transaction()`, so a plain connection works end to
    # end (ADR 0025, guarded per ADR 0027; the plain-connection test in test_worker.py proves
    # it). Kept because it protects a future statement added outside a transaction block from
    # silently opening the implicit transaction. ask_smoke.py's wiring comment carries the
    # original argument for the mode.
    conn.autocommit = True
    try:
        run(
            conn,
            # The embedder constructs itself from config (ADR 0018: never hardcode a model id);
            # the chunk window is threaded explicitly because the names are provisional spike
            # parameters (ADR 0019) - see `process_one`'s docstring.
            embedder=OpenAIEmbeddings(),
            chunk_size=CHUNK_SIZE_WORDS,
            chunk_overlap=CHUNK_OVERLAP_WORDS,
        )
    except KeyboardInterrupt:
        # Ctrl-C, or the SIGTERM `_interrupt` routes onto the same stack. Either way the run
        # committed nothing (ADR 0020), and `process_one`'s shutdown guard has already handed
        # the in-flight job back to `queued` on the way out, so the next start claims it at once
        # instead of waiting out its lease (ADR 0028 §Consequences, shutdown-release bullet).
        # Nothing left to do here but exit quietly: an operator who stopped the worker does not
        # need a traceback, and the exception has already done its work upstream.
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    main()
