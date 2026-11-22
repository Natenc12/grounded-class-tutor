"""Replay the committed recordings of the practice exam with no model at all (#198).

A recording is found again only when the model, its settings and every message sent are
identical, so a change to the instructions, to how passages are cut, to which passages search
hands over, or to the model makes the replay miss, and the miss names the question.

To re-record after an intended change: `uv run scripts/record_replies.py --stand-in --again`
(free), and for Claude the owner runs `uv run scripts/record_replies.py --again` on his Mac.
"""

from __future__ import annotations

import dataclasses
import functools
from pathlib import Path

import pytest

import gct.grounder.answer
import gct.ingest.chunk
import gct.library.store
from gct.eval.questions import load_questions
from gct.eval.runner import ReplayMissed, ask_all, ask_each, load_course, run_suite
from gct.ingest.chunk import chunk_units
from gct.providers.claude import DEFAULT_MODEL, ClaudeGeneration
from gct.providers.replay import Recorder, Recording, Replayer, setup_of
from gct.providers.standin import StandInGeneration
from gct.search import DEFAULT_K

SYNTHETIC = Path(__file__).resolve().parents[3] / "eval" / "synthetic"
REPLIES = SYNTHETIC / "replies"
COURSE = load_course(SYNTHETIC / "course")
QUESTIONS = load_questions(SYNTHETIC / "questions.jsonl")
SETUPS = {
    "standin": setup_of(StandInGeneration()),
    "claude": setup_of(ClaudeGeneration(model=DEFAULT_MODEL)),
}
RECORDINGS = sorted(REPLIES.glob("*.json"))
RE_RECORD = (
    "If this change was intended, re-record: `uv run scripts/record_replies.py --stand-in "
    "--again` for the stand-in, and the owner re-records Claude with --again on his Mac."
)


def comparable(answers):
    """Everything an answer shows, by label, file and page; passage ids differ in every library."""
    return {
        question_id: (
            asked.result.state,
            asked.result.answer_prose,
            [(c.label, c.file, c.page_or_slide) for c in asked.result.citations],
            asked.result.coverage,
            asked.result.integrity,
            asked.result.error,
            [(r.file, r.page_or_slide, r.text, r.score) for r in asked.retrieved],
            asked.retrieval_ran,
        )
        for question_id, asked in answers.items()
    }


def test_every_recording_file_is_one_the_merge_check_knows_how_to_replay():
    assert REPLIES / "standin.json" in RECORDINGS
    assert {path.stem for path in RECORDINGS} <= set(SETUPS)


@pytest.mark.parametrize("path", RECORDINGS, ids=[path.stem for path in RECORDINGS])
def test_the_committed_recording_replays_every_question_with_no_model(path):
    recording = Recording.read(path)

    assert set(recording.questions) == {q.id for q in QUESTIONS}
    try:
        suite = run_suite(COURSE, QUESTIONS, generator=Replayer(recording, SETUPS[path.stem]))
    except ReplayMissed as missed:
        pytest.fail(f"{missed}\n{RE_RECORD}")
    assert suite.total == len(QUESTIONS)


def test_the_stand_in_recording_replays_exactly_what_the_stand_in_answers_today():
    recording = Recording.read(REPLIES / "standin.json")

    replayed = ask_all(COURSE, QUESTIONS, generator=Replayer(recording, SETUPS["standin"]))
    live = ask_all(COURSE, QUESTIONS, generator=StandInGeneration())

    assert comparable(replayed) == comparable(live)


def test_recording_then_replaying_gives_identical_answers_and_scores_with_no_model():
    recorder = Recorder(StandInGeneration())
    recorded = ask_all(COURSE, QUESTIONS, generator=recorder)
    replayer = Replayer(recorder.recording, SETUPS["standin"])

    replayed = ask_all(COURSE, QUESTIONS, generator=replayer)

    assert not recorder.failures
    assert comparable(replayed) == comparable(recorded)
    assert run_suite(COURSE, QUESTIONS, generator=replayer) == run_suite(
        COURSE, QUESTIONS, generator=StandInGeneration()
    )


