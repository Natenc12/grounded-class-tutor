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
    own registration text made (pyproject.toml; epic #9's pass-2 flag). Hand-declared, `live`
    fails in two directions at once: a real-model test that FORGETS the mark runs in CI, which
    has no Claude login (red for the wrong reason), and one that carries it silently leaves CI
    coverage. Deriving from the fixture leaves no second copy to drift. The prefix is
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
def live_claude_generator():
    """The REAL Claude provider, on this Mac's own Claude login. Taking it makes a test `live`.

    No key and no money: it draws from the owner's Claude plan limits instead. Skips without the
    Claude Agent SDK installed (`uv sync --extra claude`).
    """
    pytest.importorskip("claude_agent_sdk", reason="the Claude Agent SDK is not installed")
    from gct.providers.claude import ClaudeGeneration

    return ClaudeGeneration()
