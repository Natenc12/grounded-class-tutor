"""The request-body bound - ONE gate, in front of every parser (issue #125).

Every route in the adapter used to accept a JSON body of any size and any shape. Two things
followed, and they are one gap rather than two: a body nested deeply enough came back **500
`internal`** - the server blaming itself for a request that was merely malformed - and a
multi-megabyte class name was accepted and STORED, because `MAX_STAGE_BYTES` (ADR 0010) bounds an
uploaded FILE and nothing bounded a REQUEST.

The 500 is worth stating exactly, because the obvious fix is the wrong one. Pydantic rejects the
field and echoes the offending value into the 422's `detail`; `errors._validation` then runs
`jsonable_encoder` over that echo, which walks the nested value RECURSIVELY, and rendering is the
LAST step - so it raises INSIDE the exception handler, the 422 envelope is never built, and the
client gets a generic 500 with the per-field list it needed gone. Hardening the render (a bigger
recursion limit, an iterative encoder) buys levels and leaves the size half untouched. Bounding the
body in front of the parser closes both, which is why this module exists and `errors.py` was not
changed.

WHY THIS IS MIDDLEWARE AND NOT A FIELD RULE. A per-field constraint runs after the whole body is
parsed in memory, so it cannot bound what gets parsed - `routers/ask.py`'s `MAX_QUESTION_CHARS` is
a bound on a VALUE and deliberately stays one. And a bound written per-router would be one writer
per route for a rule that belongs to the adapter, the same reason `validate_name` lives in
`gct.classes` rather than in `routers/classes.py`.

THE MULTIPART EXEMPTION IS THE WHOLE DESIGN CONSTRAINT. On `POST /files` the uploaded file IS the
request body, `gct.staging.stage` reads it in `CHUNK_BYTES` reads and bounds it at
`MAX_STAGE_BYTES` = 100 MiB, and the dogfood corpus runs to 6.8 MB. A single raw body-size check -
the obvious reading of "bound it before the parser" - would apply to that stream too and refuse
every real course upload. So a multipart request is passed through with the SAME `receive`
callable object it arrived with: not a copy, not a wrapper, the same object, which is the
strongest available statement that this module did not touch the upload stream.

Exempted by CONTENT-TYPE, not by a path list. A path list goes stale the day a route is added -
it would have to be edited by an issue that has no reason to look here - whereas the content type
is the request's own declaration of which parser it is headed for. It opens no hole: an 8 MB body
LABELLED `multipart/form-data` and posted to a JSON-body route is refused 422 by starlette's form
parser (measured), never parsed as JSON and never stored.

THIS MODULE RENDERS ITS REFUSALS; IT MUST NEVER RAISE THEM. That is not a style choice and a
future edit would break it silently. Starlette's stack is `ServerErrorMiddleware` -> USER
middleware (this) -> `ExceptionMiddleware` -> the route. `app.add_exception_handler(ApiError, ...)`
registers on the INNERMOST of those, so an `ApiError` raised HERE travels outward past it and is
caught only by the `Exception` handler on the outermost - arriving at the client as exactly the
500 `internal` this module was written to remove. Measured, both for pure-ASGI and for
`BaseHTTPMiddleware`. So a refusal is built with `errors.envelope(...)` and awaited as an ASGI app,
which keeps the envelope's one writer and reaches the client with the status and `kind` chosen
below. `test_limits.py` pins it.

REJECTED, and why:
  - **A `content-length` pre-check.** It would refuse an oversized body without buffering a byte,
    but it cannot be the authoritative check - a chunked request carries no such header and a
    client controls the value - so it would be a second, weaker copy of the rule below, and the
    streaming check already bounds memory at the limit plus one chunk.
  - **A method allowlist** (bound only POST/PUT/PATCH). A body-less GET costs one `receive()` of
    an empty message, and a list of methods is a thing that goes stale.
"""

from __future__ import annotations

import email.message

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gct.api.errors import envelope
from gct.config import MAX_JSON_BODY_BYTES, MAX_JSON_BODY_DEPTH

# The two refusals this module mints. Tokens, not sentences - a client switches on these
# (`schemas.ErrorBody`), and the prose below is what a human reads.
KIND_TOO_LARGE = "body_too_large"
KIND_TOO_NESTED = "body_too_nested"

