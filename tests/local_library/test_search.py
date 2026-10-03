"""Offline search behavior, scope isolation, query syntax, and context budgets."""

from __future__ import annotations

import pytest

from gct.grounder.answer import _build_labeled_context
from gct.ingest.parse import ParsedUnit
from gct.local.search import MAX_CONTEXT_CHARS, MAX_QUERY_TERMS, query_expression
from gct.local.store import Library, LibraryError


def add(library, class_id, texts, *, name="Lecture.pdf", salt=""):
    return library.import_document(
        class_id,
        name,
        (name + salt + "\n".join(texts)).encode(),
        [ParsedUnit(text, name, page) for page, text in enumerate(texts, start=1)],
        len(texts),
    )


def test_scope_is_applied_before_limit_even_when_other_class_dominates_rank(tmp_path):
    with Library(tmp_path / "library.db") as library:
        selected = library.create_class("Selected")["id"]
        other = library.create_class("Other")["id"]
        for index in range(20):
            add(library, other, ["recall"], salt=str(index))
        own = add(library, selected, ["A much longer passage about an unrelated topic and recall."])
        chunks = library.retrieve(selected, "recall", k=1)
        assert len(chunks) == 1
        assert chunks[0].text.startswith("A much longer")
        assert chunks == library.retrieve(selected, "recall", document_id=own["id"], k=1)


def test_page_and_document_scope_precede_limit_and_cannot_cross_classes(tmp_path):
    with Library(tmp_path / "library.db") as library:
        class_id = library.create_class("A")["id"]
        other = library.create_class("B")["id"]
        document = add(library, class_id, ["Recall short.", "Recall is practiced through memory."])
        add(library, class_id, ["Recall."], salt="another")
        chunks = library.retrieve(class_id, "recall", document_id=document["id"], pages=[2], k=1)
        assert len(chunks) == 1
        assert chunks[0].page_or_slide == 2
        assert "practiced" in chunks[0].text
        with pytest.raises(LibraryError):
            library.retrieve(other, "recall", document_id=document["id"])


@pytest.mark.parametrize(
    "question",
    [
        '" OR 1=1; DROP TABLE documents; --',
        'recall NEAR("secret", 5)',
        "recall: {a b} * - ^",
        "recall\x00 OR spaced",
        'recall" ) AND ("spaced',
        "recall😊漢字 café",
        "the what and",
        "... ___ ***",
    ],
)
def test_fts_control_syntax_is_literal_text_and_never_errors(tmp_path, question):
    with Library(tmp_path / "library.db") as library:
        class_id = library.create_class("A")["id"]
        document = add(library, class_id, ["Recall and spaced practice are useful at the café."])
        library.retrieve(class_id, question)
        assert library.list_documents(class_id) == [document]


def test_query_bound_and_stop_words(tmp_path):
    expression = query_expression(" ".join(f"word{i}" for i in range(100)))
    assert expression.count(" OR ") == MAX_QUERY_TERMS - 1
    assert query_expression("the what according to these materials") == ""
    assert query_expression('recall OR recall: "spaced"') == '"recall" OR "spaced"'
    with Library(tmp_path / "library.db") as library:
        class_id = library.create_class("A")["id"]
        add(library, class_id, ["Recall."])
        assert library.retrieve(class_id, "the what and") == []
        for question in ["", "   ", None, "x" * 2001]:
            with pytest.raises(LibraryError):
                library.retrieve(class_id, question)


@pytest.mark.parametrize(
    "options",
    [
        {"k": 0},
        {"k": 9},
        {"k": True},
        {"pages": [1]},
        {"pages": []},
        {"pages": [1, 1]},
        {"pages": [True]},
        {"pages": [3]},
        {"pages": "1"},
    ],
)
def test_invalid_limits_and_pages_are_rejected(tmp_path, options):
    with Library(tmp_path / "library.db") as library:
        class_id = library.create_class("A")["id"]
        document = add(library, class_id, ["Recall.", "Memory."])
        # The bare pages=[1] case deliberately omits document scope.
        if options != {"pages": [1]}:
            options = {"document_id": document["id"], **options}
        with pytest.raises(LibraryError):
            library.retrieve(class_id, "recall", **options)


def test_search_bound_counts_grounder_labels_and_does_not_truncate_source_text(tmp_path):
    with Library(tmp_path / "library.db") as library:
        class_id = library.create_class("A")["id"]
        texts = [f"recall {index} " + "abcdefghij" * 2000 for index in range(8)]
        add(library, class_id, texts)
        chunks = library.retrieve(class_id, "recall")
        assert 0 < len(chunks) < 8
        context, _ = _build_labeled_context(chunks)
        assert len(context) <= MAX_CONTEXT_CHARS
        assert all(chunk.text in texts for chunk in chunks)


def test_results_cap_at_eight_and_scores_are_reciprocal_rank(tmp_path):
    with Library(tmp_path / "library.db") as library:
        class_id = library.create_class("A")["id"]
        add(library, class_id, [f"Recall fact number {index}." for index in range(20)])
        chunks = library.retrieve(class_id, "recall")
        assert len(chunks) == 8
        assert [chunk.score for chunk in chunks] == [1 / rank for rank in range(1, 9)]
        assert len({chunk.chunk_id for chunk in chunks}) == 8


def test_retrieval_short_circuits_empty_class_and_unmatched_query(tmp_path):
    with Library(tmp_path / "library.db") as library:
        class_id = library.create_class("A")["id"]
        assert library.retrieve(class_id, "recall") == []
        add(library, class_id, ["Recall facts through memory retrieval."])
        assert library.retrieve(class_id, "quasiparticleunobtainium") == []
        assert library.retrieve(class_id, "retrieving")
