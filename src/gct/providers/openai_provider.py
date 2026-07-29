"""OpenAI implementations of the provider interfaces (ADR 0005 / 0007). Defaults, not
commitments — the whole point of the interface is that these are swappable."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from ..config import (
    ACTIVE_EMBEDDING_MODEL_ID,
    DEFAULT_GENERATION_MODEL,
    EMBEDDING_DIM,
    load_settings,
)
from .base import Message, TransientEmbeddingError, TransientGenerationError

# OpenAI's "try again" errors: rate limit (429), timeout, server (5xx), network drop. Anything
# else (bad key, malformed request, permission, not found) is terminal — retrying is futile, so
# we let it propagate untouched.
_TRANSIENT_OPENAI_ERRORS = (
    RateLimitError,
    APITimeoutError,
    InternalServerError,
    APIConnectionError,
)

# OpenAI embeddings caps: 2048 inputs AND ~300k tokens per request. A single over-cap call is a
# hard failure, not a throughput issue — the adapter sub-batches under BOTH so the worker's
# "embed all" stays honest (PM-1 correctness floor). The token cap is usually the binding one:
# the crossover is ~146 tokens/input, and real chunks run well above that.
_EMBED_INPUT_CAP = 2048

# The 300k token cap, held back by a safety margin. We *estimate* tokens (below), so we bias
# every knob toward OVER-counting: undercounting would pack too much and blow the real cap (a
# hard failure), while overcounting just costs one extra API call. English is ~4 chars/token, and
# we divide by 3 (plus the sub-300k budget) so ordinary English/Latin-script text over-counts.
# CAVEAT: this does NOT hold for CJK, which runs ~0.5–1.5 tokens/char — chars/3 under-counts it
# 3–5×, which could breach the real cap. Fine for the current English corpus; revisit before
# ingesting CJK.
# TODO: swap the char heuristic for tiktoken if we ingest CJK (or other token-dense scripts).
_EMBED_TOKEN_BUDGET = 250_000
_CHARS_PER_TOKEN = 3


def _est_tokens(text: str) -> int:
    # +1 so any non-empty text counts for at least one token (never rounds to zero).
    return len(text) // _CHARS_PER_TOKEN + 1


def _sub_batches(texts: Sequence[str]) -> Iterator[list[str]]:
    """Greedily pack `texts` (in order) into batches under both provider caps.

    Yields nothing for empty input. A lone text whose own estimate exceeds the token budget
    still ships as a singleton batch — we can't split one text; keeping chunks embeddable is the
    chunker's contract (ADR 0019), not the adapter's.
    """
    batch: list[str] = []
    batch_tokens = 0
    for text in texts:
        est = _est_tokens(text)
        # Flush before adding if this text would breach either cap (skip when batch is empty, so
        # an oversized lone text isn't dropped — it goes out on its own next).
        if batch and (len(batch) >= _EMBED_INPUT_CAP or batch_tokens + est > _EMBED_TOKEN_BUDGET):
            yield batch
            batch = []
            batch_tokens = 0
        batch.append(text)
        batch_tokens += est
    if batch:
        yield batch


def _client(client: OpenAI | None) -> OpenAI:
    return client or OpenAI(api_key=load_settings().openai_api_key)


class OpenAIEmbeddings:
    def __init__(
        self,
        model_id: str = ACTIVE_EMBEDDING_MODEL_ID,
        dim: int = EMBEDDING_DIM,
        client: OpenAI | None = None,
    ) -> None:
        self._model_id = model_id
        self._dim = dim
        self._client = _client(client)

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for batch in _sub_batches(texts):
            try:
                resp = self._client.embeddings.create(
                    model=self._model_id, input=batch, dimensions=self._dim
                )
            except _TRANSIENT_OPENAI_ERRORS as err:
                raise TransientEmbeddingError(str(err)) from err
            # Concat is positional, so realign to input order by `index` — never trust `data` to
            # arrive sorted. A silent misalignment would index wrong vectors → wrong citations.
            out.extend(item.embedding for item in sorted(resp.data, key=lambda d: d.index))
        return out


class OpenAIGeneration:
    def __init__(
        self, model_id: str = DEFAULT_GENERATION_MODEL, client: OpenAI | None = None
    ) -> None:
        self._model_id = model_id
        self._client = _client(client)

    @property
    def model_id(self) -> str:
        return self._model_id

    def generate(self, messages: Sequence[Message]) -> str:
        # Same classification as `embed` above, and for the same reason: the Grounder's retry
        # budget (ADR 0015/0016) has to know which failures are worth a second attempt, and it
        # cannot import `openai` to find out without dragging provider specifics above the seam
        # (ADR 0013). Terminal errors stay unwrapped and surface as a Grounder ERROR unretried.
        try:
            resp = self._client.chat.completions.create(
                model=self._model_id, messages=list(messages)
            )
        except _TRANSIENT_OPENAI_ERRORS as err:
            raise TransientGenerationError(str(err)) from err
        return resp.choices[0].message.content or ""
