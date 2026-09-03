"""DB-backed tests for `create_class` (issue #106).

Every assertion about what `create_class` WROTE goes through `db_other`, a second connection. A
connection sees its own uncommitted work, so reading back through `db` would hold whether or not
anything was published — the green-while-publishing-nothing failure ADR 0025 describes and
`db_other` exists to catch.

Each pin here carries an arm on BOTH sides: the accepted input is shown to publish AND the
refused input is shown to publish nothing. A one-sided "not in" survives a writer that never
writes at all.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from gct.classes import create_class


def _rows(db_other, owner_id: str, name: str) -> list[tuple[str, str]]:
    """`(owner_id, name)` for every class row matching, as a DIFFERENT connection sees it."""
    return db_other.execute(
        "select owner_id, name from classes where owner_id = %s and name = %s",
        (owner_id, name),
    ).fetchall()


def test_create_class_publishes_a_row_and_returns_its_id_as_str(db, db_other):
    """One call, one row visible to a second connection, and the id comes back as `str`."""
    conn, owner_id, _ = db
    conn.autocommit = True  # the wiring every writer's precondition names (ADR 0025/0027)

    class_id = create_class(conn, owner_id=owner_id, name="Philosophy of Religion")

    assert isinstance(class_id, str)
    assert str(uuid.UUID(class_id)) == class_id, "returned id is not a canonical uuid string"

    assert _rows(db_other, owner_id, "Philosophy of Religion") == [
        (owner_id, "Philosophy of Religion")
    ]
    # The id returned names THAT row — not a fabricated uuid that happens to parse.
    assert db_other.execute(
        "select owner_id, name from classes where class_id = %s::uuid", (class_id,)
    ).fetchone() == (owner_id, "Philosophy of Religion")


def test_create_class_scopes_the_row_to_the_owner_it_was_given(db, db_other):
    """`owner_id` is stamped from the argument, not defaulted — F12 starts at the insert."""
    conn, owner_id, _ = db
    conn.autocommit = True
    other_owner = f"test-owner-{uuid.uuid4()}"

    class_id = create_class(conn, owner_id=owner_id, name="scoped")

    assert db_other.execute(
        "select count(*) from classes where class_id = %s::uuid and owner_id = %s",
        (class_id, owner_id),
    ).fetchone() == (1,)
    assert db_other.execute(
        "select count(*) from classes where class_id = %s::uuid and owner_id = %s",
        (class_id, other_owner),
    ).fetchone() == (0,)


def test_create_class_refuses_a_blank_name_and_stores_an_accepted_one_verbatim(db, db_other):
    """`''` and whitespace-only are refused with a remedy-naming error, BEFORE any statement;
    an accepted name is stored exactly as given — refused, never trimmed.

    The schema cannot do this: `name text not null` admits `''` (proved below through
    `db_other` by inserting one directly), so the rule lives in the library or nowhere.
    """
    conn, owner_id, _ = db
    conn.autocommit = True

    for blank in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match=r"create_class\(\) refuses a blank name"):
            create_class(conn, owner_id=owner_id, name=blank)
        assert _rows(db_other, owner_id, blank) == [], f"a refused name {blank!r} was stored"

    # The refusal is the library's, not the schema's: Postgres takes the blank happily.
    db_other.execute("insert into classes (owner_id, name) values (%s, %s)", (owner_id, ""))
    assert _rows(db_other, owner_id, "") == [(owner_id, "")]

    # Accepted names are stored verbatim — surrounding whitespace is not silently rewritten.
    create_class(conn, owner_id=owner_id, name="  Metaphysics  ")
    assert _rows(db_other, owner_id, "  Metaphysics  ") == [(owner_id, "  Metaphysics  ")]
    assert _rows(db_other, owner_id, "Metaphysics") == []


def test_create_class_refuses_a_connection_already_in_a_transaction(db, db_other):
    """The ADR 0025 precondition, enforced (ADR 0027) — same shape as `queue.py`'s census.

    ONE bare `SELECT` is the whole setup: psycopg opens its implicit transaction on the first
    statement of any kind. The refused call must have written NOTHING through `db_other` — a
    guard that raised after committing half the work would be worse than none. The positive
    arm runs on the same connection after a rollback, so the pin is "refused there, published
    here" and not merely "nothing happened".
    """
    conn, owner_id, _ = db

    conn.execute("select 1").fetchone()  # the accidental transaction — a read, not a write
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS

    with pytest.raises(RuntimeError, match=r"create_class\(\) requires a connection"):
        create_class(conn, owner_id=owner_id, name="refused")
    # The remedy is named in the message, not left to the reader to rediscover.
    with pytest.raises(RuntimeError, match="autocommit"):
        create_class(conn, owner_id=owner_id, name="refused")

    assert _rows(db_other, owner_id, "refused") == [], "a refused create_class still wrote a row"

    conn.rollback()
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    create_class(conn, owner_id=owner_id, name="accepted")
    assert _rows(db_other, owner_id, "accepted") == [(owner_id, "accepted")]
