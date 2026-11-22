"""The whole synthetic suite, end to end, with the free stand-in models (issue #199).

The stand-ins match words; they do not read. So nothing here says anything about answer quality.
What it proves is that the machinery works on every pull request: the course is saved through the
library's own save, every question is asked through the library (#230) with no Postgres, and every
answer is scored. The same course must give the same scores on every run, whatever order its files
are loaded in, or a later recording of real replies (#198) could not be replayed.
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

from gct.eval.questions import load_questions
from gct.eval.runner import CourseFile, SuiteResult, load_course, run_suite
from gct.eval.scoring import GroundingCounts, Outcome
from gct.grounder.answer import GrounderState
from gct.providers.standin import StandInGeneration

SYNTHETIC = Path(__file__).resolve().parents[3] / "eval" / "synthetic"
TIME_LIMIT_SECONDS = 30  # #199's "Done when"; a local run takes well under a second


def run(course: list[CourseFile] | None = None) -> tuple[SuiteResult, float]:
    started = time.perf_counter()
    suite = run_suite(
        load_course(SYNTHETIC / "course") if course is None else course,
        load_questions(SYNTHETIC / "questions.jsonl"),
        generator=StandInGeneration(),
    )
    return suite, time.perf_counter() - started


def test_the_whole_suite_runs_and_every_question_is_scored():
    suite, seconds = run()

    assert seconds < TIME_LIMIT_SECONDS
    assert suite.total == 100
    assert not [r.question_id for r in suite.results if r.state is GrounderState.ERROR]
    assert Counter(r.expectation for r in suite.results) == {
        "answer": 60,
        "partial": 10,
        "refuse": 30,
    }
    metrics = suite.metrics
    assert (metrics.answer.total, metrics.partial_expected.total, metrics.refuse.total) == (
        60,
        10,
        30,
    )
    assert metrics.error_count == 0
    hits, applicable = suite.citation_hits
    assert applicable == 70 and 0 <= hits <= applicable
    assert suite.attribution.answers_measured + suite.attribution.answers_unmeasured == 100

    by_id = {r.question_id: r for r in suite.results}
    # Hand-checked outcomes. A plain one-page fact, quoted and cited: passes.
    assert by_id["s013"].passed and by_id["s013"].citation_hit
    # Common knowledge the course never states: refused.
    assert by_id["r003"].state is GrounderState.REFUSAL and by_id["r003"].passed
    # A fact that exists only in the OTHER class: refused, so classes stay separate.
    assert by_id["r022"].state is GrounderState.REFUSAL and by_id["r022"].passed
    # The stand-in answers the covered half and claims it is complete: the strict #199 rule
    # scores that a failure, even though the citation is right.
    assert by_id["p001"].state is GrounderState.GROUNDED
    assert by_id["p001"].outcome is Outcome.FAIL and by_id["p001"].citation_hit
    # The hijack page's marker word did not reach the answer.
    assert by_id["i001"].strings.forbidden_found == ()


PINNED = (
    "These stand-in totals are not a quality bar: the stand-in only matches words. They changed "
    "because search or the stand-in changed. If that was intended, update the numbers in the same "
    "pull request and say why in its description."
)


def counts(grounded: int, refusal: int, total: int) -> GroundingCounts:
    return GroundingCounts(
        total=total,
        scored=total,
        grounded=grounded,
        partial=0,
        refusal=refusal,
        integrity_flagged=0,
        error=0,
    )


def test_the_stand_in_totals_are_pinned_so_any_change_shows_up():
    suite, _ = run()

    assert (suite.passed, suite.total) == (61, 100), PINNED
    assert suite.citation_hits == (63, 70), PINNED
    assert suite.metrics.answer == counts(grounded=55, refusal=5, total=60), PINNED
    assert suite.metrics.partial_expected == counts(grounded=8, refusal=2, total=10), PINNED
    assert suite.metrics.refuse == counts(grounded=12, refusal=18, total=30), PINNED


def test_the_same_course_scores_the_same_in_any_load_order():
    course = load_course(SYNTHETIC / "course")
    first, _ = run(course)
    again, _ = run(course)
    reversed_order, _ = run(course[::-1])

    assert again == first
    assert reversed_order == first
