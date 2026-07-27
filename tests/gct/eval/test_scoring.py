"""Tests for ADR 0023's scoring rule (issue #8) - pure, offline, no DB and no provider.

Organised by the thing being defended:

  - `TestScoreStateTable` - all ten cells of ADR 0023 §2, exhaustively.
  - `TestScoreOnStateNeverCoverage` - issue #8's inherited warning, pinned twice: by behavior and
    by the function's SIGNATURE.
  - `TestRetrievalHit` - the any-match reading of §1, and the not-applicable `None`.
  - `TestComputeMetrics` - the denominator rules of §3: ERROR excluded and visible, PARTIAL kept
    in, zero denominators reported as "no data".
"""

from __future__ import annotations

import inspect

import pytest

from gct.eval.questions import ExpectedSource
from gct.eval.scoring import (
    EvalRecord,
    Outcome,
    compute_metrics,
    retrieval_hit,
    score_state,
)
from gct.grounder.answer import Coverage, GrounderResult, GrounderState, Integrity
from gct.retriever.retrieve import RetrievedChunk

# ADR 0023 §2's table, transcribed INDEPENDENTLY of the module's own dict. Writing the parameters
# out cell by cell is the point: importing `_SCORE_TABLE` and asserting it equals itself would
# pass for any table at all. This list is the ADR; the module must agree with it.
TABLE_CELLS = [
    # (expectation, state, outcome)
    ("answer", GrounderState.GROUNDED, Outcome.PASS),
    ("answer", GrounderState.PARTIAL, Outcome.TRACKED),
    ("answer", GrounderState.REFUSAL, Outcome.FAIL),
    ("answer", GrounderState.INTEGRITY_FLAGGED, Outcome.FAIL),
    ("answer", GrounderState.ERROR, Outcome.EXCLUDED),
    ("refuse", GrounderState.GROUNDED, Outcome.FAIL),
    ("refuse", GrounderState.PARTIAL, Outcome.FAIL),
    ("refuse", GrounderState.REFUSAL, Outcome.PASS),
    ("refuse", GrounderState.INTEGRITY_FLAGGED, Outcome.FAIL),
    ("refuse", GrounderState.ERROR, Outcome.EXCLUDED),
]


def chunk(file: str, page: int, score: float = 0.9) -> RetrievedChunk:
    """A RetrievedChunk with only the fields the hit predicate reads carrying meaning."""
    return RetrievedChunk(
        chunk_id=f"chunk-{file}-{page}", text="…", file=file, page_or_slide=page, score=score
    )


def records(expectation: str, **states: int) -> list[EvalRecord]:
    """`records("answer", GROUNDED=7, PARTIAL=2)` -> 9 records. Keyword = state name."""
    return [
        EvalRecord(expectation=expectation, state=GrounderState[name])
        for name, count in states.items()
        for _ in range(count)
    ]


class TestScoreStateTable:
    """ADR 0023 §2, cell by cell. Ten cells, ten assertions - no shortcuts."""

    @pytest.mark.parametrize(
        ("expectation", "state", "expected"),
        TABLE_CELLS,
        ids=[f"{exp}-{state.value}" for exp, state, _ in TABLE_CELLS],
    )
    def test_every_cell(self, expectation, state, expected):
        assert score_state(state, expectation) is expected

    def test_partial_in_corpus_is_neither_pass_nor_fail(self):
        """ADR 0023 §5's anti-erosion guardrail, as an assertion.

        Stated separately from the table row because the table would still pass if someone
        redefined `Outcome.TRACKED` to be an alias of PASS. What §5 protects is that PARTIAL is a
        THIRD thing: "if PARTIAL looks like an awkward third thing, that awkwardness is the point".
        """
        outcome = score_state(GrounderState.PARTIAL, "answer")

        assert outcome is Outcome.TRACKED
        assert outcome is not Outcome.PASS
        assert outcome is not Outcome.FAIL

    def test_error_is_excluded_on_both_sides_never_failed(self):
        """A provider outage is not a corpus verdict (ADR 0016) - scoring it as a FAIL would
        make an infra flake look like a grounding regression and mis-rank a bake-off."""
        assert score_state(GrounderState.ERROR, "answer") is Outcome.EXCLUDED
        assert score_state(GrounderState.ERROR, "refuse") is Outcome.EXCLUDED

    def test_unknown_expectation_raises(self):
        with pytest.raises(ValueError, match="unknown expectation"):
            score_state(GrounderState.GROUNDED, "maybe")

    def test_a_state_replayed_as_a_plain_string_scores_identically(self):
        """`GrounderState` subclasses `str`, so a run report reloaded from JSON scores without
        rehydrating the enum - what makes V3 re-scoring a stored V1 run possible."""
        assert score_state("GROUNDED", "answer") is Outcome.PASS


