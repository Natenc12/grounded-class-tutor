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

from gct.api import errors
from gct.api.routers import ask, classes, files, health
from gct.config import load_settings
from gct.providers.base import Embeddings, Generation


def require_openai_key() -> None:
    """Refuse to start without `OPENAI_API_KEY`.

    `load_settings()` defaults the key to `""`. Whether the OpenAI client then refuses at
    construction or only on the first PAID call - the first student's first question - has
    varied by SDK version (the one pinned today refuses; earlier ones accepted an empty key).
    Neither is the failure this process should have: refuse at startup, in our words, naming
    the remedy in the repo's terms.
    """
    if not load_settings().openai_api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set, so the API cannot embed queries or generate answers. "
            "Set it in `.env` (see `.env.example`) or the environment and start again."
        )


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
        if embedder is None or generator is None:
            # Imported here, not at module top: the OpenAI adapter is only a dependency of a
            # process that actually builds a real client.
            from gct.providers.openai_provider import OpenAIEmbeddings, OpenAIGeneration

        # The embedder constructs itself from config (ADR 0018: never hardcode a model id).
        app.state.embedder = embedder if embedder is not None else OpenAIEmbeddings()
        app.state.generator = generator if generator is not None else OpenAIGeneration()
        yield

    app = FastAPI(title="Grounded Class Tutor", lifespan=lifespan)
    errors.install(app)
    # Mounted HERE, once, so #107/#108/#110 each edit only their own router module and never
    # this file. `health` is the skeleton's own surface: the one route this ticket owns.
    for router in (health.router, classes.router, files.router, ask.router):
        app.include_router(router)
    return app


app = create_app()
