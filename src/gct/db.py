"""Postgres access. A thin helper now; the data layer stays local Postgres in V1 and swaps to
Supabase in V2 with no rewrite (C4 / ADR 0006)."""

from __future__ import annotations

import psycopg
from pgvector.psycopg import register_vector

from .config import load_settings


def require_idle(conn: psycopg.Connection, fn: str) -> None:
    """Reject a connection that is already inside a transaction. Called first by every writer
    that wraps its work in `with conn.transaction():`.

    ADR 0025's failure in one line: psycopg opens an implicit transaction on a connection's
    first statement (a bare SELECT is enough), and `conn.transaction()` on an already-open
    connection issues a SAVEPOINT rather than BEGIN - so the write commits NOTHING while the
    function returns successfully. Silent, and invisible to any test that reads back through
    the same connection. This guard converts that silent wrong answer into a loud one at the
    boundary it protects (ADR 0025, guarded per ADR 0027 - adopted across `queue.py`'s writers
    first, then `index_file` via #75; both measurements are in the ADR).

    HOW MANY WRITERS THAT IS IS DELIBERATELY NOT RECORDED HERE. It was five when ADR 0027 measured
    it, and `queue.py` has gained verbs since, so a count written down here is a copy with a second
    writer and it goes stale on the next one - which is how the "five" this line used to carry
    outlived the fact it described. There is no single census: `queue.py`'s verbs are enumerated
    by `test_every_writer_refuses_a_connection_already_in_a_transaction`, and each writer outside
    that module (`index_file`, `create_class`) carries its own refuse test beside it.

    Deliberately NOT caught anywhere: a caller in the wrong transaction state is a programming
    error at wiring, not a runtime condition to handle. The message names the remedy because an
    error that only says "no" costs the reader the same debugging session twice.
    """
    status = conn.info.transaction_status
    if status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError(
            f"{fn}() requires a connection that is NOT already inside a transaction; "
            f"psycopg reports {status.name}. Its write would degrade to a SAVEPOINT and "
            "publish nothing while returning successfully (ADR 0025). Fix at the caller: "
            "set `conn.autocommit = True` at wiring, or commit before calling. NB a bare "
            "SELECT opens the implicit transaction exactly as a write does."
        )


def connect() -> psycopg.Connection:
    """Open a connection with the pgvector type adapter registered.

    Requires the `vector` extension to already exist (created by migrations/0001_init.sql),
    so run scripts/migrate.py first. The migration script itself connects WITHOUT this helper.
    """
    conn = psycopg.connect(load_settings().database_url)
    register_vector(conn)
    return conn
