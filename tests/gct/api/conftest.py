"""Fixtures for the api suite (issue #104).

The one rule these fixtures exist to keep: THE APP'S CONNECTION DEPENDENCY IS NEVER OVERRIDDEN.
The obvious TestClient fixture injects the `db` fixture into `get_conn`, and `db` is NOT
autocommit (`db_other` is - tests/conftest.py) - so a handler that reads then writes fails
`require_idle` under test while working in production. The suite would then be red for a reason
production never has, or green after "fixing" the handler into a shape production does not need.
Here the app builds its own connection per request exactly as shipped (`gct.api.deps.get_conn`),
and the test lines its rows up for teardown by OWNER instead: `db`'s unique per-test owner is
handed to the app through the `owner_id` dependency, so everything a handler writes lands under
an owner `db`'s teardown already deletes (chunks -> jobs -> files -> classes, FK order).

So `api` DEPENDS on `db` without ever handing its connection to the app. Three things ride on
that dependency: the skip-locally / hard-fail-in-CI decision about an unreachable Postgres is
made in one place, the `db` marker is derived transitively (`pytest_collection_modifyitems`
reads `db` out of `fixturenames`), and the teardown pattern has one writer.

Read-back goes through `db_other`, never through the handler's connection or `db`'s: the handler
closed its connection when the request ended, and `db`'s can see uncommitted work. A test whose
point is that something was WRITTEN takes `db_other` (CLAUDE.md).

The providers are stubs that never build a client. Nothing here is a `live_*` fixture, so nothing
here is paid; the stubs are also what let `create_app` skip the `OPENAI_API_KEY` requirement -
by construction, not by a flag (`gct.api.app.create_app`).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gct.api.app import create_app
from gct.api.deps import owner_id
from gct.config import EMBEDDING_DIM
from gct.providers.base import Message


class StubEmbeddings:
    """An `Embeddings` that never talks to anyone. `create_app` needs a value whether or not the
    test under it ever embeds, and a real client is the one thing a test here must never
    construct: it would spend money and make this suite depend on a network."""

    model_id = "stub-embed"
    dim = EMBEDDING_DIM

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]


class StubGeneration:
    model_id = "stub-gen"

    def generate(self, messages: Sequence[Message]) -> str:
        return ""


@dataclass(frozen=True)
class Api:
    """What a test gets: the app (to mount a test-only route on before the client opens), the
    client, and the owner/class the app's rows are scoped under."""

    app: FastAPI
    client: TestClient
    owner_id: str
    class_id: str


@pytest.fixture
def api_app(db) -> FastAPI:
    """The app with stub providers and `db`'s owner substituted for the V1 constant.

    Separate from `api` so a test can mount a test-only route BEFORE the client enters the
    lifespan. Only `owner_id` is overridden - see the module docstring for why `get_conn` is not.
    """
    _, owner, _ = db
    app = create_app(embedder=StubEmbeddings(), generator=StubGeneration())
    app.dependency_overrides[owner_id] = lambda: owner
    return app


@pytest.fixture
def api(db, api_app) -> Iterator[Api]:
    """A live TestClient over `api_app`, inside its lifespan (so `app.state` is populated).

    `raise_server_exceptions=False` so an unhandled exception comes back as the 500 envelope the
    way a browser would see it, instead of re-raising into the test.
    """
    _, owner, class_id = db
    with TestClient(api_app, raise_server_exceptions=False) as client:
        yield Api(app=api_app, client=client, owner_id=owner, class_id=class_id)


@pytest.fixture
def offline_app() -> FastAPI:
    """An app with stub providers and NO database dependency - for the tests of the envelope and
    the owner dependency, which never reach Postgres. Not `db`-marked, so it runs anywhere."""
    return create_app(embedder=StubEmbeddings(), generator=StubGeneration())
