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

WHAT IS A STORABLE CLASS NAME is this module's question, all of it (issue #107). `text not null`
is not the answer: it admits `''`, and it admits two strings psycopg refuses to send at all - one
containing a NUL byte, and one containing an unpaired surrogate. Those two used to escape as raw
`psycopg.DataError` and `UnicodeEncodeError`, which is a caller error wearing an implementation
detail, and every caller had to know psycopg to tell it from a bug. `gct.staging.validate_filename`
already answers the identical question for a FILEname - same two characters, refused at the
library boundary as a typed error whose docstring names this exact failure ("a `UnicodeEncodeError`
on a lone surrogate ... would render a bad request as a 500"). `ClassNameError` is that shape,
applied to the writer that lacked it.
"""

from __future__ import annotations

import psycopg

from gct.db import require_idle
from gct.ids import canonical_uuid

# The closed set of reasons a name is refused - a token a caller switches on, exactly as
# `gct.staging.STAGING_REASONS` is. Widening it is a change the API's rendering map is required to
# notice: `tests/gct/api/test_classes_router.py::test_every_class_name_reason_has_a_rendering`
# compares the two sets and fails the moment a reason ships without a sentence to go with it.
CLASS_NAME_REASONS = ("blank", "unstorable")


class ClassNameError(ValueError):
    """A name refused BEFORE any statement runs - no row, and the connection as it was found.

    `reason` is a `CLASS_NAME_REASONS` token for an adapter to switch on; `remedy` is what the
    caller can DO, and `str(exc)` carries detail and remedy together, so a caller that only logs
    the exception still names the fix. Deliberately the `StagingError` shape (`gct.staging`),
    because it is deliberately the same job.

    A `ValueError` SUBCLASS, unlike `StagingError`, for one backward-compatibility reason and no
    design one: `create_class` shipped the blank refusal as a bare `ValueError` in issue #106 and
    `tests/gct/test_classes.py` pins that type and message. Subclassing keeps every existing
    caller working. It is NOT an invitation to catch `ValueError` around this call - doing that is
    what let a lone surrogate render as "name must not be blank", since `UnicodeEncodeError` is a
    `ValueError` too. Catch this class and switch on `reason`.
    """

    def __init__(self, reason: str, detail: str, *, remedy: str) -> None:
        assert reason in CLASS_NAME_REASONS, f"unknown class-name reason: {reason!r}"
        super().__init__(f"{detail} - {remedy}")
        self.reason = reason
        self.detail = detail
        self.remedy = remedy


def validate_name(name: str) -> str:
    """Return `name` UNCHANGED if it can be stored as a class name; raise `ClassNameError` if not.

    REJECT, NEVER NORMALIZE - the same rule, for the same reason, as `validate_filename`: an
    accepted name is stored verbatim and displayed verbatim, so a trimmed or scrubbed name would
    be a label the student never typed. Every check runs BEFORE any statement, so a refused call
    leaves the connection exactly as it found it.

    Refused:
      - blank: `''`, `'   '`, `'\t\n'`. `not null` is not `not blank`, and a class with an
        invisible name is unusable on every surface that lists classes.
      - not encodable as UTF-8 (an unpaired surrogate such as `'\ud800'`) - psycopg cannot put
        the value on the wire, and no `text` column can hold it.
      - a NUL (0x00) anywhere - Postgres `text` cannot contain one, whatever the encoding.

    THE THREE SETS ARE DISJOINT, so no check shadows another and the order below is not
    load-bearing: NUL and a surrogate are not whitespace, so a string containing either is never
    blank, and a whitespace-only string always encodes. `test_the_name_rules_do_not_shadow_each
    _other` asserts the reason each input gets rather than trusting that.

    Kept as-is: everything else. Surrounding whitespace, astral characters and emoji, other C0
    control characters - Postgres stores all of them in a `text` column, so refusing them here
    would be this module inventing a rule about display that no component owns (see
    `gct.api.routers.classes` on why there is no length cap either).
    """
    if not name.strip():
        raise ClassNameError(
            "blank",
            f"create_class() refuses a blank name ({name!r}): `classes.name` is what every "
            "surface displays, and the schema's NOT NULL does not reject whitespace",
            remedy="pass the name the student typed; the value is stored verbatim",
        )
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise ClassNameError(
            "unstorable",
            f"class name {name!r} is not valid UTF-8 (it contains an unpaired surrogate), so "
            "psycopg cannot send it and no text column can hold it",
            remedy="pass text decoded as UTF-8; an unpaired surrogate means it was decoded wrong",
        ) from None
    if "\x00" in name:
        raise ClassNameError(
            "unstorable",
            f"class name {name!r} contains a NUL (0x00) byte, which a PostgreSQL text field "
            "cannot store",
            remedy="remove the NUL byte from the name",
        )
    return name


def create_class(conn: psycopg.Connection, *, owner_id: str, name: str) -> str:
    """Create one `classes` row for `owner_id`; commit; return its `class_id` as a `str`.

    `str`, not psycopg's `UUID`: every downstream `gct` signature types ids as `str` and casts
    `%s::uuid` at the SQL boundary (`enqueue`, `retrieve`, `ask`), so this returns the shape the
    next call takes rather than one the caller has to convert.

    `class_id` is DB-generated (`gen_random_uuid()` default) and RETURNed - one round trip, and
    no client-side uuid to be handed a colliding value. `_resolve_class` minted its own client-side
    with no reason recorded for doing so; the library lets the DB mint, as `enqueue` does.

    AN UNUSABLE NAME IS REFUSED, not stored and not trimmed - `validate_name` above holds the
    whole rule and its argument. It runs AFTER `require_idle`, so a call that is invalid in both
    ways reports the connection first; see the comment on that line. Refusing at the library
    boundary means no adapter has to remember it, and refusing rather than trimming means the
    caller's value is stored VERBATIM when it is accepted: a name with surrounding whitespace is
    not silently rewritten into a different name, which is the kind of quiet conversion this
    codebase does not do at boundaries. Both guards run BEFORE any statement, so a rejected call
    leaves the connection exactly as it found it.

    RAISES `ClassNameError`, NEVER A BARE `ValueError`, and that distinction is the fix for a
    shipped defect (issue #107's fix round): `UnicodeEncodeError` IS a `ValueError`, so a caller
    catching `ValueError` around this call to mean "blank" told a client that a name containing
    an unpaired surrogate was blank - and the only remedy that message named could not fix the
    request. A NUL byte missed such a catch entirely, as `psycopg.DataError`, and shipped a
    caller error as a 500. Both are now refused by `validate_name` before psycopg sees them, with
    a `reason` an adapter switches on.

    PRECONDITION ON `conn` (ADR 0025): it MUST NOT already be inside a transaction, or the block
    below degrades to a SAVEPOINT and this function returns having published nothing. Enforced by
    `require_idle` (ADR 0027), whose message names the remedy. Commits before returning, same
    contract as `enqueue`.
    """
    # Connection guard FIRST, then the payload guard. Not a taste call and not this function's
    # own idea: `gct.db.require_idle` states the rule ("Called first by every writer") and
    # `index_file` carries the argument for it, which is not restated here. Both fire before the
    # transaction opens, so a refused call writes nothing either way - what the order decides is
    # WHICH error a doubly-invalid call gets, and it must be the same answer every writer gives.
    require_idle(conn, "create_class")
    validate_name(name)

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
    `gct.ids.canonical_uuid` is the parse, shared with the library's other id-taking boundaries so
    that the SET OF EXCEPTIONS the parser can raise has one writer. It shipped here catching only
    `ValueError`, which let `None` (a `TypeError`) and an already-parsed `uuid.UUID` (an
    `AttributeError`) escape with the parser's own message and no remedy - the opposite of what
    this guard is for, and the whole of #126 at this site.

    A READER, so deliberately NO `require_idle`: a SELECT inside a transaction is correct, and
    this function must be callable from anywhere. But a SELECT is also enough to OPEN psycopg's
    implicit transaction on a non-autocommit connection (ADR 0025), so a caller that checks
    ownership here and then calls `enqueue` on the same connection arms the trap the writers
    guard against. That is the caller's wiring to get right - autocommit, which is the API's
    connection contract (`gct.api.deps.get_conn`).
    """
    canonical = canonical_uuid(
        class_id,
        fn="class_exists",
        param="class_id",
        remedy=(
            "An id that is not a uuid cannot name any class - reject it at the boundary rather "
            "than asking the database, which would abort the connection's open transaction."
        ),
    )

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
