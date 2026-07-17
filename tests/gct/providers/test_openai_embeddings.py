"""Unit tests for `gct.providers.openai_provider` embeddings adapter - no network / API key.

Two halves:
- the pure token-aware packer (`_sub_batches`) - order, both provider caps, edge cases;
- `OpenAIEmbeddings.embed` against a hand-built fake client (the `client=` injection seam),
  proving order/length end-to-end, empty-input short-circuit, and transient-vs-terminal error
  classification (`TransientEmbeddingError`, ADR 0011 / 0013).
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest
from openai import AuthenticationError, RateLimitError

from gct.providers.base import TransientEmbeddingError
from gct.providers.openai_provider import (
    _CHARS_PER_TOKEN,
    _EMBED_INPUT_CAP,
    _EMBED_TOKEN_BUDGET,
    OpenAIEmbeddings,
    _est_tokens,
    _sub_batches,
)


# --- Fake OpenAI client -----------------------------------------------------------------------
# Mimics only the one call our code makes: `client.embeddings.create(model, input, dimensions)`
# returning `resp.data` (one item per input, each with `.embedding`). It records every batch it
# was handed (so we can assert on packing) and tags each input with a monotonic id, so the output
# vectors are `[[0.0], [1.0], ...]` in receive order - any reordering in the adapter shows up here.


@dataclass
class _FakeItem:
    embedding: list[float]
    index: int  # position within the request's input, as the real API reports it


@dataclass
class _FakeResponse:
    data: list[_FakeItem]


class _FakeEmbeddingsAPI:
    def __init__(
        self,
        calls: list[list[str]],
        error: Exception | None,
        fail_on_call: int | None,
        reverse_data: bool,
    ) -> None:
        self.calls = calls
        self._error = error
        self._fail_on_call = fail_on_call  # 1-indexed; None → fail on every call
        self._reverse_data = reverse_data
        self._counter = 0

    def create(self, *, model: str, input: list[str], dimensions: int) -> _FakeResponse:
        self.calls.append(list(input))
        if self._error is not None and self._fail_on_call in (None, len(self.calls)):
            raise self._error
        data = []
        for i, _ in enumerate(input):
            data.append(_FakeItem([float(self._counter)], index=i))
            self._counter += 1
        # Each item carries its true `index`, so a reordered `data` must still realign correctly.
        if self._reverse_data:
            data.reverse()
        return _FakeResponse(data)


class FakeOpenAI:
    def __init__(
        self,
        error: Exception | None = None,
        fail_on_call: int | None = None,
        reverse_data: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self.embeddings = _FakeEmbeddingsAPI(self.calls, error, fail_on_call, reverse_data)


def _make(
    error: Exception | None = None,
    fail_on_call: int | None = None,
    reverse_data: bool = False,
) -> tuple[OpenAIEmbeddings, FakeOpenAI]:
    fake = FakeOpenAI(error, fail_on_call, reverse_data)
    return OpenAIEmbeddings(client=fake), fake


def _openai_error(exc_type: type, status: int, message: str):
    """Build a real openai APIStatusError subclass (they require an httpx response)."""
    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    response = httpx.Response(status, request=request)
    return exc_type(message, response=response, body=None)


# --- The pure packer --------------------------------------------------------------------------


class TestSubBatches:
    def test_empty_input_yields_no_batches(self):
        assert list(_sub_batches([])) == []

    def test_order_preserved_across_batches(self):
        texts = [f"chunk-{i}" for i in range(_EMBED_INPUT_CAP + 50)]

        flattened = [t for batch in _sub_batches(texts) for t in batch]

        assert flattened == texts  # nothing dropped, reordered, or duplicated

    def test_splits_on_count_cap(self):
        texts = ["a"] * (_EMBED_INPUT_CAP + 5)  # tiny texts: only the count cap can bite

        batches = list(_sub_batches(texts))

        assert len(batches[0]) == _EMBED_INPUT_CAP
        assert [len(b) for b in batches] == [_EMBED_INPUT_CAP, 5]

    def test_splits_on_token_budget_under_count_cap(self):
        # The regression this issue fixes: a batch legal on count (well under 2048) but over the
        # token budget must still split.
        big = "x" * (_CHARS_PER_TOKEN * 40_000)  # ~40k est tokens each
        texts = [big] * 12  # ~480k est tokens total, but only 12 inputs

        batches = list(_sub_batches(texts))

        assert len(batches) > 1
        assert all(len(b) < _EMBED_INPUT_CAP for b in batches)

    def test_every_batch_under_both_caps(self):
        big = "x" * (_CHARS_PER_TOKEN * 40_000)
        texts = ([big] * 20) + (["tiny"] * 100)

        for batch in _sub_batches(texts):
            assert len(batch) <= _EMBED_INPUT_CAP
            assert sum(_est_tokens(t) for t in batch) <= _EMBED_TOKEN_BUDGET

    def test_lone_oversized_text_ships_as_singleton(self):
        # One text whose own estimate exceeds the budget: we can't split it (chunker's contract,
        # ADR 0019), so it must still go out on its own rather than be dropped.
        huge = "x" * (_CHARS_PER_TOKEN * (_EMBED_TOKEN_BUDGET + 10))

        assert list(_sub_batches([huge])) == [[huge]]

    def test_oversized_text_does_not_swallow_its_neighbour(self):
        huge = "x" * (_CHARS_PER_TOKEN * (_EMBED_TOKEN_BUDGET + 10))
        texts = [huge, "after"]

        assert list(_sub_batches(texts)) == [[huge], ["after"]]


# --- embed() end-to-end via the fake client ---------------------------------------------------


class TestEmbed:
    def test_order_and_length_preserved_across_batches(self):
        embedder, _ = _make()
        texts = [f"chunk-{i}" for i in range(_EMBED_INPUT_CAP + 3)]  # forces >1 batch

        vectors = embedder.embed(texts)

        assert len(vectors) == len(texts)
        # fake tags inputs 0,1,2,... in receive order → correct order gives a clean run.
        assert vectors == [[float(i)] for i in range(len(texts))]

    def test_vectors_realign_when_provider_returns_data_out_of_order(self):
        # Grounding-critical: OpenAI *currently* returns `data` sorted by index, but the concat is
        # positional — if data ever arrives reordered, vectors must still map to the right input by
        # `index`, or wrong vectors get indexed → wrong citations. The fake reverses `data` here.
        embedder, _ = _make(reverse_data=True)
        texts = [f"chunk-{i}" for i in range(5)]  # single batch, returned reversed

        vectors = embedder.embed(texts)

        # counter tags inputs 0..4 in receive order; realigned by index, output must be that run.
        assert vectors == [[float(i)] for i in range(len(texts))]

    def test_empty_input_returns_empty_without_calling_provider(self):
        embedder, fake = _make()

        assert embedder.embed([]) == []
        assert fake.calls == []  # no wasted API call

    def test_each_provider_call_stays_under_both_caps(self):
        embedder, fake = _make()
        big = "x" * (_CHARS_PER_TOKEN * 40_000)
        texts = ([big] * 20) + (["tiny"] * 100)

        embedder.embed(texts)

        assert len(fake.calls) > 1  # actually split
        for batch in fake.calls:
            assert len(batch) <= _EMBED_INPUT_CAP
            assert sum(_est_tokens(t) for t in batch) <= _EMBED_TOKEN_BUDGET

    def test_transient_provider_error_is_reclassified(self):
        embedder, _ = _make(error=_openai_error(RateLimitError, 429, "slow down"))

        with pytest.raises(TransientEmbeddingError) as excinfo:
            embedder.embed(["anything"])

        # `raise ... from err` preserves the real cause for the worker's logs / debugging.
        assert isinstance(excinfo.value.__cause__, RateLimitError)

    def test_transient_error_on_a_later_batch_aborts_the_whole_call(self):
        # A mid-stream batch failure discards the whole call (no partial `out`) - the "retry
        # reprocesses from scratch" semantics the Slice-2 worker relies on (ADR 0011 / 0020).
        embedder, fake = _make(
            error=_openai_error(RateLimitError, 429, "slow down"), fail_on_call=2
        )
        texts = ["a"] * (_EMBED_INPUT_CAP + 3)  # forces exactly two batches

        with pytest.raises(TransientEmbeddingError):
            embedder.embed(texts)

        assert len(fake.calls) == 2  # first batch ran; second raised, nothing returned

    def test_terminal_provider_error_propagates_untouched(self):
        embedder, _ = _make(error=_openai_error(AuthenticationError, 401, "bad key"))

        # retrying a bad key is futile → not wrapped as transient.
        with pytest.raises(AuthenticationError):
            embedder.embed(["anything"])
