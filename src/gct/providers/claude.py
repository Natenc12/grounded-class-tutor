"""Answers written by Claude through the Claude Agent SDK, on the Claude login already on this Mac.

Claude only turns the Grounder's messages into text: no tools, no skills, no MCP servers, no
settings files, one turn, and an empty folder to start in. Citations and answer states stay in
`gct.grounder.answer`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
from collections.abc import Awaitable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing, contextmanager
from typing import Any, Protocol, runtime_checkable

from gct.providers.base import Message, TransientGenerationError

MODEL_ENV = "GCT_CLAUDE_MODEL"
DEFAULT_MODEL = "sonnet"
TIME_LIMIT_SECONDS = 120.0

LOGIN_OVERRIDES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_WORKSPACE_ID",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_AWS_API_KEY",
    "ANTHROPIC_AWS_WORKSPACE_ID",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
    "CLAUDE_CODE_CLIENT_CERT",
    "CLAUDE_CODE_CLIENT_KEY",
    "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
)

CLI_ENV = {"CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1", "DISABLE_DOCTOR_COMMAND": "1"}
SKILLS_OFF = ("design", "doctor", "plugin-authoring")
CLI_SETTINGS = json.dumps({"skillOverrides": {name: "off" for name in SKILLS_OFF}})

INSTALL_HINT = (
    "The Claude Agent SDK is not installed. In the project folder run `uv sync --extra claude` "
    "(or `--extra dev`), then try again."
)
MISSING_PROGRAM = (
    "The Claude program that comes with the Claude Agent SDK could not be found. In the project "
    "folder run `uv sync --extra claude --reinstall-package claude-agent-sdk`, then try again."
)
NOT_SIGNED_IN = (
    "Claude is not signed in on this Mac, or the login has expired. Open Terminal, run `claude`, "
    "sign in with /login, then try again."
)
NO_ANSWER = "Claude finished without writing an answer."
NO_REASON = "Claude reported an error without saying what went wrong."
UNCLOSABLE = (
    "The installed Claude Agent SDK gave back a reply that cannot be stopped early, so the tutor "
    "did not read it. This SDK version does not work with the tutor."
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


@runtime_checkable
class _Closable(Protocol):
    """A reply stream that can be stopped early, so a timeout or cancel ends Claude at once.

    `sdk.query` is an async generator, which always has `aclose`, but the SDK annotates it as a
    plain `AsyncIterator`, which does not. `_ask` checks for it rather than assuming it.
    """

    def aclose(self) -> Awaitable[object]: ...


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
        settings=CLI_SETTINGS,
        skills=[],
        plugins=[],
        permission_mode="dontAsk",
        max_turns=1,
        cwd=folder,
        add_dirs=[],
        env=dict(CLI_ENV),
    )


def request(sdk: Any, model: str, messages: Sequence[Message], folder: str) -> tuple[str, Any]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    prompt = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
    return prompt, build_options(sdk, model, system, folder)


class ClaudeGeneration:
    def __init__(self, model: str | None = None, time_limit: float = TIME_LIMIT_SECONDS) -> None:
        self._model = model or os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL
        self._time_limit = time_limit
        self.last_start: dict[str, Any] = {}

    @property
    def model_id(self) -> str:
        return f"claude-agent-sdk:{self._model}"

    def generate(self, messages: Sequence[Message]) -> str:
        with login_overrides_removed(), ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(self._ask(messages))).result()

    async def _ask(self, messages: Sequence[Message]) -> str:
        try:
            import anyio
            import claude_agent_sdk as sdk
        except ImportError as err:
            raise ClaudeNotInstalledError(INSTALL_HINT) from err

        kind: str | None = None
        result: Any = None
        with tempfile.TemporaryDirectory(prefix="gct-claude-") as folder:
            prompt, options = request(sdk, self._model, messages, folder)
            try:
                with anyio.fail_after(self._time_limit):
                    stream = sdk.query(prompt=prompt, options=options)
                    if not isinstance(stream, _Closable):
                        raise ClaudeError(UNCLOSABLE)
                    async with aclosing(stream) as replies:
                        async for message in replies:
                            if isinstance(message, sdk.SystemMessage) and message.subtype == "init":
                                self.last_start = {
                                    key: message.data.get(key)
                                    for key in (
                                        "apiKeySource",
                                        "model",
                                        "tools",
                                        "mcp_servers",
                                        "skills",
                                    )
                                }
                            elif isinstance(message, sdk.AssistantMessage) and message.error:
                                kind = message.error
                            elif isinstance(message, sdk.ResultMessage):
                                result = message
            except TimeoutError as err:
                raise TransientGenerationError(
                    f"Claude did not answer within {self._time_limit:g} seconds."
                ) from err
            except sdk.CLINotFoundError as err:
                raise ClaudeNotInstalledError(MISSING_PROGRAM) from err
            except sdk.ResultError as err:
                raise _failure(kind, err.api_error_status, str(err)) from err
            except sdk.ClaudeSDKError as err:
                raise ClaudeError(f"Claude stopped before answering: {err}") from err

        if result is None:
            raise ClaudeError("Claude ended without a reply.")
        if result.is_error:
            detail = result.result or "; ".join(result.errors or []) or NO_REASON
            raise _failure(kind, result.api_error_status, f"Claude returned an error: {detail}")
        if result.result is None:
            raise ClaudeError(NO_ANSWER)
        return result.result


def _failure(kind: str | None, status: int | None, detail: str) -> Exception:
    if kind in _NOT_SIGNED_IN_KINDS or status in _NOT_SIGNED_IN_STATUSES:
        return ClaudeNotSignedInError(NOT_SIGNED_IN)
    if kind in _TRANSIENT_KINDS or status in _TRANSIENT_STATUSES:
        return TransientGenerationError(detail)
    return ClaudeError(detail)
