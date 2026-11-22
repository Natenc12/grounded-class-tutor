"""Provider interfaces (ADR 0013). Deliberately thin: the Grounder owns the prompt and the
citation parsing; these just move messages ⇄ text."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

# {"role": "system" | "user" | "assistant", "content": "..."}
Message = dict[str, str]


class TransientGenerationError(Exception):
    """A retryable generation failure (rate limit / timeout / server / network).

    An adapter catches its provider's "try again" errors and re-raises this, so callers never
    touch provider-specific exception types. Terminal errors (bad key, malformed request) are
    NOT wrapped - they propagate untouched, since retrying them is futile.

    The retry budget that consumes THIS lives in the Grounder (`gct.grounder.answer`, at most 2
    generation attempts per ask - ADR 0015/0016). The Grounder sits on the synchronous read path
    and owns the whole ask-level budget, so it is the box that has to tell "retry once" from
    "give up and ERROR".
    """


class Generation(Protocol):
    """Turn a message list into text. Text-parse in V1; structured-output/provider-lock is a
    pre-registered V3 trigger (ADR 0013)."""

    @property
    def model_id(self) -> str: ...

    def generate(self, messages: Sequence[Message]) -> str: ...
