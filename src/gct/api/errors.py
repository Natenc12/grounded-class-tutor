"""The error envelope - SHAPE only (issue #104).

Every non-2xx response the adapter produces is `ErrorEnvelope` (`schemas.py`): a route's own
`ApiError`, the framework's `HTTPException` (an unknown path), a request-validation failure, and
an uncaught exception all render through the same four handlers below. Which STATUS a given
failure maps to is deliberately NOT decided here - that belongs to each route issue (#107, #108,
#110), or this module gets rewritten three times. `ApiError` carries the status its raiser chose.

One thing the envelope is not: the Grounder's ERROR state. That is a RETURNED outcome
(ADR 0016; "failure states are returned, not raised"), rendered inside a success body by #108.
A refusal is the product working, and this module never sees one.
"""

from __future__ import annotations

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


def envelope(status_code: int, kind: str, message: str, detail: Any | None = None) -> JSONResponse:
    """Render one envelope. Built through the pydantic model so the wire shape and the
    documented shape cannot drift apart."""
    body = ErrorEnvelope(error=ErrorBody(kind=kind, message=message, detail=detail))
    return JSONResponse(status_code=status_code, content=body.model_dump())


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
