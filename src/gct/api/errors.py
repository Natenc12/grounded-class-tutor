"""The error envelope - SHAPE only (issue #104).

Every non-2xx response the adapter produces is `ErrorEnvelope` (`schemas.py`): a route's own
`ApiError`, the framework's `HTTPException` (an unknown path), a request-validation failure, and
an uncaught exception all render through the same four handlers below. Which STATUS a given
failure maps to is deliberately NOT decided here - that belongs to each route issue (#107, #108,
#110), or this module gets rewritten three times. `ApiError` carries the status its raiser chose.

One thing the envelope is not: a Grounder REFUSAL. A refusal is the product working - the
corpus was searched and did not cover the question - so `POST /ask` renders all four GROUNDING
states as 200 bodies and this module never sees one. The fifth state, transport-level ERROR, does
leave through this envelope: `routers/ask.py` picks the status per `error.kind` and adopts that
kind as the envelope's. No contradiction with ADR 0016's "failure states are returned, not
raised" - the ADR governs how `ask()` RETURNS the state, and the HTTP rendering is the route's
own decision, made there.
"""

from __future__ import annotations

import json
import math
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from gct.api.schemas import ErrorBody, ErrorEnvelope
from gct.config import MAX_ERROR_DETAIL_BYTES, MAX_ERROR_ECHO_CHARS

# The three kinds this module emits itself. Routes mint their own (a domain token per failure
# they render); these are the framework-level ones no route raises.
KIND_VALIDATION = "validation"
KIND_HTTP = "http"
KIND_INTERNAL = "internal"

# The `type` of the one entry this module ever ADDS to a `detail` list (`_detail_marker`). A
# `detail` entry is otherwise always pydantic's; this is the token that says which is which.
_DETAIL_TRUNCATED = "gct.detail_truncated"


