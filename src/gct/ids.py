"""Ids - the one place a caller-supplied uuid is turned into a spelling Postgres accepts, or
refused with a message naming the remedy (#126).

WHY THIS IS A MODULE AND NOT FOUR COPIES. Every id-taking callable in the library binds into a
`%(...)s::uuid` cast, and every one of them therefore needs the same two facts: which spellings
`uuid.UUID` accepts that the cast does not, and which exceptions `uuid.UUID` raises. The second
fact is what drifted - `class_exists` and `get_file_status` shipped catching only `ValueError`,
so two of the parser's three failure modes escaped carrying its own message and no remedy, which
is the opposite of what a boundary guard is for. A tuple of exception types copied at four sites
is a fact with four writers; this module makes it one.

THE TWO SHAPES ARE A REAL CHOICE, NOT A CONVENIENCE PAIR, and the criterion is WHERE THE ID COMES
FROM rather than whether the caller reads or writes. `gct.jobs.queue.enqueue`'s docstring is the
writer of that argument and it is cited, not restated: an id a person typed gets `canonical_uuid`,
an id read back out of the database - where it is canonical by construction - gets
`require_canonical_uuid`, because any other spelling there is an upstream bug worth hearing about
rather than quietly converting.

NOT ADOPTED EVERYWHERE, deliberately. `enqueue` and `ingest.pipeline.ingest_file` keep the inline
guards they shipped with: each carries a long docstring arguing its own case, and moving the code
out from under that prose would leave the prose describing something that is no longer there.
They are the shape this module was extracted from, so the two remaining copies of the exception
set are visible rather than hidden - which is the honest cost of scoping this to the sites #126
names.
"""

from __future__ import annotations

import uuid

# `uuid.UUID` reports a bad argument in three different ways depending on what it was handed, and
# the set is the whole reason this module exists:
#   - a malformed STRING          -> ValueError
#   - `None`                      -> TypeError ("one of the hex, bytes, ... arguments must be
#                                    given")
#   - anything without `.replace` -> AttributeError, raised from inside the parser: an `int`, a
#     `list`, or - the case that actually happens - an already-parsed `uuid.UUID` a caller passed
#     instead of `str(...)`.
# Named rather than inlined so the two callables below cannot drift apart from each other.
_PARSER_REJECTIONS = (ValueError, AttributeError, TypeError)


def canonical_uuid(value: object, *, fn: str, param: str, remedy: str) -> str:
    """LENIENT: return the canonical spelling of `value`, or raise `ValueError` naming the remedy.

    For ids a person supplies. `uuid.UUID` accepts spellings Postgres's `::uuid` cast refuses -
    `urn:uuid:<id>` is the demonstrated one - so validating the raw string and then binding THAT
    passes the check and still fails at the cast, which is the precise trap #121 closed in
    `enqueue` and #126 closed at the sites that had copied the shape without the fix. Binding
    `str(uuid.UUID(value))` normalises every accepted spelling to the one Postgres takes.

    HOW LENIENT THIS ACTUALLY IS: `uuid.UUID` is not a spelling whitelist. It strips `urn:` and
    `uuid:` prefixes, strips surrounding braces, and removes hyphens WHEREVER they fall, so an id
    with a misplaced hyphen - which the cast itself refuses - resolves here. `enqueue`'s docstring
    is the writer of why that leniency is chosen rather than accidental.

    `fn` names the callable the CALLER called, not necessarily the one that raises: a private
    helper naming itself would send the reader to a frame their code never mentions.
    """
    try:
        return str(uuid.UUID(value))  # type: ignore[arg-type]
    except _PARSER_REJECTIONS as exc:
        raise ValueError(
            f"{fn}() requires a uuid {param} as a str (pass str(id) if you hold a uuid.UUID); "
            f"got {value!r}. {remedy}"
        ) from exc


def require_canonical_uuid(value: object, *, fn: str, param: str, remedy: str) -> str:
    """STRICT: return `value` unchanged if it is already the canonical spelling, else raise.

    For ids that come back out of the database, where they are canonical by construction, so any
    other spelling is an upstream bug. Refusing it loudly is the point - converting it quietly
    would hide the bug and hand the same wrong id to the next layer. `ingest_file`'s `file_id`
    guard is the shipped instance of this argument.

    The comparison is against `value.lower()`, not `value`: `uuid.UUID` renders lowercase, so an
    all-caps hyphenated id is canonical as far as Postgres is concerned and only the case differs.
    """
    try:
        canonical = str(uuid.UUID(value)) == value.lower()  # type: ignore[union-attr, arg-type]
    except _PARSER_REJECTIONS:
        canonical = False
    if not canonical:
        raise ValueError(
            f"{fn}() requires a canonical uuid {param} (pass str(id)); got {value!r}. {remedy}"
        )
    return value  # type: ignore[return-value]
