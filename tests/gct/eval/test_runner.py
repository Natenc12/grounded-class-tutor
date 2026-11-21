"""The pure half of the eval runner and the scoring it adds (issue #199). No database, no models."""

from __future__ import annotations

import pytest

from gct.ask import AskResult
from gct.eval import scoring
from gct.eval.questions import EvalQuestion, ExpectedSource
from gct.eval.runner import CourseFile, score_answers
from gct.eval.scoring import (
    EvalRecord,
    Outcome,
    check_strings,
    citation_hit,
    compute_metrics,
    score_state,
)
from gct.grounder.answer import Citation, Coverage, GrounderResult, GrounderState, Integrity
from gct.retriever.retrieve import RetrievedChunk

PAGE = ExpectedSource(file="Lecture 02 Founding.pdf", page_or_slide=1)


def question(
    qid: str,
    expectation: str,
    *,
    sources: tuple[ExpectedSource, ...] = (PAGE,),
    required: tuple[str, ...] = (),
    forbidden: tuple[str, ...] = (),
) -> EvalQuestion:
    return EvalQuestion(
        id=qid,
        question="Who founded the League, and when?",
        class_slug="his-140",
        expectation=expectation,
        expected_sources=list(sources),
        answer_notes="",
        suites=["synthetic"],
        tags=[],
        added="2026-11-21",
        required=list(required),
        forbidden=list(forbidden),
    )


def asked(
    state: GrounderState,
    prose: str | None = None,
    cited: tuple[tuple[str, int], ...] = (),
    retrieved: tuple[tuple[str, int], ...] = (),
    retrieval_ran: bool = True,
) -> AskResult:
    return AskResult(
        result=GrounderResult(
            state=state,
            answer_prose=prose,
            citations=[
                Citation(label=f"S{i}", file=f, page_or_slide=p, chunk_id=f"c{i}")
                for i, (f, p) in enumerate(cited, start=1)
            ],
            coverage=Coverage(complete=state is GrounderState.GROUNDED, gaps=[]),
            integrity=Integrity(ok=True, reasons=[]),
            error=None,
        ),
        retrieved=[
            RetrievedChunk(chunk_id=f"r{i}", text="t", file=f, page_or_slide=p, score=0.5)
            for i, (f, p) in enumerate(retrieved, start=1)
        ],
        retrieval_ran=retrieval_ran,
    )


class TestPartialExpectation:
    def test_only_a_partial_answer_passes(self):
        assert score_state(GrounderState.PARTIAL, "partial") is Outcome.PASS
        assert score_state(GrounderState.REFUSAL, "partial") is Outcome.FAIL
        assert score_state(GrounderState.INTEGRITY_FLAGGED, "partial") is Outcome.FAIL
        assert score_state(GrounderState.ERROR, "partial") is Outcome.EXCLUDED

    def test_a_full_answer_that_hides_the_gap_fails(self):
        """The owner's #199 default (strict). Flip it in ONE place:
        `scoring.FULL_ANSWER_ON_PARTIAL_QUESTION`; this test pins the current choice."""
        assert scoring.FULL_ANSWER_ON_PARTIAL_QUESTION is Outcome.FAIL
        assert score_state(GrounderState.GROUNDED, "partial") is Outcome.FAIL

    def test_partial_rows_get_their_own_tally_and_leave_the_others_alone(self):
        metrics = compute_metrics(
            [
                EvalRecord("partial", GrounderState.PARTIAL, hit=True),
                EvalRecord("partial", GrounderState.GROUNDED, hit=False),
                EvalRecord("partial", GrounderState.ERROR, hit=None),
                EvalRecord("answer", GrounderState.GROUNDED, hit=True),
            ]
        )
        p = metrics.partial_expected
        assert (p.total, p.scored, p.partial, p.grounded, p.error) == (3, 2, 1, 1, 1)
        assert (metrics.answer.total, metrics.refuse.total) == (1, 0)
        assert metrics.retrieval.applicable == 3  # partial rows are in-corpus for their half
        assert metrics.error_count == 1

    def test_runs_without_partial_rows_report_an_empty_partial_tally(self):
        metrics = compute_metrics([EvalRecord("answer", GrounderState.GROUNDED, hit=True)])
        assert metrics.partial_expected.total == 0


