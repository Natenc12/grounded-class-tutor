"""Classes - the unit an ask is scoped to (F6), and the row every file hangs off (issue #106).

One writer. `create_class` exists because the API adapter may not carry business logic
(ADR 0009; roadmap Slice 3 "No business logic"), and until this module every caller hand-rolled
the `insert into classes` itself - `scripts/ask_smoke.py`'s `_resolve_class` was the shape this
was hoisted from. Its find-or-create-by-name semantics deliberately stayed in the script:
`classes` has no unique constraint on `(owner_id, name)`, so "find or create" is a policy about
duplicates that a smoke suite wants and an upload API has no business deciding.

The writer REFUSES a connection that is already inside a transaction rather than silently
degrading to a SAVEPOINT that publishes nothing (`gct.db.require_idle`; ADR 0025, guarded per
ADR 0027) - the same precondition as every writer in `gct.jobs.queue` and `index_file`.
"""

from __future__ import annotations

import psycopg

from gct.db import require_idle


def create_class(conn: psycopg.Connection, *, owner_id: str, name: str) -> str:
    """Create one `classes` row for `owner_id`; commit; return its `class_id` as a `str`.

    `str`, not psycopg's `UUID`: every downstream `gct` signature types ids as `str` and casts
    `%s::uuid` at the SQL boundary (`enqueue`, `retrieve`, `ask`), so this returns the shape the
    next call takes rather than one the caller has to convert.

    `class_id` is DB-generated (`gen_random_uuid()` default) and RETURNed - one round trip, and
    no client-side uuid to be handed a colliding value. `_resolve_class` minted its own because it
    needed the id before the insert for a log line; nothing here does.

    A BLANK NAME IS REFUSED, not stored and not trimmed. The schema's `name text not null` accepts
    `''` and `'   '` - `not null` is not `not blank` - and a class with an invisible name is
    unusable on every surface that lists classes (Slice 4's create-class screen is the first).
    Refusing at the library boundary means no adapter has to remember the rule, and refusing
    rather than trimming means the caller's value is stored VERBATIM when it is accepted: a name
    with surrounding whitespace is not silently rewritten into a different name, which is the
    kind of quiet conversion this codebase does not do at boundaries. The check runs BEFORE any
    statement so a rejected call leaves the connection exactly as it found it.

    PRECONDITION ON `conn` (ADR 0025): it MUST NOT already be inside a transaction, or the block
    below degrades to a SAVEPOINT and this function returns having published nothing. Enforced by
    `require_idle` (ADR 0027), whose message names the remedy. Commits before returning, same
    contract as `enqueue`.
    """
    if not name.strip():
        raise ValueError(
            f"create_class() refuses a blank name ({name!r}): `classes.name` is what every "
            "surface displays, and the schema's NOT NULL does not reject whitespace. Pass the "
            "name the student typed; the value is stored verbatim."
        )
    require_idle(conn, "create_class")

    with conn.transaction():
        row = conn.execute(
            """
            insert into classes (owner_id, name)
            values (%(owner_id)s, %(name)s)
            returning class_id
            """,
            {"owner_id": owner_id, "name": name},
        ).fetchone()
    return str(row[0])
