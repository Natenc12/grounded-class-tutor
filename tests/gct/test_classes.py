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

from gct.classes import class_exists, create_class
from gct.jobs.queue import enqueue


def _rows(db_other, owner_id: str, name: str) -> list[tuple[str, str]]:
    """`(owner_id, name)` for every class row matching, as a DIFFERENT connection sees it."""
    return db_other.execute(
        "select owner_id, name from classes where owner_id = %s and name = %s",
        (owner_id, name),
    ).fetchall()


def test_create_class_publishes_a_row_and_returns_its_id_as_str(db, db_other):
    """One call, one row visible to a second connection, and the id comes back as `str`."""
    conn, owner_id, _ = db
    conn.autocommit = (
        True  # the wiring every writer's precondition names (ADR 0025, guarded per ADR 0027)
    )

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


@pytest.fixture
def foreign_class(db_other):
    """A class row owned by SOMEONE ELSE, with its own teardown.

    `db`'s teardown deletes by owner and this row is not that owner's, so it is cleaned up here —
    children first (FK order), because a test may well have queued a file against it, which is
    precisely the cross-owner write `class_exists` exists to prevent.
    """
    other_owner = f"test-other-owner-{uuid.uuid4()}"
    (class_id,) = db_other.execute(
        "insert into classes (owner_id, name) values (%s, %s) returning class_id",
        (other_owner, "someone else's class"),
    ).fetchone()
    try:
        yield other_owner, str(class_id)
    finally:
        db_other.execute("delete from jobs where class_id = %s::uuid", (str(class_id),))
        db_other.execute("delete from chunks where class_id = %s::uuid", (str(class_id),))
        db_other.execute("delete from files where class_id = %s::uuid", (str(class_id),))
        db_other.execute("delete from classes where class_id = %s::uuid", (str(class_id),))


def test_class_exists_is_true_for_the_owner_and_false_for_everyone_else(db, foreign_class):
    """Both arms on ONE class_id, so the difference is the owner and nothing else. A reader that
    ignored `owner_id` — the failure this function exists to rule out — passes the first
    assertion and fails the second."""
    conn, owner_id, class_id = db
    other_owner, other_class_id = foreign_class

    assert class_exists(conn, class_id=class_id, owner_id=owner_id) is True
    assert class_exists(conn, class_id=other_class_id, owner_id=other_owner) is True

    assert class_exists(conn, class_id=other_class_id, owner_id=owner_id) is False
    assert class_exists(conn, class_id=class_id, owner_id=other_owner) is False


def test_class_exists_is_false_for_an_id_that_names_nothing(db):
    """The other case behind the same `False`: a well-formed uuid with no row. Indistinguishable
    from the cross-owner case above by construction, which is what makes 404-for-both free."""
    conn, owner_id, _ = db
    assert class_exists(conn, class_id=str(uuid.uuid4()), owner_id=owner_id) is False


def test_class_exists_refuses_a_non_uuid_before_touching_the_connection(db):
    """A malformed id is refused at the boundary, and the connection is left as it was found —
    still IDLE, so the writer that follows in the same handler is not poisoned by a failed cast.
    """
    conn, owner_id, _ = db
    conn.rollback()
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE

    with pytest.raises(ValueError, match=r"class_exists\(\) requires a uuid class_id"):
        class_exists(conn, class_id="not-a-uuid", owner_id=owner_id)

    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_class_exists_binds_the_canonical_form_of_every_spelling_it_accepts(db):
    """`urn:uuid:` is the load-bearing case: `uuid.UUID` accepts it, Postgres's `::uuid` cast does
    NOT, so binding the raw string would pass the boundary check and still raise from the
    database. Every spelling of the seeded class_id must come back True."""
    conn, owner_id, class_id = db
    bare = uuid.UUID(class_id)
    for spelling in (
        class_id,
        class_id.upper(),
        bare.hex,
        f"urn:uuid:{class_id}",
        f"{{{class_id}}}",
    ):
        assert class_exists(conn, class_id=spelling, owner_id=owner_id) is True, spelling


def test_the_foreign_key_alone_admits_the_cross_owner_write(db, db_other, foreign_class, tmp_path):
    """WHY `class_exists` IS NOT REPLACEABLE BY CATCHING AN IntegrityError.

    `files.class_id` references `classes(class_id)` with no owner predicate, so enqueuing against
    ANOTHER owner's class satisfies the constraint and publishes: the row lands with this owner's
    `owner_id` and the victim's `class_id`. Demonstrated here rather than argued, because it is
    the entire reason the route checks ownership itself (F6/F12).
    """
    conn, owner_id, _ = db
    _, other_class_id = foreign_class
    conn.autocommit = True
    path = tmp_path / "lecture-3.pdf"
    path.write_bytes(b"%PDF-1.4\n")

    file_id = enqueue(conn, path=path, owner_id=owner_id, class_id=other_class_id)

    published = db_other.execute(
        "select owner_id, class_id::text from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert published == (owner_id, other_class_id), "the FK let a cross-owner write through"
    assert class_exists(conn, class_id=other_class_id, owner_id=owner_id) is False