class TestCitationHit:
    def test_not_applicable_without_expected_pages(self):
        assert citation_hit([], []) is None

    def test_any_cited_expected_page_is_a_hit(self):
        cite = Citation(label="S1", file=PAGE.file, page_or_slide=1, chunk_id="c")
        other = Citation(label="S2", file=PAGE.file, page_or_slide=2, chunk_id="d")
        assert citation_hit([PAGE], [other, cite]) is True
        assert citation_hit([PAGE], [other]) is False


class TestCheckStrings:
    def test_case_insensitive_required_and_forbidden(self):
        check = check_strings("Marra Vell founded it. pelican", ["marra vell", "year"], ["PELICAN"])
        assert check.missing_required == ("year",)
        assert check.forbidden_found == ("PELICAN",)
        assert not check.ok

    @pytest.mark.parametrize("prose", ["The limit is 1500 mg.", "It is 1,500.", "Fifty-0.50"])
    def test_a_number_inside_a_bigger_number_does_not_count(self, prose):
        """Review of PR #215: a required "50" (question i002) was met by "1500"."""
        assert check_strings(prose, ["50"], []).missing_required == ("50",)

    def test_terms_match_on_word_and_number_boundaries(self):
        check = check_strings("The limit is 50 mg; it holds 41.6.", ["50", "41.6", "limit"], [])
        assert check.ok
        assert check_strings("Pelicans nest here.", [], ["PELICAN"]).forbidden_found == ()

    def test_no_prose_misses_every_required_word_and_trips_no_forbidden_one(self):
        check = check_strings(None, ["Vell"], ["PELICAN"])
        assert check.missing_required == ("Vell",) and check.forbidden_found == ()


class TestScoreAnswers:
    def test_partial_question_answered_partially_and_cited_passes(self):
        q = question("p001", "partial", required=("Marra Vell",))
        suite = score_answers(
            [q],
            {
                "p001": asked(
                    GrounderState.PARTIAL,
                    "Marra Vell founded it [S1]. The year is not given.",
                    cited=((PAGE.file, 1),),
                    retrieved=((PAGE.file, 1),),
                )
            },
        )
        (result,) = suite.results
        assert result.passed and result.citation_hit and result.retrieval_hit
        assert (suite.passed, suite.total, suite.citation_hits) == (1, 1, (1, 1))

    def test_a_forbidden_word_fails_an_otherwise_passing_answer(self):
        q = question("i001", "answer", forbidden=("PELICAN",))
        suite = score_answers(
            [q],
            {"i001": asked(GrounderState.GROUNDED, "Selby [S1]. PELICAN", cited=((PAGE.file, 1),))},
        )
        (result,) = suite.results
        assert result.outcome is Outcome.PASS and not result.passed
        assert suite.forbidden_violations == 1

    def test_retrieval_that_never_ran_is_not_a_miss(self):
        q = question("s001", "answer")
        suite = score_answers([q], {"s001": asked(GrounderState.ERROR, retrieval_ran=False)})
        assert suite.results[0].retrieval_hit is None
        assert suite.metrics.retrieval.applicable == 0

    def test_a_question_with_no_answer_is_an_error_not_a_skip(self):
        with pytest.raises(ValueError, match="no answer for question"):
            score_answers([question("s001", "answer")], {})


def test_course_file_pages_become_units_numbered_from_one():
    units = CourseFile(class_slug="c", file="f.pdf", pages=("one", "two")).units()
    assert [(u.file, u.page_or_slide, u.text) for u in units] == [
        ("f.pdf", 1, "one"),
        ("f.pdf", 2, "two"),
    ]
