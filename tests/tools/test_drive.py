"""The free drive command, `tools/drive.py` (#188).

The drive is a tool, not part of the `gct` package, so it is loaded from its file rather than
imported.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import gct.search.keyword as keyword
from gct.providers import active
from gct.providers.claude import ClaudeGeneration
from gct.providers.openai_provider import OpenAIGeneration
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
    "ask a question this class does not cover",
    "ask a question the slides cover",
    "check the library",
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


def test_the_environment_cannot_point_the_drive_at_a_real_model(monkeypatch):
    def real_model(*_: object, **__: object):
        raise AssertionError("the drive reached a real model")

    monkeypatch.setenv(active.PROVIDERS_ENV, active.CLAUDE)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")
    monkeypatch.setattr(ClaudeGeneration, "generate", real_model)
    monkeypatch.setattr(OpenAIGeneration, "generate", real_model)
    monkeypatch.setattr(active, "build_generator", real_model)
    monkeypatch.setattr(active, "require_usable", real_model)
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
    assert len(asked) == 2


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