def misses_after(change, *, model_id=None, k=DEFAULT_K, monkeypatch):
    change(monkeypatch)
    setup = setup_of(StandInGeneration())
    if model_id is not None:
        setup = dataclasses.replace(setup, model_id=model_id)
    recording = Recording.read(REPLIES / "standin.json")
    with pytest.raises(ReplayMissed) as missed:
        run_suite(COURSE, QUESTIONS, generator=Replayer(recording, setup), k=k)
    return missed.value


ASKED = {q for q, calls in Recording.read(REPLIES / "standin.json").questions.items() if calls}


def test_an_edited_system_prompt_misses_on_every_question_that_asks_the_model(monkeypatch):
    def edit(patch):
        patch.setattr(gct.grounder.answer, "_SYSTEM_PROMPT", "You may use outside knowledge.")

    missed = misses_after(edit, monkeypatch=monkeypatch)

    assert {line.split(":")[0] for line in missed.misses} == ASKED
    assert all("no recording" in line for line in missed.misses)
    assert "s001: no recording matches what would be sent now" in str(missed)


def test_passages_cut_smaller_than_a_page_miss_and_name_the_passages(monkeypatch):
    def resize(patch):
        smaller = functools.partial(chunk_units, size=40, overlap=10)
        patch.setattr(gct.library.store, "chunk_units", smaller)

    missed = misses_after(resize, monkeypatch=monkeypatch)

    assert missed.misses
    assert all("no recording" in line for line in missed.misses)
    assert any("the passages changed" in line for line in missed.misses)


@pytest.mark.parametrize(
    ("setting", "value", "now"),
    [
        ("CHUNK_SIZE_WORDS", 240, "240 words, overlapping by 40"),
        ("CHUNK_OVERLAP_WORDS", 30, "250 words, overlapping by 30"),
    ],
)
def test_any_change_to_the_passage_cutting_misses_even_where_no_page_is_cut(
    setting, value, now, monkeypatch
):
    missed = misses_after(
        lambda patch: patch.setattr(gct.ingest.chunk, setting, value), monkeypatch=monkeypatch
    )

    assert {line.split(":")[0] for line in missed.misses} == ASKED
    assert all(
        f"the passage cutting changed (recorded 250 words, overlapping by 40, now {now}); it "
        "decides what real course files send" in line
        for line in missed.misses
    )


def test_a_changed_search_result_count_misses_and_names_the_questions(monkeypatch):
    missed = misses_after(lambda patch: None, k=DEFAULT_K - 1, monkeypatch=monkeypatch)

    assert missed.misses
    assert all("no recording" in line and "the passages changed" in line for line in missed.misses)


def test_a_changed_model_name_misses_on_every_question_that_asks_the_model(monkeypatch):
    missed = misses_after(
        lambda patch: None, model_id="standin-quoted-sentence-v2", monkeypatch=monkeypatch
    )

    assert {line.split(":")[0] for line in missed.misses} == ASKED
    assert all("the model changed" in line for line in missed.misses)


def test_a_replay_miss_is_never_scored_as_an_ordinary_error(monkeypatch):
    monkeypatch.setattr(gct.grounder.answer, "_SYSTEM_PROMPT", "edited")
    recording = Recording.read(REPLIES / "standin.json")

    replayer = Replayer(recording, SETUPS["standin"])
    first = next(iter(ask_each(COURSE, QUESTIONS, generator=replayer)))

    assert first.question.id == "s001"
    assert first.replay_misses == (
        "s001: no recording matches what would be sent now: the instructions, the question, a "
        "passage's text, or how the provider puts the request together changed",
    )
    with pytest.raises(ReplayMissed, match="s001: no recording"):
        _ = first.answer
