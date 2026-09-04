"""`POST /files` - the upload surface (issue #110).

A multipart upload becomes a queued file in three steps and no more: check the class belongs to
the caller, stage the bytes durably, enqueue the job. Every one of those is a library callable
(ADR 0009) - this module validates, calls, and renders, and holds no ingest logic of its own.

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

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel

from gct.api.deps import Conn, OwnerId
from gct.api.errors import ApiError
from gct.classes import class_exists
from gct.jobs.queue import enqueue
from gct.staging import StagingError, stage

router = APIRouter(prefix="/files", tags=["files"])

# The HTTP status each request-time staging refusal renders as (`gct.staging.STAGING_REASONS` is
# the closed set; a reason missing from this map is a KeyError, not a silent default, so widening
# that set cannot quietly ship as a 500).
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
      - `class_id` is not a uuid -> 400. `class_exists` raises `ValueError` for this and its
        message is developer-facing (it explains a connection-abort concern a client cannot act
        on), so the sentence below is the route's own.
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
        owned = class_exists(conn, class_id=class_id, owner_id=owner)
    except ValueError as exc:
        raise ApiError(
            400,
            "bad_class_id",
            "class_id must be a uuid. Use the id returned when the class was created.",
        ) from exc
    if not owned:
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

    file_id = enqueue(conn, path=staged, owner_id=owner, class_id=class_id)
    return UploadAccepted(file_id=file_id, filename=Path(staged).name)
