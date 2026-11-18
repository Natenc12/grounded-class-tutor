"""Fixtures for the grounder suite (issue #6).

The whole suite is PURE: no DB, no network, no paid provider - so it carries neither the `db`
nor the `live` marker (tests/conftest.py derives `db` from its fixture; `live` is for the paid
tests arriving with #8). The Grounder's only outside edge is `Generation.generate`, and a stub
is not a weaker test here: the box's entire job is deciding what to trust about the text that
comes back, so SCRIPTING that text is how you reach every branch deterministically. A real model
would make the suite slow, paid, and flaky, and would still never reliably produce a dangling
`[S5]` on demand.

  - `ScriptedGeneration` / `scripted` - a `Generation`-shaped stub that hands back queued
    replies in order and RAISES any exception left in the queue, which is what makes the shared
    retry budget testable (transient-then-success, transient-twice, and the mixed sequences).
    It records every call, so "no generation call" is an assertable fact, not an assumption.
  - `chunks` - build `RetrievedChunk`s with predictable provenance, so a resolved Citation can
    be checked field by field against the chunk it came from.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from gct.providers.base import Message
from gct.retriever.retrieve import RetrievedChunk


class ScriptedGeneration:
    """Deterministic `Generation` stub: replays `script` in order, one item per `generate` call.

    A `str` item is returned as the model's reply; an `Exception` INSTANCE is raised instead.
    Mixing the two in one script is the point - `[TransientGenerationError(...), "good reply"]`
    is exactly the retry-once path, and nothing else exercises it.

    Running off the end of the script fails the test, never returns a silent default: a test that
    scripts two replies is asserting the budget stops at two, and a stub that quietly kept
    answering would turn a budget regression into a green run.

    That over-budget signal MUST NOT be a plain `Exception`, and this is not a style choice.
    `answer()` deliberately catches broad `Exception` around `generate()` (a thin provider seam
    can't enumerate a provider's error types - ADR 0013), so an `AssertionError` raised here
    would be swallowed and re-reported as a tidy `state=ERROR`: a budget regression would come
    back looking like a provider outage. `pytest.fail` raises pytest's own `Failed`, which
    derives from `BaseException` and therefore travels straight through that except clause.
    """

    def __init__(self, *script: str | Exception, model_id: str = "fake-gen-1") -> None:
        self._script = list(script)
        self._model_id = model_id
        self.calls: list[list[Message]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def generate(self, messages: Sequence[Message]) -> str:
        self.calls.append(list(messages))
        if not self._script:
            pytest.fail(
                f"generate() called {len(self.calls)} time(s) but only "
                f"{len(self.calls) - 1} reply/replies were scripted - the caller exceeded "
                "its generation budget"
            )
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def scripted():
    """Factory: `scripted("reply", SomeError("boom"), ...)` -> a `ScriptedGeneration`."""

    def _make(*script: str | Exception, model_id: str = "fake-gen-1") -> ScriptedGeneration:
        return ScriptedGeneration(*script, model_id=model_id)

    return _make


@pytest.fixture
def chunks():
    """Factory: `chunks(n=2)` -> n `RetrievedChunk`s in rank order, with distinct provenance.

    `chunk_id`/`file`/`page_or_slide` are all derived from the rank so an assertion can name the
    exact chunk a citation must have resolved to - `S2` must carry `chunk-2`, `lecture-2.pdf`,
    page 2. Scores descend but are otherwise arbitrary: they are PLUMBED, NOT GATED in V1
    (ADR 0008), and a test that made them matter would be testing a V3 filter that isn't there.
    """

    def _make(n: int = 2) -> list[RetrievedChunk]:
        return [
            RetrievedChunk(
                chunk_id=f"chunk-{i}",
                text=f"Source text number {i}.",
                file=f"lecture-{i}.pdf",
                page_or_slide=i,
                score=1.0 - i / 100,
            )
            for i in range(1, n + 1)
        ]

    return _make
