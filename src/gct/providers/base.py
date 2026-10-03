"""Generation interface (ADR 0013). The Grounder owns prompts and citation parsing;
the desktop connection supplies completed text without exposing account credentials."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

# {"role": "system" | "user" | "assistant", "content": "..."}
Message = dict[str, str]


class TransientGenerationError(Exception):
    """A retryable generation failure (rate limit, timeout, server or network).

    Adapters classify provider errors; the Grounder owns the shared budget of at
    most two generation attempts per ask (ADR 0015/0016). Terminal errors propagate
    untouched rather than being retried.
    """


class Generation(Protocol):
    """Turn a message list into text. Text-parse in V1; structured-output/provider-lock is a
    pre-registered V3 trigger (ADR 0013)."""

    @property
    def model_id(self) -> str: ...

    def generate(self, messages: Sequence[Message]) -> str: ...
