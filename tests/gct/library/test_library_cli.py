"""The owner's Terminal commands for the library (#188): `uv run tutor ...`.

Every file is generated in the test and every answer comes from the free stand-in model or a fake
that fails on purpose. Each ordinary mistake must end in a plain message and exit code 1, never a
stack trace.
"""

from __future__ import annotations

import contextlib
import functools
import io
import logging
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Inches
from reportlab.pdfgen import canvas

import gct.library.cli as cli
from gct.grounder.answer import GrounderState
from gct.library import Library
from gct.providers import active
from gct.providers.base import Generation, Message, TransientGenerationError
from gct.providers.claude import INSTALL_HINT, NOT_SIGNED_IN, ClaudeNotSignedInError
from gct.providers.standin import StandInGeneration

HYDROLOGY_PAGES = [
    "Hydrology studies how water moves over and under the land.",
    "The base flow of a river is the part of its flow fed by groundwater.",
]


def write_pdf(path: Path, pages: list[str | None]) -> Path:
    pdf = canvas.Canvas(str(path), pagesize=(612, 792))
    for text in pages:
        if text is not None:
            pdf.drawString(72, 700, text)
        pdf.showPage()
    pdf.save()
    return path


def write_pptx(path: Path, slides: list[str]) -> Path:
    deck = Presentation()
    for text in slides:
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1)).text_frame.text = text
    deck.save(str(path))
    return path


class Failing:
    model_id = "failing"

    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    def generate(self, messages: Sequence[Message]) -> str:
        raise self.failure


class Scripted:
    model_id = "scripted"

    def __init__(self, reply: str) -> None:
        self.reply = reply

    def generate(self, messages: Sequence[Message]) -> str:
        return self.reply


def never() -> Generation:
    raise AssertionError("this command must not build a model")


@pytest.fixture
def library(tmp_path: Path) -> Path:
    return tmp_path / "tutor.sqlite3"


@pytest.fixture
def run(library: Path):
    def run(
        *argv: str, generator: Callable[[], Generation] = StandInGeneration
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            [argv[0], "--library", str(library), *argv[1:]], generator=generator, out=out, err=err
        )
        header = f"Library: {library}\n"
        assert out.getvalue().startswith(header)
        return code, out.getvalue().removeprefix(header), err.getvalue()

    return run


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(cli.LIBRARY_ENV, raising=False)
    monkeypatch.delenv(active.PROVIDERS_ENV, raising=False)
    return home


@pytest.fixture
def hydrology(run, tmp_path: Path) -> Path:
    pdf = write_pdf(tmp_path / "lecture-03.pdf", HYDROLOGY_PAGES)
    assert run("add-class", "Hydrology")[0] == 0
    assert run("import", "Hydrology", str(pdf))[0] == 0
    return pdf


def test_add_class_makes_the_library_and_says_what_to_do_next(run, library: Path):
    code, out, err = run("add-class", "Hydrology", generator=never)

    assert (code, err) == (0, "")
    assert out == (
        "This is a new library; it was made just now.\n"
        "Made the class Hydrology. Add files to it with:\n"
        f"  uv run tutor import --library {library} Hydrology FILE\n"
    )
    with Library(library) as opened:
        assert [found.name for found in opened.list_classes()] == ["Hydrology"]


def test_add_class_quotes_a_class_name_with_spaces_in_the_next_command(run, library: Path):
    code, out, _ = run("add-class", "Water Science")

    assert code == 0
    assert f"import --library {library} 'Water Science' FILE" in out


def test_a_second_class_in_the_same_library_does_not_announce_a_new_library(run):
    run("add-class", "Hydrology")

    code, out, _ = run("add-class", "History")

    assert code == 0
    assert "new library" not in out


def test_a_taken_class_name_is_refused_plainly(run):
    run("add-class", "Hydrology")

    assert run("add-class", "Hydrology") == (
        1,
        "",
        "A class called 'Hydrology' already exists; choose another name.\n",
    )


