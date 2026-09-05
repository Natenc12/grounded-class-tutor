"""DB-backed tests for `get_file_status` (issue #106).

The reader is driven through the REAL write path where one exists: `enqueue` creates the
`queued` row. The later transitions are set by SQL on `db_other` (autocommit, a second
connection) because the worker's `_bury` is the only production writer of `failed_reason` and
driving it needs a claimed job, a lease and a `ParseError` — machinery that would test the
worker, not this reader. Setting the columns directly is what a reader test can honestly do,
and the CHECK constraint (not this file) is what keeps the values legal: the negative arm of
`test_reads_back_every_reason_the_constraint_admits` proves that constraint is real by having it
refuse a value outside the set.

The reason set is DERIVED from `pg_catalog` at test time, never copied here: a copy is a second
writer for a fact `migrations/` owns and has already widened once (ADR 0020, terminal set
extended per ADR 0029).
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from pathlib import Path

import psycopg
import pytest

from gct.files import FileStatus, get_file_status
from gct.ingest.parse import TERMINAL_REASONS
from gct.jobs.queue import enqueue


def _lecture(tmp_path: Path) -> Path:
    """A real file on disk, so `staging_ref`'s `resolve()` has something honest to resolve."""
    source = tmp_path / "lecture-3.pdf"
    source.write_bytes(b"%PDF-1.4 not a real pdf")
    return source


def _enqueued(db, tmp_path: Path) -> tuple[str, str]:
    """`(file_id, owner_id)` for one file put through the real write path's first step."""
    conn, owner_id, class_id = db
    conn.autocommit = True
    file_id = enqueue(conn, path=_lecture(tmp_path), owner_id=owner_id, class_id=class_id)
    return file_id, owner_id


def _set(db_other, file_id: str, status: str, failed_reason: str | None) -> None:
    """Stamp the two status columns directly — see the module docstring for why that is honest."""
    db_other.execute(
        "update files set status = %s, failed_reason = %s where file_id = %s::uuid",
        (status, failed_reason, file_id),
    )


def _reasons_the_constraint_admits(db_other) -> set[str]:
    """The `failed_reason` CHECK's member set, read off `pg_catalog` — the schema's own answer.

    `pg_get_constraintdef` deparses the check as `CHECK ((failed_reason = ANY (ARRAY['a'::text,
    ...])))`; the quoted literals are the members. Reading it here means a migration that widens
    the set widens this test with it, with no copy to fall behind.
    """
    (definition,) = db_other.execute(
        "select pg_get_constraintdef(oid) from pg_constraint where conname = %s",
        ("files_failed_reason_check",),
    ).fetchone()
    members = set(re.findall(r"'([a-z_]+)'::text", definition))
    assert members, f"could not read the member set out of {definition!r}"
    return members


def test_the_constraint_set_is_the_parser_taxonomy_plus_transient_exhausted(db_other):
    """The two writers of the reason taxonomy agree: `parse.py`'s terminal tuple plus the one
    reason the worker mints itself (`transient_exhausted`, ADR 0020 §1's transient half) is
    EXACTLY the schema's set. A migration that widens one without the other fails here, in
    either direction.
    """
    assert _reasons_the_constraint_admits(db_other) == set(TERMINAL_REASONS) | {
        "transient_exhausted"
    }


def test_a_queued_file_reads_back_as_queued_with_no_reason(db, db_other, tmp_path):
    """The real write path's first stamp, read back exactly: `(queued, None, filename)`."""
    file_id, owner_id = _enqueued(db, tmp_path)
    conn = db[0]

    got = get_file_status(conn, file_id=file_id, owner_id=owner_id)

    assert got == FileStatus(status="queued", failed_reason=None, filename="lecture-3.pdf")
    assert isinstance(got, FileStatus)


