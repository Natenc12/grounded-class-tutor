from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sys
import time
import uuid

import claude_agent_sdk as sdk
import pytest
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from gct.contracts import RetrievedChunk
from gct.grounder.answer import ERROR_KIND_PROVIDER_TERMINAL, GrounderState, answer
from gct.providers import active, claude
from gct.providers.base import TransientGenerationError
from gct.providers.claude import (
    INSTALL_HINT,
    MISSING_PROGRAM,
    NOT_SIGNED_IN,
    ClaudeError,
    ClaudeGeneration,
    ClaudeNotInstalledError,
    ClaudeNotSignedInError,
)
from gct.providers.standin import StandInEmbeddings

MESSAGES = [
    {"role": "system", "content": "Answer only from the sources."},
    {"role": "user", "content": "SOURCES\n[S1] (water.pdf, page 3)\nRain falls.\n\nQUESTION\nWhy?"},
]

LOCKED_DOWN = {
    "model": "sonnet",
    "system_prompt": "Answer only from the sources.",
    "tools": [],
    "disallowed_tools": ["*"],
    "strict_mcp_config": True,
    "setting_sources": [],
    "settings": '{"skillOverrides": {"design": "off", "doctor": "off", "plugin-authoring": "off"}}',
    "skills": [],
    "permission_mode": "dontAsk",
    "max_turns": 1,
    "env": {"CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1", "DISABLE_DOCTOR_COMMAND": "1"},
}

WATER = RetrievedChunk(
    chunk_id="c1",
    text="An aquifer is a body of rock porous enough to store and transmit groundwater. "
    "Groundwater moves slowly, sometimes taking centuries to travel a few kilometres.",
    file="hydrology-lecture-01.pdf",
    page_or_slide=2,
    score=0.9,
)
ROCKS = RetrievedChunk(
    chunk_id="c2",
    text="Granite is an igneous rock. It cools slowly below the surface.",
    file="rocks.pdf",
    page_or_slide=7,
    score=0.2,
)


def _init(**data):
    return sdk.SystemMessage(subtype="init", data={"apiKeySource": "none", **data})


def _said(text, error=None):
    return sdk.AssistantMessage(content=[sdk.TextBlock(text=text)], model="m", error=error)


def _result(text, *, is_error=False, status=None):
    return sdk.ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s",
        result=text,
        api_error_status=status,
    )


def _result_error(text, *, status=None):
    return sdk.ResultError(
        f"Claude Code returned an error result: {text}",
        data={"subtype": "success", "result": text, "api_error_status": status},
        exit_code=1,
    )


HANG = object()


class FakeQuery:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.closed = False

    def __call__(self, *, prompt, options):
        self.calls.append(
            {
                "prompt": prompt,
                "options": options,
                "env": dict(os.environ),
                "folder": sorted(os.listdir(options.cwd)) if os.path.isdir(options.cwd) else None,
            }
        )
        return self._stream()

    async def _stream(self):
        try:
            for item in self.script:
                if item is HANG:
                    await asyncio.sleep(3600)
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            self.closed = True


@pytest.fixture
def fake(monkeypatch):
    def install(*script):
        stub = FakeQuery(*script)
        monkeypatch.setattr(sdk, "query", stub)
        return stub

    monkeypatch.delenv(claude.MODEL_ENV, raising=False)
    return install


def _changed_options(options):
    defaults = sdk.ClaudeAgentOptions()
    return {
        field.name: getattr(options, field.name)
        for field in dataclasses.fields(options)
        if getattr(options, field.name) != getattr(defaults, field.name)
    }


def test_claude_gets_exactly_the_locked_down_options_and_an_empty_folder_it_never_keeps(fake):
    stub = fake(_init(), _said("Rain falls [S1]."), _result("Rain falls [S1]."))

    assert ClaudeGeneration().generate(MESSAGES) == "Rain falls [S1]."

    [call] = stub.calls
    changed = _changed_options(call["options"])
    folder = changed.pop("cwd")
    assert changed == LOCKED_DOWN
    assert call["folder"] == []
    assert not os.path.exists(folder)
    assert call["options"].mcp_servers == {}
    assert call["options"].allowed_tools == []
    assert call["options"].add_dirs == []
    assert call["options"].plugins == []
    assert call["prompt"] == MESSAGES[1]["content"]


