"""The recording command the owner runs once on his Mac (#198). No real model is ever called here:
the stand-in plays every model, and a small fake program plays the Claude login check."""

from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

import pytest

import gct.grounder.answer
from gct.providers.base import TransientGenerationError
from gct.providers.claude import NOT_SIGNED_IN, ClaudeGeneration
from gct.providers.replay import Recording
from gct.providers.standin import StandInGeneration

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "record_replies.py"
_spec = importlib.util.spec_from_file_location("record_replies_under_test", SCRIPT)
assert _spec is not None and _spec.loader is not None
command = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(command)

COMMITTED = command.REPLIES / "standin.json"


@pytest.fixture
def replies(tmp_path, monkeypatch):
    for name in command.CI_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(command, "ROOT", tmp_path)
    monkeypatch.setattr(command, "REPLIES", tmp_path / "replies")
    return tmp_path / "replies"


class StopsAfter(StandInGeneration):
    def __init__(self, calls: int, stop: BaseException) -> None:
        self.left = calls
        self.stop = stop
        self.calls = 0

    def generate(self, messages):
        if self.left == 0:
            raise self.stop
        self.left -= 1
        self.calls += 1
        return super().generate(messages)


def fake_claude_program(tmp_path: Path, logged_in: bool | str) -> str:
    """Prints what `claude auth status` prints: JSON with a `loggedIn` field, by default."""
    program = tmp_path / "claude"
    if isinstance(logged_in, str):
        output = logged_in
    else:
        output = f'{{"loggedIn": {"true" if logged_in else "false"}, "authMethod": "claude.ai"}}'
    program.write_text(f"#!/bin/sh\necho '{output}'\n", encoding="utf-8")
    program.chmod(program.stat().st_mode | stat.S_IEXEC)
    return str(program)


def never_called(self, messages):
    raise AssertionError("the model was asked")


