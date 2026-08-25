"""Run the ingestion worker - a separate OS process polling the job queue (ADR 0011 addendum).

Thin peer caller (ADR 0009): every decision lives in `gct.jobs.worker`; this file only wires
the real dependencies - the connection, the real embedder, the chunk window - and starts the
loop.

Usage:  uv run python scripts/worker.py
"""

from gct.db import connect
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.jobs.worker import run
from gct.providers.openai_provider import OpenAIEmbeddings


def main() -> None:
    conn = connect()
    # AUTOCOMMIT is load-bearing, not style (ADR 0025, guarded per ADR 0027): without it the
    # first statement opens psycopg's implicit transaction and every writer downstream refuses
    # the connection. ask_smoke.py's wiring comment carries the full argument.
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
