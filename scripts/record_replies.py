"""Record a model's replies to the practice exam, for the merge check to replay free (#198).

    uv run scripts/record_replies.py             # Claude on this Mac's login; uses the plan
    uv run scripts/record_replies.py --stand-in  # the free stand-in model
    add --again to replace a finished recording

It saves after every question, so Ctrl-C loses nothing and running it again carries on.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNTHETIC = ROOT / "eval" / "synthetic"
REPLIES = SYNTHETIC / "replies"
CI_VARIABLES = ("CI", "GITHUB_ACTIONS")
LOGIN_CHECK_SECONDS = 60
SECONDS_PER_QUESTION = 6


class Stop(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--stand-in", action="store_true", help="record the free stand-in model")
    parser.add_argument("--again", action="store_true", help="replace a finished recording")
    args = parser.parse_args(argv)
    try:
        return record(stand_in=args.stand_in, again=args.again)
    except Stop as stop:
        print(f"\n{stop}", file=sys.stderr)
        return 1


def record(*, stand_in: bool, again: bool) -> int:
    from gct.eval.questions import load_questions
    from gct.eval.runner import ask_all, ask_each, load_course
    from gct.providers.replay import Recorder, Recording, Replayer

    running_in = [name for name in CI_VARIABLES if os.environ.get(name)]
    if running_in:
        raise Stop(
            f"This records real replies and must not run in the merge check ({running_in[0]} is "
            "set). Run it on your own Mac."
        )

    course = load_course(SYNTHETIC / "course")
    questions = load_questions(SYNTHETIC / "questions.jsonl")
    path = REPLIES / ("standin.json" if stand_in else "claude.json")
    shown = path.relative_to(ROOT)

    resume = None
    if path.exists() and not again:
        resume = Recording.read(path)
        if {q.id for q in questions} <= set(resume.questions):
            raise Stop(
                f"{shown} already holds a finished recording, so nothing was asked. To replace "
                "it, run the same command with --again."
            )

    model = _stand_in() if stand_in else _claude()
    recorder = Recorder(model, resume=resume)
    to_ask = [q.id for q in questions if not recorder.already(q.id)]

    print(f"Recording {model.model_id}'s replies to the practice exam.")
    print(f"  Questions: {len(questions)}, of which {len(to_ask)} will be asked now", end="")
    print(f" ({len(questions) - len(to_ask)} already recorded)." if resume else ".")
    if stand_in:
        print("  Model:     the free stand-in, which matches words and costs nothing.")
    else:
        print("  Model:     Claude, on this Mac's Claude login. This uses your Claude plan:")
        minutes = max(1, round(len(to_ask) * SECONDS_PER_QUESTION / 60))
        print(f"             about {minutes} minutes for these questions.")
    print(f"  Saves to:  {shown}, after every question.")
    print("  Ctrl-C stops at any time; run the same command again to carry on from there.")
    if not stand_in:
        try:
            input("\nPress Enter to start, or Ctrl-C to cancel. ")
        except (KeyboardInterrupt, EOFError) as err:
            raise Stop("Cancelled. Nothing was asked.") from err

    REPLIES.mkdir(parents=True, exist_ok=True)
    asked = 0
    try:
        for result in ask_each(course, questions, generator=recorder):
            question_id = result.question.id
            if result.replay_misses:
                raise Stop(
                    f"The part of {shown} saved earlier no longer matches what the app sends "
                    f"({result.replay_misses[0]}). Start again with --again."
                )
            if question_id in recorder.failures:
                raise Stop(_failed(question_id, recorder.failures[question_id], shown))
            if question_id in to_ask:
                asked += 1
                recorder.recording.write(path)
                state = result.answer.result.state
                calls = len(recorder.recording.questions[question_id])
                note = {0: "no passages found, nothing to ask", 1: "1 reply"}.get(
                    calls, f"{calls} replies, after a retry"
                )
                print(f"[{asked:>3}/{len(to_ask)}] {question_id}  {state}  ({note})", flush=True)
    except KeyboardInterrupt:
        saved = len(recorder.recording.questions)
        print(
            f"\nStopped. {saved} of {len(questions)} questions are recorded and saved in "
            f"{shown}. Run the same command to carry on."
        )
        return 130

    recording = recorder.recording
    ask_all(course, questions, generator=Replayer(recording, recording.setup))
    replies = sum(len(calls) for calls in recording.questions.values())
    print(f"\nDone. {shown} holds {len(recording.questions)} questions and {replies} replies.")
    print("Checked: it replays with no model and no misses.")
    print("To commit it:")
    print(f"  git add {shown}")
    print(f'  git commit -m "Record {model.model_id} replies to the practice exam (#198)"')
    return 0


def _stand_in():
    from gct.providers.standin import StandInGeneration

    return StandInGeneration()


def _claude():
    from gct.providers.claude import (
        DEFAULT_MODEL,
        INSTALL_HINT,
        MODEL_ENV,
        ClaudeGeneration,
        sdk_installed,
    )

    if not sdk_installed():
        raise Stop(INSTALL_HINT)
    _check_signed_in()
    chosen = os.environ.get(MODEL_ENV, "").strip()
    if chosen and chosen != DEFAULT_MODEL:
        print(
            f"Note: {MODEL_ENV} is set to {chosen!r}, but the merge check replays the default "
            f"model, so this records {DEFAULT_MODEL!r}."
        )
    return ClaudeGeneration(model=DEFAULT_MODEL)


def _claude_program() -> str | None:
    import claude_agent_sdk

    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    return str(bundled) if bundled.is_file() else shutil.which("claude")


def _check_signed_in() -> None:
    from gct.providers.claude import CLI_ENV, LOGIN_OVERRIDES, MISSING_PROGRAM, NOT_SIGNED_IN

    program = _claude_program()
    if program is None:
        raise Stop(MISSING_PROGRAM)
    environment = {k: v for k, v in os.environ.items() if k not in LOGIN_OVERRIDES} | CLI_ENV
    print("Checking the Claude login on this Mac...", flush=True)
    try:
        finished = subprocess.run(
            [program, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            env=environment,
            timeout=LOGIN_CHECK_SECONDS,
            check=False,
        )
        signed_in = json.loads(finished.stdout).get("loggedIn") is True
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError) as err:
        raise Stop(f"Could not check the Claude login ({err}). Nothing was asked.") from err
    if not signed_in:
        raise Stop(f"{NOT_SIGNED_IN} Nothing was asked.")


def _failed(question_id: str, error: Exception, shown: Path) -> str:
    from gct.providers.base import TransientGenerationError
    from gct.providers.claude import ClaudeError

    if isinstance(error, TransientGenerationError):
        reason = f"Claude is busy, or the plan's limit was reached ({error}). Wait a while first."
    elif isinstance(error, ClaudeError):
        reason = str(error)
    else:
        reason = f"The model failed ({type(error).__name__}: {error})."
    return (
        f"Stopped at {question_id}, which was not saved. {reason}\nThe questions already in "
        f"{shown} are kept; run the same command to carry on."
    )


if __name__ == "__main__":
    sys.exit(main())
