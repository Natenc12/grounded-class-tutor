"""The free drive command, `tools/drive.py` (#188).

The drive is a tool, not part of the `gct` package, so it is loaded from its file rather than
imported.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import gct.grounder.answer as grounder
import gct.library.cli as cli
import gct.library.importer as importer
import gct.search.keyword as keyword
from gct.grounder.answer import GrounderState
from gct.library import Library
from gct.providers import active
from gct.providers.claude import ClaudeGeneration
from gct.providers.standin import StandInGeneration

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "drive.py"
_spec = importlib.util.spec_from_file_location("drive_under_test", SCRIPT)
assert _spec is not None and _spec.loader is not None
drive_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drive_script)

STEPS = [
    "make a throwaway library",
    "build a synthetic PDF and PowerPoint",
    "make two classes",
    "import the PDF",
    "import the PowerPoint",
    "list the library",
    "ask a covered question",
    "ask a question only the PDF's last page answers",
    "ask a question this class does not cover",
    "ask a question the slides cover",
    "refuse to vouch for an answer with no citations",
    "show a model that declines as a refusal",
    "print what a half-covered answer leaves out",
    "check the library",
    "find the damage in a damaged copy of the library",
    "delete the throwaway library",
]


@pytest.fixture(autouse=True)
def scratch(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def drive() -> tuple[int, list[str]]:
    out = io.StringIO()
    code = drive_script.drive(out)
    return code, out.getvalue().splitlines()


def test_the_drive_runs_every_step_and_passes(scratch: Path):
    code, lines = drive()

    assert code == 0
    assert lines == [*(f"ok    {step}" for step in STEPS), "DRIVE PASSED"]
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("choice", [active.CLAUDE, None])
def test_the_environment_cannot_point_the_drive_at_a_real_model(monkeypatch, choice):
    def real_model(*_: object, **__: object):
        raise AssertionError("the drive reached a real model")

    if choice is None:
        monkeypatch.delenv(active.PROVIDERS_ENV, raising=False)
    else:
        monkeypatch.setenv(active.PROVIDERS_ENV, choice)
    monkeypatch.setenv("GCT_LIBRARY", "/nowhere/tutor.sqlite3")
    monkeypatch.setattr(ClaudeGeneration, "__init__", real_model)
    monkeypatch.setattr("gct.library.cli.selected_generator", real_model)
    monkeypatch.setattr(ClaudeGeneration, "generate", real_model)
    asked = []
    monkeypatch.setattr(
        StandInGeneration,
        "generate",
        lambda self, messages, real=StandInGeneration.generate: (
            asked.append(1) or real(self, messages)
        ),
    )

    code, lines = drive()

    assert (code, lines[-1]) == (0, "DRIVE PASSED")
    assert len(asked) == 3


def test_search_leaking_another_classs_passages_fails_at_the_ask_step(monkeypatch):
    leaking = keyword._MATCHES.replace(
        "AND documents.class_id = :class_id", "AND :class_id IS NOT NULL"
    )
    assert leaking != keyword._MATCHES
    monkeypatch.setattr(keyword, "_MATCHES", leaking)

    code, lines = drive()

    assert code == 1
    assert lines[-1] == "DRIVE FAILED at: ask a question this class does not cover"
    assert "history-slides.pptx, slide 1" in "\n".join(lines)


def test_a_citation_to_the_wrong_page_fails_at_the_ask_step(monkeypatch):
    real = StandInGeneration.generate
    monkeypatch.setattr(
        StandInGeneration,
        "generate",
        lambda self, messages: real(self, messages).replace("[S1]", "[S2]"),
    )

    code, lines = drive()

    assert code == 1
    assert lines[-1] == "DRIVE FAILED at: ask a covered question"


def test_an_unexpected_error_names_its_step_and_shows_the_trace(monkeypatch, scratch: Path):
    def broken(*_: object, **__: object):
        raise RuntimeError("the importer broke")

    monkeypatch.setattr("gct.library.cli.import_file", broken)

    code, lines = drive()

    assert code == 1
    assert "RuntimeError: the importer broke" in lines
    assert lines[-1] == "DRIVE FAILED at: import the PDF"
    assert list(scratch.iterdir()) == []


def test_the_drive_runs_from_any_folder_as_a_plain_script(tmp_path: Path):
    finished = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GCT_PROVIDERS": "claude"},
        timeout=120,
    )

    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert finished.stdout.splitlines()[-1] == "DRIVE PASSED"
    assert finished.stderr == ""


def _fails_at(step: str) -> list[str]:
    code, lines = drive()
    assert code == 1, lines
    assert lines[-1] == f"DRIVE FAILED at: {step}"
    return lines


def test_import_dropping_a_files_last_page_fails_at_the_last_page_question(monkeypatch):
    real = importer.parse_file
    monkeypatch.setattr(importer, "parse_file", lambda path: real(path)[:-1])

    _fails_at("ask a question only the PDF's last page answers")


def test_accepting_an_answer_with_no_citations_fails_at_that_step(monkeypatch):
    real = grounder._validate

    def lenient(parsed, n_sources):
        return [reason for reason in real(parsed, n_sources) if "citation" not in reason]

    monkeypatch.setattr(grounder, "_validate", lenient)

    _fails_at("refuse to vouch for an answer with no citations")


def test_showing_a_models_refusal_as_an_answer_fails_at_that_step(monkeypatch):
    real = grounder._decide

    def answered(parsed, citations):
        decided = real(parsed, citations)
        if decided.state is GrounderState.REFUSAL:
            return dataclasses.replace(decided, state=GrounderState.GROUNDED)
        return decided

    monkeypatch.setattr(grounder, "_decide", answered)

    _fails_at("show a model that declines as a refusal")


def test_no_longer_printing_gaps_fails_at_the_half_covered_step(monkeypatch):
    monkeypatch.setattr(cli, "_shortened", lambda text: "")

    _fails_at("print what a half-covered answer leaves out")


def test_a_check_that_ignores_damage_fails_at_the_damaged_copy_step(monkeypatch):
    monkeypatch.setattr(Library, "check", lambda self: [])

    _fails_at("find the damage in a damaged copy of the library")


def test_the_command_exiting_early_is_reported_as_a_failed_step(monkeypatch):
    def exits(argv):
        raise SystemExit(2)

    monkeypatch.setattr(cli, "_parse", exits)

    lines = _fails_at("make two classes")
    assert "`add-class Hydrology` exited early with 2:" in lines
