"""Record a model's replies to the practice exam, for the merge check to replay free (#198).

    uv run scripts/record_replies.py             # Claude on this Mac's login; uses the plan
    uv run scripts/record_replies.py --stand-in  # the free stand-in model
    add --again to start a fresh recording when a finished one exists

It saves to a draft after every question, so Ctrl-C loses nothing and running the same command
again carries on. The finished file is replaced only when the new recording is complete.
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
LOGIN_UNKNOWN = (
    "Could not tell whether Claude is signed in on this Mac. Open Terminal, run `claude`, check "
    "that you are signed in (type /login if not), quit it, then try again. Nothing was asked."
)


class Stop(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--stand-in", action="store_true", help="record the free stand-in model")
    parser.add_argument("--again", action="store_true", help="start afresh over a finished one")
    args = parser.parse_args(argv)
    try:
        return record(stand_in=args.stand_in, again=args.again)
    except Stop as stop:
        print(f"\n{stop}", file=sys.stderr)
        return 1


def record(*, stand_in: bool, again: bool) -> int:
    from gct.eval.questions import load_questions
    from gct.eval.runner import ask_all, ask_each, load_course
    from gct.providers.replay import Recorder, Replayer, setup_of

    running_in = [name for name in CI_VARIABLES if os.environ.get(name)]
    if running_in:
        raise Stop(
            f"This records real replies and must not run in the merge check ({running_in[0]} is "
            "set). Run it on your own Mac."
        )

    course = load_course(SYNTHETIC / "course")
    questions = load_questions(SYNTHETIC / "questions.jsonl")
    final = REPLIES / ("standin.json" if stand_in else "claude.json")
    draft = final.with_name(final.name + ".draft")
    shown, shown_draft = final.relative_to(ROOT), draft.relative_to(ROOT)

    if final.exists() and not draft.exists() and not again:
        raise Stop(
            f"{shown} already holds a finished recording, so nothing was asked. To record a "
            "fresh one, run the same command with --again; the finished one stays until the "
            "fresh one is complete."
        )

    model = _stand_in() if stand_in else _claude()
    resume = _usable_draft(draft, course, questions, setup_of(model))
    _check_writable(draft)
    recorder = Recorder(model, resume=resume)
    to_ask = [q.id for q in questions if not recorder.already(q.id)]

    print(f"Recording {model.model_id}'s replies to the practice exam.")
    print(f"  Questions: {len(questions)}, of which {len(to_ask)} are still to record", end="")
    print(f" ({len(questions) - len(to_ask)} kept from an unfinished run)." if resume else ".")
    if stand_in:
        print("  Model:     the free stand-in, which matches words and costs nothing.")
    else:
        print("  Model:     Claude, on this Mac's Claude login. This uses your Claude plan:")
        minutes = max(1, round(len(to_ask) * SECONDS_PER_QUESTION / 60))
        print(f"             about {minutes} minutes for these questions.")
    print(f"  Saves to:  {shown_draft}, after every question.")
    print(f"             {shown} is written only once all {len(questions)} are recorded.")
    print("  Ctrl-C stops at any time; run the same command again to carry on from there.")
    if to_ask and not stand_in:
        try:
            input("\nPress Enter to start, or Ctrl-C to cancel. ")
        except (KeyboardInterrupt, EOFError) as err:
            raise Stop("Cancelled. Nothing was asked.") from err

    saved = len(recorder.recording.questions)
    asked = 0
    try:
        for result in ask_each(course, questions, generator=recorder):
            question_id = result.question.id
            if result.replay_misses:
                raise Stop(
                    f"The unfinished recording no longer matches what the app sends "
                    f"({result.replay_misses[0]}). Run the same command to start it afresh."
                )
            if question_id in recorder.failures:
                raise Stop(_failed(question_id, recorder.failures[question_id], shown_draft))
            if question_id in to_ask:
                asked += 1
                _save(recorder, draft, shown_draft, saved)
                saved += 1
                state = result.answer.result.state
                calls = recorder.recording.questions[question_id]
                note = {0: "no passages found, nothing to ask", 1: "1 reply"}.get(
                    len(calls), f"{len(calls)} calls, after a retry"
                )
                print(f"[{asked:>3}/{len(to_ask)}] {question_id}  {state}  ({note})", flush=True)
    except KeyboardInterrupt:
        print(
            f"\nStopped. {saved} of {len(questions)} questions are recorded and saved in "
            f"{shown_draft}. Run the same command to carry on."
        )
        return 130

    recording = recorder.recording
    ask_all(course, questions, generator=Replayer(recording, recording.setup))
    _save(recorder, draft, shown_draft, saved)
    try:
        os.replace(draft, final)
    except OSError as err:
        raise Stop(
            f"Could not move {shown_draft} to {shown} ({err.strerror or err}). Everything is "
            "recorded and kept; fix the folder, then run the same command to finish."
        ) from err
    replies = sum(1 for calls in recording.questions.values() for c in calls if c.reply is not None)
    print(f"\nDone. {shown} holds {len(recording.questions)} questions and {replies} replies.")
    print("Checked: it replays with no model and no misses.")
    print("To commit it:")
    print(f"  git add {shown}")
    print(f'  git commit -m "Record {model.model_id} replies to the practice exam (#198)"')
    return 0


def _usable_draft(draft: Path, course, questions, setup):
    from gct.eval.runner import ReplayMissed, ask_all
    from gct.providers.replay import Recording, RecordingFileError, Replayer

    if not draft.exists():
        return None
    try:
        unfinished = Recording.read(draft)
        known = {q.id for q in questions}
        if not set(unfinished.questions) <= known:
            raise ReplayMissed(sorted(set(unfinished.questions) - known))
        kept = [q for q in questions if q.id in unfinished.questions]
        ask_all(course, kept, generator=Replayer(unfinished, setup))
    except (RecordingFileError, ReplayMissed) as err:
        reason = err.misses[0] if isinstance(err, ReplayMissed) else "it could not be read"
        draft.unlink()
        print(
            f"The unfinished recording from an earlier run no longer matches what the app sends "
            f"now ({reason}), so it was removed and a fresh one starts. Nothing in it is reused."
        )
        return None
    return unfinished


def _check_writable(draft: Path) -> None:
    check = draft.with_name(draft.name + ".check")
    try:
        draft.parent.mkdir(parents=True, exist_ok=True)
        check.write_text("check\n", encoding="utf-8")
        check.unlink()
    except OSError as err:
        raise Stop(
            f"Cannot save in {draft.parent} ({err.strerror or err}). Nothing was asked. Make "
            "room on the disk or fix the folder's permissions, then try again."
        ) from err


def _save(recorder, draft: Path, shown_draft: Path, saved: int) -> None:
    try:
        recorder.recording.write(draft)
    except OSError as err:
        raise Stop(
            f"Could not save to {shown_draft} ({err.strerror or err}). The {saved} questions "
            "saved before this one are kept. Make room on the disk or fix the folder, then run "
            "the same command to carry on."
        ) from err


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
        raise Stop(LOGIN_UNKNOWN) from err
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
