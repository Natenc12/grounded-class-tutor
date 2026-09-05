"""`POST /classes` - the create-a-class surface (issue #107).

The thinnest route in the slice, and deliberately so: validate, call ONE library callable, render
(ADR 0009). What a class *is* lives in `gct.classes`; this module owns the wire shape and which
status each refusal wears, and nothing else.

WHAT THIS ROUTE DOES NOT VALIDATE, AND WHY THAT IS THE DECISION. Every rule about what a name
may BE - blank is refused, unstorable is refused, an accepted one is stored VERBATIM - is
`gct.classes.validate_name`'s, so `NewClass` carries no `min_length` and no validator.
`min_length=1` would reject `''` and ACCEPT `'   '`: a second writer for HALF the rule, and the
half nobody trips over, with the two halves landing on different statuses (pydantic's 422 against
this route's 400). Two bodies behind one condition is what makes a client parse by trial - the
same reasoning `files.py`'s `_STAGING_STATUS` gives for keeping `bad_filename` off 422. A
validator restating `not name.strip()` would be worse still: that is the business rule itself,
copied into the adapter ADR 0009 forbids it in. So a refused name arrives here as one typed
exception carrying a `reason`, and this module renders it.

THIS ROUTE ONCE CAUGHT A BARE `ValueError` AND CALLED IT "blank". It shipped two wrong answers
(issue #107's fix round): `UnicodeEncodeError` is a `ValueError` subclass, so a name containing an
unpaired surrogate - reachable over the wire as the pure-ASCII body `{"name": "Bio \ud800 101"}`,
since JSON escapes decode to one - was answered "name must not be blank", and the only remedy that
sentence named could not fix the request. A NUL byte (`{"name": "\u0000"}`) raised
`psycopg.DataError`, which is not a `ValueError` at all, missed the catch and rendered a caller's
bad request as a 500. Catching an exception class BROADER than the rule it stands for is the
defect, and it is not fixed by catching two more classes here: the adapter would then be catching
psycopg's exceptions to make a decision about what a class name is, which is the library's
question. So the library refuses both before psycopg sees them and hands over a token
(`CLASS_NAME_REASONS`), and `_NAME_REFUSAL` below is the only place a token becomes a sentence.

There is NO length cap on a name, in this module or anywhere behind it. `classes.name` is `text`
(`migrations/0001_init.sql`; data-model.md calls it "display"), so a cap invented here would be a
rule with no writer anywhere else: `create_class` would keep accepting a name this route refuses,
and a script and the API would disagree about what a valid class is. Contrast `too_long` for
files, which the route can refuse honestly because `gct.config.MAX_INGEST_WORDS` and ADR 0029 own
the number. The first surface that actually has a display problem is Slice 4's class list, and a
cap belongs beside that number when one exists - not as a literal typed into one router.

`owner_id` NEVER COMES FROM THE BODY (F12). It is the `OwnerId` dependency, the adapter's one
source (`gct.api.deps.owner_id`), and `NewClass` forbids extra fields precisely so a client that
sends one anyway is TOLD rather than having it silently dropped.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from gct.api.deps import Conn, OwnerId
from gct.api.errors import ApiError
from gct.classes import ClassNameError, create_class

router = APIRouter(prefix="/classes", tags=["classes"])

# What a refused name becomes on the wire: `reason` token -> (`kind`, the sentence a client reads).
# `gct.classes.CLASS_NAME_REASONS` is the closed set this must cover, and a reason missing from it
# raises `KeyError` INSIDE the handler - which `errors._unhandled` renders as
# `{"error":{"kind":"internal"}}`, i.e. a bad request shipped as a server fault, which is exactly
# the defect this map was added to fix. The map cannot guard itself against that, so the guard is
# a test: `test_every_class_name_reason_has_a_rendering` compares the two key sets.
#
# The sentences are the ROUTE's, not `str(exc)` - the opposite of what `files.py` does with
# `StagingError`, and the difference is not style. `StagingError`'s messages were written for a
# student ("name the file (for example lecture-3.pdf) and upload it again"); `ClassNameError`'s
# name a Python function, `classes.name` and the schema's NOT NULL, because its first audience is
# a library caller. Rendering is the half a library leaves to its adapter (`files.py`'s
# `_FAILED_MESSAGE` is the same split), so the token crosses the seam and the prose does not.
#
# Every reason is 400, not 422, for the reason the module docstring gives: 422 is FastAPI's own
# request-validation status and its body carries pydantic's per-field list under `detail`.
_NAME_REFUSAL = {
    "blank": (
        "blank_name",
        "name must not be blank. Send the class name as you want it displayed - it is "
        "stored exactly as you type it.",
    ),
    "unstorable": (
        "unstorable_name",
        "name contains a character that cannot be stored - either a null byte or an unpaired "
        "surrogate, which almost always means the text was decoded with the wrong encoding "
        "somewhere upstream. Send the name as UTF-8 text with those characters removed.",
    ),
}


class NewClass(BaseModel):
    """The request body: a name, and nothing else.

    `extra="forbid"` rather than pydantic's default of dropping unknown keys, because of exactly
    one field. `owner_id` is on every OTHER row in this system and data-model.md lists it on
    `classes` too, so it is the key a client is most likely to send - and dropping it silently
    means a caller who believes they chose an owner gets rows under V1's hardcoded one with no
    signal at all (F12; ADR 0004). Forbidding turns that into a 422 that names the field. The
    cost is that a client sending a field this version does not know breaks instead of being
    tolerated, which is the direction this codebase takes at a boundary.

    `name: str` and no constraint - see the module docstring for why the blank rule is not
    restated here.
    """

    model_config = ConfigDict(extra="forbid")

    name: str


class ClassCreated(BaseModel):
    """The 201 body: the id every later call takes, and the name the class was created under.

    `class_id` is the handle for the whole rest of the loop - `POST /files` takes it as a form
    field and `POST /ask` takes it in its JSON body - so it is the one field this response exists
    for.

    `name` is echoed for the reason `UploadAccepted` echoes `filename`: it is what every surface
    that lists classes will display, and this is the one moment a client can catch a name it did
    not mean to send. The echo is the REQUEST's name rather than a re-read of the row because
    `create_class` stores an accepted name verbatim and says so - it neither trims nor rewrites.
    That is a contract this module leans on rather than re-checks, so it is pinned from this side
    too: `test_a_name_with_surrounding_whitespace_is_created_and_stored_verbatim` reads the row
    back on a second connection and fails the moment the library starts trimming.
    """

    class_id: str
    name: str


@router.post("", status_code=201)
def create(conn: Conn, owner: OwnerId, body: NewClass) -> ClassCreated:
    """Create one class for the caller and return its id; 201, because the row exists.

    201 rather than the 202 `POST /files` returns. Nothing here is deferred to another process:
    `create_class` commits before it returns, so by the time this response is written the row is
    published and the id in it is immediately usable - a client can hand it straight to
    `POST /files`. 202 would say work is pending when none is, and would leave a client with no
    way to know when the class became real.

    NO `Location` HEADER, though 201 invites one. It would have to point at `GET /classes/{id}`,
    a route no issue in this slice creates - a header naming a 404 is worse than no header. It
    becomes correct the day a class-read surface exists, and belongs to that issue.

    Refusals:
      - a name `validate_name` refuses -> **400**, with the `kind` and sentence `_NAME_REFUSAL`
        gives that reason: `blank_name` for `''`/`'   '`, `unstorable_name` for a NUL byte or an
        unpaired surrogate. The RULE is the library's, the SENTENCE is this route's - see
        `_NAME_REFUSAL` for why this route substitutes where `files.py` adopts.
      - a missing or non-string `name`, or any extra field -> the framework's 422, untouched
        (`errors._validation`). Those are shape failures, which pydantic owns; what a name may BE
        is a rule about the value, which the library owns. Two owners, two statuses, no overlap.

    NOTHING ELSE IS CAUGHT, and the catch is `ClassNameError` rather than `ValueError` on
    purpose. A database that is unreachable, or a `require_idle` violation, is a 500 `internal` -
    a client cannot act on either, and swallowing them into a 4xx would report the caller's
    request as the problem. The broad catch that used to be here was not merely imprecise: it
    silently annexed `UnicodeEncodeError`, a `ValueError` subclass, and answered it with a
    sentence about blankness. A catch this narrow cannot do that again, because the only thing
    that raises `ClassNameError` is the validator whose reasons `_NAME_REFUSAL` enumerates.

    The `require_idle` precondition itself (ADR 0025,
    guarded per ADR 0027) is satisfied by construction here: this handler reads nothing before it
    writes, and `gct.api.deps.get_conn` yields an autocommit connection anyway - that dependency
    is the one writer of the contract, and a route that builds its own connection breaks it.

    Named `create`, not `create_class`: the library callable owns that name in this module.
    """
    try:
        class_id = create_class(conn, owner_id=owner, name=body.name)
    except ClassNameError as exc:
        kind, message = _NAME_REFUSAL[exc.reason]
        raise ApiError(400, kind, message) from exc
    return ClassCreated(class_id=class_id, name=body.name)
