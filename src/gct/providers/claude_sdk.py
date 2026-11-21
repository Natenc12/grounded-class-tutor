"""PROTOTYPE (spike #210, never merged): answers written by Claude through the Claude Agent SDK,
on the Claude login already on this Mac. No API key.

The model gets no tools, no MCP servers, no settings files and an empty scratch folder as its
working directory: it only turns the Grounder's prompt into text. Citation parsing and the answer
states stay in `gct.grounder.answer`, unchanged (ADR 0013).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Sequence

from gct.providers.base import Message, TransientGenerationError

MODEL_ENV = "GCT_CLAUDE_MODEL"
DEFAULT_MODEL = "sonnet"

# Variables that would make the CLI use something other than the local subscription login
# (docs: code.claude.com/docs/en/authentication#authentication-precedence), plus the base URL,
# which would send requests somewhere other than Anthropic.
CREDENTIAL_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
)

# HTTP statuses worth the Grounder's one retry (rate limit, overload, server error).
_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})


def scrub_credentials() -> list[str]:
    """Remove every credential override from this process, so the SDK's CLI child inherits none
    of them and falls back to the subscription login. Returns the NAMES removed, never values."""
    return [name for name in CREDENTIAL_ENV_VARS if os.environ.pop(name, None) is not None]


class ClaudeSDKGeneration:
    def __init__(self, model_id: str | None = None) -> None:
        self._model_id = model_id or os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL
        self.scrubbed = scrub_credentials()
        self.last_call: dict[str, object] = {}

    @property
    def model_id(self) -> str:
        return f"claude-agent-sdk:{self._model_id}"

    def generate(self, messages: Sequence[Message]) -> str:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        prompt = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
        return asyncio.run(self._generate(system, prompt))

    async def _generate(self, system: str, prompt: str) -> str:
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ProcessError,
            ResultMessage,
            SystemMessage,
            query,
        )

        with tempfile.TemporaryDirectory(prefix="gct-claude-") as empty_dir:
            options = ClaudeAgentOptions(
                model=self._model_id,
                system_prompt=system,
                tools=[],  # `--tools ""`: no built-in tools at all
                disallowed_tools=["*"],  # and deny any tool that still appears
                mcp_servers={},
                strict_mcp_config=True,  # ignore MCP servers from any config file
                setting_sources=[],  # no user/project/local settings, CLAUDE.md or skills
                permission_mode="dontAsk",  # anything that would prompt is denied
                max_turns=1,
                cwd=empty_dir,
            )
            result: ResultMessage | None = None
            init: dict[str, object] = {}
            try:
                async for message in query(prompt=prompt, options=options):
                    if isinstance(message, ResultMessage):
                        result = message
                    elif isinstance(message, SystemMessage) and message.subtype == "init":
                        # What the CLI says it started with: credential source, tools, MCP.
                        init = {
                            k: message.data.get(k)
                            for k in ("apiKeySource", "tools", "mcp_servers", "model", "skills")
                        }
            except ProcessError as err:
                status = getattr(err, "api_error_status", None)
                if status in _TRANSIENT_STATUSES:
                    raise TransientGenerationError(str(err)) from err
                raise

        if result is None:
            raise RuntimeError("Claude Agent SDK returned no result message")
        self.last_call = {
            "init": init,
            "duration_ms": result.duration_ms,
            "duration_api_ms": result.duration_api_ms,
            "num_turns": result.num_turns,
            "subtype": result.subtype,
            "usage": result.usage,
            "model_usage_models": sorted((result.model_usage or {}).keys()),
            "permission_denials": result.permission_denials,
            "total_cost_usd_estimate": result.total_cost_usd,
        }
        if result.is_error or result.result is None:
            if result.api_error_status in _TRANSIENT_STATUSES:
                raise TransientGenerationError(f"{result.subtype}: {result.errors}")
            raise RuntimeError(f"Claude Agent SDK error result: {result.subtype} {result.errors}")
        return result.result
