"""Answers written by Claude through the Claude Agent SDK, on the Claude login already on this Mac.

Claude only turns the Grounder's messages into text: no tools, no skills it can run, no MCP
servers, no settings files, one turn, and an empty folder to start in. Citations and answer states
stay in `gct.grounder.answer`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

from gct.providers.base import Message, TransientGenerationError

MODEL_ENV = "GCT_CLAUDE_MODEL"
DEFAULT_MODEL = "sonnet"

LOGIN_OVERRIDES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_IDENTITY_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_MANTLE",
)

CLI_ENV = {"CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1"}

INSTALL_HINT = (
    "The Claude Agent SDK is not installed. In the project folder run `uv sync --extra claude` "
    "(or `--extra dev`), then ask again."
)
MISSING_PROGRAM = (
    "The Claude program that comes with the Claude Agent SDK could not be found. In the project "
    "folder run `uv sync --extra claude --reinstall-package claude-agent-sdk`, then ask again."
)
NOT_SIGNED_IN = (
    "Claude is not signed in on this Mac, or the login has expired. Open Terminal, run `claude`, "
    "sign in with /login, then ask again."
)

_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})
_TRANSIENT_KINDS = frozenset({"rate_limit", "server_error"})
_NOT_SIGNED_IN_KINDS = frozenset({"authentication_failed"})
_NOT_SIGNED_IN_STATUSES = frozenset({401})

_env_lock = threading.Lock()
_log = logging.getLogger("gct.providers")


class ClaudeError(RuntimeError):
    pass


class ClaudeNotInstalledError(ClaudeError):
    pass


class ClaudeNotSignedInError(ClaudeError):
    pass


def sdk_installed() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


@contextmanager
def login_overrides_removed() -> Iterator[list[str]]:
    with _env_lock:
        saved = {name: os.environ.pop(name) for name in LOGIN_OVERRIDES if name in os.environ}
        if saved:
            _log.info("Claude uses this Mac's login; set aside for the call: %s", sorted(saved))
        try:
            yield sorted(saved)
        finally:
            os.environ.update(saved)


def build_options(sdk: Any, model: str, system: str, folder: str) -> Any:
    return sdk.ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        tools=[],
        allowed_tools=[],
        disallowed_tools=["*"],
        mcp_servers={},
        strict_mcp_config=True,
        setting_sources=[],
        skills=[],
        plugins=[],
        permission_mode="dontAsk",
        max_turns=1,
        cwd=folder,
        add_dirs=[],
        env=dict(CLI_ENV),
    )


class ClaudeGeneration:
    def __init__(self, model: str | None = None) -> None:
        self._model = model or os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL
        self.last_start: dict[str, Any] = {}

    @property
    def model_id(self) -> str:
        return f"claude-agent-sdk:{self._model}"

    def generate(self, messages: Sequence[Message]) -> str:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        prompt = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
        with login_overrides_removed(), ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(self._ask(system, prompt))).result()

    async def _ask(self, system: str, prompt: str) -> str:
        try:
            import claude_agent_sdk as sdk
        except ImportError as err:
            raise ClaudeNotInstalledError(INSTALL_HINT) from err

        kind: str | None = None
        result: Any = None
        with tempfile.TemporaryDirectory(prefix="gct-claude-") as folder:
            options = build_options(sdk, self._model, system, folder)
            try:
                async for message in sdk.query(prompt=prompt, options=options):
                    if isinstance(message, sdk.SystemMessage) and message.subtype == "init":
                        self.last_start = {
                            key: message.data.get(key)
                            for key in ("apiKeySource", "model", "tools", "mcp_servers", "skills")
                        }
                    elif isinstance(message, sdk.AssistantMessage) and message.error:
                        kind = message.error
                    elif isinstance(message, sdk.ResultMessage):
                        result = message
            except sdk.CLINotFoundError as err:
                raise ClaudeNotInstalledError(MISSING_PROGRAM) from err
            except sdk.ResultError as err:
                raise _failure(kind, err.api_error_status, str(err)) from err
            except sdk.ClaudeSDKError as err:
                raise ClaudeError(f"Claude stopped before answering: {err}") from err

        if result is None:
            raise ClaudeError("Claude ended without a reply.")
        if result.is_error or result.result is None:
            detail = result.result or "; ".join(result.errors or []) or result.subtype
            raise _failure(kind, result.api_error_status, f"Claude returned an error: {detail}")
        return result.result


def _failure(kind: str | None, status: int | None, detail: str) -> Exception:
    if kind in _NOT_SIGNED_IN_KINDS or status in _NOT_SIGNED_IN_STATUSES:
        return ClaudeNotSignedInError(NOT_SIGNED_IN)
    if kind in _TRANSIENT_KINDS or status in _TRANSIENT_STATUSES:
        return TransientGenerationError(detail)
    return ClaudeError(detail)
