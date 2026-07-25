"""Provider interfaces (ADR 0013). Deliberately thin: the Grounder owns the prompt and the
citation parsing; these just move text ⇄ vectors and messages ⇄ text."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

# {"role": "system" | "user" | "assistant", "content": "..."}
Message = dict[str, str]


class TransientEmbeddingError(Exception):
    """A retryable embedding failure (rate limit / timeout / server / network).

    The provider-agnostic mirror of the *transient* class: an adapter catches its provider's
    "try again" errors and re-raises this so callers never touch provider-specific exception
    types. Like `parse.py`'s `ParseError` (terminal), the adapter only *classifies* — the retry
    loop + budget live in the Slice-2 worker (ADR 0011), never here. Terminal errors (bad key,
    malformed request) are not wrapped; they propagate untouched, since retrying them is futile.
    """


class TransientGenerationError(Exception):
    """A retryable generation failure (rate limit / timeout / server / network).

    The generation-side mirror of `TransientEmbeddingError`, and it exists for the same reason:
    an adapter catches its provider's "try again" errors and re-raises this, so callers never
    touch provider-specific exception types. Terminal errors (bad key, malformed request) are
    NOT wrapped - they propagate untouched, since retrying them is futile.

    The one asymmetry with the embedding side: the retry budget that consumes THIS lives in the
    Grounder (`gct.grounder.answer`, at most 2 generation attempts per ask - ADR 0015/0016), not
    in the Slice-2 worker. The Grounder sits on the synchronous read path and owns the whole
    ask-level budget, so it is the box that has to tell "retry once" from "give up and ERROR".
    """


class Embeddings(Protocol):
    """Turn text into vectors. The active implementation's `model_id` is the ADR 0018
    invariant subject — it gets stamped onto every chunk and asserted at query time."""

    @property
    def model_id(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed all `texts`. The adapter owns sub-batching under the provider cap (PM-2);
        callers just say 'embed all' and stay honest."""
        ...


class Generation(Protocol):
    """Turn a message list into text. Text-parse in V1; structured-output/provider-lock is a
    pre-registered V3 trigger (ADR 0013)."""

    @property
    def model_id(self) -> str: ...

    def generate(self, messages: Sequence[Message]) -> str: ...
