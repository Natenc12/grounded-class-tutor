"""Unit tests for `gct.providers.openai_provider` generation adapter - no network / API key.

Mirrors `test_openai_embeddings.py`'s pattern: `OpenAIGeneration.generate` against a hand-built
fake client (the `client=` injection seam), proving real-openai-exception-type classification
(`TransientGenerationError`, ADR 0011 / 0013) and the `None`-content coercion. The Grounder's
whole retry/ERROR ladder keys off exactly this classification (`gct.grounder.answer`, ~546-558),
and this adapter had no coverage of it at all before this file.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    InternalServerError,
    RateLimitError,
)

from gct.providers.base import TransientEmbeddingError, TransientGenerationError
from gct.providers.openai_provider import OpenAIGeneration

# --- Fake OpenAI client -----------------------------------------------------------------------
# Mimics only the one call our code makes: `client.chat.completions.create(model, messages)`
# returning `resp.choices[0].message.content`.


@dataclass
class _FakeMessage:
    content: str | None


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice]


class _FakeCompletionsAPI:
    def __init__(
        self, calls: list[list[dict]], error: Exception | None, content: str | None
    ) -> None:
        self.calls = calls
        self._error = error
        self._content = content

    def create(self, *, model: str, messages: list[dict]) -> _FakeResponse:
        self.calls.append(list(messages))
        if self._error is not None:
            raise self._error
        return _FakeResponse([_FakeChoice(_FakeMessage(self._content))])


class _FakeChatAPI:
    def __init__(
        self, calls: list[list[dict]], error: Exception | None, content: str | None
    ) -> None:
        self.completions = _FakeCompletionsAPI(calls, error, content)


class FakeOpenAI:
    def __init__(self, error: Exception | None = None, content: str | None = "hello") -> None:
        self.calls: list[list[dict]] = []
        self.chat = _FakeChatAPI(self.calls, error, content)


def _make(
    error: Exception | None = None, content: str | None = "hello"
) -> tuple[OpenAIGeneration, FakeOpenAI]:
    fake = FakeOpenAI(error, content)
    return OpenAIGeneration(client=fake), fake


def _openai_error(exc_type: type, status: int, message: str):
    """Build a real openai APIStatusError subclass (they require an httpx response)."""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return exc_type(message, response=response, body=None)


def _timeout_error() -> APITimeoutError:
    # APITimeoutError's constructor takes only `request` - no message/response, unlike the
    # APIStatusError subclasses above.
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return APITimeoutError(request=request)


def _connection_error() -> APIConnectionError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return APIConnectionError(message="connection dropped", request=request)


class TestGenerate:
    def test_returns_message_content(self):
        generator, fake = _make(content="the answer is [S1]")

        result = generator.generate([{"role": "user", "content": "question"}])

        assert result == "the answer is [S1]"
        assert fake.calls == [[{"role": "user", "content": "question"}]]

    def test_none_content_coerces_to_empty_string(self):
        # `resp.choices[0].message.content` can come back `None` (e.g. a refusal/finish reason
        # with no text). The adapter's `content or ""` turns that into an empty string, which the
        # Grounder's parser then reads as zero citation markers - a structural failure that
        # triggers a retry rather than a crash. Worth pinning: `None` is falsy but is NOT `""`
        # until this coercion runs.
        generator, _ = _make(content=None)

        result = generator.generate([{"role": "user", "content": "question"}])

        assert result == ""

    @pytest.mark.parametrize(
        "build_error",
        [
            lambda: _openai_error(RateLimitError, 429, "slow down"),
            _timeout_error,
            _connection_error,
            lambda: _openai_error(InternalServerError, 500, "server error"),
        ],
        ids=["rate_limit", "timeout", "connection", "5xx"],
    )
    def test_transient_provider_error_is_reclassified(self, build_error):
        error = build_error()
        generator, _ = _make(error=error)

        with pytest.raises(TransientGenerationError) as excinfo:
            generator.generate([{"role": "user", "content": "question"}])

        # `raise ... from err` preserves the real cause for the worker's logs / debugging.
        assert excinfo.value.__cause__ is error

    def test_terminal_error_propagates_unwrapped_via_generation_path(self):
        # `AuthenticationError` is terminal (retrying a bad key is futile), so it must propagate
        # completely untouched - not wrapped as `TransientGenerationError`, and not accidentally
        # wrapped as its embedding-side twin either. The two adapters share the same transient
        # tuple and a similar except clause; a copy-paste that reused the wrong wrapper would
        # still raise "an error" but would send it down the wrong rung of the Grounder's retry
        # ladder (ADR 0015/0016), silently.
        generator, _ = _make(error=_openai_error(AuthenticationError, 401, "bad key"))

        with pytest.raises(AuthenticationError) as excinfo:
            generator.generate([{"role": "user", "content": "question"}])

        assert not isinstance(excinfo.value, TransientGenerationError)
        assert not isinstance(excinfo.value, TransientEmbeddingError)