@pytest.mark.parametrize("status", ["queued", "processing", "ready"])
def test_non_failed_statuses_carry_no_reason(db, db_other, tmp_path, status):
    """Every non-`failed` status reads back with `failed_reason=None`; the arm on the other side
    is that the SAME row, moved to `failed` with a reason, reads that reason back — so `None` is
    the column's value, not the reader dropping it.
    """
    file_id, owner_id = _enqueued(db, tmp_path)
    conn = db[0]

    _set(db_other, file_id, status, None)
    assert get_file_status(conn, file_id=file_id, owner_id=owner_id) == FileStatus(
        status=status, failed_reason=None, filename="lecture-3.pdf"
    )

    _set(db_other, file_id, "failed", "empty")
    assert get_file_status(conn, file_id=file_id, owner_id=owner_id) == FileStatus(
        status="failed", failed_reason="empty", filename="lecture-3.pdf"
    )


def test_reads_back_every_reason_the_constraint_admits(db, db_other, tmp_path):
    """All six reasons — derived, not listed — pass through UNTRANSLATED, and a value outside the
    set is refused by the schema (the arm that proves the derived set is the real gate).
    """
    file_id, owner_id = _enqueued(db, tmp_path)
    conn = db[0]
    reasons = _reasons_the_constraint_admits(db_other)

    seen = set()
    for reason in sorted(reasons):
        _set(db_other, file_id, "failed", reason)
        got = get_file_status(conn, file_id=file_id, owner_id=owner_id)
        assert got == FileStatus(status="failed", failed_reason=reason, filename="lecture-3.pdf")
        seen.add(got.failed_reason)
    assert seen == reasons

    with pytest.raises(psycopg.errors.CheckViolation):
        _set(db_other, file_id, "failed", "not_a_reason")
    # The refused write changed nothing: the last legal reason is still what the reader sees.
    assert get_file_status(conn, file_id=file_id, owner_id=owner_id).failed_reason == max(reasons)


def test_another_owners_file_is_indistinguishable_from_a_missing_one(db, db_other, tmp_path):
    """F12: the cross-owner read and the read of a uuid that names nothing return EQUAL values,
    and the owner's own read of the same row is the positive arm that proves the row exists.
    """
    file_id, owner_id = _enqueued(db, tmp_path)
    conn = db[0]
    other_owner = f"test-owner-{uuid.uuid4()}"
    missing_id = str(uuid.uuid4())

    as_other = get_file_status(conn, file_id=file_id, owner_id=other_owner)
    as_missing = get_file_status(conn, file_id=missing_id, owner_id=owner_id)
    as_owner = get_file_status(conn, file_id=file_id, owner_id=owner_id)

    assert as_other == as_missing
    assert as_other is None
    assert as_owner == FileStatus(status="queued", failed_reason=None, filename="lecture-3.pdf")
    # The row really is there for the other owner to have been refused from — not a missing row
    # twice over.
    assert db_other.execute(
        "select count(*) from files where file_id = %s::uuid", (file_id,)
    ).fetchone() == (1,)


def test_a_malformed_file_id_is_refused_before_any_statement(db, tmp_path):
    """A non-uuid id raises `ValueError` naming the remedy and leaves the connection's
    transaction state untouched — letting Postgres reject the cast would abort the open
    transaction and poison every later statement on a non-autocommit connection.
    """
    file_id, owner_id = _enqueued(db, tmp_path)
    conn = db[0]
    conn.autocommit = False
    conn.execute("select 1").fetchone()  # an open transaction, as a read-then-write handler has
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS

    with pytest.raises(ValueError, match=r"get_file_status\(\) requires a uuid file_id"):
        get_file_status(conn, file_id="lecture-3", owner_id=owner_id)

    # Not aborted: the same transaction still answers, and answers correctly.
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
    assert get_file_status(conn, file_id=file_id, owner_id=owner_id) == FileStatus(
        status="queued", failed_reason=None, filename="lecture-3.pdf"
    )
    conn.rollback()


