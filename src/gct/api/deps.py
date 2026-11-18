"""Request-scoped dependencies - the composition root's per-request half (issue #104).

Three things every route needs and none may invent for itself: a database connection a library
writer will accept, the owner every scoped query filters on, and the provider singletons `ask()`
takes. Each is ONE function here so the routes stay thin and parallel-safe: three routers each
inventing a connection or an owner is the two-writers failure this repo is built against.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import psycopg
from fastapi import Depends, Request

from gct.config import V1_OWNER_ID
from gct.db import connect
from gct.providers.base import Embeddings, Generation


def get_conn() -> Iterator[psycopg.Connection]:
    """One connection per request, autocommit, closed when the request ends.

    THE CONNECTION CONTRACT. Every library writer refuses a connection that is already inside a
    transaction (`gct.db.require_idle`; ADR 0025, guarded per ADR 0027), and psycopg opens the
    implicit transaction on a bare SELECT. So the natural handler shape - "does this class belong
    to this owner?" then `enqueue(...)` - raises on its second line unless the connection is
    autocommit. Every caller that works today sets it at wiring (`scripts/worker.py`,
    `scripts/ask_smoke.py`, `scripts/ingest_smoke.py`); this is the API's wiring.

    `gct.db.connect()`, not raw psycopg and not a pool: it registers the pgvector type, and
    `ask()` needs that - the query vector only adapts to `vector(1536)` on such a connection
    (`gct.ask.ask` docstring). A pool is a V2 question and not one a single local user asks.

    A yield-dependency so the `finally` runs when the request ends, response or exception. Without
    it every request leaks one connection, and the API walks to Postgres's `max_connections` in
    silence. Sync (`def`, not `async def`) on purpose: `connect()` blocks, and FastAPI runs a
    sync dependency in its threadpool instead of on the event loop.

    The suite's FIXTURES never override this dependency: injecting the `db` fixture would hand the
    handler a NON-autocommit connection, so a read-then-write test fails `require_idle` while
    production works - `tests/gct/api/conftest.py` scopes teardown by owner instead. Two tests do
    substitute it (grep `dependency_overrides`), for a NEGATIVE arm only; every positive arm ships.
    """
    conn = connect()
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def owner_id() -> str:
    """The owner every scoped query filters on - ONE source for the V1 hardcoded user.

    V1 has one user and no auth (ADR 0004): the value is `gct.config.V1_OWNER_ID`, and this is
    the only place the API reads it. Routes take `Depends(owner_id)` and never read an owner from
    the request - a body or header field would be an unauthenticated client choosing whose rows
    it sees (F12). V3 replaces this ONE function with the authenticated principal, and no route
    changes.

    A dependency rather than a bare import of the constant so tests can scope each test's rows
    under a unique owner through `app.dependency_overrides`, which is what makes owner-scoped
    teardown possible.
    """
    return V1_OWNER_ID


def get_embedder(request: Request) -> Embeddings:
    """The app-wide `Embeddings` singleton, built once at startup (`gct.api.app.create_app`).

    `ask()`'s collaborators are keyword-only and must not be rebuilt per request: a provider
    client is a connection pool and a config read, and the active model id is read from config
    exactly once at construction (ADR 0018). Read off `app.state` - the app instance whose
    lifespan built it - not a module cache, so two apps in one process (a test's and another
    test's) never share one.
    """
    return request.app.state.embedder


def get_generator(request: Request) -> Generation:
    """The app-wide `Generation` singleton; same reasoning as `get_embedder`."""
    return request.app.state.generator


# The spellings routes use. `Annotated` rather than `= Depends(...)` defaults because ruff's B008
# (a call in an argument default) fires on the default form, and silencing it per parameter in
# three routers is three copies of a workaround. A route's signature reads
# `def handler(conn: Conn, owner: OwnerId, ...)` and nothing else.
Conn = Annotated[psycopg.Connection, Depends(get_conn)]
OwnerId = Annotated[str, Depends(owner_id)]
Embedder = Annotated[Embeddings, Depends(get_embedder)]
Generator = Annotated[Generation, Depends(get_generator)]
