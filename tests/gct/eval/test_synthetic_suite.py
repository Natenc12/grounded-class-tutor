"""The whole synthetic suite, end to end, with the free stand-in models (issue #199).

The stand-ins match words; they do not read. So nothing here says anything about answer quality.
What it proves is that the machinery works on every pull request: the course ingests through the
real upload path, every question is asked against the real database, and every answer is scored.
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

from gct.eval.questions import load_questions
from gct.eval.runner import SuiteResult, load_course, run_suite
from gct.eval.scoring import Outcome
from gct.grounder.answer import GrounderState
from gct.providers.standin import StandInEmbeddings, StandInGeneration

SYNTHETIC = Path(__file__).resolve().parents[3] / "eval" / "synthetic"
TIME_LIMIT_SECONDS = 30  # #199's "Done when"; a local run takes well under a second


def run(db) -> tuple[SuiteResult, float]:
    conn, owner_id, _ = db
    started = time.perf_counter()
    suite = run_suite(
        conn,
        load_course(SYNTHETIC / "course"),
        load_questions(SYNTHETIC / "questions.jsonl"),
        owner_id=owner_id,
        embedder=StandInEmbeddings(),
        generator=StandInGeneration(),
    )
    return suite, time.perf_counter() - started


def test_the_whole_suite_runs_and_every_question_is_scored(db):
    suite, seconds = run(db)

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
    assert by_id["r001"].state is GrounderState.REFUSAL and by_id["r001"].passed
    # A fact that exists only in the OTHER class: refused, so classes stay separate.
    assert by_id["r022"].state is GrounderState.REFUSAL and by_id["r022"].passed
    # The stand-in answers the covered half and claims it is complete: the strict #199 rule
    # scores that a failure, even though the citation is right.
    assert by_id["p001"].state is GrounderState.GROUNDED
    assert by_id["p001"].outcome is Outcome.FAIL and by_id["p001"].citation_hit
    # The hijack page's marker word did not reach the answer.
    assert by_id["i001"].strings.forbidden_found == ()