# The statuses, and why each is not 422. `routers/files.py`'s `_STAGING_STATUS` is the precedent
# this follows: 422 is FastAPI's own request-validation status and its body already carries
# pydantic's per-field list under `detail`, so putting a second, differently-shaped body behind
# that status is what makes a client parse by trial.
#   too many bytes -> 413. The request was well-formed and refused for its SIZE, which is the one
#     thing a client can act on without reading prose - the same reading `too_large` gets on
#     `POST /files`.
#   too deeply nested -> 400. NOT 413: the body that first produced the 500 was under 2 KB, so
#     "payload too large" would be a false statement about what is wrong with it. Nothing about
#     its size is the problem; its shape is.
_STATUS_TOO_LARGE = 413
_STATUS_TOO_NESTED = 400

# The four structural characters, as raw bytes. Scanning bytes rather than decoded text is safe
# for exactly one reason and it is worth stating: every one of these is ASCII, and every
# continuation byte of a multi-byte UTF-8 sequence is >= 0x80, so no character outside ASCII can
# ever contribute a byte that looks like one of these. A body may therefore be scanned BEFORE it
# is decoded - which is the point, since decoding it is work this module is refusing to do.
_OPEN = (0x5B, 0x7B)  # [ {
_CLOSE = (0x5D, 0x7D)  # ] }
_QUOTE = 0x22  # "
_BACKSLASH = 0x5C  # \


def scan_depth(body: bytes, *, limit: int) -> int:
    """Return the deepest JSON container nesting in `body`, giving up once it passes `limit`.

    Structural brackets only: a bracket INSIDE a string literal is data, not nesting, so a
    perfectly ordinary question - `{"question": "what does [[[ mean in this notation?"}` - must
    scan as depth 1 and not as depth 4. Hence the string-literal state machine, and hence `\\"`
    and `\\\\` are tracked: without the escape rule, `"he said \\"[[[\\""` would be read as leaving
    the string early and the brackets after it would count.

    Returns as soon as the depth exceeds `limit`, so the work is bounded by where the answer stops
    mattering rather than by the body's length. The number it returns is therefore "at least this
    deep" once it is over the limit, which is all the caller asks it.

    A close bracket clamps at zero rather than going negative. Unbalanced JSON never reaches the
    recursive render at all - `json.loads` refuses it first - so this cannot change an outcome;
    it is here so that a leading `]]]]` cannot subtract from a genuine nesting that follows it,
    i.e. so the count can never UNDER-report.
    """
    depth = 0
    deepest = 0
    in_string = False
    escaped = False
    for char in body:
        if in_string:
            if escaped:
                escaped = False
            elif char == _BACKSLASH:
                escaped = True
            elif char == _QUOTE:
                in_string = False
            continue
        if char == _QUOTE:
            in_string = True
        elif char in _OPEN:
            depth += 1
            if depth > deepest:
                deepest = depth
                if deepest > limit:
                    return deepest
        elif char in _CLOSE:
            depth = max(0, depth - 1)
    return deepest


def _content_type(scope: Scope) -> str | None:
    """The raw `content-type` header, or `None`. ASGI header names arrive lower-cased already,
    but the VALUE is the client's and may be any casing (`APPLICATION/JSON` was measured to reach
    the JSON parser), which is why both predicates below parse it rather than compare strings."""
    for name, value in scope.get("headers", ()):
        if name == b"content-type":
            return value.decode("latin-1")
    return None


def _parsed_type(content_type: str) -> email.message.Message:
    # `email.message.Message` is how FastAPI itself splits a content type from its parameters
    # (`fastapi/routing.py`), so `application/json; charset=utf-8` and case variants are handled
    # here exactly as they are there rather than by a second, hand-rolled parser.
    parsed = email.message.Message()
    parsed["content-type"] = content_type
    return parsed


def is_multipart(content_type: str | None) -> bool:
    """True for the streamed upload path, which this module must not touch at all."""
    if not content_type:
        return False
    return _parsed_type(content_type).get_content_type() == "multipart/form-data"


