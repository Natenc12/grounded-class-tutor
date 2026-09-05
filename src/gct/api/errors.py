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

# The three kinds this module emits itself. Routes mint their own (a domain token per failure
# they render); these are the framework-level ones no route raises.
KIND_VALIDATION = "validation"
KIND_HTTP = "http"
KIND_INTERNAL = "internal"


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


def _json_safe(value: Any) -> Any:
    """Replace every non-finite float with its repr, recursively.

    `NaN`, `Infinity` and `-Infinity` are not JSON, and `allow_nan=False` - starlette's own
    setting, kept here - makes `json.dumps` RAISE on one rather than emit it. A client puts one
    in the body without trying: `json.dumps(float("nan"))` emits a bare `NaN` by default, and
    FastAPI's parser accepts it. Pydantic then rejects the field and echoes the offending value
    into the 422's `detail`, which is how a float nobody in this codebase produced reaches the
    render. Stringified rather than dropped so the client can still see what was rejected.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class _SafeJSONResponse(JSONResponse):
    """Starlette's JSONResponse, hardened against the two ways rendering an envelope can RAISE.

    Both hazards are the same shape and both were measured, not argued: the offending value comes
    from the CLIENT, pydantic echoes it into the 422's `detail`, and `render` is the last step. It
    dies INSIDE the exception handler, so the envelope collapses to a 500 `internal` - the server
    blaming itself for a request that was merely malformed, with the per-field list the client
    needed gone. Neither input is exotic; each is an ordinary client's mistake.

    - **A lone surrogate.** Starlette renders with `ensure_ascii=False`, so `.encode("utf-8")`
      raises on one. JSON carries a surrogate as a `\\uXXXX` escape, so the bytes on the wire are
      plain ASCII and nothing rejects them before pydantic holds a `str`. Fixed by escaping.
      `jsonable_encoder` in `_validation` does not reach it: that fixes a non-serialisable `ctx`,
      and a surrogate survives it as a perfectly good `str`.
    - **A non-finite float.** `allow_nan=False` raises on `NaN`/`Infinity`. Fixed by `_json_safe`.
      This one is NOT new here - starlette 1.6.0 already passes `allow_nan=False`, so it 500s the
      same way on the plain `JSONResponse` this class replaces.

    `allow_nan`, `indent` and `separators` are starlette 1.6.0's own values, restated because
    overriding `render` means restating all of them; `ensure_ascii` is the only deliberate change.

    Applied to EVERY envelope rather than to the 422 alone, because any handler that echoes
    caller-supplied text reaches the same last step.
    """

    def render(self, content: Any) -> bytes:
        return json.dumps(
            _json_safe(content),
            ensure_ascii=True,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")


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
    return envelope(
        422, KIND_VALIDATION, "request validation failed", jsonable_encoder(exc.errors())
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