class TestScoreOnStateNeverCoverage:
    """Issue #8's inherited warning (from #6, PR #35): score on `state`, NEVER on `coverage`."""

    def test_refusal_with_complete_coverage_scores_on_state(self):
        """THE R6 PIN. A REFUSAL whose `coverage` reads `complete` must score as a REFUSAL.

        This is not a hypothetical shape. `grounder/answer.py::_decide` documents the one
        incoherent combination - `COVERAGE: complete` with nothing asserted and nothing cited -
        and routes it to REFUSAL while reporting the model's coverage statement UNREWRITTEN, so
        telemetry sees what the model actually said. The shipped Grounder genuinely emits this.

        A scorer that read `coverage.complete` would call that row a covered answer: a PASS on an
        in-corpus question the system actually declined, and a FAIL on the out-of-corpus question
        it correctly declined. Both directions are wrong, and both are silent - the run would post
        a better in-corpus pass rate AND a worse refusal rate off the same single misread. The
        rates are the artifact's whole output, so nothing downstream would catch it.
        """
        result = GrounderResult(
            state=GrounderState.REFUSAL,
            answer_prose=None,
            citations=[],
            coverage=Coverage(complete=True, gaps=[]),  # the incoherent-but-real combination
            integrity=Integrity(ok=True, reasons=[]),
            error=None,
        )

        assert result.coverage.complete is True  # the trap is really baited
        assert score_state(result.state, "refuse") is Outcome.PASS
        assert score_state(result.state, "answer") is Outcome.FAIL

    def test_score_state_cannot_be_handed_coverage(self):
        """The invariant as a SIGNATURE check, not just a behavior check.

        The test above proves today's implementation ignores coverage; this one proves coverage
        is not even in scope to read. Two parameters, `state` and `expectation` - widening this
        to take a `GrounderResult` would work identically at first and rot the moment someone
        reaches for another field, which is exactly how the warning describes the failure.
        """
        assert list(inspect.signature(score_state).parameters) == ["state", "expectation"]


class TestRetrievalHit:
    """ADR 0023 §1's retrieval signal: any-match on exact `(file, page_or_slide)`."""

    def test_multi_source_any_match_one_of_two_present(self):
        """q001's shape: two expected sources, one retrieved. ANY-match, so this is a HIT.

        The settled reading of §1's "was an `expected_sources` chunk in the retrieved top-k" -
        singular, phrased as membership. All-match was considered and rejected: it would couple
        one question's score to k (the very lever the spikes tune), and a missed second leg is
        not lost signal - it surfaces as PARTIAL in the grounding column instead.
        """
        expected = [
            ExpectedSource(file="Lecture 18 Cosmogony.pptx", page_or_slide=4),
            ExpectedSource(file="Lecture 19 Types of Cosmogony.pptx", page_or_slide=2),
        ]
        retrieved = [chunk("Lecture 18 Cosmogony.pptx", 4), chunk("Livingston Cosmogony.pdf", 4)]

        assert retrieval_hit(expected, retrieved) is True

    def test_multi_source_neither_present_is_a_miss(self):
        expected = [
            ExpectedSource(file="Lecture 18 Cosmogony.pptx", page_or_slide=4),
            ExpectedSource(file="Lecture 19 Types of Cosmogony.pptx", page_or_slide=2),
        ]
        retrieved = [chunk("Livingston Cosmogony.pdf", 4), chunk("Lecture 20 Debate.pptx", 2)]

        assert retrieval_hit(expected, retrieved) is False

    def test_empty_expected_sources_is_none_not_false(self):
        """A refuse question has nothing to retrieve, so the signal is NOT APPLICABLE.

        `False` would drag the four refuse questions into the recall denominator as permanent
        misses, capping the hit rate at 8/12 however good retrieval got - a metric that cannot
        reach 1.0 cannot rank anything, which is its only job.
        """
        assert retrieval_hit([], [chunk("Lecture 18 Cosmogony.pptx", 4)]) is None
        assert retrieval_hit([], []) is None

    def test_same_file_wrong_page_is_a_miss(self):
        """Exact equality on BOTH fields. A citation to the wrong page is wrong, not nearly
        right - the never-span honesty guarantee (ADR 0019) says so about the page number."""
        expected = [ExpectedSource(file="Lecture 18 Cosmogony.pptx", page_or_slide=4)]

        assert retrieval_hit(expected, [chunk("Lecture 18 Cosmogony.pptx", 5)]) is False

    def test_right_page_wrong_file_is_a_miss(self):
        expected = [ExpectedSource(file="Lecture 18 Cosmogony.pptx", page_or_slide=4)]

        assert retrieval_hit(expected, [chunk("Livingston Cosmogony.pdf", 4)]) is False

    def test_empty_retrieval_is_a_miss_not_none(self):
        """An empty class retrieved nothing, but the question DID expect a source: that is a
        measured miss, and it must stay in the denominator."""
        expected = [ExpectedSource(file="Lecture 18 Cosmogony.pptx", page_or_slide=4)]

        assert retrieval_hit(expected, []) is False

    def test_match_anywhere_in_the_top_k_not_just_rank_one(self):
        """§1 asks about membership in the top-k, not about the winning chunk."""
        expected = [ExpectedSource(file="Lecture 18 Cosmogony.pptx", page_or_slide=4)]
        retrieved = [
            chunk("a.pdf", 1, score=0.9),
            chunk("b.pdf", 2, score=0.8),
            chunk("Lecture 18 Cosmogony.pptx", 4, score=0.4),
        ]

        assert retrieval_hit(expected, retrieved) is True


