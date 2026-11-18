"""The app factory - the composition root's once-per-process half (issue #104).

`create_app()` builds the FastAPI app, installs the error envelope, mounts the four routers, and
owns STARTUP: the provider singletons and the one requirement the process refuses to start
without. `app` at the bottom is the ASGI callable uvicorn serves (`uvicorn gct.api.app:app`).
The worker is a SEPARATE OS process, never a task in this loop (ADR 0011, PM-3 addendum).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gct.api import errors, limits
from gct.api.routers import ask, classes, files, health
from gct.providers import active
from gct.providers.base import Embeddings, Generation


def require_openai_key() -> None:
    """Refuse to start when the selected providers need a key and there is none."""
    active.require_usable()


def create_app(
    *, embedder: Embeddings | None = None, generator: Generation | None = None
) -> FastAPI:
    """Build the app. Pass `embedder`/`generator` to substitute providers (tests: fakes).

    Providers are built in the LIFESPAN, not here, and that placement is load-bearing twice:
      - `app` below is constructed at import, and the module must import without a key - CI
        runs with an empty `OPENAI_API_KEY` and the api suite imports this module - so the
        requirement runs when the server STARTS, which is what "startup" means to uvicorn too.
      - The key requirement belongs to constructing the REAL provider. Inject both and nothing
        checks the key, because nothing needs it; inject neither and the check runs before
        either client exists. Tests substitute fakes and thereby never build an OpenAI client
        (no `live_*` fixture, no paid call) - the bypass is by construction, not a flag.

    The singletons live on `app.state`, the instance whose lifespan built them, and routes read
    them through `deps.get_embedder`/`deps.get_generator`. A module-level cache was rejected:
    it is one provider per PROCESS, so two apps in one test run would share whichever was built
    first, and a test substituting fakes would have to reach in and clear it.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if embedder is None or generator is None:
            require_openai_key()
        app.state.embedder = embedder if embedder is not None else active.build_embedder()
        app.state.generator = generator if generator is not None else active.build_generator()
        yield

    app = FastAPI(title="Grounded Class Tutor", lifespan=lifespan)
    errors.install(app)
    # The body bound goes on EVERY app this factory builds, including the ones tests build, so
    # no route can be reached without it (issue #125). It is middleware and the handlers above
    # are not, which is why it renders its own refusals rather than raising - `limits`'s module
    # docstring carries the mechanism.
    limits.install(app)
    # Mounted HERE, once, so #107/#108/#110 each edit only their own router module and never
    # this file. `health` is the skeleton's own surface: the one route this ticket owns.
    for router in (health.router, classes.router, files.router, ask.router):
        app.include_router(router)
    return app


app = create_app()