def test_a_blank_class_name_is_refused_plainly(run):
    assert run("add-class", "  ") == (
        1,
        "",
        "Give the class a name with at least one visible character.\n",
    )


@pytest.mark.parametrize(
    "command", [["list"], ["check"], ["import", "X", "a.pdf"], ["ask", "X", "q"]]
)
def test_a_wrong_library_path_is_a_plain_message_and_makes_no_file(run, library: Path, command):
    code, out, err = run(*command, generator=never)

    assert (code, out) == (1, "")
    assert err == (
        f"There is no library at {library}. Check the path, or make a new library with:\n"
        f"  uv run tutor add-class --library {library} CLASS\n"
    )
    assert not library.exists()


def test_a_library_in_a_folder_that_does_not_exist_is_a_plain_message(tmp_path: Path):
    err = io.StringIO()
    missing = tmp_path / "no-such-folder" / "tutor.sqlite3"

    code = cli.main(["add-class", "--library", str(missing), "Hydrology"], generator=never, err=err)

    assert code == 1
    assert err.getvalue() == f"Could not open {missing}; check the folder exists.\n"


def test_a_file_that_is_not_a_library_is_refused_and_left_alone(tmp_path: Path):
    pdf = write_pdf(tmp_path / "lecture-03.pdf", HYDROLOGY_PAGES)
    before = pdf.read_bytes()
    err = io.StringIO()

    code = cli.main(["list", "--library", str(pdf)], generator=never, err=err)

    assert code == 1
    assert err.getvalue() == f"{pdf} is not a class library; choose another.\n"
    assert pdf.read_bytes() == before


def test_a_library_open_in_another_window_is_a_plain_message(run, library: Path, monkeypatch):
    run("add-class", "Hydrology")
    monkeypatch.setattr(cli, "Library", functools.partial(Library, timeout=0.05))
    other = sqlite3.connect(library, isolation_level=None)
    other.execute("BEGIN EXCLUSIVE")
    try:
        code, _, err = run("add-class", "History")
    finally:
        other.execute("ROLLBACK")
        other.close()

    assert code == 1
    assert "another window is using the library; close it and try again" in err


