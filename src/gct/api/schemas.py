"""Response models the whole adapter shares (issue #104).

Only the error envelope lives here. Route request/response models belong to the route issues
(#107 classes, #108 ask, #110 files) and live beside their routers - one owner per shape.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ErrorBody(BaseModel):
    """`kind` is a stable, machine-readable token a client switches on; `message` is for a
    human and names the remedy where there is one; `detail` is optional structure (a
    validation error's field list, for instance) and null otherwise. The `kind`/`message`
    pair is deliberately the vocabulary `GrounderError` already uses (`grounder.md`
    §Interface), so a client learns one shape for "something went wrong"."""

    kind: str
    message: str
    detail: Any | None = None


class ErrorEnvelope(BaseModel):
    """Every non-2xx response body is exactly this: `{"error": {kind, message, detail}}`.

    Nested under `error` so a client can tell an error from any success body with one key
    check, and so no route's success fields can ever collide with the envelope's. Which STATUS
    a given failure maps to is each route issue's decision, not this module's."""

    error: ErrorBody
