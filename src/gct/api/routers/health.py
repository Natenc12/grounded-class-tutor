"""`GET /health` - the skeleton's one route, and the composition root's smoke surface."""

from __future__ import annotations

from fastapi import APIRouter

from gct.api.deps import Conn

router = APIRouter(tags=["health"])


@router.get("/health")
def health(conn: Conn) -> dict[str, str]:
    """`{"status": "ok"}` once Postgres has answered a round trip on the request connection.

    Reads through `get_conn` rather than returning a constant so the probe proves the contract
    the routes depend on - a connection is built, reaches the database, and is closed - instead
    of proving that the process is up. It reads only; the write half of the contract is pinned
    by the api suite, not by a production route calling a writer for no reason.
    """
    conn.execute("select 1").fetchone()
    return {"status": "ok"}
