"""Fixtures shared across every test package.

Home to the `live_*` fixtures, which build a REAL model provider, and to the hook that marks any
test taking one as `live`. pytest only exposes a conftest to its own directory and below, so
fixtures every suite may need live at the root.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items):
    """Apply the `live` marker to every test that requests a `live_*` fixture.

    The marker is DERIVED from the dependency, never hand-declared — the promise the marker's
    own registration text made ("derive it from an OpenAI-client fixture the same way the moment
    a paid test exists", pyproject.toml; epic #9's pass-2 flag). Hand-declared, `live` fails in
    two directions at once: a paid test that FORGETS the mark runs in CI against an empty
    `OPENAI_API_KEY` (money, or red for the wrong reason), and one that carries it silently
    leaves CI coverage. Deriving from the fixture leaves no second copy to drift. The prefix is
    the convention: constructing a REAL provider client is what makes a test paid, so every such
    fixture lives in this file and is named `live_*`. The edge this cannot cover: a test that
    builds a real client inline, without taking such a fixture, gets no mark. Take the fixture
    and it stays true.
    """
    for item in items:
        fixturenames = getattr(item, "fixturenames", ())
        if any(name.startswith("live_") for name in fixturenames):
            item.add_marker("live")


@pytest.fixture
def live_openai_embedder():
    """The REAL OpenAI embedder — taking this fixture is what makes a test `live` (paid).

    The `live_` prefix is load-bearing: `pytest_collection_modifyitems` derives the `live` marker
    from it, so a test can no more forget the mark than forget the client it spends money
    through. Skips (never fails) without a key: `live` tests run locally with `.env` secrets by
    design (pyproject.toml), and on a machine without them "cannot run" is a skip-shaped fact:
    there is no environment that GUARANTEES a key and could hard-fail instead.
    """
    from gct.config import load_settings
    from gct.providers.openai_provider import OpenAIEmbeddings

    if not load_settings().openai_api_key:
        pytest.skip("no OPENAI_API_KEY in the environment/.env — live tests need real secrets")
    return OpenAIEmbeddings()


@pytest.fixture
def live_claude_generator():
    """The REAL Claude provider, on this Mac's own Claude login. Taking it makes a test `live`.

    No key and no money: it draws from the owner's Claude plan limits instead. Skips without the
    Claude Agent SDK installed (`uv sync --extra claude`).
    """
    pytest.importorskip("claude_agent_sdk", reason="the Claude Agent SDK is not installed")
    from gct.providers.claude import ClaudeGeneration

    return ClaudeGeneration()
