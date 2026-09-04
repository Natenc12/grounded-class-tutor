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

`class_exists` is the READ half, added for `POST /files` (issue #110): the upload route has to
answer "is this the caller's class?" before it enqueues anything, and that question is scoped
(F6/F12), so it cannot be left to the foreign key - see its docstring.
"""

from __future__ import annotations

import uuid

import psycopg

from gct.db import require_idle


def create_class(conn: psycopg.Connection, *, owner_id: str, name: str) -> str:
    """Create one `classes` row for `owner_id`; commit; return its `class_id` as a `str`.

    `str`, not psycopg's `UUID`: every downstream `gct` signature types ids as `str` and casts
    `%s::uuid` at the SQL boundary (`enqueue`, `retrieve`, `ask`), so this returns the shape the
    next call takes rather than one the caller has to convert.

    `class_id` is DB-generated (`gen_random_uuid()` default) and RETURNed - one round trip, and
    no client-side uuid to be handed a colliding value. `_resolve_class` minted its own client-side
    with no reason recorded for doing so; the library lets the DB mint, as `enqueue` does.

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


def class_exists(conn: psycopg.Connection, *, class_id: str, owner_id: str) -> bool:
    """True if `class_id` names a class belonging to `owner_id`; False otherwise.

    ONE False FOR TWO CASES, like `get_file_status`'s one `None`: a class that does not exist and
    a class belonging to ANOTHER owner are indistinguishable here, because the scope is a single
    `WHERE class_id AND owner_id` and no code path learns which clause failed. A route that
    renders False as 404 therefore cannot leak cross-owner existence (F12) even by accident.

    WHY THIS EXISTS AT ALL, RATHER THAN LETTING THE FOREIGN KEY DECIDE. `files.class_id`
    references `classes(class_id)` with NO owner predicate - the constraint is satisfied by the
    row existing, whoever owns it. So an upload naming another owner's `class_id` INSERTS
    CLEANLY: the FK is satisfied, the `files` row is written with the caller's `owner_id` and the
    victim's `class_id`, and the file is queued into a class its uploader may not see. Catching
    an `IntegrityError` from `enqueue` is therefore NOT an equivalent implementation of this
    check; it is a strictly weaker one that admits exactly the cross-owner write F6/F12 forbid.
    The FK still does its own job - it stops a `class_id` that names nothing - and this reader
    does the job the FK cannot express.

    `class_id` IS VALIDATED AS A UUID BEFORE ANY STATEMENT, AND THE CANONICAL FORM IS BOUND -
    the same shape, for the same two reasons, as `get_file_status` (see its docstring): a
    malformed id is a caller error that deserves a message rather than a psycopg `DataError`,
    and on a non-autocommit connection the failed `::uuid` cast would abort the implicit
    transaction and take every later statement with it. Python's `uuid.UUID` accepts spellings
    Postgres's cast rejects (`urn:uuid:<id>`), so validating the raw string and then binding
    THAT would pass the check and still hit the exact abort the check prevents; binding
    `str(uuid.UUID(class_id))` normalises every accepted spelling to the one Postgres takes.

    A READER, so deliberately NO `require_idle`: a SELECT inside a transaction is correct, and
    this function must be callable from anywhere. But a SELECT is also enough to OPEN psycopg's
    implicit transaction on a non-autocommit connection (ADR 0025), so a caller that checks
    ownership here and then calls `enqueue` on the same connection arms the trap the writers
    guard against. That is the caller's wiring to get right - autocommit, which is the API's
    connection contract (`gct.api.deps.get_conn`).
    """
    try:
        canonical = str(uuid.UUID(class_id))
    except ValueError as exc:
        raise ValueError(
            f"class_exists() requires a uuid class_id; got {class_id!r}. An id that is not a "
            "uuid cannot name any class - reject it at the boundary rather than asking the "
            "database, which would abort the connection's open transaction."
        ) from exc

    row = conn.execute(
        """
        select 1
        from classes
        where class_id = %(class_id)s::uuid
          and owner_id = %(owner_id)s
        """,
        {"class_id": canonical, "owner_id": owner_id},
    ).fetchone()
    return row is not None
