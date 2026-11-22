"""The recording and replaying wrappers around `Generation` (#198), one behaviour at a time."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import gct.providers.claude
from gct.providers.base import TransientGenerationError
from gct.providers.claude import ClaudeGeneration
from gct.providers.replay import (
    Recorder,
    Recording,
    RecordingFileError,
    Replayer,
    ReplayMiss,
    setup_of,
)

ASKED = [
    {"role": "system", "content": "Answer from the sources."},
    {"role": "user", "content": "SOURCES\n[S1] (Notes.pdf, p.2)\nWater is wet.\n\nQUESTION\nWet?"},
]


class Scripted:
    model_id = "scripted-v1"

    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)
        self.calls = 0

    def generate(self, messages):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def record(question_id: str, model, times: int = 1) -> Recorder:
    recorder = Recorder(model)
    with recorder.question(question_id) as recording:
        for _ in range(times):
            recording.generate(ASKED)
    return recorder


def test_replies_play_back_in_order_for_identical_messages():
    recorder = record("q1", Scripted("first\nCOVERAGE: complete", "second"), times=2)

    with Replayer(recorder.recording, recorder.setup).question("q1") as replaying:
        assert replaying.generate(ASKED) == "first\nCOVERAGE: complete"
        assert replaying.generate(ASKED) == "second"


def test_a_third_call_with_two_recorded_is_a_miss():
    recorder = record("q1", Scripted("a", "b"), times=2)

    with Replayer(recorder.recording, recorder.setup).question("q1") as replaying:
        replaying.generate(ASKED)
        replaying.generate(ASKED)
        with pytest.raises(ReplayMiss, match="no recording for call 3: only 2 recorded"):
            replaying.generate(ASKED)


def test_one_changed_character_in_the_messages_is_a_miss():
    recorder = record("q1", Scripted("a"))
    edited = [ASKED[0], {"role": "user", "content": ASKED[1]["content"] + " "}]

    with Replayer(recorder.recording, recorder.setup).question("q1") as replaying:
        with pytest.raises(ReplayMiss, match="the instructions, the question, a passage"):
            replaying.generate(edited)


def test_a_reply_recorded_for_one_question_never_answers_another():
    recorder = record("q1", Scripted("a"))

    with pytest.raises(ReplayMiss, match="no recording for this question"):
        with Replayer(recorder.recording, recorder.setup).question("q2") as replaying:
            replaying.generate(ASKED)


def test_an_unrecorded_question_misses_even_when_it_asks_nothing():
    recorder = record("q1", Scripted("a"))

    with pytest.raises(ReplayMiss, match="no recording for this question"):
        with Replayer(recorder.recording, recorder.setup).question("q2"):
            pass


def test_a_recorded_reply_left_unasked_is_a_miss():
    recorder = record("q1", Scripted("a"))

    with pytest.raises(ReplayMiss, match="1 recorded reply went unasked"):
        with Replayer(recorder.recording, recorder.setup).question("q1"):
            pass


def test_a_question_whose_model_call_failed_is_not_saved():
    recorder = Recorder(Scripted(RuntimeError("signed out")))

    with recorder.question("q1") as recording, pytest.raises(RuntimeError):
        recording.generate(ASKED)

    assert "q1" not in recorder.recording.questions
    assert str(recorder.failures["q1"]) == "signed out"


def test_a_resumed_recording_replays_what_it_has_and_asks_only_the_rest():
    first = record("q1", Scripted("a"))
    model = Scripted("b")
    resumed = Recorder(model, resume=first.recording)

    for question_id in ("q1", "q2"):
        with resumed.question(question_id) as recording:
            recording.generate(ASKED)

    assert model.calls == 1
    assert {q: [c.reply for c in calls] for q, calls in resumed.recording.questions.items()} == {
        "q1": ["a"],
        "q2": ["b"],
    }


def test_the_file_is_sorted_readable_and_reads_back_identically(tmp_path):
    recorder = Recorder(Scripted("one\ntwo [S1]\nCOVERAGE: complete", "x", ""))
    for question_id in ("q2", "q1", "q3"):
        with recorder.question(question_id) as recording:
            recording.generate(ASKED)
    path = tmp_path / "replies.json"

    recorder.recording.write(path)
    text = path.read_text(encoding="utf-8")
    Recording.read(path).write(tmp_path / "again.json")

    assert Recording.read(path) == recorder.recording
    assert (tmp_path / "again.json").read_text(encoding="utf-8") == text
    assert text.index('"q1"') < text.index('"q2"') < text.index('"q3"')
    assert '"Notes.pdf, p.2"' in text
    assert '"two [S1]",\n' in text
    assert not list(tmp_path.glob("*.writing"))


def test_a_damaged_file_is_refused_with_its_name(tmp_path):
    path = tmp_path / "replies.json"
    path.write_text('{"format": 1, "model": "m"', encoding="utf-8")

    with pytest.raises(RecordingFileError, match="replies.json is not a readable recording"):
        Recording.read(path)


def test_the_claude_setup_covers_the_model_name_and_the_providers_options(monkeypatch):
    sonnet = setup_of(ClaudeGeneration(model="sonnet"))

    assert setup_of(ClaudeGeneration(model="sonnet")) == sonnet
    assert setup_of(ClaudeGeneration(model="opus")).model_id != sonnet.model_id
    monkeypatch.setattr(gct.providers.claude, "CLI_SETTINGS", '{"skillOverrides": {}}')
    assert setup_of(ClaudeGeneration(model="sonnet")).settings != sonnet.settings


class OfflineClaude(ClaudeGeneration):
    def generate(self, messages):
        return "a"


def test_a_changed_claude_option_misses_with_its_reason(monkeypatch):
    recorder = record("q1", OfflineClaude(model="sonnet"))
    monkeypatch.setattr(gct.providers.claude, "CLI_ENV", {})
    now = setup_of(ClaudeGeneration(model="sonnet"))

    with Replayer(recorder.recording, now).question("q1") as replaying:
        with pytest.raises(ReplayMiss, match="the model's settings changed"):
            replaying.generate(ASKED)


def test_a_reply_that_is_not_a_list_of_lines_is_refused(tmp_path):
    recorder = record("q1", Scripted("a"))
    path = tmp_path / "replies.json"
    recorder.recording.write(path)
    path.write_text(path.read_text(encoding="utf-8").replace('[\n          "a"\n        ]', '"a"'))

    with pytest.raises(RecordingFileError, match="expected a list of lines"):
        Recording.read(path)


def test_a_busy_first_try_recovered_by_the_retry_is_saved_and_replays_the_same_way():
    recorder = Recorder(Scripted(TransientGenerationError("rate limited"), "a"))
    with recorder.question("q1") as recording:
        with pytest.raises(TransientGenerationError):
            recording.generate(ASKED)
        assert recording.generate(ASKED) == "a"

    assert not recorder.failures
    with Replayer(recorder.recording, recorder.setup).question("q1") as replaying:
        with pytest.raises(TransientGenerationError, match="busy on this try when recorded"):
            replaying.generate(ASKED)
        assert replaying.generate(ASKED) == "a"


def test_the_text_of_a_busy_try_never_reaches_the_file(tmp_path):
    recorder = Recorder(Scripted(TransientGenerationError("account 1234 over limit"), "a"))
    with recorder.question("q1") as recording:
        with pytest.raises(TransientGenerationError):
            recording.generate(ASKED)
        recording.generate(ASKED)
    path = tmp_path / "replies.json"

    recorder.recording.write(path)

    assert "1234" not in path.read_text(encoding="utf-8")
    assert '"busy": true' in path.read_text(encoding="utf-8")
    assert Recording.read(path) == recorder.recording


def test_busy_on_both_tries_is_not_saved():
    recorder = Recorder(
        Scripted(TransientGenerationError("busy"), TransientGenerationError("still"))
    )
    with recorder.question("q1") as recording:
        for _ in range(2):
            with pytest.raises(TransientGenerationError):
                recording.generate(ASKED)

    assert "q1" not in recorder.recording.questions
    assert str(recorder.failures["q1"]) == "still"


class SentToClaude(Exception):
    pass


def test_the_real_claude_call_and_the_key_use_the_same_request_function(monkeypatch):
    real = gct.providers.claude.request
    seen: list[tuple[str, list]] = []

    def spy(sdk, model, messages, folder):
        offline = isinstance(sdk, SimpleNamespace)
        seen.append(("key" if offline else "call", list(messages)))
        if not offline:
            raise SentToClaude
        return real(sdk, model, messages, folder)

    monkeypatch.setattr(gct.providers.claude, "request", spy)
    record("q1", OfflineClaude(model="sonnet"))
    with pytest.raises(SentToClaude):
        ClaudeGeneration(model="sonnet").generate(ASKED)

    assert ("key", ASKED) in seen and ("call", ASKED) in seen


def test_a_change_to_how_claude_joins_the_messages_misses(monkeypatch):
    recorder = record("q1", OfflineClaude(model="sonnet"))
    real = gct.providers.claude.request

    def joined_differently(sdk, model, messages, folder):
        prompt, options = real(sdk, model, messages, folder)
        return prompt.replace("\n\n", "\n"), options

    monkeypatch.setattr(gct.providers.claude, "request", joined_differently)

    with Replayer(recorder.recording, recorder.setup).question("q1") as replaying:
        with pytest.raises(ReplayMiss, match="how the provider puts the request together"):
            replaying.generate(ASKED)