class ApiError(Exception):
    """A failure a route CHOOSES to render: the status it picked, plus the envelope's fields.

    Routes raise this instead of `HTTPException` so the body is the envelope by construction and
    `kind` is always a route-owned token, never a framework default.
    """

    def __init__(
        self, status_code: int, kind: str, message: str, detail: Any | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind
        self.message = message
        self.detail = detail


def _bounded(text: str) -> str:
    """Truncate one string to `MAX_ERROR_ECHO_CHARS`, marked with the length it really was.

    The result is never longer than the bound AT ANY VALUE OF THE BOUND, which is what makes this
    idempotent - `_validation` runs `_json_safe` over the error list and `render` runs it again
    over the finished envelope, so a result over the bound would be truncated and marked a second
    time on the way out.

    The final clamp is the load-bearing part of that, not belt-and-braces. Budgeting for the
    marker (`MAX_ERROR_ECHO_CHARS - len(marker)`) holds the bound only while the bound is the
    larger of the two; below `len(marker)` the subtraction floors at 0 and the marker ALONE is
    already over. `MAX_ERROR_ECHO_CHARS` is PROVISIONAL by declaration (`config.py`), so the
    value that makes the unclamped form correct is exactly the one a future retune may change.
    At 512 the clamp is a no-op - `keep + len(marker) == MAX_ERROR_ECHO_CHARS` by construction -
    so it costs nothing on the path anyone actually takes.

    What is NOT promised: that a marker in the output was written here. The surviving prefix is
    the client's own text, so a client can send marker-shaped bytes and get them echoed beside
    the real one. Bounding the echo is the guarantee; authenticating it is not, and could only be
    bought by dropping `input` - the shape change this function exists to avoid.
    """
    if len(text) <= MAX_ERROR_ECHO_CHARS:
        return text
    marker = f"...[truncated from {len(text)} chars]"
    return (text[: max(0, MAX_ERROR_ECHO_CHARS - len(marker))] + marker)[:MAX_ERROR_ECHO_CHARS]


def _json_safe(value: Any) -> Any:
    """Make every leaf of an envelope renderable AND bounded, recursively.

    Three leaf hazards, one arrival: the value is the CLIENT's, pydantic echoes it into the 422's
    `detail`, and this runs at the last step - so an unhandled one dies INSIDE the exception
    handler and the envelope collapses to a 500, the server taking the blame for a request that
    was merely malformed. Each was measured, none is exotic.

      - **A non-finite float.** `NaN`, `Infinity` and `-Infinity` are not JSON, and
        `allow_nan=False` - starlette's own setting, kept in `render` - makes `json.dumps` RAISE
        on one rather than emit it. A client puts one in the body without trying:
        `json.dumps(float("nan"))` emits a bare `NaN` by default, and FastAPI's parser accepts
        it. Stringified rather than dropped so the client can still see what was rejected.
      - **Undecodable bytes.** When FastAPI cannot parse a body at all - no content type, or one
        that is not JSON - pydantic's `input` is the RAW BODY, and one `0xff` byte is enough:
        `jsonable_encoder`'s handler for `bytes` is `lambda o: o.decode()`, which raises
        `UnicodeDecodeError`, and `json.dumps` would raise `TypeError` on the bytes regardless
        (issue #134). Decoded with `errors="replace"`. That is not the repo's refuse-don't-convert
        rule bending: the request was already REFUSED, 422, and this is the description of what
        was refused - the bytes are not being accepted as input to anything.
      - **A string the client sized.** The echo used to be as large as the request: 2,000,000
        bytes in, 2,000,215 out, because a `multipart/form-data` label takes a body off #125's
        request bound (`limits.py`) and pydantic then hands the whole thing back. Bounded by
        `MAX_ERROR_ECHO_CHARS`.

    BOUNDED AT EVERY STRING, not at the `input` key it was reported through. A key list is a
    second writer that goes stale the first time pydantic adds a key, and `input` is not the only
    client-controlled one: an `extra="forbid"` model echoes the client's own key into `loc`, and
    `gct.staging.validate_filename` puts an unbounded `filename` into the sentence a route hands
    to `message`. Dict KEYS are bounded too, for that first case; two keys that differ only past
    the bound collapse into one, which is the accepted cost of describing a body nobody will
    read twice.

    REJECTED: dropping `input` instead of truncating it. It fixes the same two symptoms and is
    shorter, but it changes the SHAPE of every 422 entry - `api.md` documents `detail` as
    pydantic's per-field list - where truncating changes only the VALUE of a pathological one.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, bytes):
        return _bounded(value.decode("utf-8", "replace"))
    if isinstance(value, str):
        return _bounded(value)
    if isinstance(value, dict):
        return {_json_safe(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _serialise(value: Any) -> str:
    """The one writer of this response's JSON settings.

    `render` renders with these and `_capped_detail` MEASURES with them, so the size the cap
    counts and the size that reaches the wire cannot drift apart. `ensure_ascii=True` also makes
    a character a byte here - the output is ASCII by construction - so `len()` is the byte count
    without an encode per entry.

    `allow_nan`, `indent` and `separators` are starlette 1.6.0's own values, restated because
    overriding `render` means restating all of them. `allow_nan=False` is why a non-finite float
    is a hazard at all, and it is NOT new here - starlette 1.6.0 already passes it, so a plain
    `JSONResponse` 500s on `NaN` the same way. `ensure_ascii` is the one deliberate change; see
    `_SafeJSONResponse` for the surrogate it exists for.
    """
    return json.dumps(value, ensure_ascii=True, allow_nan=False, indent=None, separators=(",", ":"))


def _detail_marker(dropped: int) -> dict[str, Any]:
    """The ONE entry `_capped_detail` adds, saying how many it dropped.

    It sits in a list a client iterates, so it carries exactly the four keys every pydantic entry
    has - `type`, `loc`, `msg`, `input` - and a naive `entry["loc"]` / `entry["msg"]` reads it
    without special-casing. Four is the whole set here, not a subset: FastAPI builds the list with
    `include_url=False` (`fastapi/_compat/v2.py`), so no entry in this adapter carries pydantic's
    `url` - and inventing one would both add a key no real entry has and point at the docs for a
    pydantic error type this is not.

    It does NOT impersonate a pydantic entry, which is the opposite hazard:
      - `type` is namespaced (`gct.` + a name), and no pydantic error type contains a dot - so
        the discriminator cannot collide with one this or any later pydantic version emits.
      - `loc` is `["detail"]`: pydantic roots every request-validation loc at a request part
        (`body`, `query`, ...) and never at `detail`, so no client can read this as something it
        sent. The ROOT is the discriminator, not the length - a missing or non-object body gets a
        one-element `["body"]`, so length distinguishes nothing
        (`test_a_real_entrys_loc_is_never_rooted_at_detail`).
      - `input` is `null`: there is no rejected value here, and the client sent nothing that
        corresponds to this entry.

    THE CONTRACT A CLIENT MAY RELY ON: at most one entry per envelope has
    `type == "gct.detail_truncated"`, it is always the LAST, `ctx.dropped` is how many pydantic
    entries were removed, and every OTHER entry is pydantic's own entry, byte-for-byte what the
    SAME entry would be in an envelope that never hit this cap. That is the honest form of the
    sentence, and "unmodified" was not: `_bounded` may already have truncated a string inside a
    surviving entry, marked with its true length, exactly as it does when no cap fires (#134). The
    cap adds no modification of its own - it keeps entries or drops them - which is the claim a
    client can act on, and `test_a_surviving_entry_is_what_an_uncapped_envelope_would_have_shown`
    is the one that runs it. The count is machine-readable in `ctx` rather than only in the
    sentence because `ctx` is pydantic's own key for an entry's structured extras, so reading it
    needs no parser.

    Unlike `_bounded`'s marker, this one is authentic and a client cannot forge it: that marker
    lives in a truncated string the client supplied, while `type` is a key the client never
    controls - pydantic mints it from its own catalogue, and a raising validator becomes
    `value_error`, never this.
    """
    return {
        "type": _DETAIL_TRUNCATED,
        "loc": ["detail"],
        "msg": (
            f"{dropped} more validation error(s) are not shown: the detail list exceeded "
            f"{MAX_ERROR_DETAIL_BYTES} bytes. Send a smaller request to see them all."
        ),
        "input": None,
        "ctx": {"dropped": dropped},
    }


def _capped_detail(detail: list[Any]) -> list[Any]:
    """Bound the whole `detail` LIST at `MAX_ERROR_DETAIL_BYTES`, dropping the tail.

    The bound `_bounded` gives is per-STRING and cannot see this one: an `extra="forbid"` model
    emits one entry per unexpected key, the client chooses how many keys to send, and every
    string in the result is far under 512 characters (issue #137 - `config.py` carries what the
    number must admit). An entry is kept or dropped WHOLE and its contents are never walked, so
    nothing here can shorten a `loc` - corrupting a field path would be worse than the
    amplification this exists to remove.

    THE TAIL, not the count: `api.md` documents `detail` as pydantic's per-field list, so cutting
    entry #161 silently would tell a client its request was fine in a field that was not. The
    marker is what makes the cut honest, and it is why this cannot be a plain slice.

    THE MARKER'S OWN BYTES ARE THE CIRCULARITY: how many entries fit depends on how long the
    marker is, which depends on how many were dropped, which depends on how many fit. Broken with
    a RESERVE that needs no second pass and no fixed point - the marker for `len(detail)` dropped,
    the largest count that can occur, since a decimal count's length is non-decreasing in the
    count. So the marker finally written is never longer than the space held for it, and no loop
    is needed to prove it.

    WHAT THE GUARANTEE IS, at values of the cap a future retune could pick. Serialised `detail`
    is <= the cap exactly, for every cap at or above `len(_serialise(_detail_marker(len(detail))))
    + 2` - the marker for this list's own count, plus its brackets. That floor is a FUNCTION OF
    THE LIST, not a constant, because the marker spells the count out in decimal: measured at the
    shipped cap, 211 bytes for a single-digit count and 217 at a thousand entries. (It moves with
    the cap too, by the same few bytes - the marker's sentence names the cap.) A fixed "~200" was
    wrong in the direction that matters, since a floor quoted too low reads as a guarantee that
    holds where it does not. Below it the result is the marker alone and
    EXCEEDS the cap, because a list has no equivalent of `_bounded`'s final clamp: truncating a
    string mid-character still yields a string, while truncating a JSON array mid-token yields
    something no client can parse. Exceeding a cap nobody can honour, by the marker, is the honest
    failure; emitting invalid JSON is not. At 16 KiB the reserve is ~1% of the budget.

    WHEN THE FIRST ENTRY ALONE IS OVER THE CAP, `detail` becomes the marker and nothing else -
    measured at 54,333 bytes for one entry echoing a whole 65 KB body. Keeping it anyway was
    REJECTED: it voids the bound in exactly the case the bound exists for, and it re-opens a
    response the size of the request, which is #134's finding one axis over. Nothing legitimate
    reaches it - every string is already bounded at `MAX_ERROR_ECHO_CHARS`, so a 16 KiB entry
    takes a body with hundreds of keys - and a client that hits it is still told the kind, the
    message and the count, which is the remedy it needs.
    """
    if len(_serialise(detail)) <= MAX_ERROR_DETAIL_BYTES:
        return detail
    reserve = len(_serialise(_detail_marker(len(detail)))) + 1  # the marker, and its comma
    budget = MAX_ERROR_DETAIL_BYTES - 2 - reserve  # the two brackets
    kept: list[Any] = []
    used = 0
    for entry in detail:
        cost = len(_serialise(entry)) + (1 if kept else 0)
        if used + cost > budget:
            break
        used += cost
        kept.append(entry)
    return [*kept, _detail_marker(len(detail) - len(kept))]


class _SafeJSONResponse(JSONResponse):
    """Starlette's JSONResponse, hardened against the ways rendering an envelope can go wrong.

    Every hazard is the same shape and each was measured, not argued: the offending value comes
    from the CLIENT, pydantic echoes it into the 422's `detail`, and `render` is the last step. A
    raise here happens INSIDE the exception handler, so the envelope collapses to a 500
    `internal` - the server blaming itself for a request that was merely malformed, with the
    per-field list the client needed gone. None of them is exotic; each is an ordinary client
    mistake.

    Most of them are properties of a LEAF, and `_json_safe` is their one writer - the non-finite
    float, the undecodable byte string, the client-sized string. It is applied to EVERY envelope
    rather than to the 422 alone, because any handler that echoes caller-supplied text reaches
    this same last step.

    ONE is a property of this render rather than of any leaf's type, so it lives here: **a lone
    surrogate.** Starlette renders with `ensure_ascii=False`, so `.encode("utf-8")` raises on
    one. JSON carries a surrogate as a `\\uXXXX` escape, so the bytes on the wire are plain ASCII
    and nothing rejects them before pydantic holds a `str`. Fixed by escaping - `ensure_ascii` is
    the only deliberate change below. `jsonable_encoder` in `_validation` does not reach it: that
    fixes a non-serialisable `ctx`, and a surrogate survives it as a perfectly good `str`.

    The settings live in `_serialise`, which measures as well as renders - see it.

    ONE hazard is a property of neither a leaf nor this render but of the finished ENVELOPE, and
    it is bounded here for that reason: **a `detail` list the client sized** (issue #137). This
    is the only place that sees an envelope after every handler and every bound has run, so a
    route that grows a list-valued `detail` later is caught without knowing this exists.
    `_capped_detail` carries the mechanism. The ORDER is load-bearing: it runs AFTER `_json_safe`,
    because that is what bounds each string, and measuring an entry before it would drop entries
    that fit once bounded - and would measure a `NaN` that `allow_nan=False` is about to raise on.
    """

    def render(self, content: Any) -> bytes:
        body = _json_safe(content)
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and isinstance(error.get("detail"), list):
            error["detail"] = _capped_detail(error["detail"])
        return _serialise(body).encode("utf-8")


def envelope(status_code: int, kind: str, message: str, detail: Any | None = None) -> JSONResponse:
    """Render one envelope. Built through the pydantic model so the wire shape and the
    documented shape cannot drift apart."""
    body = ErrorEnvelope(error=ErrorBody(kind=kind, message=message, detail=detail))
    return _SafeJSONResponse(status_code=status_code, content=body.model_dump())


async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    return envelope(exc.status_code, exc.kind, exc.message, exc.detail)


async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # The framework's own raises (404 for an unknown path, 405) - re-shaped, status untouched.
    return envelope(exc.status_code, KIND_HTTP, str(exc.detail))


async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 422 is FastAPI's status for this and stays; only the body changes. `detail` carries the
    # per-field list pydantic produced, which is the actionable part.
    # `jsonable_encoder` is load-bearing, not tidiness: when a `@field_validator` raises,
    # pydantic puts the EXCEPTION OBJECT in the entry's `ctx`, and a bare `exc.errors()` then
    # fails JSON serialisation inside this handler - so the 422 envelope collapses into a bare
    # 500 `internal` and the validation message the client needed is gone. FastAPI's own default
    # 422 handler runs the list through the same encoder for the same reason.
    #
    # `_json_safe` runs FIRST, before the encoder, and the ORDER is the whole point: the encoder
    # is the step that dies on the client's bytes (issue #134), so leaving the leaves to
    # `render` - which applies `_json_safe` to every envelope anyway - would still 500 on one
    # `0xff`. Applying it twice is free: it is idempotent (`_bounded`), and the second pass is
    # what bounds whatever the encoder itself produced from a `ctx` exception.
    return envelope(
        422,
        KIND_VALIDATION,
        "request validation failed",
        jsonable_encoder(_json_safe(exc.errors())),
    )


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    # The 500. The client gets the envelope, NOT the exception text: the traceback belongs in the
    # server log (Starlette re-raises after this handler runs, so uvicorn logs it), and echoing
    # `str(exc)` would ship psycopg's DSN-bearing messages and provider errors to the browser.
    return envelope(500, KIND_INTERNAL, "internal error")


def install(app: FastAPI) -> None:
    """Register the four handlers on `app`. Called once by `create_app`."""
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation)
    app.add_exception_handler(Exception, _unhandled)