def test_the_library_path_may_start_with_a_tilde(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    code = cli.main(
        ["add-class", "--library", "~/tutor.sqlite3", "Hydrology"],
        generator=never,
        out=io.StringIO(),
    )

    assert code == 0
    assert (tmp_path / "tutor.sqlite3").exists()


def test_list_shows_each_class_with_its_files(run, hydrology: Path, tmp_path: Path):
    deck = write_pptx(tmp_path / "week-02.pptx", ["The Ostren League was founded in 1204."])
    run("add-class", "History")
    run("import", "History", str(deck))
    run("add-class", "Art")

    assert run("list", generator=never) == (
        0,
        "Art\n  (no files yet)\n"
        "History\n  week-02.pptx (1 slide)\n"
        "Hydrology\n  lecture-03.pdf (2 pages)\n",
        "",
    )


def test_list_of_an_empty_library_says_how_to_make_a_class(run, library: Path):
    Library(library).close()

    assert run("list") == (
        0,
        "This library has no classes yet. Make one with:\n"
        f"  uv run tutor add-class --library {library} CLASS\n",
        "",
    )


def test_import_prints_the_importers_own_message_and_the_next_step(
    run, library: Path, tmp_path: Path
):
    run("add-class", "Hydrology")
    pdf = write_pdf(tmp_path / "lecture-03.pdf", HYDROLOGY_PAGES)

    assert run("import", "Hydrology", str(pdf), generator=never) == (
        0,
        "Imported lecture-03.pdf (2 pages).\n"
        "Ask the class a question with:\n"
        f"  uv run tutor ask --library {library} Hydrology QUESTION\n",
        "",
    )
    assert run("import", "Hydrology", str(pdf)) == (
        0,
        "lecture-03.pdf is already in this class, so nothing was added.\n"
        "Ask the class a question with:\n"
        f"  uv run tutor ask --library {library} Hydrology QUESTION\n",
        "",
    )


def test_a_path_with_no_file_and_a_folder_get_plain_messages(run, tmp_path: Path):
    run("add-class", "Hydrology")
    missing = tmp_path / "lectur-03.pdf"
    folder = tmp_path / "Week 1"
    folder.mkdir()

    assert run("import", "Hydrology", str(missing), str(folder), generator=never) == (
        1,
        f"There is no file at {missing}. Check the file's name and folder, then try again; "
        "you can drag the file from Finder onto Terminal to type its path.\n"
        f"{folder} is a folder, not a file. Import the PDF or PowerPoint files inside it "
        "instead.\n",
        "",
    )


def test_import_of_several_files_reports_each_and_fails_if_any_is_refused(
    run, tmp_path: Path, caplog
):
    run("add-class", "Hydrology")
    good = write_pdf(tmp_path / "lecture-03.pdf", HYDROLOGY_PAGES)
    scan = write_pdf(tmp_path / "scan.pdf", [None])
    cut = cut_pdf(tmp_path / "cut.pdf")

    code, out, err = run(
        "import", "Hydrology", str(tmp_path / "lectur-03.pdf"), str(scan), str(cut), str(good)
    )

    assert (code, err) == (1, "")
    assert out.splitlines()[:-2] == [
        f"There is no file at {tmp_path / 'lectur-03.pdf'}. Check the file's name and folder, "
        "then try again; you can drag the file from Finder onto Terminal to type its path.",
        "scan.pdf has no text the tutor can search; it is probably a scan or made only of "
        "pictures. Use a version with text you can select, or run text recognition (OCR) on it "
        "first.",
        "cut.pdf could not be read. It may be damaged, only partly downloaded, or not really a "
        "PDF. Download it again, or open it and save a fresh copy, then import that.",
        "Imported lecture-03.pdf (2 pages).",
    ]
    assert [record.name for record in caplog.records if record.name.startswith("pypdf")] == []


def cut_pdf(path: Path) -> Path:
    whole = write_pdf(path.with_name("whole.pdf"), ["Recharge is slow under clay."]).read_bytes()
    path.write_bytes(whole[: len(whole) // 2])
    return path


def test_a_damaged_pdf_shows_only_the_tutors_message_in_terminal(tmp_path: Path):
    library = tmp_path / "tutor.sqlite3"
    cut = cut_pdf(tmp_path / "cut.pdf")
    tutor = [sys.executable, "-m", "gct.library"]
    subprocess.run([*tutor, "add-class", "--library", str(library), "Hydrology"], check=True)

    finished = subprocess.run(
        [*tutor, "import", "--library", str(library), "Hydrology", str(cut)],
        capture_output=True,
        text=True,
    )

    assert finished.returncode == 1
    assert finished.stderr == ""
    assert finished.stdout.startswith(f"Library: {library}\ncut.pdf could not be read.")


def test_the_pdf_reader_is_quiet_only_while_a_command_runs(run, tmp_path: Path):
    reader_log = logging.getLogger("pypdf")
    reader_log.setLevel(logging.INFO)
    run("add-class", "Hydrology")
    try:
        run("import", "Hydrology", str(cut_pdf(tmp_path / "cut.pdf")))
        assert reader_log.level == logging.INFO
    finally:
        reader_log.setLevel(logging.NOTSET)


def test_an_unknown_class_names_the_classes_there_are(run, hydrology: Path):
    run("add-class", "History")

    for command in (["import", "Hydrolgy", str(hydrology)], ["ask", "Hydrolgy", "What is it?"]):
        assert run(*command, generator=never) == (
            1,
            "",
            "There is no class called 'Hydrolgy' in this library. "
            "Its classes are: History, Hydrology.\n",
        )


def test_an_unknown_class_in_a_library_with_none_says_how_to_make_it(run, library: Path):
    Library(library).close()

    assert run("ask", "Hydrology", "What is base flow?", generator=never) == (
        1,
        "",
        "There is no class called 'Hydrology'; this library has no classes yet. Make one with:\n"
        f"  uv run tutor add-class --library {library} Hydrology\n",
    )


def test_a_class_name_matches_whatever_its_capitals(run, hydrology: Path):
    code, out, _ = run("ask", "  hydrology ", "What feeds the base flow of a river?")

    assert code == 0
    assert out.startswith("\nGROUNDED:")


def test_an_exact_name_wins_over_one_that_differs_only_in_capitals(
    run, library: Path, tmp_path: Path
):
    Library(library).close()
    with Library(library) as opened:
        opened.create_class("Bio")
        opened.create_class("bio")
    pdf = write_pdf(tmp_path / "cells.pdf", ["Cells divide by mitosis."])

    run("import", "bio", str(pdf))

    with Library(library) as opened:
        counts = {
            found.name: len(opened.list_documents(found.id)) for found in opened.list_classes()
        }
    assert counts == {"Bio": 0, "bio": 1}
    assert run("ask", "BIO", "How do cells divide?", generator=never) == (
        1,
        "",
        "More than one class matches 'BIO': Bio, bio. Type one exactly as it is written, capital "
        "letters included.\n",
    )
    assert run("add-class", "Bio", generator=never) == (
        1,
        "",
        "A class called 'Bio' already exists; choose another name.\n",
    )


def test_ask_prints_the_state_the_answer_and_each_source_with_its_page(run, hydrology: Path):
    assert run("ask", "Hydrology", "What feeds the base flow of a river?") == (
        0,
        "\nGROUNDED: answered from your files.\n"
        "\n"
        "The base flow of a river is the part of its flow fed by groundwater. [S1]\n"
        "\n"
        "Sources:\n"
        "  [S1] lecture-03.pdf, page 2\n"
        "\n"
        "Answered by the free stand-in model (standin-quoted-sentence-v1), which does not "
        "really read.\n",
        "",
    )


def test_a_source_in_a_powerpoint_is_named_by_its_slide(run, tmp_path: Path):
    deck = write_pptx(
        tmp_path / "week-02.pptx",
        ["Its fleet guarded the amberwool trade.", "The Ostren League was founded in 1204."],
    )
    run("add-class", "History")
    run("import", "History", str(deck))

    code, out, _ = run("ask", "History", "When was the Ostren League founded?")

    assert code == 0
    assert "  [S1] week-02.pptx, slide 2\n" in out


def test_ask_says_plainly_when_nothing_in_the_files_matched(run, hydrology: Path):
    assert run("ask", "Hydrology", "Who won the 1966 World Cup?") == (
        0,
        "\nREFUSAL: not answered, because your files for this class do not cover it.\n"
        "\n"
        "Nothing in your files for this class matched this question.\n",
        "",
    )


def test_ask_prints_what_a_partial_answer_leaves_out(run, hydrology: Path):
    reply = "Base flow is fed by groundwater [S1].\nCOVERAGE: gaps: how fast it moves"

    code, out, _ = run(
        "ask", "Hydrology", "What feeds base flow?", generator=lambda: Scripted(reply)
    )

    assert code == 0
    assert out.startswith("\nPARTIAL: partly answered from your files;")
    assert "\nNot covered by your files:\n  how fast it moves\n" in out
    assert "  [S1] lecture-03.pdf, page " in out


def test_ask_flags_an_answer_whose_sources_cannot_be_checked(run, hydrology: Path):
    reply = "Base flow is fed by groundwater [S9].\nCOVERAGE: complete"

    code, out, _ = run(
        "ask", "Hydrology", "What feeds base flow?", generator=lambda: Scripted(reply)
    )

    assert code == 0
    assert out.startswith("\nINTEGRITY_FLAGGED: the answer's sources could not be checked")
    assert "Base flow is fed by groundwater [S9]." in out
    assert "\nWhy it could not be checked:\n  " in out


def test_not_signed_in_to_claude_prints_claudes_own_plain_message(run, hydrology: Path):
    failing = Failing(ClaudeNotSignedInError(NOT_SIGNED_IN))

    assert run("ask", "Hydrology", "What feeds base flow?", generator=lambda: failing) == (
        1,
        f"\nERROR: no answer.\n\n{NOT_SIGNED_IN}\n",
        "",
    )


def test_a_model_that_keeps_timing_out_prints_its_own_message(run, hydrology: Path):
    failing = Failing(TransientGenerationError("Claude did not answer within 120 seconds."))

    code, out, _ = run("ask", "Hydrology", "What feeds base flow?", generator=lambda: failing)

    assert code == 1
    assert out == "\nERROR: no answer.\n\nClaude did not answer within 120 seconds.\n"


def test_every_answer_state_has_a_plain_heading():
    assert set(cli.STATE_HEADINGS) == set(GrounderState)


def test_the_claude_add_on_not_installed_is_a_plain_message(run, hydrology: Path, monkeypatch):
    monkeypatch.setenv(active.PROVIDERS_ENV, active.CLAUDE)
    monkeypatch.setattr(cli, "sdk_installed", lambda: False)

    assert run("ask", "Hydrology", "What feeds base flow?", generator=cli.selected_generator) == (
        1,
        "",
        f"{INSTALL_HINT}\n",
    )


@pytest.mark.parametrize("typo", ["cluade", "claud", "open ai"])
def test_a_misspelt_model_choice_points_to_claude_never_the_paid_one(
    run, hydrology: Path, monkeypatch, typo
):
    monkeypatch.setenv(active.PROVIDERS_ENV, typo)

    code, out, err = run("ask", "Hydrology", "x", generator=cli.selected_generator)

    assert (code, out) == (1, "")
    assert err == (
        f"GCT_PROVIDERS is set to {typo!r}, which the tutor does not recognise. To get answers "
        "from Claude on this Mac's login, remove the setting: unset GCT_PROVIDERS. To try the "
        "tutor free with the stand-in model, which does not really read, use "
        "GCT_PROVIDERS=standin.\n"
    )
    assert "openai" not in err.casefold()


@pytest.mark.parametrize("written", ["Claude", " CLAUDE ", "claude"])
def test_a_model_choice_ignores_capitals_and_spaces(built, monkeypatch, written):
    monkeypatch.setenv(active.PROVIDERS_ENV, written)

    cli.selected_generator()

    assert built == ["claude"]


@pytest.mark.parametrize("written", ["openai", "OpenAI"])
def test_openai_is_no_longer_a_choice_and_gets_the_plain_message(
    hydrology: Path, library: Path, monkeypatch, written
):
    monkeypatch.setenv(active.PROVIDERS_ENV, written)
    tutor = Path(sys.executable).parent / "tutor"

    finished = subprocess.run(
        [str(tutor), "ask", "--library", str(library), "Hydrology", "What feeds base flow?"],
        capture_output=True,
        text=True,
    )

    assert finished.returncode == 1
    assert finished.stdout == f"Library: {library}\n"
    assert finished.stderr == (
        f"GCT_PROVIDERS is set to {written!r}, which the tutor does not recognise. To get "
        "answers from Claude on this Mac's login, remove the setting: unset GCT_PROVIDERS. To "
        "try the tutor free with the stand-in model, which does not really read, use "
        "GCT_PROVIDERS=standin.\n"
    )


def test_the_owners_commands_use_the_model_the_environment_selects(monkeypatch):
    monkeypatch.setenv(active.PROVIDERS_ENV, active.STANDIN)
    assert isinstance(cli.selected_generator(), StandInGeneration)

    monkeypatch.setenv(active.PROVIDERS_ENV, active.CLAUDE)
    assert cli.selected_generator().model_id.startswith("claude-agent-sdk:")


def test_check_reports_a_sound_library(run, hydrology: Path):
    assert run("check", generator=never) == (0, "The library is in good order.\n", "")


def test_check_lists_problems_and_fails(run, hydrology: Path, monkeypatch):
    monkeypatch.setattr(Library, "check", lambda self: ["The keyword-search index is damaged."])

    assert run("check") == (
        1,
        "The library has problems:\n  The keyword-search index is damaged.\n",
        "",
    )


def test_stopping_with_control_c_says_stopped(run, hydrology: Path, monkeypatch):
    def interrupted(*_: object, **__: object):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "ask", interrupted)

    assert run("ask", "Hydrology", "What feeds base flow?") == (130, "", "Stopped.\n")


def test_the_module_runs_from_terminal_without_a_stack_trace(tmp_path: Path):
    missing = tmp_path / "tutor.sqlite3"

    finished = subprocess.run(
        [sys.executable, "-m", "gct.library", "list", "--library", str(missing)],
        capture_output=True,
        text=True,
    )

    assert finished.returncode == 1
    assert finished.stdout == f"Library: {missing}\n"
    assert finished.stderr.startswith(f"There is no library at {missing}.")
    assert "Traceback" not in finished.stderr


def test_tutor_with_nothing_after_it_lists_what_you_can_do():
    out = io.StringIO()

    assert cli.main([], generator=never, out=out) == 0
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("Your class tutor:")
    for command in ("add-class", "import", "ask", "list", "check"):
        assert any(line.startswith(f"  uv run tutor {command} ") for line in lines)


def run_plain(*argv: str, generator: Callable[[], Generation] = StandInGeneration):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), generator=generator, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_with_no_library_given_the_default_is_tutor_sqlite3_in_your_home(home: Path):
    code, out, _ = run_plain("add-class", "Hydrology")

    assert code == 0
    assert out == (
        f"Library: {home / 'tutor.sqlite3'}\n"
        "This is a new library; it was made just now.\n"
        "Made the class Hydrology. Add files to it with:\n"
        "  uv run tutor import Hydrology FILE\n"
    )
    assert (home / "tutor.sqlite3").exists()


def test_the_library_variable_wins_over_the_default(home: Path, monkeypatch):
    monkeypatch.setenv(cli.LIBRARY_ENV, "~/term.sqlite3")

    code, out, _ = run_plain("add-class", "Hydrology")

    assert code == 0
    assert out.startswith(f"Library: {home / 'term.sqlite3'}\n")
    assert not (home / "tutor.sqlite3").exists()


def test_the_library_flag_wins_over_the_variable(home: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv(cli.LIBRARY_ENV, str(home / "term.sqlite3"))
    flagged = tmp_path / "flag.sqlite3"

    code, out, _ = run_plain("add-class", "--library", str(flagged), "Hydrology")

    assert code == 0
    assert out.startswith(f"Library: {flagged}\n")
    assert flagged.exists()
    assert not (home / "term.sqlite3").exists()


@pytest.mark.parametrize(
    "command", [["list"], ["check"], ["import", "X", "a.pdf"], ["ask", "X", "q"]]
)
def test_only_add_class_makes_the_default_library(home: Path, command):
    code, out, err = run_plain(*command, generator=never)

    assert code == 1
    assert out == f"Library: {home / 'tutor.sqlite3'}\n"
    assert err.endswith("make a new library with:\n  uv run tutor add-class CLASS\n")
    assert list(home.iterdir()) == []


@pytest.fixture
def built(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        "gct.providers.claude.ClaudeGeneration.__init__",
        lambda self, *_, **__: seen.append("claude"),
    )
    return seen


@pytest.mark.parametrize("unset", [None, "", "  "])
def test_an_empty_model_choice_also_means_claude(built, monkeypatch, unset):
    if unset is not None:
        monkeypatch.setenv(active.PROVIDERS_ENV, unset)

    cli.selected_generator()

    assert built == ["claude"]


def test_a_model_chosen_on_purpose_is_respected(built, monkeypatch):
    monkeypatch.setenv(active.PROVIDERS_ENV, active.STANDIN)

    assert isinstance(cli.selected_generator(), StandInGeneration)
    assert built == []


def test_with_no_model_chosen_and_claude_not_signed_in_you_get_claudes_message(
    run, hydrology: Path, monkeypatch
):
    def not_signed_in(self, messages):
        raise ClaudeNotSignedInError(NOT_SIGNED_IN)

    monkeypatch.setattr("gct.providers.claude.ClaudeGeneration.generate", not_signed_in)

    assert run("ask", "Hydrology", "What feeds base flow?", generator=cli.selected_generator) == (
        1,
        f"\nERROR: no answer.\n\n{NOT_SIGNED_IN}\n",
        "",
    )


def test_with_no_model_chosen_and_no_claude_add_on_you_get_the_install_message(
    run, hydrology: Path, monkeypatch
):
    monkeypatch.setattr(cli, "sdk_installed", lambda: False)

    assert run("ask", "Hydrology", "What feeds base flow?", generator=cli.selected_generator) == (
        1,
        "",
        f"{INSTALL_HINT}\n",
    )


def test_the_tutor_command_is_installed_and_runs(home: Path):
    tutor = Path(sys.executable).parent / "tutor"

    finished = subprocess.run([str(tutor)], capture_output=True, text=True)

    assert finished.returncode == 0
    assert finished.stdout.startswith("Your class tutor:")
    assert finished.stderr == ""


def test_every_help_page_has_an_example():
    for command in ([], ["add-class"], ["list"], ["import"], ["ask"], ["check"]):
        out = io.StringIO()
        with pytest.raises(SystemExit) as done, contextlib.redirect_stdout(out):
            cli.main([*command, "--help"], generator=never)
        assert done.value.code == 0
        assert "Example: uv run tutor" in out.getvalue()


def test_a_question_without_quotes_is_joined_into_one(run, hydrology: Path):
    code, out, _ = run(
        "ask", "Hydrology", "What", "feeds", "the", "base", "flow", "of", "a", "river"
    )

    assert code == 0
    assert out.startswith("\nGROUNDED:")


def test_the_library_flag_may_come_before_the_command(library: Path):
    out = io.StringIO()
    Library(library).close()

    assert cli.main(["--library", str(library), "list"], generator=never, out=out) == 0
    assert out.getvalue().startswith(f"Library: {library}\n")


def usage(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), generator=never, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize(
    ("argv", "plain", "example"),
    [
        (
            ["add-class", "Water", "Science"],
            "A class name with spaces needs quotes around it.",
            'uv run tutor add-class "Water Science"',
        ),
        (
            ["aks", "Hydrology", "x"],
            "There is no command called 'aks'. The commands are add-class, import, ask, list and "
            "check; run uv run tutor on its own to see what each one does.",
            'uv run tutor ask Hydrology "What is base flow?"',
        ),
        (
            ["ask", "Hydrology"],
            "Say which class to ask, then the question.",
            'uv run tutor ask Hydrology "What is base flow?"',
        ),
        (
            ["import", "Hydrology"],
            "Say which class, then the file or files to add.",
            "uv run tutor import Hydrology ~/Downloads/notes.pdf",
        ),
        (
            ["add-class"],
            "Say the name of the class to make.",
            'uv run tutor add-class "Water Science"',
        ),
        (["list", "extra"], "The list command takes nothing after it.", "uv run tutor list"),
        (
            ["list", "--libary", "x"],
            "There is no option --libary; the only option is --library PATH.",
            "uv run tutor list",
        ),
        (
            ["--library"],
            "Put the library's path after --library.",
            'uv run tutor ask Hydrology "What is base flow?"',
        ),
        (
            ["--library", "x"],
            "Say what to do: add-class, import, ask, list or check.",
            'uv run tutor ask Hydrology "What is base flow?"',
        ),
    ],
)
def test_a_wrong_command_line_gets_one_plain_line_and_an_example(argv, plain, example):
    assert usage(*argv) == (2, "", f"{plain}\nFor example: {example}\n")


def test_an_unquoted_class_name_with_spaces_gets_a_hint_about_quotes(run):
    run("add-class", "Water Science")

    code, _, err = run("ask", "Water", "Science", "What", "is", "it", generator=never)

    assert code == 1
    assert err.endswith('A class name with spaces needs quotes around it, like "Water Science".\n')


@pytest.mark.parametrize("question", [[""], ["  "], ["???"], ["?", "!"]])
def test_an_empty_question_says_to_type_one_and_calls_no_model(run, hydrology: Path, question):
    assert run("ask", "Hydrology", *question, generator=never) == (
        1,
        "",
        "Type a question after the class name, for example:\n"
        f"  uv run tutor ask --library {hydrology.parent / 'tutor.sqlite3'} Hydrology "
        "'What is base flow?'\n",
    )


def test_a_class_with_no_files_says_so_and_calls_no_model(run, library: Path):
    run("add-class", "Hydrology")

    assert run("ask", "Hydrology", "What is base flow?", generator=never) == (
        1,
        "",
        "The class Hydrology has no files yet, so there is nothing to answer from. Add one "
        f"with:\n  uv run tutor import --library {library} Hydrology FILE\n",
    )


def test_a_class_name_differing_only_in_capitals_is_refused(run, library: Path):
    run("add-class", "Biology")

    assert run("add-class", "biology") == (
        1,
        "",
        "There is already a class called 'Biology'. Names that differ only in capital letters "
        "are too easy to mix up; choose another name.\n",
    )
    with Library(library) as opened:
        assert [found.name for found in opened.list_classes()] == ["Biology"]


@pytest.mark.parametrize(
    ("written", "line"),
    [
        (None, "Answers come from Claude on this Mac's login."),
        ("standin", "Answers come from the free stand-in model (GCT_PROVIDERS=standin)"),
        ("OpenAI", "GCT_PROVIDERS is set to 'OpenAI', which the tutor does not recognise."),
        ("cluade", "GCT_PROVIDERS is set to 'cluade', which the tutor does not recognise."),
    ],
)
def test_the_overview_names_the_model_actually_in_use(monkeypatch, written, line):
    if written is not None:
        monkeypatch.setenv(active.PROVIDERS_ENV, written)

    code, out, _ = usage()

    assert code == 0
    assert out.splitlines()[-1].startswith(line)


def test_a_long_gap_is_shortened(run, hydrology: Path):
    long = "how " + "very " * 80 + "fast it moves"
    reply = f"Base flow is fed by groundwater [S1].\nCOVERAGE: gaps: {long}"

    code, out, _ = run(
        "ask", "Hydrology", "What feeds base flow?", generator=lambda: Scripted(reply)
    )

    assert code == 0
    gap = out.split("Not covered by your files:\n  ", 1)[1].splitlines()[0]
    assert len(gap) <= cli.LONGEST_GAP
    assert gap.endswith("…")


def test_the_overview_and_the_ask_help_say_why_the_question_needs_quotes():
    why = (
        "Keep the question in quotes. Without them, a question ending in ? gets 'no matches "
        "found' from Terminal before the tutor runs."
    )
    assert why == cli.QUOTES_WHY
    assert why in " ".join(usage()[1].split())
    out = io.StringIO()
    with pytest.raises(SystemExit), contextlib.redirect_stdout(out):
        cli.main(["ask", "--help"], generator=never)
    assert " ".join(why.split()) in " ".join(out.getvalue().split())
