"""Ask a class a question through the library (#230).

Every library here is filled through `Library.save_document` and every answer comes from the free
stand-in model, which quotes the source sentence sharing the most words with the question.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from gct.grounder import answer as grounder
from gct.grounder.answer import GrounderState
from gct.ingest.parse import ParsedUnit
from gct.library import Library, UnknownPassageError
from gct.library import ask as library_ask
from gct.library.ask import (
    ERROR_KIND_SEARCH_FAILED,
    ERROR_KIND_UNKNOWN_CLASS,
    ERROR_KIND_UNKNOWN_DOCUMENT,
    ask,
)
from gct.providers.base import Message
from gct.providers.standin import StandInGeneration

HYDROLOGY = [
    "The base flow of a river is the share of its flow fed by groundwater.",
    "Groundwater recharge is slow where clay sits above the aquifer.",
]
HISTORY = [
    "The Ostren League was founded by four harbour towns in the year 1204.",
    "Its fleet protected the amberwool trade along the northern coast.",
]


class Recording(StandInGeneration):
    def __init__(self) -> None:
        self.sent: list[list[Message]] = []

    def generate(self, messages: Sequence[Message]) -> str:
        self.sent.append(list(messages))
        return super().generate(messages)


class Failing:
    model_id = "failing"

    def generate(self, messages: Sequence[Message]) -> str:
        raise RuntimeError("the model is not reachable")


def _save(library: Library, class_id: str, filename: str, pages: list[str]) -> str:
    units = [
        ParsedUnit(text=text, file=filename, page_or_slide=number)
        for number, text in enumerate(pages, start=1)
    ]
    original = f"%PDF {filename} {pages!r}".encode()
    return library.save_document(class_id, filename, original, len(pages), units).document.id


def _fill(library: Library) -> tuple[str, str]:
    hydrology = library.create_class("Hydrology").id
    history = library.create_class("History").id
    _save(library, hydrology, "lecture-03.pdf", HYDROLOGY)
    _save(library, history, "week-02.pdf", HISTORY)
    return hydrology, history


@pytest.fixture
def library(tmp_path: Path) -> Iterator[Library]:
    with Library(tmp_path / "course.sqlite3") as opened:
        yield opened


@pytest.fixture
def classes(library: Library) -> tuple[str, str]:
    return _fill(library)


def test_a_covered_question_is_answered_with_citations_from_that_class(
    library: Library, classes: tuple[str, str]
):
    hydrology, _ = classes

    asked = ask(library, hydrology, "What is the base flow of a river?", generator=Recording())

    assert asked.result.state is GrounderState.GROUNDED
    assert asked.retrieval_ran
    assert asked.result.citations
    for citation in asked.result.citations:
        passage = library.passage(hydrology, citation.chunk_id)
        assert (passage.filename, passage.page_or_slide) == (
            citation.file,
            citation.page_or_slide,
        )
    assert {c.chunk_id for c in asked.result.citations} <= {c.chunk_id for c in asked.retrieved}


def test_the_model_is_shown_file_page_and_text_and_nothing_that_changes_between_libraries(
    tmp_path: Path,
):
    sent = []
    for name in ("first", "second"):
        with Library(tmp_path / f"{name}.sqlite3") as library:
            hydrology, history = _fill(library)
            generator = Recording()
            asked = ask(library, hydrology, "What feeds the base flow?", generator=generator)
            prompt = "\n".join(message["content"] for message in generator.sent[0])
            assert "(lecture-03.pdf, p.1)\n" + HYDROLOGY[0] in prompt
            for leaked in (hydrology, history, *(c.chunk_id for c in asked.retrieved)):
                assert leaked not in prompt
            assert not [str(c.score) for c in asked.retrieved if str(c.score) in prompt]
            sent.append(generator.sent)

    assert sent[0] == sent[1]


def test_a_question_the_class_does_not_cover_is_refused(library: Library, classes: tuple[str, str]):
    hydrology, _ = classes

    asked = ask(library, hydrology, "Who wrote the play Hamlet?", generator=Recording())

    assert asked.result.state is GrounderState.REFUSAL
    assert asked.result.citations == []


def test_a_fact_from_another_class_is_refused(library: Library, classes: tuple[str, str]):
    hydrology, _ = classes

    asked = ask(library, hydrology, "When was the Ostren League founded?", generator=Recording())

    assert asked.result.state is GrounderState.REFUSAL
    assert all(c.file == "lecture-03.pdf" for c in asked.retrieved)


@pytest.mark.parametrize("question", ["", "   ", "What is the?", "?!"])
def test_a_question_with_no_real_words_is_refused_without_asking_the_model(
    library: Library, classes: tuple[str, str], question: str
):
    hydrology, _ = classes
    generator = Recording()

    asked = ask(library, hydrology, question, generator=generator)

    assert asked.result.state is GrounderState.REFUSAL
    assert (asked.retrieved, asked.retrieval_ran, generator.sent) == ([], True, [])


def test_an_empty_class_refuses_without_asking_the_model(library: Library):
    empty = library.create_class("Empty").id
    generator = Recording()

    asked = ask(library, empty, "What is the base flow of a river?", generator=generator)

    assert asked.result.state is GrounderState.REFUSAL
    assert generator.sent == []


def test_an_unknown_class_is_an_error_result_not_an_exception(
    library: Library, classes: tuple[str, str]
):
    hydrology, _ = classes
    library.delete_class(hydrology)
    generator = Recording()

    asked = ask(library, hydrology, "What is the base flow of a river?", generator=generator)

    assert asked.result.state is GrounderState.ERROR
    assert asked.result.error is not None
    assert asked.result.error.kind == ERROR_KIND_UNKNOWN_CLASS
    assert "refresh the list" in asked.result.error.message
    assert (asked.retrieved, asked.retrieval_ran, generator.sent) == ([], False, [])


def test_asking_one_document_cites_only_that_document(library: Library):
    hydrology = library.create_class("Hydrology").id
    _save(library, hydrology, "lecture-03.pdf", HYDROLOGY)
    second = _save(library, hydrology, "lecture-04.pdf", ["Base flow falls in a long drought."])

    asked = ask(library, hydrology, "What is base flow?", generator=Recording(), document_id=second)

    assert asked.result.state is GrounderState.GROUNDED
    assert {c.file for c in asked.result.citations} == {"lecture-04.pdf"}
    assert {c.file for c in asked.retrieved} == {"lecture-04.pdf"}


def test_a_document_from_another_class_is_an_error_result(library: Library):
    hydrology = library.create_class("Hydrology").id
    history = library.create_class("History").id
    _save(library, hydrology, "lecture-03.pdf", HYDROLOGY)
    elsewhere = _save(library, history, "week-02.pdf", HISTORY)

    asked = ask(
        library, hydrology, "Who founded the League?", generator=Recording(), document_id=elsewhere
    )

    assert asked.result.state is GrounderState.ERROR
    assert asked.result.error is not None
    assert asked.result.error.kind == ERROR_KIND_UNKNOWN_DOCUMENT
    assert asked.retrieval_ran is False


def test_a_library_file_that_is_gone_is_an_error_result(tmp_path: Path):
    path = tmp_path / "course.sqlite3"
    with Library(path) as library:
        hydrology, _ = _fill(library)
        path.unlink()

        asked = ask(library, hydrology, "What is base flow?", generator=Recording())

    assert asked.result.state is GrounderState.ERROR
    assert asked.result.error is not None
    assert asked.result.error.kind == ERROR_KIND_SEARCH_FAILED
    assert "try again" in asked.result.error.message


def test_the_library_open_in_another_window_still_answers(tmp_path: Path):
    path = tmp_path / "course.sqlite3"
    with Library(path) as library, Library(path) as other_window:
        hydrology, _ = _fill(other_window)

        asked = ask(library, hydrology, "What is the base flow of a river?", generator=Recording())

    assert asked.result.state is GrounderState.GROUNDED


def test_a_library_held_by_another_window_mid_write_is_an_error_result(tmp_path: Path):
    path = tmp_path / "course.sqlite3"
    with Library(path) as library:
        hydrology, _ = _fill(library)
        writer = sqlite3.connect(path, isolation_level=None)
        try:
            writer.execute("BEGIN EXCLUSIVE")
            started = time.monotonic()
            asked = ask(library, hydrology, "What is base flow?", generator=Recording())
            waited = time.monotonic() - started
        finally:
            writer.close()

    assert asked.result.state is GrounderState.ERROR
    assert asked.result.error is not None
    assert asked.result.error.kind == ERROR_KIND_SEARCH_FAILED
    assert waited < 30


def test_a_model_that_fails_is_an_error_result_with_the_passages_it_was_shown(
    library: Library, classes: tuple[str, str]
):
    hydrology, _ = classes

    asked = ask(library, hydrology, "What is the base flow of a river?", generator=Failing())

    assert asked.result.state is GrounderState.ERROR
    assert asked.result.error is not None
    assert asked.result.error.kind == grounder.ERROR_KIND_PROVIDER_TERMINAL
    assert asked.retrieval_ran and asked.retrieved


def test_the_result_count_is_passed_to_search(library: Library, classes: tuple[str, str]):
    hydrology, _ = classes
    generator = Recording()

    asked = ask(library, hydrology, "groundwater", generator=generator, k=1)

    assert len(asked.retrieved) == 1
    assert "[S2]" not in generator.sent[0][-1]["content"]
    with pytest.raises(ValueError):
        ask(library, hydrology, "groundwater", generator=generator, k=0)


def test_a_search_error_has_the_same_shape_as_the_grounders_own_error():
    from gct.grounder.answer import _error

    ours = library_ask._search_error(UnknownPassageError("some message"))
    theirs = _error(ERROR_KIND_SEARCH_FAILED, "some message")

    assert ours == theirs
