"""Files - the read side of the student-facing processing status (issue #106).

`files.status` is the DOMAIN truth `GET /files/:id` returns, distinct from `jobs.state`, which is
the execution substrate (data-model.md §`files`; ADR 0011). The write path stamps `status` and
`failed_reason` - `enqueue` writes `queued`, the worker moves it to `processing` / `ready`, and
`_bury` is the ONLY production writer of `failed_reason` - and until this module nothing in
`src/gct` read them back. This is that reader, so the API adapter can render without carrying
the query itself (ADR 0009).

`failed_reason` is a CLOSED taxonomy whose source is the CHECK constraint on the column, not a
prose list: five values in `migrations/0001_init.sql` plus `too_long` from
`migrations/0003_failed_reason_too_long.sql` (ADR 0020, terminal set extended per ADR 0029).
This module passes the stored value through UNTRANSLATED; the constraint is what keeps it
one of the values the route knows how to render actionably. The membership test lives in
`tests/gct/test_files.py`, which reads the set off the constraint at test time so it cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from gct.ids import canonical_uuid


@dataclass(frozen=True)
class FileStatus:
    """What the student is told about one uploaded file.

    PUBLIC CONTRACT - the API adapter (`GET /files/:id`) renders this.

    `status` is one of `queued · processing · ready · failed` (the column's CHECK). `failed_reason`
    is populated on `failed` and `None` otherwise - the CHECK admits NULL, and the worker's
    `processing` write clears a reason left by an earlier attempt (#24, closed on the
    out-of-order side by #86), so a non-failed row with a reason is not a state this reader ever
    has to represent. `filename` is the original
    upload name, the same string the chunks carry as their citation label.

    A frozen dataclass rather than a tuple or dict, for the reason `Job` and `AskResult` are:
    the fields are named at the seam, so a route that renders `result.failed_reason` cannot
    silently pick up the wrong positional slot when a field is added.
    """

    status: str
    failed_reason: str | None
    filename: str


def get_file_status(conn: psycopg.Connection, *, file_id: str, owner_id: str) -> FileStatus | None:
    """The status row for `file_id` as seen by `owner_id`, or `None` if there is no such row.

    ONE `None` FOR TWO CASES, BY CONSTRUCTION. A file that does not exist and a file that belongs
    to ANOTHER owner return the same value, because the scope is a single `WHERE file_id AND
    owner_id` and there is no code path that learns which clause failed. That is F12 - never leak
    cross-owner existence - enforced where it cannot be forgotten: a route that renders `None` as
    404 gets the same 404 for both without having to know there are two cases. The rejected
    shape was a raised not-found exception: the two cases would then need the same exception
    with the same message, a property every future edit to the message could break, and a
    missing row is not a programming error to begin with - it is a legitimate answer, in the
    same sense that a refusal is a successful grounder outcome (ADR 0014-0016), and callers
    check it rather than catch it.

    `file_id` IS VALIDATED AS A UUID BEFORE ANY STATEMENT, AND THE CANONICAL FORM IS WHAT GETS
    BOUND. Letting Postgres reject the cast would raise a psycopg `DataError` the adapter has no
    business catching, and - worse - on a non-autocommit connection the failed statement aborts the
    implicit transaction, so every later statement on that connection fails with
    `InFailedSqlTransaction`. A malformed id is a caller error, so it is refused with a message
    naming the remedy and the connection is left as it was found. A well-formed id that matches
    nothing is the `None` case above, not this one.

    Binding `str(uuid.UUID(file_id))` rather than the raw string is load-bearing, not tidiness:
    Python's parser accepts forms Postgres's `::uuid` cast does not (`urn:uuid:<id>` is refused
    with `InvalidTextRepresentation`), so validating the raw string and then binding it would pass
    the check and still hit the exact abort the check exists to prevent. Every id the parser
    accepts - hyphenated, braced, bare 32-hex, urn - reaches the database as the one spelling it
    accepts too. `gct.ids.canonical_uuid` is that parse, shared with the library's other id-taking
    boundaries so the set of exceptions `uuid.UUID` can raise has one writer. It shipped here
    catching only `ValueError`, so `None` (a `TypeError`) and an already-parsed `uuid.UUID` (an
    `AttributeError`) escaped with the parser's own message and no remedy - #126 at this site.

    This is a READER and deliberately does not call `require_idle`: a SELECT inside a
    transaction is correct. But a SELECT is also enough to OPEN psycopg's implicit transaction on a
    non-autocommit connection (ADR 0025), so a caller that reads status and then calls a writer on
    the same connection arms exactly the trap the writers guard against. The remedy is the
    caller's wiring - autocommit at the request-scoped connection, which is the API's contract.
    """
    canonical = canonical_uuid(
        file_id,
        fn="get_file_status",
        param="file_id",
        remedy=(
            "An id that is not a uuid cannot name any file - reject it at the boundary rather "
            "than asking the database, which would abort the connection's open transaction."
        ),
    )

    row = conn.execute(
        """
        select status, failed_reason, filename
        from files
        where file_id = %(file_id)s::uuid
          and owner_id = %(owner_id)s
        """,
        {"file_id": canonical, "owner_id": owner_id},
    ).fetchone()
    if row is None:
        return None
    return FileStatus(status=row[0], failed_reason=row[1], filename=row[2])
