"""Fixtures shared across every test package.

Home to `db` — hoisted here from `tests/gct/ingest/conftest.py` when the retriever suite
(issue #5) needed the same real-Postgres connection. pytest only exposes a conftest to its
own directory and below, so a fixture two suites share has to live at the root. One copy
keeps the FK-ordered teardown (chunks -> files -> classes) from drifting between them.

Ingest-specific fixtures (the PDF/PPTX factories, `FakeEmbeddings`) deliberately stayed put.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from gct.db import connect


def pytest_collection_modifyitems(items):
    """Apply the `db` marker to every test that requests the `db` fixture.

    The marker is DERIVED from the dependency, never hand-declared. `--strict-markers` catches a
    typo'd mark but is blind to a missing one: a new DB-backed test that forgets `@pytest.mark.db`
    still runs under CI's `-m "not live"`, so nothing goes red — `-m db` just quietly stops
    collecting it. That drift would surface only when someone trusted the count. Taking the `db`
    fixture IS what makes a test a db test, so read it off the fixture and leave no second copy of
    the fact to fall out of sync.

    Note the edge this CANNOT cover: a test that reaches Postgres by calling `gct.db.connect()`
    itself, rather than taking the fixture, gets no mark, no teardown, and no CI hard-fail.
    Nothing in the suite does that today — take the fixture and it stays true.

    `live` is derived the same way, from any fixture named `live_*` — the promise the marker's
    own registration text made ("derive it from an OpenAI-client fixture the same way the moment
    a paid test exists", pyproject.toml; epic #9's pass-2 flag). Hand-declared, `live` fails in
    two directions at once: a paid test that FORGETS the mark runs in CI against an empty
    `OPENAI_API_KEY` (money, or red for the wrong reason), and one that carries it silently
    leaves CI coverage. Deriving from the fixture leaves no second copy to drift. The prefix is
    the convention: constructing a REAL provider client is what makes a test paid, so every such
    fixture lives in this file and is named `live_*` — the same "take the fixture and it stays
    true" edge as `db` applies.
    """
    for item in items:
        fixturenames = getattr(item, "fixturenames", ())
        if "db" in fixturenames:
            item.add_marker("db")
        if any(name.startswith("live_") for name in fixturenames):
            item.add_marker("live")


def _in_ci() -> bool:
    """True only when CI is affirmatively set.

    Truthiness on the raw value is wrong here: plenty of tooling exports `CI=false` or `CI=0`
    to mean "not CI", and a bare `if os.environ.get("CI")` reads those as CI — turning the
    fixture's local skip-when-Postgres-is-down courtesy into a hard error on a laptop.
    """
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.fixture
def db():
    """Yield `(conn, owner_id, class_id)` on the real local Postgres with a seeded class row.

    `owner_id` is unique per test so teardown can delete exactly this test's rows
    (chunks → files → classes, respecting FKs). Skips the test if the DB is UNREACHABLE, so a
    machine without Postgres doesn't hard-fail the suite — but ONLY locally; see below.

    Tests using this carry the `db` marker (pyproject.toml), and CI runs them against the
    pgvector service container after the migrate step.

    The catch is deliberately narrow. `connect()` does `psycopg.connect()` and THEN
    `register_vector()`, so a broad `except Exception` would render "you forgot to run
    migrate.py" (`vector type not found in the database`) as "local Postgres unavailable" and
    skip — a silent green blaming the wrong thing, which is the failure this fixture exists to
    kill. Only `OperationalError` means "no DB here"; everything else is a real error and must
    surface as one.
    """
    try:
        conn = connect()
    except psycopg.OperationalError as exc:
        if _in_ci():
            # In CI the service container guarantees a DB, so unreachable means the job is
            # misconfigured. Skipping would go green with every `db` test unrun — the exact
            # silent pass the container exists to kill. Fail loud instead.
            raise RuntimeError(f"CI Postgres service unreachable: {exc}") from exc
        pytest.skip(f"local Postgres unavailable: {exc}")

    owner_id = f"test-owner-{uuid.uuid4()}"
    class_id = str(uuid.uuid4())
    try:
        conn.execute(
            "insert into classes (class_id, owner_id, name) values (%s::uuid, %s, %s)",
            (class_id, owner_id, "test class"),
        )
        conn.commit()
        yield conn, owner_id, class_id
    finally:
        # `close()` gets its own finally: if the rollback or any delete below raises (a poisoned
        # connection, a lock wait), the rest of this block is skipped, and without the nesting the
        # connection would leak AND the teardown error would mask the test's real failure.
        try:
            # A test that errored mid-statement leaves the connection in an aborted transaction,
            # and Postgres then refuses every later command — including these deletes, which would
            # bury the test's own failure under InFailedSqlTransaction. Clear that state first.
            conn.rollback()
            conn.execute("delete from chunks where owner_id = %s", (owner_id,))
            # jobs.file_id FKs to files, so it must go before files or the delete
            # below raises ForeignKeyViolation (issue #70 is the table's first writer).
            conn.execute("delete from jobs where owner_id = %s", (owner_id,))
            conn.execute("delete from files where owner_id = %s", (owner_id,))
            conn.execute("delete from classes where owner_id = %s", (owner_id,))
            conn.commit()
        finally:
            conn.close()


@pytest.fixture
def db_other(db):
    """A SECOND connection to the same database — the only way to prove a write was PUBLISHED.

    A connection can always see its own uncommitted work, so every assertion read back through
    `db`'s connection is true whether or not anything was committed. That is not a hypothetical:
    `test_ingest_file_uses_a_caller_supplied_file_id` shipped green while publishing nothing,
    because its stand-in for `enqueue` left the connection INTRANS and `index_file`'s
    `conn.transaction()` degraded to a savepoint (ADR 0025). Four assertions passed on rows that
    no other connection could see and the teardown rollback then discarded.

    ADR 0025 records that no SINGLE-connection test can catch that. True — and it is a statement
    about single-connection tests, not about testing. Reading back through a second connection
    catches it, which is what this fixture is for. Assert through `db`'s connection for what the
    code computed; assert through this one for what actually survives.

    Depends on `db` for two reasons, both load-bearing: the skip/hard-fail decision about an
    unreachable Postgres is made in exactly one place, and `pytest_collection_modifyitems` reads
    `db` out of `fixturenames` transitively, so a test taking only this fixture still derives the
    `db` marker rather than silently escaping `-m db`.

    Autocommit because it is a read-only verifier: every statement becomes its own transaction, so
    it can never hold an old snapshot and report a stale answer regardless of the server's default
    isolation level. It only ever reads, so it has nothing to clean up — `db` owns teardown.
    """
    conn = connect()
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def live_openai_embedder():
    """The REAL OpenAI embedder — taking this fixture is what makes a test `live` (paid).

    The `live_` prefix is load-bearing: `pytest_collection_modifyitems` derives the `live` marker
    from it, so a test can no more forget the mark than forget the client it spends money
    through. Skips (never fails) without a key: `live` tests run locally with `.env` secrets by
    design (pyproject.toml), and on a machine without them "cannot run" is a skip-shaped fact —
    unlike CI's `db`, there is no environment that GUARANTEES a key and could hard-fail instead.
    """
    from gct.config import load_settings
    from gct.providers.openai_provider import OpenAIEmbeddings

    if not load_settings().openai_api_key:
        pytest.skip("no OPENAI_API_KEY in the environment/.env — live tests need real secrets")
    return OpenAIEmbeddings()
