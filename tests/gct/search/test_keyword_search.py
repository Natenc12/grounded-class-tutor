"""Keyword search inside one class (#228).

Every library here is filled through `Library.save_document`, the library's one write path, so the
passages, their ids and the keyword index are exactly what the app itself would store.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from gct.contracts import RetrievedChunk
from gct.ingest.parse import ParsedUnit
from gct.library import Library, LibraryError, UnknownClassError, UnknownDocumentError
from gct.search import DEFAULT_K, keyword, search


@pytest.fixture
def library_path(tmp_path: Path) -> Path:
    return tmp_path / "course.sqlite3"


@pytest.fixture
def library(library_path: Path) -> Iterator[Library]:
    with Library(library_path) as opened:
        yield opened


def _save(library: Library, class_id: str, filename: str, pages: list[str], salt: str = "") -> str:
    units = [
        ParsedUnit(text=text, file=filename, page_or_slide=number)
        for number, text in enumerate(pages, start=1)
    ]
    original = f"%PDF {filename} {salt} {pages!r}".encode()
    return library.save_document(class_id, filename, original, len(pages), units).document.id


def _places(chunks: list[RetrievedChunk]) -> list[tuple[str, int]]:
    return [(chunk.file, chunk.page_or_slide) for chunk in chunks]


@pytest.fixture
def hydrology(library: Library) -> str:
    class_id = library.create_class("Hydrology").id
    _save(
        library,
        class_id,
        "lecture-03.pdf",
        [
            'The "base flow" of a river is the share of its flow fed by groundwater.',
            "Groundwater is non-renewable on human timescales when aquifers recharge slowly.",
            "Do NOT confuse infiltration AND percolation; NEAR the surface water infiltrates.",
            "Evaporation OR transpiration together are called evapotranspiration.",
        ],
    )
    return class_id


@pytest.mark.parametrize(
    "question",
    [
        'What does "base flow" mean?',
        'What does "base flow mean?',
        "Is groundwater non-renewable?",
        "Why should I NOT confuse infiltration and percolation?",
        "infiltration AND percolation",
        "evaporation OR transpiration",
        "NEAR(infiltration percolation)",
        "infiltration NOT percolation",
        "- infiltration",
        "text: base flow",
        "base* ^flow (river) {groundwater} + aquifers",
        "base flow?!;'\\",
    ],
)
def test_a_question_is_plain_words_never_search_syntax(
    library: Library, hydrology: str, question: str
):
    assert search(library, hydrology, question)


def test_search_keywords_in_a_question_are_matched_as_words(library: Library, hydrology: str):
    assert _places(search(library, hydrology, "NOT", k=1)) == [("lecture-03.pdf", 3)]
    assert _places(search(library, hydrology, "or", k=1)) == []
    assert _places(search(library, hydrology, "near", k=1)) == [("lecture-03.pdf", 3)]


def test_words_in_capitals_and_subject_words_are_kept(library: Library):
    class_id = library.create_class("Environmental science").id
    _save(
        library,
        class_id,
        "lecture-07.pdf",
        [
            "The US Geological Survey measures streams.",
            "IT support for the field laptops is in room 3.",
            "The WHO sets drinking water limits.",
            "Tin cans rust; a will names heirs.",
        ],
    )
    assert _places(search(library, class_id, "Who measures for the US?", k=1)) == [
        ("lecture-07.pdf", 1)
    ]
    assert _places(search(library, class_id, "Where is IT?", k=1)) == [("lecture-07.pdf", 2)]
    assert _places(search(library, class_id, "What is the WHO?", k=1)) == [("lecture-07.pdf", 3)]
    assert _places(search(library, class_id, "Why does a can rust?", k=1)) == [
        ("lecture-07.pdf", 4)
    ]
    assert _places(search(library, class_id, "What is a will?", k=1)) == [("lecture-07.pdf", 4)]
    assert search(library, class_id, "who is it?") != search(library, class_id, "WHO")


def test_a_quoted_phrase_does_not_have_to_appear_as_a_phrase(library: Library, hydrology: str):
    assert _places(search(library, hydrology, '"river groundwater"', k=1)) == [
        ("lecture-03.pdf", 1)
    ]


def test_the_best_matching_passage_comes_first(library: Library, hydrology: str):
    found = search(library, hydrology, "How slowly do aquifers recharge?")
    assert _places(found)[0] == ("lecture-03.pdf", 2)
    assert [chunk.score for chunk in found] == sorted(
        (chunk.score for chunk in found), reverse=True
    )
    assert all(chunk.score > 0 for chunk in found)


def test_any_word_of_a_question_can_match(library: Library, hydrology: str):
    assert _places(search(library, hydrology, "Which aquifers are near the volcano?", k=1)) == [
        ("lecture-03.pdf", 2)
    ]


def test_word_endings_and_letter_case_do_not_matter(library: Library, hydrology: str):
    assert _places(search(library, hydrology, "AQUIFER recharging", k=1)) == [("lecture-03.pdf", 2)]


def test_accents_and_non_english_letters_are_searchable(library: Library):
    class_id = library.create_class("Geographie").id
    _save(
        library,
        class_id,
        "kurs.pdf",
        ["Das Grundwasser unter der Straße fließt langsam.", "地下水 是 可再生 资源", "Le café"],
    )
    assert _places(search(library, class_id, "Wie fließt das Grundwasser?", k=1)) == [
        ("kurs.pdf", 1)
    ]
    assert _places(search(library, class_id, "STRASSE straße", k=1)) == [("kurs.pdf", 1)]
    assert _places(search(library, class_id, "可再生", k=1)) == [("kurs.pdf", 2)]
    assert _places(search(library, class_id, "Was ist ein Cafe?", k=1)) == [("kurs.pdf", 3)]


def test_a_very_long_question_still_searches(library: Library, hydrology: str):
    question = " ".join(f"filler{n}" for n in range(20_000)) + " evapotranspiration?"
    assert _places(search(library, hydrology, question, k=1)) == [("lecture-03.pdf", 4)]


@pytest.mark.parametrize(
    "question", ["?", "", "   ", "!!! ...", "What is this?", "I A", "Who are they and why?", "_ __"]
)
def test_a_question_with_no_searchable_words_finds_nothing(
    library: Library, hydrology: str, question: str
):
    assert search(library, hydrology, question) == []


def test_a_class_never_returns_another_classs_passages(library: Library):
    first = library.create_class("Hydrology 210").id
    second = library.create_class("Hydrology 310").id
    _save(library, first, "notes.pdf", ["Groundwater recharge in aquifers."])
    for copy in range(10):
        _save(
            library,
            second,
            "notes.pdf",
            ["Groundwater recharge in aquifers, groundwater recharge, aquifers."],
            salt=str(copy),
        )
    found = search(library, first, "groundwater recharge aquifers", k=50)
    assert len(found) == 1
    assert library.passage(first, found[0].chunk_id).text == found[0].text
    with pytest.raises(LibraryError):
        library.passage(second, found[0].chunk_id)
    assert len(search(library, second, "groundwater recharge aquifers", k=50)) == 10


def test_the_class_filter_applies_before_the_result_count(library: Library):
    wanted = library.create_class("Small class").id
    crowded = library.create_class("Crowded class").id
    for copy in range(40):
        _save(library, crowded, f"copy-{copy:02}.pdf", ["aquifer aquifer aquifer"])
    _save(library, wanted, "a.pdf", ["An aquifer, among many other words in a longer passage."])
    _save(library, wanted, "b.pdf", ["Another aquifer, among many other words in a passage."])
    assert sorted(_places(search(library, wanted, "aquifer", k=2))) == [("a.pdf", 1), ("b.pdf", 1)]


def test_equal_scores_come_back_in_one_fixed_order(library_path: Path):
    text = "Aquifers store groundwater between grains of sand."
    with Library(library_path) as library:
        class_id = library.create_class("Hydrology").id
        _save(library, class_id, "z-notes.pdf", [text])
        _save(library, class_id, "a-notes.pdf", [text, text])
        _save(library, class_id, "m-notes.pdf", [text], salt="second copy")
        _save(library, class_id, "m-notes.pdf", [text], salt="first copy")
        first = search(library, class_id, "Where do aquifers store groundwater?", k=10)
        second = search(library, class_id, "Where do aquifers store groundwater?", k=10)
        hashes = {doc.id: doc.sha256 for doc in library.list_documents(class_id)}
    assert len({chunk.score for chunk in first}) == 1
    assert _places(first) == [
        ("a-notes.pdf", 1),
        ("a-notes.pdf", 2),
        ("m-notes.pdf", 1),
        ("m-notes.pdf", 1),
        ("z-notes.pdf", 1),
    ]
    with Library(library_path) as reopened:
        third = search(reopened, class_id, "Where do aquifers store groundwater?", k=10)
        m_notes = [
            reopened.passage(class_id, chunk.chunk_id).document_id
            for chunk in third
            if chunk.file == "m-notes.pdf"
        ]
    assert first == second == third
    assert [hashes[doc] for doc in m_notes] == sorted(hashes[doc] for doc in m_notes)


def test_passages_on_one_page_keep_their_order(library: Library):
    class_id = library.create_class("Hydrology").id
    page = " ".join(["aquifer recharge"] * 400)
    _save(library, class_id, "long.pdf", [page])
    found = search(library, class_id, "aquifer", k=10)
    ids = [chunk.chunk_id for chunk in found]
    assert len(found) > 1
    assert search(library, class_id, "aquifer", k=10) == found
    assert len(set(ids)) == len(ids)


def test_limiting_to_one_document_returns_only_its_passages(library: Library):
    class_id = library.create_class("Hydrology").id
    wanted = _save(library, class_id, "lecture-01.pdf", ["Aquifers recharge.", "Rivers flow."])
    _save(library, class_id, "lecture-02.pdf", ["Aquifers recharge, aquifers, aquifers."])
    found = search(library, class_id, "aquifers recharge", document_id=wanted, k=10)
    assert _places(found) == [("lecture-01.pdf", 1)]
    assert search(library, class_id, "?", document_id=wanted) == []


def test_a_document_from_another_class_is_refused(library: Library):
    class_id = library.create_class("Hydrology").id
    other = library.create_class("History").id
    elsewhere = _save(library, other, "lecture-01.pdf", ["Aquifers recharge."])
    with pytest.raises(UnknownDocumentError):
        search(library, class_id, "aquifers", document_id=elsewhere)
    with pytest.raises(UnknownDocumentError):
        search(library, class_id, "?", document_id="no-such-document")


def test_an_unknown_class_is_refused_even_for_an_empty_question(library: Library):
    with pytest.raises(UnknownClassError):
        search(library, "no-such-class", "aquifers")
    with pytest.raises(UnknownClassError):
        search(library, "no-such-class", "?")


def test_an_empty_class_finds_nothing(library: Library, hydrology: str):
    empty = library.create_class("Empty").id
    assert search(library, empty, "groundwater") == []


def test_the_result_count_defaults_to_five_and_is_respected(library: Library):
    class_id = library.create_class("Hydrology").id
    _save(library, class_id, "notes.pdf", [f"Aquifer fact number {n}." for n in range(1, 9)])
    assert DEFAULT_K == 5
    assert len(search(library, class_id, "aquifer")) == 5
    assert len(search(library, class_id, "aquifer", k=1)) == 1
    assert len(search(library, class_id, "aquifer", k=100)) == 8


@pytest.mark.parametrize("k", [0, -1, True, 2.0, "5", None])
def test_a_result_count_below_one_or_not_a_whole_number_is_refused(
    library: Library, hydrology: str, k: object
):
    with pytest.raises(ValueError):
        search(library, hydrology, "groundwater", k=k)  # type: ignore[arg-type]


def test_a_result_carries_what_the_answer_needs(library: Library, hydrology: str):
    (chunk,) = search(library, hydrology, "evapotranspiration", k=1)
    passage = library.passage(hydrology, chunk.chunk_id)
    assert (chunk.text, chunk.file, chunk.page_or_slide) == (
        passage.text,
        passage.filename,
        passage.page_or_slide,
    )


def test_searching_leaves_the_file_untouched(library: Library, library_path: Path, hydrology: str):
    before = library_path.read_bytes()
    for question in ['"base flow"', "DROP TABLE passages; --", "NOT AND OR NEAR"]:
        search(library, hydrology, question)
    assert library_path.read_bytes() == before
    assert sorted(path.name for path in library_path.parent.iterdir()) == [library_path.name]


def test_search_sees_changes_made_through_another_window(library: Library, hydrology: str):
    other = Library(library.path)
    try:
        doomed = _save(other, hydrology, "lecture-09.pdf", ["Aquifers in karst."])
        found = search(library, hydrology, "karst aquifers", document_id=doomed)
        other.delete_document(hydrology, doomed)
        assert _places(found) == [("lecture-09.pdf", 1)]
        with pytest.raises(UnknownDocumentError):
            search(library, hydrology, "karst aquifers", document_id=doomed)
    finally:
        other.close()


def test_a_class_deleted_mid_search_cannot_split_the_check_from_the_search(
    library: Library, hydrology: str, monkeypatch: pytest.MonkeyPatch
):
    """Another window deletes the class just after the search checked it exists.

    The check and the search read one moment of the file, so the delete waits for the search
    instead of landing between them and turning "this class exists" into an empty result.
    """
    other = Library(library.path, timeout=0)
    outcomes: list[str] = []

    def delete_during_search(statement: str) -> None:
        if "MATCH" in statement and not outcomes:
            try:
                other.delete_class(hydrology)
                outcomes.append("deleted")
            except LibraryError:
                outcomes.append("refused")

    connect = keyword.sqlite3.connect

    def traced(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(delete_during_search)
        return connection

    monkeypatch.setattr(keyword.sqlite3, "connect", traced)
    try:
        found = search(library, hydrology, "evapotranspiration")
    finally:
        other.close()
    assert outcomes == ["refused"]
    assert _places(found) == [("lecture-03.pdf", 4)]


def test_search_recovers_a_library_left_mid_write_by_a_crash(
    library: Library, library_path: Path, hydrology: str
):
    crash = textwrap.dedent(
        f"""
        import os, sqlite3
        connection = sqlite3.connect({str(library_path)!r}, isolation_level=None)
        connection.execute("PRAGMA cache_size = 1")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE passages SET text = hex(zeroblob(200000)) || text")
        os._exit(0)
        """
    )
    subprocess.run([sys.executable, "-c", crash], check=True)
    assert (library_path.parent / (library_path.name + "-journal")).exists()
    found = search(library, hydrology, "evapotranspiration", k=1)
    assert found[0].text.startswith("Evaporation OR transpiration")


def test_a_missing_library_file_is_an_error_not_a_new_file(library_path: Path):
    with Library(library_path) as library:
        class_id = library.create_class("Hydrology").id
    library_path.unlink()
    with pytest.raises(LibraryError):
        search(library, class_id, "groundwater")
    assert not library_path.exists()
