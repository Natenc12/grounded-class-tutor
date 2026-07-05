"""OpenAI implementations of the provider interfaces (ADR 0005 / 0007). Defaults, not
commitments — the whole point of the interface is that these are swappable."""
from __future__ import annotations

from typing import Sequence

from openai import OpenAI

from ..config import (
    ACTIVE_EMBEDDING_MODEL_ID,
    DEFAULT_GENERATION_MODEL,
    EMBEDDING_DIM,
    load_settings,
)
from .base import Message

# OpenAI embeddings cap: 2048 inputs (and ~300k tokens) per request. The adapter batches under
# this so the worker's "embed all" call stays honest (PM-2).
_EMBED_INPUT_CAP = 2048


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
        for start in range(0, len(texts), _EMBED_INPUT_CAP):
            batch = texts[start : start + _EMBED_INPUT_CAP]
            resp = self._client.embeddings.create(
                model=self._model_id, input=list(batch), dimensions=self._dim
            )
            out.extend(item.embedding for item in resp.data)
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
        resp = self._client.chat.completions.create(
            model=self._model_id, messages=list(messages)
        )
        return resp.choices[0].message.content or ""