class TestComputeMetrics:
    """ADR 0023 §3: primaries, secondaries over the SAME denominator, health kept visible."""

    def test_primaries_over_their_own_denominators(self):
        metrics = compute_metrics(
            records("answer", GROUNDED=6, PARTIAL=1, REFUSAL=1)
            + records("refuse", REFUSAL=3, GROUNDED=1)
        )

        assert metrics.answer.scored == 8
        assert metrics.refuse.scored == 4
        assert metrics.grounded_pass_rate == 6 / 8
        assert metrics.correct_refusal_rate == 3 / 4

    def test_raw_counts_are_exposed_beside_the_rates(self):
        """8 and 4 are tiny denominators - one question moves the primary by 12.5 or 25 points.
        A report that can only print "75%" invites reading noise as a result (ADR 0023 §4)."""
        metrics = compute_metrics(
            records("answer", GROUNDED=5, PARTIAL=2, INTEGRITY_FLAGGED=1)
            + records("refuse", REFUSAL=2, PARTIAL=1, INTEGRITY_FLAGGED=1)
        )

        assert (metrics.answer.grounded, metrics.answer.scored) == (5, 8)
        assert metrics.answer.partial == 2
        assert metrics.answer.integrity_flagged == 1
        assert (metrics.refuse.refusal, metrics.refuse.scored) == (2, 4)
        # The refuse-side FAIL decomposition: 1 PARTIAL = an assertion it should not have made,
        # 1 INTEGRITY_FLAGGED = a format defect. Only the first breaks the trust promise.
        assert metrics.refuse.hallucinated == 1
        assert metrics.refuse.integrity_flagged == 1
        assert metrics.hallucination_rate == 1 / 4

    def test_secondaries_share_the_primary_denominator(self):
        """§3: the secondaries ride "over the same `N_scored`". Different denominators would make
        the buckets non-additive and the distribution unreadable."""
        metrics = compute_metrics(records("answer", GROUNDED=4, PARTIAL=2, REFUSAL=1, ERROR=1))

        assert metrics.answer.scored == 7
        assert metrics.grounded_pass_rate == 4 / 7
        assert metrics.partial_rate == 2 / 7
        assert metrics.false_refusal_rate == 1 / 7
        # The three non-error buckets partition N_scored exactly.
        assert (
            metrics.answer.grounded
            + metrics.answer.partial
            + metrics.answer.refusal
            + metrics.answer.integrity_flagged
            == metrics.answer.scored
        )

    def test_error_is_excluded_from_the_denominator_and_still_reported(self):
        """§3's escape hatch, both halves: ERROR leaves N_scored AND stays visible.

        The shrinking denominator is the danger this reports against - 6 of 8 in-corpus questions
        erroring leaves a perfect 2/2 pass rate, which reads as a flawless run unless the error
        count is printed beside it.
        """
        metrics = compute_metrics(records("answer", GROUNDED=2, ERROR=6))

        assert metrics.answer.total == 8
        assert metrics.answer.scored == 2  # not 8 - the ERRORs are gone
        assert metrics.grounded_pass_rate == 1.0  # and it looks perfect
        assert metrics.answer.error == 6  # which is why THIS must be printed too
        assert metrics.error_count == 6
        assert metrics.error_rate == 6 / 8

    def test_fail_to_partial_trade_leaves_primary_flat(self):
        """THE ANTI-GAMING PROPERTY - ADR 0023 §3's own example, 70/20/10 vs 70/0/30.

        If PARTIAL were dropped from `N_scored`, trading a FAIL for a PARTIAL would SHRINK the
        denominator and raise `grounded_pass_rate` - rewarding a change that grounded nothing
        extra. Keeping PARTIAL in makes the primary flat while `partial_rate` moves: the shift
        is observable rather than buried, which is the whole reason the bucket exists.

        `INTEGRITY_FLAGGED` stands in for the FAIL bucket here; `REFUSAL` would read identically.
        """
        mixed = compute_metrics(records("answer", GROUNDED=70, PARTIAL=20, INTEGRITY_FLAGGED=10))
        harsh = compute_metrics(records("answer", GROUNDED=70, INTEGRITY_FLAGGED=30))

        # Same primary - the trade earns nothing.
        assert mixed.grounded_pass_rate == harsh.grounded_pass_rate == 0.7
        assert mixed.answer.scored == harsh.answer.scored == 100
        # The secondaries are what distinguish them (§3's closing sentence).
        assert (mixed.partial_rate, mixed.integrity_flag_rate) == (0.2, 0.1)
        assert (harsh.partial_rate, harsh.integrity_flag_rate) == (0.0, 0.3)

    def test_zero_denominator_rates_are_none_not_zero(self):
        """A scored-nothing run must not render as 0%.

        A run that errored on every question and a run that failed every question are opposite
        facts - one is an outage, the other is a regression - and only the second should stop a
        bake-off. `0.0` would make them identical in the report.
        """
        metrics = compute_metrics([])

        assert metrics.grounded_pass_rate is None
        assert metrics.correct_refusal_rate is None
        assert metrics.partial_rate is None
        assert metrics.retrieval_hit_rate is None
        assert metrics.error_rate is None
        assert metrics.answer.scored == metrics.refuse.scored == 0

    def test_all_errors_gives_none_primary_but_a_visible_error_count(self):
        """The strongest form of the case above: N_scored is 0, so there is no rate - but the
        report is not empty, it says every question errored."""
        metrics = compute_metrics(records("answer", ERROR=8) + records("refuse", ERROR=4))

        assert metrics.grounded_pass_rate is None
        assert metrics.correct_refusal_rate is None
        assert metrics.error_count == 12
        assert metrics.error_rate == 1.0

    def test_retrieval_rate_counts_measured_misses_and_skips_not_applicable(self):
        """The retrieval denominator is in-corpus questions with a MEASURED hit.

        `hit=False` is a measured miss and must stay in - a `if record.hit:` bug would drop every
        miss and drive the hit rate to 1.0. `hit=None` (a refuse row) must stay out.
        """
        metrics = compute_metrics(
            [
                EvalRecord("answer", GrounderState.GROUNDED, hit=True),
                EvalRecord("answer", GrounderState.GROUNDED, hit=True),
                EvalRecord("answer", GrounderState.REFUSAL, hit=False),
                EvalRecord("refuse", GrounderState.REFUSAL, hit=None),
            ]
        )

        assert metrics.retrieval.applicable == 3  # the refuse row is not applicable
        assert metrics.retrieval.hits == 2
        assert metrics.retrieval_hit_rate == 2 / 3

    def test_retrieval_signal_is_independent_of_the_final_state(self):
        """§1: retrieval misses surface "regardless of the final state", so an ERRORed row whose
        retrieval DID run still counts - excluding it would make the two signals depend on each
        other, which is the conflation the two-signal split exists to prevent."""
        metrics = compute_metrics(
            [
                EvalRecord("answer", GrounderState.ERROR, hit=True),
                EvalRecord("answer", GrounderState.GROUNDED, hit=True),
            ]
        )

        assert metrics.answer.scored == 1  # the ERROR is out of the GROUNDING denominator
        assert metrics.retrieval.applicable == 2  # but not out of the RETRIEVAL one
        assert metrics.retrieval_hit_rate == 1.0

    def test_unknown_expectation_raises_rather_than_being_dropped(self):
        """A dropped record would silently shrink a denominator - the exact failure mode this
        module is built to avoid. Fail loud instead."""
        with pytest.raises(ValueError, match="unknown expectation"):
            compute_metrics([EvalRecord("answers", GrounderState.GROUNDED)])
