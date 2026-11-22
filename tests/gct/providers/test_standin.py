from __future__ import annotations

import math

from gct.config import EMBEDDING_DIM
from gct.contracts import RetrievedChunk
from gct.grounder.answer import GrounderState, answer
from gct.providers.openai_provider import OpenAIEmbeddings, OpenAIGeneration
from gct.providers.standin import StandInEmbeddings, StandInGeneration

WATER = RetrievedChunk(
    chunk_id="c1",
    text="Residence time is how long a water molecule stays in one part of the cycle. "
    "In the atmosphere it is about nine days.",
    file="water.pdf",
    page_or_slide=3,
    score=0.9,
)
ROCKS = RetrievedChunk(
    chunk_id="c2",
    text="Granite is an igneous rock. It cools slowly below the surface.",
    file="rocks.pdf",
    page_or_slide=7,
    score=0.2,
)


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_embeddings_have_the_schema_dimension_and_unit_length_even_for_empty_text():
    for vector in StandInEmbeddings().embed(["water cycle", ""]):
        assert len(vector) == EMBEDDING_DIM
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0)


def test_text_sharing_words_with_the_question_ranks_above_text_that_shares_none():
    question, near, far = StandInEmbeddings().embed(
        ["how long is residence time in the atmosphere", WATER.text, ROCKS.text]
    )
    assert _cosine(question, near) > _cosine(question, far)
    assert StandInEmbeddings().embed([WATER.text]) == StandInEmbeddings().embed([WATER.text])


def test_a_covered_question_is_grounded_and_cites_only_the_source_that_covers_it():
    result = answer(
        "How long is residence time in the atmosphere?",
        [ROCKS, WATER],
        "owner",
        generator=StandInGeneration(),
    )
    assert result.state is GrounderState.GROUNDED
    assert [(c.label, c.file, c.page_or_slide) for c in result.citations] == [
        ("S2", "water.pdf", 3)
    ]
    assert result.answer_prose.startswith("Residence time is how long")


def test_an_uncovered_question_is_refused_with_no_citations():
    result = answer(
        "What were the main causes of the French Revolution?",
        [ROCKS, WATER],
        "owner",
        generator=StandInGeneration(),
    )
    assert result.state is GrounderState.REFUSAL
    assert result.citations == []


def test_stand_in_model_ids_can_never_be_mistaken_for_the_real_ones(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-used")
    assert StandInEmbeddings.model_id != OpenAIEmbeddings().model_id
    assert StandInGeneration.model_id != OpenAIGeneration().model_id
