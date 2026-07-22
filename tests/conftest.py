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

import pytest


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
    (chunks → files → classes, respecting FKs). Skips the test if the DB is unreachable, so a
    machine without Postgres doesn't hard-fail the suite — but ONLY locally; see below.

    Tests using this carry the `db` marker (pyproject.toml), and CI runs them against the
    pgvector service container after the migrate step.
    """
    try:
        from gct.db import connect

        conn = connect()
    except Exception as exc:  # noqa: BLE001 — any connect failure means "no DB here"
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
        # A test that errored mid-statement leaves the connection in an aborted transaction, and
        # Postgres then refuses every later command — including these deletes, which would bury
        # the test's own failure under InFailedSqlTransaction. Clear that state first.
        conn.rollback()
        conn.execute("delete from chunks where owner_id = %s", (owner_id,))
        conn.execute("delete from files where owner_id = %s", (owner_id,))
        conn.execute("delete from classes where owner_id = %s", (owner_id,))
        conn.commit()
        conn.close()
