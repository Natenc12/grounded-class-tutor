from __future__ import annotations

from gct.contracts import RetrievedChunk
from gct.grounder.answer import GrounderState, answer
from gct.providers.claude import MODEL_ENV, ClaudeGeneration
from gct.providers.standin import StandInGeneration

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
    for model in (None, "sonnet", "opus"):
        if model is None:
            monkeypatch.delenv(MODEL_ENV, raising=False)
        else:
            monkeypatch.setenv(MODEL_ENV, model)
        assert StandInGeneration.model_id != ClaudeGeneration().model_id
    assert not StandInGeneration.model_id.startswith("claude")