def test_it_refuses_to_run_in_the_merge_check(replies, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert command.main(["--stand-in"]) == 1
    assert "must not run in the merge check" in capsys.readouterr().err
    assert not replies.exists()


def test_the_stand_in_recording_matches_the_committed_one_byte_for_byte(replies, capsys):
    assert command.main(["--stand-in"]) == 0

    assert (replies / "standin.json").read_bytes() == COMMITTED.read_bytes()
    out = capsys.readouterr().out
    assert "Questions: 100, of which 100 are still to record." in out
    assert "Checked: it replays with no model and no misses." in out
    assert "git add replies/standin.json" in out


def test_a_finished_recording_is_never_replaced_without_again(replies, monkeypatch, capsys):
    command.main(["--stand-in"])
    before = (replies / "standin.json").read_bytes()
    monkeypatch.setattr(StandInGeneration, "generate", never_called)

    assert command.main(["--stand-in"]) == 1
    assert "already holds a finished recording, so nothing was asked" in capsys.readouterr().err
    assert (replies / "standin.json").read_bytes() == before


def test_again_replaces_a_finished_recording(replies):
    command.main(["--stand-in"])
    (replies / "standin.json").write_text(TAMPERED, encoding="utf-8")

    assert command.main(["--stand-in", "--again"]) == 0
    assert (replies / "standin.json").read_bytes() == COMMITTED.read_bytes()
    assert not (replies / "standin.json.draft").exists()


class Budget(StandInGeneration):
    """The stand-in, stopped by Ctrl-C after a given number of calls in each run."""

    def __init__(self) -> None:
        self.total = 0
        self.left = 0

    def generate(self, messages):
        if self.left == 0:
            raise KeyboardInterrupt
        self.left -= 1
        self.total += 1
        return super().generate(messages)


def run_until(budget: Budget, calls: int, args: list[str]) -> int:
    budget.left = calls
    return command.main(args)


TAMPERED = COMMITTED.read_text(encoding="utf-8").replace("COVERAGE: complete", "COVERAGE: old")
UNLIMITED = 10_000


@pytest.mark.parametrize("stops", [(0, 1, 37), (1, 0, 0, 60), (94,)])
def test_an_interrupted_first_recording_carries_on_and_asks_nothing_twice(
    replies, monkeypatch, capsys, stops
):
    budget = Budget()
    monkeypatch.setattr(command, "_stand_in", lambda: budget)

    for calls in stops:
        assert run_until(budget, calls, ["--stand-in"]) == 130
        assert not (replies / "standin.json").exists()
        assert "Run the same command to carry on." in capsys.readouterr().out
    assert run_until(budget, UNLIMITED, ["--stand-in"]) == 0

    assert budget.total == 95
    assert (replies / "standin.json").read_bytes() == COMMITTED.read_bytes()
    assert not (replies / "standin.json.draft").exists()


@pytest.mark.parametrize(
    "runs",
    [
        [(1, ["--again"]), (30, ["--again"]), (0, ["--again"])],
        [(10, ["--again"]), (20, []), (5, [])],
    ],
)
def test_an_interrupted_re_record_never_touches_the_finished_file_until_it_is_complete(
    replies, monkeypatch, runs
):
    replies.mkdir()
    (replies / "standin.json").write_text(TAMPERED, encoding="utf-8")
    budget = Budget()
    monkeypatch.setattr(command, "_stand_in", lambda: budget)

    for calls, flags in runs:
        assert run_until(budget, calls, ["--stand-in", *flags]) == 130
        assert (replies / "standin.json").read_text(encoding="utf-8") == TAMPERED
    assert run_until(budget, UNLIMITED, ["--stand-in", *runs[-1][1]]) == 0

    assert budget.total == 95
    assert (replies / "standin.json").read_bytes() == COMMITTED.read_bytes()


def test_a_draft_made_under_different_instructions_is_not_reused(replies, monkeypatch, capsys):
    budget = Budget()
    monkeypatch.setattr(command, "_stand_in", lambda: budget)
    run_until(budget, 5, ["--stand-in"])
    monkeypatch.setattr(gct.grounder.answer, "_SYSTEM_PROMPT", "Edited instructions.")
    capsys.readouterr()

    assert run_until(budget, UNLIMITED, ["--stand-in"]) == 0

    out = capsys.readouterr().out
    assert "no longer matches what the app sends now (s001: no recording" in out
    assert "so it was removed and a fresh one starts. Nothing in it is reused." in out
    assert "of which 100 are still to record." in out
    assert budget.total == 5 + 95
    assert (replies / "standin.json").read_bytes() != COMMITTED.read_bytes()


class Flaky(StandInGeneration):
    def __init__(self, busy_on: set[int]) -> None:
        self.busy_on = busy_on
        self.calls = 0

    def generate(self, messages):
        self.calls += 1
        if self.calls in self.busy_on:
            raise TransientGenerationError("rate limited")
        return super().generate(messages)


def test_a_busy_try_rescued_by_the_retry_is_recorded_and_the_run_goes_on(replies, monkeypatch):
    model = Flaky({3})
    monkeypatch.setattr(command, "_stand_in", lambda: model)

    assert command.main(["--stand-in"]) == 0

    recording = Recording.read(replies / "standin.json")
    assert [(c.busy, c.reply is None) for c in recording.questions["s003"]][0] == (True, True)
    assert "rate limited" not in (replies / "standin.json").read_text(encoding="utf-8")
    assert len(recording.questions["s003"]) == 2
    assert model.calls == 96


def test_busy_on_both_tries_stops_the_run_and_that_question_is_not_saved(
    replies, monkeypatch, capsys
):
    monkeypatch.setattr(command, "_stand_in", lambda: Flaky({3, 4}))

    assert command.main(["--stand-in"]) == 1
    err = capsys.readouterr().err
    assert "Stopped at s003, which was not saved. Claude is busy" in err
    assert "run the same command to carry on" in err
    assert set(Recording.read(replies / "standin.json.draft").questions) == {"s001", "s002"}


def test_a_folder_it_cannot_write_stops_before_asking_anything(replies, monkeypatch, capsys):
    replies.mkdir()
    replies.chmod(0o500)
    monkeypatch.setattr(StandInGeneration, "generate", never_called)
    try:
        assert command.main(["--stand-in"]) == 1
    finally:
        replies.chmod(0o700)

    err = capsys.readouterr().err
    assert "Cannot save in" in err and "Nothing was asked." in err
    assert "Traceback" not in err


def test_a_full_disk_mid_run_stops_plainly_and_keeps_what_was_saved(replies, monkeypatch, capsys):
    real_write = Recording.write
    writes = []

    def fills_up(self, path):
        writes.append(path)
        if len(writes) == 3:
            raise OSError(28, "No space left on device")
        real_write(self, path)

    monkeypatch.setattr(Recording, "write", fills_up)

    assert command.main(["--stand-in"]) == 1
    err = capsys.readouterr().err
    assert "Could not save to replies/standin.json.draft (No space left on device)." in err
    assert "The 2 questions saved before this one are kept." in err
    assert set(Recording.read(replies / "standin.json.draft").questions) == {"s001", "s002"}


def test_claude_not_signed_in_stops_before_asking_anything(replies, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(command, "_claude_program", lambda: fake_claude_program(tmp_path, False))
    monkeypatch.setattr(ClaudeGeneration, "generate", never_called)

    assert command.main([]) == 1
    assert f"{NOT_SIGNED_IN} Nothing was asked." in capsys.readouterr().err
    assert not replies.exists()


def test_claude_without_its_program_stops_before_asking_anything(replies, monkeypatch, capsys):
    monkeypatch.setattr(command, "_claude_program", lambda: None)
    monkeypatch.setattr(ClaudeGeneration, "generate", never_called)

    assert command.main([]) == 1
    assert "could not be found" in capsys.readouterr().err


def test_claude_signed_in_says_it_uses_the_plan_waits_for_enter_and_records(
    replies, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(command, "_claude_program", lambda: fake_claude_program(tmp_path, True))
    monkeypatch.setattr(
        ClaudeGeneration, "generate", lambda self, messages: StandInGeneration().generate(messages)
    )
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", prompts.append)

    assert command.main([]) == 0

    out = capsys.readouterr().out
    assert out.index("This uses your Claude plan") < out.index("[  1/100]")
    assert prompts == ["\nPress Enter to start, or Ctrl-C to cancel. "]
    recording = Recording.read(replies / "claude.json")
    assert recording.setup.model_id == "claude-agent-sdk:sonnet"
    assert len(recording.questions) == 100


def test_claude_cancelled_at_the_prompt_asks_nothing(replies, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(command, "_claude_program", lambda: fake_claude_program(tmp_path, True))
    monkeypatch.setattr(ClaudeGeneration, "generate", never_called)

    def cancel(prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", cancel)

    assert command.main([]) == 1
    assert "Cancelled. Nothing was asked." in capsys.readouterr().err
    assert not (replies / "claude.json").exists()


def test_unexpected_output_from_the_login_check_says_what_to_do(
    replies, tmp_path, monkeypatch, capsys
):
    program = fake_claude_program(tmp_path, "Welcome! Something unexpected happened.")
    monkeypatch.setattr(command, "_claude_program", lambda: program)
    monkeypatch.setattr(ClaudeGeneration, "generate", never_called)

    assert command.main([]) == 1
    err = capsys.readouterr().err
    assert command.LOGIN_UNKNOWN in err
    assert "Expecting value" not in err


def test_login_json_without_the_logged_in_field_is_not_called_signed_out(
    replies, tmp_path, monkeypatch, capsys
):
    program = fake_claude_program(tmp_path, '{"signedIn": true, "authMethod": "claude.ai"}')
    monkeypatch.setattr(command, "_claude_program", lambda: program)
    monkeypatch.setattr(ClaudeGeneration, "generate", never_called)

    assert command.main([]) == 1
    err = capsys.readouterr().err
    assert command.LOGIN_UNKNOWN in err
    assert NOT_SIGNED_IN not in err
