"""`POST /files` + `GET /files/{file_id}` - the upload and status surface (issue #110).

A multipart upload becomes a queued file in three steps and no more: check the class belongs to
the caller, stage the bytes durably, enqueue the job. Every one of those is a library callable
(ADR 0009) - this module validates, calls, and renders, and holds no ingest logic of its own.

Rendering is the half the library deliberately leaves to this module. `get_file_status` passes
`files.failed_reason` through UNTRANSLATED, because what a student should DO about a failure is a
product decision and not a database one; `_FAILED_MESSAGE` below is where that decision lives,
and it is the only place in the repo that turns a reason token into a sentence.

THE ORDER OF THE THREE IS A DECISION, not the order they happened to be written in. The ownership
check runs FIRST because it is the only refusal that can be decided before anything durable
exists: a 404 for someone else's class then leaves nothing behind. Staging first would write a
file into the staging directory and then refuse the request, and nothing ever deletes it (ADR
0010 keeps staged files by design). The remaining window - stage succeeded, `enqueue` raised - is
accepted for that same reason and recorded in `upload_file`'s docstring rather than swept up.

THE CONNECTION CONTRACT IS WHAT MAKES THIS SHAPE LEGAL. `class_exists` is a SELECT and psycopg
opens its implicit transaction on any first statement, so on a non-autocommit connection
`enqueue`'s `require_idle` would raise on the next line (ADR 0025, guarded per ADR 0027). It does
not, because `gct.api.deps.get_conn` yields an autocommit connection - that dependency is the one
writer of this precondition, and a route that builds its own connection breaks this handler.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel

from gct.api.deps import Conn, OwnerId
from gct.api.errors import ApiError
from gct.classes import class_exists
from gct.config import MAX_INGEST_WORDS
from gct.files import get_file_status
from gct.jobs.queue import enqueue
from gct.staging import StagingError, stage

router = APIRouter(prefix="/files", tags=["files"])

# The HTTP status each request-time staging refusal renders as. `gct.staging.STAGING_REASONS` is
# the closed set this must cover, and a reason missing from it raises `KeyError` INSIDE the
# handler - which `errors._unhandled` renders as `{"error":{"kind":"internal"}}`, i.e. a bad
# request shipped as a server fault. The map cannot guard itself against that, so the guard is a
# test: `test_every_staging_reason_has_a_status` compares `set(_STAGING_STATUS)` against
# `set(STAGING_REASONS)`, and fails the moment the library widens the set without a status here.
#   `bad_filename` -> 400, not 422: 422 is FastAPI's own request-validation status and its body
#   already carries pydantic's per-field list under `detail` (`errors._validation`). Two different
#   bodies behind one status is what makes a client parse by trial.
#   `too_large` -> 413: the request was well-formed and refused for its SIZE, which is the one
#   thing a client can act on without reading prose - it retries with a smaller file.
_STAGING_STATUS = {"bad_filename": 400, "too_large": 413}


class UploadAccepted(BaseModel):
    """The 202 body: the id to poll, and the name the file was accepted under.

    `file_id` is what `GET /files/{file_id}` takes - the upload is ACCEPTED here, not done, so
    this is the only handle the client gets on work that has not started. `filename` is the name
    as stored, which is the citation label every answer will show the student
    (`gct.staging.validate_filename` accepts or refuses a name, never rewrites it), so echoing it
    is the one moment a client can catch a name it did not mean to send.

    `status` is deliberately NOT here. It would be a literal `"queued"` copied from `enqueue`'s
    INSERT - a second writer for the initial status, drifting the first time that value changes -
    and the 202 already says the work has not happened. The status surface is `GET /files/{id}`.
    """

    file_id: str
    filename: str


@router.post("", status_code=202)
def upload_file(
    conn: Conn,
    owner: OwnerId,
    class_id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> UploadAccepted:
    """Stage an uploaded file and queue it for ingestion; 202 with the id the client polls.

    202 rather than 201: nothing has been ingested when this returns. The bytes are on disk and a
    `jobs` row exists, and a SEPARATE worker process picks it up later (ADR 0006/0011) - so the
    honest statement is "accepted", and the client learns the outcome by polling
    `GET /files/{file_id}`, never from this response.

    Refusals, and what each leaves behind:
      - the class is not this owner's, or does not exist -> **404**, never 403, and the two cases
        are one `False` from `class_exists`, so this route cannot leak cross-owner existence even
        by accident (F12). Nothing has been staged at this point: this check is first precisely
        so a refusal is free.
      - `class_id` is not a uuid -> 400, and the sentence is the route's own: `class_exists`
        does raise `ValueError` here, but its message explains a connection-abort concern a
        client cannot act on.

    THE ROUTE PARSES `class_id` ONCE, AT THE TOP, and that parse is what RAISES the 400 above -
    it runs before any library call, so the client reads the route's own sentence rather than
    `class_exists`'s, which explains a connection concern a client cannot act on. Passing the
    canonical spelling on from there costs nothing and keeps both calls talking about one id.

    It is no longer load-bearing for CORRECTNESS, and it was: until #121 `enqueue` bound
    `class_id` raw into `%(class_id)s::uuid` while `class_exists` normalised first, so a
    `urn:uuid:<id>` - which `uuid.UUID` accepts and the cast refuses - cleared the ownership
    check and then aborted the INSERT after `stage` had written the bytes: a 500, a staged file
    no `files` row points at, and nothing to poll. `enqueue` now canonicalises at its own
    boundary like every other library entry point, so that orphan is closed at the writer rather
    than worked around in this one caller. The parse STAYS anyway, for the 400 above: deleting it
    would hand the client `class_exists`'s message instead.
      - the filename is unusable -> 400 `bad_filename`; the upload exceeds the byte cap -> 413
        `too_large`. Both come from `stage`, both are REQUEST-time (no `files` row exists, so
        there is nothing for a `failed_reason` to hang on - `gct.staging`'s module docstring), and
        both carry the library's own sentence, which already names the remedy.

    ONE ACCEPTED LEAK, RECORDED RATHER THAN SWEPT UP: if `enqueue` raises after `stage` returned -
    the database is unreachable, or the class was deleted in the window between the check and the
    insert - the staged file stays on disk with no row pointing at it. It is NOT deleted here.
    ADR 0010 rejected delete-after-ingest as V1's default and keeps staged files deliberately
    (they let chunking be re-tuned without re-uploading), so a `finally` that unlinked this one
    would be a second, contradictory policy for the same directory, reachable only on a path that
    is already failing. The leak is bounded by the byte cap `stage` enforces, and the request
    fails loudly as a 500 rather than reporting a file that was never queued.

    Content is NOT judged here - not the extension, not emptiness. A `.txt`, a corrupt PDF and a
    zero-byte file all stage and enqueue cleanly and fail TERMINALLY at parse with a reason the
    student can read (ADR 0020), which is the designed route and keeps `parse_file` the one
    writer of "is this file usable". `file.filename` is passed to `stage` unchecked for the same
    reason: `starlette.UploadFile.filename` is `str | None` and `validate_filename` is written to
    refuse `None` itself, so a pre-check here would be a second writer for that judgement.
    """
    try:
        class_uuid = str(uuid.UUID(class_id))
    except ValueError as exc:
        raise ApiError(
            400,
            "bad_class_id",
            "class_id must be a uuid. Use the id returned when the class was created.",
        ) from exc
    if not class_exists(conn, class_id=class_uuid, owner_id=owner):
        raise ApiError(
            404,
            "class_not_found",
            "No such class. Check the class id, or create the class before uploading to it.",
        )

    try:
        staged = stage(file.file, filename=file.filename)
    except StagingError as exc:
        # `kind` IS the library's reason token, adopted verbatim rather than re-minted: it is
        # already a closed, machine-readable set (`STAGING_REASONS`) and a second vocabulary for
        # the same two failures would be a copy to keep in step. `message` is `str(exc)` - the
        # detail sentence AND the remedy, which is exactly what `ErrorBody.message` documents
        # itself to be. `detail` stays null: it is documented as optional STRUCTURE, and putting
        # the human sentence there would hand the first client to parse it prose where the schema
        # promised shape.
        raise ApiError(_STAGING_STATUS[exc.reason], exc.reason, str(exc)) from exc

    # `class_uuid`, the form this route already parsed for its 400 - not because `enqueue` needs
    # it (since #121 it canonicalises whatever spelling it is handed) but because re-handing the
    # caller's raw string here would send one id through two parsers for no reason.
    file_id = enqueue(conn, path=staged, owner_id=owner, class_id=class_uuid)
    return UploadAccepted(file_id=file_id, filename=Path(staged).name)


# WHAT THE STUDENT READS, one sentence per state. Both maps are keyed by a value the DATABASE
# constrains - `files.status` and `files.failed_reason` each have a CHECK - and neither can be
# derived, because prose is not derivable: what to DO about `protected` is a product judgement.
# So the guard against drift is a test, not a clever import: `test_files_router.py` reads both
# member sets off `pg_constraint` at test time and asserts these maps cover them exactly, which
# fails the moment a migration widens either set without a sentence to go with it.
#
# Deliberately NOT sourced from `gct.ingest.parse.TERMINAL_REASONS`: that tuple is FIVE values
# and omits `transient_exhausted`, which the worker mints itself (ADR 0020 §1's transient half),
# so a map built from it would be missing exactly the reason a student is least able to diagnose.
_STATUS_MESSAGE = {
    "queued": "This file is in line to be processed. Nothing is needed from you - check back "
    "in a moment.",
    "processing": "This file is being read and indexed right now. Check back in a moment.",
    "ready": "This file is ready. You can ask questions about it now.",
}

_FAILED_MESSAGE = {
    "unparseable": "We could not read any text out of this file - it may be a scan, a photo of "
    "a page, or a damaged export. Upload a version whose text you can select and copy (run a "
    "scan through OCR first), then try again.",
    "protected": "This file is password-protected, so it could not be opened. Save or export an "
    "unprotected copy and upload that instead.",
    # The supported set is `parse_file`'s dispatch on the suffix (`gct/ingest/parse.py`), which
    # is its writer; if a parser is added there, this sentence is the copy that has to follow.
    "unsupported": "This kind of file cannot be read. Upload the material as a PDF (.pdf) or a "
    "PowerPoint deck (.pptx) and try again.",
    "empty": "This file opened, but there is no text in it - it may be blank, or every page may "
    "be an image. Check the file, then upload a version with text you can select.",
    # The number comes from the config constant the ingest ceiling is set by (ADR 0029), not
    # from a literal typed here: a student told "too long" without the bound cannot act on it,
    # and a bound restated by hand is wrong the day it moves.
    "too_long": f"This file is longer than the {MAX_INGEST_WORDS:,}-word limit for a single "
    "upload. Split it up - one lecture, or a few chapters, per file - and upload the parts.",
    "transient_exhausted": "Processing kept failing for reasons on our side and we have stopped "
    "retrying. Upload the file again; if it keeps failing the service is having trouble, and "
    "there is nothing wrong with your file.",
}

# The render for a `failed` row whose reason this module has no sentence for - a widened CHECK
# constraint, or the NULL the constraint still admits. It is NOT a swallowed error: `status` and
# `failed_reason` are returned beside it unchanged, so the raw token is never hidden. Raising
# instead would turn a legitimate, correctly-recorded outcome into a 500 that tells the student
# nothing at all, which is a worse answer than a general one.
_UNKNOWN_FAILURE = (
    "This file could not be processed. Upload it again, or try a different export of the same "
    "material; if it keeps failing, the file itself may not be usable."
)


class FileStatusResponse(BaseModel):
    """What `GET /files/{file_id}` tells the student about one uploaded file.

    `status` and `failed_reason` are the stored values, passed through untouched - the machine
    -readable pair a client switches on, and the two columns the worker actually writes.
    `message` is this route's rendering of them: one sentence naming what happened and what to
    do next, which is the slice's Exit criterion ("the status surface exposes actionable
    terminal reasons") and the reason `failed_reason` alone is not a sufficient answer.

    NO `file_id` FIELD. The caller supplied it in the URL, so echoing it adds nothing a client
    does not have - and echoing it honestly would mean canonicalising the spelling
    (`get_file_status` accepts `urn:uuid:...` and bare 32-hex), i.e. parsing the uuid a second
    time here purely to avoid returning an id that differs from the one `POST /files` handed
    back. A field that has to be defended that hard is a field the response is better without.
    """

    filename: str
    status: str
    failed_reason: str | None
    message: str


@router.get("/{file_id}")
def file_status(conn: Conn, owner: OwnerId, file_id: str) -> FileStatusResponse:
    """The processing status of one file: `queued · processing · ready · failed`, plus advice.

    THE POLLING HALF OF `POST /files`. `files.status` is the domain truth (ADR 0011) - distinct
    from `jobs.state`, which is the queue's own execution axis and never appears on this
    surface: a student has no use for `attempts` or a lease deadline.

    A FILE THAT DOES NOT EXIST AND ANOTHER OWNER'S FILE BOTH RETURN 404, NEVER 403. That is not
    a policy this handler applies - `get_file_status` returns one `None` for both cases by
    construction, so this route renders one status for both without ever learning which it had
    (F12: never leak cross-owner existence). A 403 would confirm the id is real to a caller who
    is not allowed to know that.

    A `file_id` that is not a uuid is 400, not 404. It names no file - not one that is hidden,
    one that cannot exist - so the actionable answer is "that is not an id", and there is
    nothing to leak because no owner has a non-uuid id. The validation itself belongs to
    `get_file_status`, which refuses at its own boundary; this route catches that `ValueError`
    and substitutes a client-facing sentence, because the library's message explains a
    connection-abort concern that is true and useful to a developer and meaningless to a client.

    A refusal is not an error here, and neither is a failed file: a `failed` row is a 200 with
    the reason and its remedy. The non-2xx statuses are for requests this route cannot answer,
    not for answers the student dislikes - the same rule the grounder's five states follow
    (ADR 0014-0016).
    """
    try:
        result = get_file_status(conn, file_id=file_id, owner_id=owner)
    except ValueError as exc:
        raise ApiError(
            400,
            "bad_file_id",
            "file_id must be a uuid. Use the file_id returned when the file was uploaded.",
        ) from exc
    if result is None:
        raise ApiError(
            404,
            "file_not_found",
            "No such file. Check the file id returned when the file was uploaded.",
        )

    if result.status == "failed":
        message = _FAILED_MESSAGE.get(result.failed_reason, _UNKNOWN_FAILURE)
    else:
        message = _STATUS_MESSAGE[result.status]
    return FileStatusResponse(
        filename=result.filename,
        status=result.status,
        failed_reason=result.failed_reason,
        message=message,
    )
