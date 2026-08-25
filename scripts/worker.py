"""Run the ingestion worker - a separate OS process polling the job queue (ADR 0011 addendum).

Thin peer caller (ADR 0009): every decision lives in `gct.jobs.worker`; this file only wires
the real dependencies - the connection, the real embedder, the chunk window - and starts the
loop.

Usage:  uv run python scripts/worker.py
"""

import logging

from gct.db import connect
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.jobs.worker import run
from gct.providers.openai_provider import OpenAIEmbeddings


def main() -> None:
    # The library only EMITS events (`logging.getLogger(__name__)`); deciding where they go and
    # at what level is the application's call, and this script is the application (ADR 0009).
    # Without this the worker's info lines vanish - Python's default level is WARNING.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
        # Ctrl-C is the V1 shutdown story: killed mid-ingest, the run committed nothing
        # (ADR 0020) and the lease/reaper requeues the job - zero cleanup by design.
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    main()