def may_be_parsed_as_json(content_type: str | None) -> bool:
    """True if FastAPI could hand this body to a JSON parser - deliberately a SUPERSET of when it
    actually will.

    FastAPI parses a body as JSON when the content type's main type is `application` and its
    subtype is `json` or ends in `+json`, and ALSO when there is no content type at all, unless
    the route sets `strict_content_type` (`fastapi/routing.py`; it defaults to True today, which
    is why a body with no content type currently arrives as raw bytes). Answering True for a
    missing content type makes this predicate correct under BOTH settings of that flag, so
    flipping it - on one route, by an issue with no reason to read this module - cannot quietly
    reopen the 500.

    A body that declares a NON-JSON type is not scanned, and that is measured rather than assumed:
    `application/octet-stream`, `text/plain` and form encodings all reach the route as raw bytes,
    so no nesting in them can recurse the render. Scanning them anyway would be actively wrong,
    not merely wasteful - 64 KiB of random binary was measured at a bracket depth of 31, one under
    the limit, so a slightly larger or less random upload would be refused as "too deeply nested"
    when it contains no JSON at all.
    """
    if not content_type:
        return True
    parsed = _parsed_type(content_type)
    if parsed.get_content_maintype() != "application":
        return False
    subtype = parsed.get_content_subtype()
    return subtype == "json" or subtype.endswith("+json")


def _replay(messages: list[Message]) -> Receive:
    """A `receive` that hands back exactly the messages that were consumed, in order.

    The buffered chunks are replayed as the SEPARATE messages they arrived in rather than joined
    into one, so an app downstream sees the same chunk boundaries and the same `more_body` flags
    it would have seen with no middleware installed. After they run out it reports a disconnect,
    which is what a client that has finished sending its body has effectively done.
    """
    pending = list(messages)

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    return receive


class BodyLimit:
    """Bound every request body that is not a streamed multipart upload.

    Pure ASGI rather than `BaseHTTPMiddleware`, for three reasons that all point the same way: it
    is the only form in which the exempt path can be handed the SAME `receive` object (the
    guarantee the upload path needs), `BaseHTTPMiddleware` wraps every response in a
    `StreamingResponse` whether or not it does anything, and its exception and background-task
    behaviour differs from the plain app's. Nothing here needs a `Request` object, which is the
    only thing `BaseHTTPMiddleware` buys.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int = MAX_JSON_BODY_BYTES,
        max_depth: int = MAX_JSON_BODY_DEPTH,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.max_depth = max_depth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_type = _content_type(scope)
        if is_multipart(content_type):
            # THE SAME `receive`, not a wrapper around it. `stage` streams the upload through
            # this object; anything else here would put the whole file in memory.
            await self.app(scope, receive, send)
            return

        messages: list[Message] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # A disconnect. Keep it in the stream rather than dropping it, so the app sees
                # what the server sent, then stop reading - there is no more body coming.
                messages.append(message)
                break
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                # Refused WITHOUT draining the rest, so an oversized body is never fully
                # buffered. The message names the limit and not the size, because the size is
                # the one thing this branch deliberately never finished measuring.
                await self._refuse(
                    scope,
                    receive,
                    send,
                    status=_STATUS_TOO_LARGE,
                    kind=KIND_TOO_LARGE,
                    message=(
                        f"request body is larger than the {self.max_bytes}-byte limit for a "
                        f"JSON request body. Send a smaller body; a course file is uploaded to "
                        f"POST /files as a multipart form, which this limit does not apply to."
                    ),
                )
                return
            messages.append(message)
            if not message.get("more_body", False):
                break

        if may_be_parsed_as_json(content_type):
            body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.request")
            if scan_depth(body, limit=self.max_depth) > self.max_depth:
                await self._refuse(
                    scope,
                    receive,
                    send,
                    status=_STATUS_TOO_NESTED,
                    kind=KIND_TOO_NESTED,
                    message=(
                        f"request body nests JSON arrays or objects more than "
                        f"{self.max_depth} levels deep. Send the value itself rather than a "
                        f"deeply nested structure - every field this API accepts is a string."
                    ),
                )
                return

        await self.app(scope, _replay(messages), send)

    async def _refuse(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status: int,
        kind: str,
        message: str,
    ) -> None:
        # Rendered, never raised - see the module docstring. `envelope` returns a starlette
        # response, which IS an ASGI app, so awaiting it here writes the same bytes the exception
        # handlers would have written.
        response = envelope(status, kind, message)
        await response(scope, receive, send)


def install(app: FastAPI) -> None:
    """Wrap `app` in the body bound. Called once by `create_app`, beside `errors.install`.

    Its own function rather than a line inside `errors.install`: that one registers the envelope's
    handlers, and giving it a second job would make the module that owns the error SHAPE also own
    a request policy.
    """
    app.add_middleware(BodyLimit)