def test_the_real_sdk_turns_the_options_into_a_command_with_no_tools_settings_or_servers(tmp_path):
    options = claude.build_options(sdk, "sonnet", "system", str(tmp_path))
    command = SubprocessCLITransport(
        "q", dataclasses.replace(options, cli_path="/no/such/claude")
    )._build_command()

    def value_of(flag):
        return command[command.index(flag) + 1]

    assert value_of("--tools") == ""
    assert value_of("--disallowedTools") == "*"
    assert value_of("--permission-mode") == "dontAsk"
    assert value_of("--max-turns") == "1"
    assert "--strict-mcp-config" in command
    assert "--setting-sources=" in command
    assert json.loads(value_of("--settings")) == {
        "skillOverrides": {"design": "off", "doctor": "off", "plugin-authoring": "off"}
    }
    for absent in ("--allowedTools", "--add-dir", "--plugin-dir"):
        assert absent not in command


def test_login_overrides_never_reach_claude_or_the_log_and_come_back_afterwards(
    fake, monkeypatch, caplog
):
    values = {
        name: f"generated-{uuid.uuid4()}"
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
    }
    values["ANTHROPIC_CUSTOM_HEADERS"] = f"x-api-key: generated-{uuid.uuid4()}"
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    stub = fake(_init(), _result("ok"))

    with caplog.at_level(logging.DEBUG):
        ClaudeGeneration().generate(MESSAGES)

    [call] = stub.calls
    for name, value in values.items():
        assert name not in call["env"]
        assert name not in call["options"].env
        assert value not in caplog.text
        assert os.environ[name] == value
    assert "ANTHROPIC_API_KEY" in caplog.text


def test_every_documented_login_override_is_set_aside(fake, monkeypatch):
    for name in claude.LOGIN_OVERRIDES:
        monkeypatch.setenv(name, "1")
    stub = fake(_init(), _result("ok"))

    ClaudeGeneration().generate(MESSAGES)

    assert not set(claude.LOGIN_OVERRIDES) & set(stub.calls[0]["env"])


@pytest.mark.parametrize(
    "script",
    [
        [_init(), _said("API Error: 429", error="rate_limit"), _result_error("429", status=429)],
        [_init(), _said("API Error: 529 Overloaded"), _result_error("Overloaded", status=529)],
        [_init(), _said("API Error: 500", error="server_error"), _result_error("Internal")],
        [_init(), _result("API Error: 503", is_error=True, status=503)],
    ],
    ids=["rate-limit", "overloaded", "server-error", "error-result-without-exception"],
)
def test_rate_limits_overload_and_server_errors_are_worth_the_one_retry(fake, script):
    fake(*script)
    with pytest.raises(TransientGenerationError):
        ClaudeGeneration().generate(MESSAGES)


def test_a_missing_or_expired_login_stops_with_how_to_sign_in(fake):
    not_logged_in = "Not logged in · Please run /login"
    fake(
        _init(),
        _said(not_logged_in, error="authentication_failed"),
        _result(not_logged_in, is_error=True),
        _result_error(not_logged_in),
    )
    with pytest.raises(ClaudeNotSignedInError) as caught:
        ClaudeGeneration().generate(MESSAGES)
    assert str(caught.value) == NOT_SIGNED_IN
    assert not isinstance(caught.value, TransientGenerationError)


def test_a_missing_program_stops_with_how_to_reinstall(fake):
    fake(sdk.CLINotFoundError("Claude Code not found"))
    with pytest.raises(ClaudeNotInstalledError) as caught:
        ClaudeGeneration().generate(MESSAGES)
    assert str(caught.value) == MISSING_PROGRAM


def test_a_missing_sdk_stops_with_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    with pytest.raises(ClaudeNotInstalledError) as caught:
        ClaudeGeneration().generate(MESSAGES)
    assert str(caught.value) == INSTALL_HINT


@pytest.mark.parametrize(
    "script",
    [
        [_init(), sdk.ProcessError("Command failed", exit_code=1)],
        [_init(), _said("bad", error="invalid_request"), _result_error("bad", status=400)],
        [_init()],
    ],
    ids=["crash", "bad-request", "no-result"],
)
def test_other_failures_stop_without_a_retry(fake, script):
    fake(*script)
    with pytest.raises(ClaudeError) as caught:
        ClaudeGeneration().generate(MESSAGES)
    assert not isinstance(caught.value, TransientGenerationError)


@pytest.mark.parametrize(
    ("final", "message"),
    [
        (_result(None), "Claude finished without writing an answer."),
        (_result(None, is_error=True), "Claude reported an error without saying what went wrong."),
    ],
    ids=["no-answer-no-error", "error-without-a-reason"],
)
def test_an_empty_ending_stops_with_a_plain_message(fake, final, message):
    fake(_init(), final)
    with pytest.raises(ClaudeError) as caught:
        ClaudeGeneration().generate(MESSAGES)
    assert message in str(caught.value)
    assert "success" not in str(caught.value)


