"""The recording command the owner runs once on his Mac (#198). No real model is ever called here:
the stand-in plays every model, and a small fake program plays the Claude login check."""

from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

import pytest

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


def fake_claude_program(tmp_path: Path, logged_in: bool) -> str:
    program = tmp_path / "claude"
    answer = "true" if logged_in else "false"
    program.write_text(f"#!/bin/sh\necho '{{\"loggedIn\": {answer}}}'\n", encoding="utf-8")
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
    (replies / "standin.json").write_text(
        COMMITTED.read_text(encoding="utf-8").replace("COVERAGE: complete", "COVERAGE: changed"),
        encoding="utf-8",
    )

    assert command.main(["--stand-in", "--again"]) == 0
    assert (replies / "standin.json").read_bytes() == COMMITTED.read_bytes()


def test_ctrl_c_keeps_what_was_recorded_and_the_next_run_asks_only_the_rest(
    replies, monkeypatch, capsys
):
    first = StopsAfter(3, KeyboardInterrupt())
    monkeypatch.setattr(command, "_stand_in", lambda: first)

    assert command.main(["--stand-in"]) == 130
    saved = Recording.read(replies / "standin.json")
    assert "run the same command to carry on" in capsys.readouterr().out.lower()

    rest = StopsAfter(1000, KeyboardInterrupt())
    monkeypatch.setattr(command, "_stand_in", lambda: rest)
    assert command.main(["--stand-in"]) == 0

    assert first.calls == 3 and len(saved.questions) == 3
    assert rest.calls == 95 - 3
    assert "of which 97 are still to record (3 already recorded)" in capsys.readouterr().out
    assert (replies / "standin.json").read_bytes() == COMMITTED.read_bytes()


def test_a_failed_question_stops_the_run_and_is_not_saved(replies, monkeypatch, capsys):
    monkeypatch.setattr(
        command, "_stand_in", lambda: StopsAfter(2, TransientGenerationError("rate limited"))
    )

    assert command.main(["--stand-in"]) == 1
    err = capsys.readouterr().err
    assert "Stopped at s003, which was not saved. Claude is busy" in err
    assert "run the same command to carry on" in err
    assert set(Recording.read(replies / "standin.json").questions) == {"s001", "s002"}


def test_a_saved_part_made_before_a_change_stops_the_resume(replies, monkeypatch, capsys):
    monkeypatch.setattr(command, "_stand_in", lambda: StopsAfter(2, KeyboardInterrupt()))
    command.main(["--stand-in"])
    path = replies / "standin.json"
    stale = path.read_text(encoding="utf-8").replace('"sent": "', '"sent": "0', 1)
    path.write_text(stale, encoding="utf-8")
    monkeypatch.setattr(command, "_stand_in", StandInGeneration)

    assert command.main(["--stand-in"]) == 1
    err = capsys.readouterr().err
    assert "no longer matches what the app sends (s001: no recording" in err
    assert "Start again with --again" in err


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
