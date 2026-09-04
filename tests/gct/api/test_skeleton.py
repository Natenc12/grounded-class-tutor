"""The composition root's contracts (issue #104): the request connection satisfies every library
writer, the owner has one source, the providers are singletons built at startup, startup refuses
a missing key, and every failure wears one envelope."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, field_validator

from gct.api import deps
from gct.api.app import create_app, require_openai_key
from gct.api.deps import Conn, Embedder, Generator, OwnerId, get_conn, owner_id
from gct.api.errors import KIND_HTTP, KIND_INTERNAL, KIND_VALIDATION, ApiError
from gct.api.schemas import ErrorEnvelope
from gct.config import V1_OWNER_ID
from gct.jobs.queue import enqueue


def _read_then_enqueue(conn: psycopg.Connection, owner: str, class_id: str, path: Path) -> str:
    """The handler shape the ticket names: an ownership SELECT, then a `require_idle` writer.

    Scoped on owner_id AND class_id like every scoped query (F6/F12). The SELECT is what arms
    ADR 0025's trap - psycopg opens its implicit transaction on a read - so `enqueue` on the next
    line raises unless the connection is autocommit.
    """
    row = conn.execute(
        "select 1 from classes where class_id = %s::uuid and owner_id = %s", (class_id, owner)
    ).fetchone()
    assert row is not None
    return enqueue(conn, path=path, owner_id=owner, class_id=class_id)


def _mount_enqueue_route(app) -> None:
    @app.post("/_test/enqueue")
    def _route(conn: Conn, owner: OwnerId, class_id: str, path: str) -> dict[str, str]:
        return {"file_id": _read_then_enqueue(conn, owner, class_id, Path(path))}


# --- The connection contract -----------------------------------------------------------------


def test_health_reads_through_the_request_connection(api):
    resp = api.client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_a_handler_that_reads_then_writes_publishes_the_write(api, db_other, tmp_path):
    """The day-one failure, not failing. Asserted through `db_other` - a connection neither the
    handler nor the test's `db` fixture held - because that is the only reading that proves the
    row was PUBLISHED and not written into a savepoint the request's exit discarded."""
    _mount_enqueue_route(api.app)
    lecture = tmp_path / "lecture.pdf"
    lecture.write_bytes(b"%PDF-1.4\n")

    resp = api.client.post(
        "/_test/enqueue", params={"class_id": api.class_id, "path": str(lecture)}
    )
    assert resp.status_code == 200, resp.json()
    file_id = resp.json()["file_id"]

    row = db_other.execute(
        "select owner_id, class_id::text, status from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert row == (api.owner_id, api.class_id, "queued")
    job = db_other.execute(
        "select state, owner_id from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert job == ("queued", api.owner_id)


def test_the_same_handler_on_a_plain_connection_is_refused_and_publishes_nothing(
    api, db_other, tmp_path
):
    """The other direction: with `get_conn` replaced by the plain-connection shape the trap
    describes, the SAME route trips `require_idle` and comes back as the 500 envelope, and
    `db_other` sees no row. Pins that autocommit in `get_conn` is what the contract rests on -
    a mutation that dropped the line would turn the test above red and this one green-for-the-
    wrong-reason unless both are asserted."""
    _mount_enqueue_route(api.app)
    lecture = tmp_path / "lecture.pdf"
    lecture.write_bytes(b"%PDF-1.4\n")

    def plain_conn():
        conn = (
            deps.connect()
        )  # `gct.db.connect()`, autocommit left False - the `db` fixture's shape
        try:
            yield conn
        finally:
            conn.close()

    api.app.dependency_overrides[get_conn] = plain_conn
    resp = api.client.post(
        "/_test/enqueue", params={"class_id": api.class_id, "path": str(lecture)}
    )
    assert resp.status_code == 500
    assert resp.json()["error"]["kind"] == KIND_INTERNAL
    count = db_other.execute(
        "select count(*) from files where owner_id = %s", (api.owner_id,)
    ).fetchone()[0]
    assert count == 0


def test_get_conn_is_autocommit_pgvector_registered_and_closed_after_the_request(db):
    """The three properties of the contract, read straight off the dependency. `db` is taken
    only for the skip/hard-fail decision and the marker; its connection is not used."""
    gen = get_conn()
    conn = next(gen)
    assert conn.autocommit is True
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    # `gct.db.connect()` registers the pgvector type; raw psycopg would not know `vector`.
    assert conn.adapters.types.get("vector") is not None
    conn.execute("select 1").fetchone()
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE  # no implicit txn
    assert not conn.closed
    with pytest.raises(StopIteration):
        next(gen)  # the request ended
    assert conn.closed


def test_the_request_connection_is_closed_when_the_handler_raises(api, monkeypatch):
    """The `finally` in `get_conn`, pinned in the one direction that needs it. The happy path
    closes the connection with or without a `finally`; a handler that RAISES is what leaks one
    connection per request without it, and the API then walks to `max_connections` in silence.

    Spies on `deps.connect` - `get_conn` resolves that name at call time - to catch the very
    connection the request built, and reads its state after the 500 has come back. Nothing
    reaches `db`'s or `db_other`'s connection; the spy wraps the app's own."""
    built: list[psycopg.Connection] = []
    real_connect = deps.connect

    def spying_connect() -> psycopg.Connection:
        conn = real_connect()
        built.append(conn)
        return conn

    monkeypatch.setattr(deps, "connect", spying_connect)

    @api.app.get("/_test/raise-after-connect")
    def _route(conn: Conn) -> None:
        raise RuntimeError("the handler failed after the connection was built")

    resp = api.client.get("/_test/raise-after-connect")
    assert resp.status_code == 500
    assert len(built) == 1
    assert built[0].closed


# --- The owner ---------------------------------------------------------------------------------


def test_owner_id_has_one_source(offline_app, monkeypatch):
    """The dependency returns the config constant, and a route sees it without reading the
    request. Overriding it per test is the api fixtures' job; unoverridden, it is V1's one user.

    Pinned by MOVING the constant, not only by comparing to it: `owner_id() == V1_OWNER_ID` is
    green for a copy of the literal pasted into the function, which is the second writer this
    dependency exists to prevent. Patched on `deps`, where `owner_id` resolves the name at call
    time; the dependency and the route must both follow."""

    @offline_app.get("/_test/owner")
    def _route(owner: OwnerId) -> dict[str, str]:
        return {"owner_id": owner}

    with TestClient(offline_app) as client:
        assert owner_id() == V1_OWNER_ID
        assert client.get("/_test/owner").json() == {"owner_id": V1_OWNER_ID}

        monkeypatch.setattr(deps, "V1_OWNER_ID", "someone-else")
        assert owner_id() == "someone-else"
        assert client.get("/_test/owner").json() == {"owner_id": "someone-else"}


# --- Providers and startup ---------------------------------------------------------------------


def test_injected_providers_are_the_app_singletons_and_need_no_key(monkeypatch):
    """With fakes injected the lifespan builds no client and asks for no key: an empty
    `OPENAI_API_KEY` (CI's environment) starts cleanly. The routes read the very objects passed
    in, off `app.state` - one instance per app, never one per request."""
    monkeypatch.setenv("OPENAI_API_KEY", "")
    embedder, generator = object(), object()  # never called; identity is the assertion
    app = create_app(embedder=embedder, generator=generator)

    @app.get("/_test/providers")
    def _route(e: Embedder, g: Generator) -> dict[str, int]:
        return {"embedder": id(e), "generator": id(g)}

    with TestClient(app) as client:
        assert app.state.embedder is embedder
        assert app.state.generator is generator
        seen = {tuple(client.get("/_test/providers").json().values()) for _ in range(2)}
        assert seen == {(id(embedder), id(generator))}


def test_startup_refuses_a_missing_openai_key(monkeypatch):
    """No providers injected + empty key = the process does not start. Raised BEFORE any client
    is constructed, so this test never builds a real OpenAI client either."""
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        require_openai_key()
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        with TestClient(create_app()):
            pass


# --- The error envelope ------------------------------------------------------------------------


def _assert_envelope(resp, status: int, kind: str) -> dict:
    assert resp.status_code == status
    body = resp.json()
    ErrorEnvelope.model_validate(body)  # the documented shape, exactly
    assert set(body) == {"error"}
    assert set(body["error"]) == {"kind", "message", "detail"}
    assert body["error"]["kind"] == kind
    return body["error"]


def test_every_failure_wears_the_envelope(offline_app):
    """Four sources of failure, one body shape: a route's own `ApiError` (status of its
    choosing), the framework's 404/405, request validation (422, field list in `detail`), and
    an uncaught exception (500, exception text NOT echoed)."""

    @offline_app.get("/_test/api-error")
    def _api_error() -> None:
        raise ApiError(418, "teapot", "short and stout", detail={"handle": 1})

    @offline_app.post("/_test/validated")
    def _validated(n: int) -> dict[str, int]:
        return {"n": n}

    @offline_app.get("/_test/boom")
    def _boom() -> None:
        raise RuntimeError("postgresql://user:SECRET@host/db")

    with TestClient(offline_app, raise_server_exceptions=False) as client:
        err = _assert_envelope(client.get("/_test/api-error"), 418, "teapot")
        assert err == {"kind": "teapot", "message": "short and stout", "detail": {"handle": 1}}

        _assert_envelope(client.get("/_test/no-such-path"), 404, KIND_HTTP)
        _assert_envelope(client.post("/health"), 405, KIND_HTTP)

        err = _assert_envelope(
            client.post("/_test/validated", params={"n": "x"}), 422, KIND_VALIDATION
        )
        assert err["detail"][0]["loc"] == ["query", "n"]

        err = _assert_envelope(client.get("/_test/boom"), 500, KIND_INTERNAL)
        assert "SECRET" not in err["message"]
        assert err["detail"] is None


class _BlankRefusingBody(BaseModel):
    """A request model with the validator shape the route issues will write: a `@field_validator`
    that RAISES. Module-level, not local to its test, and that is load-bearing: this file runs
    under `from __future__ import annotations`, and FastAPI resolves a route's string annotations
    against the module's globals - a class local to the test function is invisible there, and
    the parameter silently becomes a required QUERY field named `body`, so the validator never
    runs and the test fails for a reason that has nothing to do with the envelope."""

    name: str

    @field_validator("name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


def test_a_raising_field_validator_still_renders_the_422_envelope(offline_app):
    """A `@field_validator` that raises puts the exception OBJECT into pydantic's error `ctx`.
    Passed to the response un-encoded, that list is not JSON-serialisable, the handler itself
    raises, and the client gets a bare 500 `internal` instead of the 422 it was promised. The
    envelope must survive the validator the route issues will actually write."""

    @offline_app.post("/_test/validated-body")
    def _route(body: _BlankRefusingBody) -> dict[str, str]:
        return {"name": body.name}

    with TestClient(offline_app, raise_server_exceptions=False) as client:
        err = _assert_envelope(
            client.post("/_test/validated-body", json={"name": "   "}), 422, KIND_VALIDATION
        )
        assert err["detail"][0]["loc"] == ["body", "name"]
        assert "name must not be blank" in err["detail"][0]["msg"]