def test_a_call_that_hangs_is_ended_at_the_time_limit_and_worth_the_one_retry(fake):
    stub = fake(_init(), HANG)
    started = time.monotonic()

    with pytest.raises(TransientGenerationError, match="did not answer within 0.2 seconds"):
        ClaudeGeneration(time_limit=0.2).generate(MESSAGES)

    assert time.monotonic() - started < 5
    assert stub.closed


def test_generate_works_from_inside_a_running_event_loop(fake):
    fake(_init(), _result("from inside a loop"))

    async def inside():
        asyncio.get_running_loop()
        return ClaudeGeneration().generate(MESSAGES)

    assert asyncio.run(inside()) == "from inside a loop"


def test_the_model_comes_from_gct_claude_model_or_defaults_to_sonnet(fake, monkeypatch):
    stub = fake(_init(), _result("ok"))
    ClaudeGeneration().generate(MESSAGES)
    monkeypatch.setenv(claude.MODEL_ENV, "  opus ")
    ClaudeGeneration().generate(MESSAGES)

    assert [call["options"].model for call in stub.calls] == ["sonnet", "opus"]
    assert ClaudeGeneration().model_id == "claude-agent-sdk:opus"


def test_the_grounder_retries_a_rate_limit_once_then_uses_the_reply(fake, monkeypatch):
    replies = iter(
        [
            [_init(), _said("429", error="rate_limit"), _result_error("429", status=429)],
            [_init(), _result("An aquifer stores groundwater [S1].\nCOVERAGE: complete")],
        ]
    )
    monkeypatch.setattr(sdk, "query", lambda **kw: FakeQuery(*next(replies))(**kw))

    result = answer("What is an aquifer?", [WATER], "owner", generator=ClaudeGeneration())

    assert result.state is GrounderState.GROUNDED
    assert [c.file for c in result.citations] == ["hydrology-lecture-01.pdf"]


def test_the_grounder_reports_a_lost_login_as_an_error_with_the_plain_message(fake):
    fake(_init(), _said("Not logged in", error="authentication_failed"), _result_error("x"))

    result = answer("What is an aquifer?", [WATER], "owner", generator=ClaudeGeneration())

    assert result.state is GrounderState.ERROR
    assert result.error.kind == ERROR_KIND_PROVIDER_TERMINAL
    assert NOT_SIGNED_IN in result.error.message


def test_gct_providers_claude_answers_with_claude_and_searches_with_the_free_stand_in(monkeypatch):
    monkeypatch.setenv(active.PROVIDERS_ENV, "claude")
    monkeypatch.setenv("OPENAI_API_KEY", "")

    active.require_usable()
    assert active.selected() == active.CLAUDE
    assert not active.costs_money()
    assert isinstance(active.build_generator(), ClaudeGeneration)
    assert isinstance(active.build_embedder(), StandInEmbeddings)


def test_gct_providers_claude_refuses_to_start_without_the_sdk(monkeypatch):
    monkeypatch.setenv(active.PROVIDERS_ENV, "claude")
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    with pytest.raises(RuntimeError, match="uv sync --extra claude"):
        active.require_usable()


def test_a_misspelt_provider_set_still_fails_with_the_existing_message(monkeypatch):
    monkeypatch.setenv(active.PROVIDERS_ENV, "claud")
    with pytest.raises(RuntimeError, match=r"GCT_PROVIDERS='claud' is not a provider set"):
        active.selected()


def test_a_real_claude_answer_on_this_macs_login_is_cited(live_claude_generator):
    started = time.monotonic()
    result = answer(
        "What is an aquifer, and how fast does groundwater move?",
        [WATER, ROCKS],
        "owner",
        generator=live_claude_generator,
    )
    seconds = time.monotonic() - started

    print(f"\nstate: {result.state.value}  ({seconds:.1f} s)")
    print(f"answer: {result.answer_prose}")
    print(f"citations: {[(c.label, c.file, c.page_or_slide) for c in result.citations]}")
    print(f"coverage: {result.coverage}")
    print(f"integrity: {result.integrity}")
    print(f"claude started with: {live_claude_generator.last_start}")

    assert live_claude_generator.last_start["apiKeySource"] == "none"
    assert live_claude_generator.last_start["tools"] == []
    assert live_claude_generator.last_start["mcp_servers"] == []
    assert live_claude_generator.last_start["skills"] == []
    assert result.state in (GrounderState.GROUNDED, GrounderState.PARTIAL)
    assert result.integrity.ok
    assert result.citations
    assert {(c.file, c.page_or_slide) for c in result.citations} <= {
        (WATER.file, WATER.page_or_slide)
    }