def test_every_spelling_pythons_parser_accepts_reaches_postgres_in_a_form_it_accepts(db, tmp_path):
    """The arm the malformed-string test cannot supply: an id Python ACCEPTS but Postgres's
    `::uuid` cast does NOT (`urn:uuid:<id>`). Validating and then binding the raw string would pass
    the check and still abort the open transaction; binding the canonical form must not. Each
    spelling returns the same row as the canonical id, on the NON-autocommit connection, and the
    transaction is still INTRANS and still answering afterwards - the braces and bare-hex forms
    ride along so the pin covers every spelling the parser admits, not just the one that bites.
    """
    file_id, owner_id = _enqueued(db, tmp_path)
    conn = db[0]
    conn.autocommit = False
    conn.execute("select 1").fetchone()
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
    expected = FileStatus(status="queued", failed_reason=None, filename="lecture-3.pdf")
    assert get_file_status(conn, file_id=file_id, owner_id=owner_id) == expected

    # Postgres itself refuses the urn spelling - measured through a savepoint so the refusal
    # cannot poison this test's own transaction. This is what makes the pin below non-trivial.
    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        with conn.transaction():
            conn.execute("select %s::uuid", (f"urn:uuid:{file_id}",)).fetchone()

    for spelling in (f"urn:uuid:{file_id}", "{" + file_id + "}", file_id.replace("-", "")):
        assert get_file_status(conn, file_id=spelling, owner_id=owner_id) == expected, spelling
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS, spelling

    # Still a live transaction, not INERROR: a following statement succeeds.
    assert conn.execute("select 1").fetchone() == (1,)
    conn.rollback()


def test_file_status_refuses_attribute_assignment(db, tmp_path):
    """`FileStatus` is a PUBLIC CONTRACT and immutable: assigning any field raises
    `FrozenInstanceError`, and the fields read back unchanged afterwards (the arm that proves the
    refusal refused, rather than raised after mutating).
    """
    file_id, owner_id = _enqueued(db, tmp_path)
    got = get_file_status(db[0], file_id=file_id, owner_id=owner_id)
    assert got == FileStatus(status="queued", failed_reason=None, filename="lecture-3.pdf")

    for field, value in (("status", "ready"), ("failed_reason", "empty"), ("filename", "x.pdf")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(got, field, value)
    assert (got.status, got.failed_reason, got.filename) == ("queued", None, "lecture-3.pdf")


# The three ways `uuid.UUID` reports a bad argument. `get_file_status` shipped catching only the
# first, so the other two escaped carrying the parser's own message and no remedy (#126).
_NOT_A_USABLE_ID = {
    "malformed": "lecture-3",  # ValueError from the parser
    "none": None,  # TypeError: no hex/bytes/fields/int argument was given
    "int": 12345,  # AttributeError: int has no `.replace`
    "already_parsed": uuid.UUID("6f1e1b4a-0f0e-4a3e-9a7d-2f0a1b2c3d4e"),  # AttributeError
}


@pytest.mark.parametrize("kind", sorted(_NOT_A_USABLE_ID))
def test_get_file_status_refuses_every_way_the_parser_can_reject_an_id(db, tmp_path, kind):
    """One remedy-naming `ValueError` for all three of `uuid.UUID`'s failure modes (#126).

    Only the malformed STRING was covered before, and it is the only one of the three a route can
    produce today - which is what made the other two a trap rather than a bug. `None` escaped as
    a `TypeError` and an already-parsed `uuid.UUID` as an `AttributeError`, both naming the
    parser's internals instead of the remedy.

    Run on a connection with an OPEN transaction, and the arm that matters is the one after the
    raise: still INTRANS rather than INERROR, and the reader still answers correctly for a real
    id. A guard that let the parser's exception through would not abort anything either - but a
    guard that had validated and then bound the RAW string would, and that is the shape this
    module's sibling test pins from the other side.
    """
    file_id, owner_id = _enqueued(db, tmp_path)
    conn = db[0]
    conn.autocommit = False
    conn.execute("select 1").fetchone()
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS

    with pytest.raises(ValueError, match=r"get_file_status\(\) requires a uuid file_id") as caught:
        get_file_status(conn, file_id=_NOT_A_USABLE_ID[kind], owner_id=owner_id)

    assert "cannot name any file" in str(caught.value), "the refusal names no remedy"
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
    assert get_file_status(conn, file_id=file_id, owner_id=owner_id) == FileStatus(
        status="queued", failed_reason=None, filename="lecture-3.pdf"
    )
    conn.rollback()
