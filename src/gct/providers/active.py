"""The one place a process picks its models. Nothing else constructs a provider."""

from __future__ import annotations

import logging
import os

from gct.config import load_settings
from gct.providers.base import Embeddings, Generation

PROVIDERS_ENV = "GCT_PROVIDERS"
OPENAI = "openai"
STANDIN = "standin"
# PROTOTYPE (spike #210): answers from Claude via the Agent SDK on the local Claude login;
# embeddings stay on the free stand-in.
CLAUDE = "claude"
CHOICES = (OPENAI, STANDIN, CLAUDE)

_log = logging.getLogger("gct.providers")


def selected() -> str:
    choice = os.environ.get(PROVIDERS_ENV, "").strip() or OPENAI
    if choice not in CHOICES:
        raise RuntimeError(
            f"{PROVIDERS_ENV}={choice!r} is not a provider set. Use one of {CHOICES}, or unset it "
            f"for the default ({OPENAI})."
        )
    return choice


def costs_money() -> bool:
    return selected() == OPENAI


def require_usable() -> None:
    if costs_money() and not load_settings().openai_api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set, so the API cannot embed queries or generate answers. "
            "Set it in `.env` (see `.env.example`) or the environment and start again. To run "
            f"without a key and without cost, set {PROVIDERS_ENV}={STANDIN}: stand-in models "
            "that prove the plumbing but do not really read."
        )


def build_embedder() -> Embeddings:
    if costs_money():
        from gct.providers.openai_provider import OpenAIEmbeddings

        return OpenAIEmbeddings()
    from gct.providers.standin import StandInEmbeddings

    _warn("embeddings", StandInEmbeddings.model_id)
    return StandInEmbeddings()


def build_generator() -> Generation:
    if costs_money():
        from gct.providers.openai_provider import OpenAIGeneration

        return OpenAIGeneration()
    if selected() == CLAUDE:
        from gct.providers.claude_sdk import ClaudeSDKGeneration

        return ClaudeSDKGeneration()
    from gct.providers.standin import StandInGeneration

    _warn("answers", StandInGeneration.model_id)
    return StandInGeneration()


def _warn(what: str, model_id: str) -> None:
    _log.warning(
        "STAND-IN MODELS (%s=%s): %s come from %s. Nothing here is a real model's output.",
        PROVIDERS_ENV,
        STANDIN,
        what,
        model_id,
    )
