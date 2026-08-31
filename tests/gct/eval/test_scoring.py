"""Tests for ADR 0023's scoring rule (issue #8) - pure, offline, no DB and no provider.

Organised by the thing being defended:

  - `TestScoreStateTable` - all ten cells of ADR 0023 §2, exhaustively.
  - `TestScoreOnStateNeverCoverage` - issue #8's inherited warning, pinned twice: by behavior and
    by the function's SIGNATURE.
  - `TestRetrievalHit` - the any-match reading of §1, and the not-applicable `None`.
  - `TestComputeMetrics` - the denominator rules of §3: ERROR excluded and visible, PARTIAL kept
    in, zero denominators reported as "no data".
  - `TestExpectedPlacement` - issue #67's DIAGNOSTIC: where the expected source landed inside the
    top-k, and the margin over the next result. Two things are defended there above all: that
    `None` and `0.0` never trade places (undefined vs measured), and that the diagnostic stays
    OUT of ADR 0023's metric vector.
  - `TestSplitSentences` / `TestMeasureAttribution` / `TestAggregateAttribution` - issue #66's
    DIAGNOSTIC: the crude sentence split behind `uncited_sentence_rate`, its known misfires
    pinned as known, and the same `None`-vs-`0.0` line drawn one layer down (nothing measured vs
    measured-and-fully-cited).
  - `TestAttributionIsNotTheVector` - #66's "reported, never enforced" claim, made mechanical.
  - `TestAttributionAcrossTheFiveStates` - the same claim driven through the REAL `answer()` on a
    scripted stub, which is also the only place the coverage-statement exclusion can be pinned:
    it belongs to `grounder/answer.py::_parse`, not to anything in this module.
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Sequence

import pytest

from gct.eval import scoring
from gct.eval.questions import ExpectedSource
from gct.eval.scoring import (
    AnswerAttribution,
    AttributionTotals,
    EvalMetrics,
    EvalRecord,
    ExpectedPlacement,
    Outcome,
    SentenceCitation,
    aggregate_attribution,
    compute_metrics,
    expected_placement,
    measure_attribution,
    retrieval_hit,
    score_state,
    split_sentences,
)
from gct.grounder import answer as grounder_answer
from gct.grounder.answer import Coverage, GrounderResult, GrounderState, Integrity
from gct.providers.base import Message, TransientGenerationError
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


# Issue #67's own measurement (2026-08-01), transcribed. The q005 top-5 is the reference case for
# every rank/margin assertion below: the expected slide ranks THIRD behind two copies of a
# different author's critique, and `retrieval_hit` reports that as a clean `hit=yes`.
LECTURE = "Lecture 20 Evolutionist-Creationist Debate.pptx"
LIVINGSTON = "Livingston Cosmogony.pdf"
Q005_TOP_FIVE = [
    chunk(LIVINGSTON, 13, score=0.6189),
    chunk(LIVINGSTON, 13, score=0.5820),
    chunk(LECTURE, 2, score=0.5648),
    chunk(LIVINGSTON, 13, score=0.5432),
    chunk(LIVINGSTON, 13, score=0.5364),
]
Q005_EXPECTED = [ExpectedSource(file=LECTURE, page_or_slide=2)]


class TestExpectedPlacement:
    """Issue #67: the sentence `hit=yes` structurally cannot say.

    `retrieval_hit` is an ANY-MATCH membership rule (ADR 0023 §1), so a hit says the expected
    chunk is SOMEWHERE in the top-k and nothing about what outranks it. These tests defend the
    three definitional choices that make the readout honest: the margin reading (headroom, not
    deficit - ADR 0026 §2), rank-as-handed (never a re-sort), and the `None` / `0.0` split.
    """

    def test_the_q005_top_five_ranks_the_expected_slide_third(self):
        """THE reference case, from issue #67's own hand-run probe.

        0.5648 - 0.5432 = 0.0216. A margin defined the other way round (deficit behind the
        leader) would report 0.6189 - 0.5648 = 0.0541 here, which is why this literal is checked
        rather than a relation.
        """
        placement = expected_placement(Q005_EXPECTED, Q005_TOP_FIVE)

        assert placement is not None
        assert placement.ranks == (3,)
        assert placement.rank == 3
        assert placement.total == 5
        assert placement.found is True
        assert placement.margin == pytest.approx(0.0216)

    def test_expected_at_rank_one_still_has_a_margin(self):
        """ADR 0026 §2's q007 shape - and rank 1 is not a special case.

        The competing "deficit behind the leader" reading is UNDEFINED here (it would report
        0.0, indistinguishable from a tie), and rank 1 is precisely the case ADR 0026 wanted a
        margin for: "'Ranks first' is supported; 'has headroom' is not."
        """
        retrieved = [
            chunk(LECTURE, 2, score=0.6189),
            chunk(LIVINGSTON, 13, score=0.5820),
            chunk(LIVINGSTON, 13, score=0.5648),
        ]

        placement = expected_placement(Q005_EXPECTED, retrieved)

        assert placement.ranks == (1,)
        assert placement.margin == pytest.approx(0.0369)

    def test_absent_expected_source_has_no_rank_and_no_margin(self):
        """A measured miss: we looked at all five rows and it was not there.

        No rank, no margin, and NO fabricated zero - there is no row to have a margin from, and
        `0.0` would be a figure nobody measured. `total` still reports what was searched.
        """
        placement = expected_placement(
            [ExpectedSource(file="Lecture 18 Cosmogony.pptx", page_or_slide=4)], Q005_TOP_FIVE
        )

        assert placement is not None
        assert placement.ranks == ()
        assert placement.rank is None
        assert placement.margin is None
        assert placement.found is False
        assert placement.total == 5

    def test_expected_source_appearing_twice_anchors_on_the_best_rank_and_reports_both(self):
        """Both positions are carried. Anchoring on the best rank and printing "rank 2" alone
        would repeat this issue's own sin - one summary number hiding what the top-k contained."""
        retrieved = [
            chunk(LIVINGSTON, 13, score=0.6189),
            chunk(LECTURE, 2, score=0.5820),
            chunk(LIVINGSTON, 13, score=0.5648),
            chunk(LECTURE, 2, score=0.5432),
            chunk(LIVINGSTON, 13, score=0.5364),
        ]

        placement = expected_placement(Q005_EXPECTED, retrieved)

        assert placement.ranks == (2, 4)
        assert placement.rank == 2  # the BEST rank, not the last one seen
        assert placement.margin == pytest.approx(0.0172)  # measured from rank 2, not rank 4

    def test_two_different_expected_sources_both_present_report_both_ranks(self):
        """q003's multi-source shape. `ranks` is over the SET of expected sources, so two
        different expected pages landing at 1 and 3 is one placement with two ranks."""
        expected = [
            ExpectedSource(file="Lecture 18 Cosmogony.pptx", page_or_slide=4),
            ExpectedSource(file="Lecture 19 Types of Cosmogony.pptx", page_or_slide=2),
        ]
        retrieved = [
            chunk("Lecture 18 Cosmogony.pptx", 4, score=0.70),
            chunk(LIVINGSTON, 13, score=0.60),
            chunk("Lecture 19 Types of Cosmogony.pptx", 2, score=0.50),
        ]

        placement = expected_placement(expected, retrieved)

        assert placement.ranks == (1, 3)
        assert placement.margin == pytest.approx(0.10)

    def test_expected_at_the_last_rank_has_no_margin(self):
        """`None`, NEVER `0.0`. There is no rank+1, so the margin is UNDEFINED - and `+0.0000`
        would read as "tied with the next row", a measurement that was never made. Same argument
        `_rate` carries about an empty denominator, one layer down."""
        retrieved = [
            chunk(LIVINGSTON, 13, score=0.6189),
            chunk(LIVINGSTON, 13, score=0.5820),
            chunk(LECTURE, 2, score=0.5648),
        ]

        placement = expected_placement(Q005_EXPECTED, retrieved)

        assert placement.ranks == (3,)
        assert placement.margin is None

    def test_a_single_result_that_is_the_expected_one(self):
        """The one-result case is the last-row case at k=1 - found, ranked, no margin."""
        placement = expected_placement(Q005_EXPECTED, [chunk(LECTURE, 2, score=0.7)])

        assert placement.ranks == (1,)
        assert placement.total == 1
        assert placement.margin is None
        assert placement.found is True

    def test_a_single_result_that_is_not_the_expected_one(self):
        """The other half of the one-row case: searched one row, missed."""
        placement = expected_placement(Q005_EXPECTED, [chunk(LIVINGSTON, 13, score=0.7)])

        assert placement.ranks == ()
        assert placement.total == 1
        assert placement.margin is None
        assert placement.found is False

    def test_empty_retrieval_is_a_measured_miss_not_not_applicable(self):
        """An empty/un-ingested class retrieved nothing, but the question DID expect a source.
        That is a measured miss, exactly as `retrieval_hit` scores it - not "not applicable"."""
        placement = expected_placement(Q005_EXPECTED, [])

        assert placement is not None  # NOT the not-applicable None
        assert placement.ranks == ()
        assert placement.total == 0
        assert placement.margin is None
        assert retrieval_hit(Q005_EXPECTED, []) is False

    @pytest.mark.parametrize(
        "retrieved", [[], Q005_TOP_FIVE], ids=["empty-retrieval", "populated-retrieval"]
    )
    def test_empty_expected_sources_returns_none_not_an_empty_placement(self, retrieved):
        """THE TRI-STATE MIRROR of `retrieval_hit`. An out-of-corpus row has NO EXPECTATION
        (ADR 0021 §3), which is a different fact from "we looked and missed".

        Returning an empty placement would collapse the two: a caller would then print "ABSENT"
        for a row that was never looking for anything. Inside the object, `rank is None` means
        the second thing, and only the second.
        """
        assert expected_placement([], retrieved) is None
        assert retrieval_hit([], retrieved) is None

    def test_tied_scores_give_a_measured_zero_margin_never_none(self):
        """The EXACT MIRROR of the last-row test - the pair IS the definition.

        Two identical scores is a real shape (`retrieve()` orders by distance with no tiebreak),
        and the honest report is `0.0`: a measurement that came out zero. Returning `None` here
        would say "undefined" about a difference that was computed.
        """
        retrieved = [
            chunk(LIVINGSTON, 13, score=0.6189),
            chunk(LECTURE, 2, score=0.5820),
            chunk(LIVINGSTON, 13, score=0.5820),
        ]

        placement = expected_placement(Q005_EXPECTED, retrieved)

        assert placement.margin == 0.0
        assert placement.margin is not None

    def test_two_clamped_zero_scores_do_not_break_the_margin(self):
        """`0.0` is a legitimate SCORE, not just a legitimate margin: ADR 0024 clamps a negative
        cosine to 0, so a bottom-of-the-list pair can genuinely both read 0.0000."""
        retrieved = [
            chunk(LIVINGSTON, 13, score=0.4),
            chunk(LECTURE, 2, score=0.0),
            chunk(LIVINGSTON, 13, score=0.0),
        ]

        placement = expected_placement(Q005_EXPECTED, retrieved)

        assert placement.ranks == (2,)
        assert placement.margin == 0.0

    def test_rank_is_position_in_the_handed_list_never_a_re_sort(self):
        """`AskResult.retrieved` is contractually "the EXACT top-k list handed to the Grounder,
        in rank order", so rank is POSITION AS HANDED. Re-sorting would report a rank the
        Grounder never saw.

        The scores here ASCEND and the expected row is handed FIRST with the LOWEST score, so a
        "sort by score first" implementation reports rank 3 instead of rank 1 - and the margin
        flips sign with it. The expected row must not sit at a position a re-sort would leave
        alone: an earlier draft of this test put it in the middle of three rows, where rank 2 is
        rank 2 either way, and the mutation ran GREEN.
        """
        retrieved = [
            chunk(LECTURE, 2, score=0.10),
            chunk(LIVINGSTON, 13, score=0.50),
            chunk(LIVINGSTON, 13, score=0.90),
        ]

        placement = expected_placement(Q005_EXPECTED, retrieved)

        assert placement.ranks == (1,)
        assert placement.margin == pytest.approx(-0.40)  # 0.10 - 0.50, not 0.90 - 0.50

    def test_a_negative_margin_is_reported_signed_not_clamped(self):
        """A negative margin means the handed list was not score-ordered - an upstream contract
        violation, and worth surfacing rather than smoothing to 0."""
        retrieved = [
            chunk(LIVINGSTON, 13, score=0.10),
            chunk(LECTURE, 2, score=0.50),
            chunk(LIVINGSTON, 13, score=0.90),
        ]

        placement = expected_placement(Q005_EXPECTED, retrieved)

        assert placement.margin == pytest.approx(-0.40)

    @pytest.mark.parametrize("count", [3, 7], ids=["shorter-than-k", "longer-than-k"])
    def test_total_is_the_list_length_and_the_function_never_sees_k(self, count):
        """`k` is not a parameter. `total` is defined over the sequence handed in, so a short
        list (normal - `retrieve()` returns <= k) and an over-long one (an upstream contract
        violation) both render rather than raising or being silently truncated."""
        assert "k" not in inspect.signature(expected_placement).parameters

        retrieved = [chunk(LIVINGSTON, 13, score=0.9 - i / 100) for i in range(count)]
        retrieved[1] = chunk(LECTURE, 2, score=retrieved[1].score)

        placement = expected_placement(Q005_EXPECTED, retrieved)

        assert placement.total == count
        assert placement.ranks == (2,)

    @pytest.mark.parametrize(
        ("expected", "retrieved"),
        [
            (Q005_EXPECTED, Q005_TOP_FIVE),
            (Q005_EXPECTED, []),
            (Q005_EXPECTED, [chunk(LIVINGSTON, 13)]),
            (Q005_EXPECTED, [chunk(LECTURE, 2)]),
            (Q005_EXPECTED, [chunk(LECTURE, 3)]),
            ([], []),
            ([], Q005_TOP_FIVE),
            (
                [
                    ExpectedSource(file="Lecture 18 Cosmogony.pptx", page_or_slide=4),
                    ExpectedSource(file=LECTURE, page_or_slide=2),
                ],
                Q005_TOP_FIVE,
            ),
        ],
        ids=[
            "q005-reference",
            "empty-retrieval",
            "single-miss",
            "single-hit",
            "wrong-page",
            "no-expectation-no-results",
            "no-expectation-with-results",
            "multi-source-one-leg-hits",
        ],
    )
    def test_placement_and_retrieval_hit_never_disagree(self, expected, retrieved):
        """THE ANTI-SECOND-WRITER PIN. Both readouts answer "is an expected source in this
        top-k", and `_expected_pairs` exists so they answer it from ONE definition.

        This is the assertion that would catch the two drifting apart - the failure this
        module's docstring names about the V1/V3 split, in miniature: a bench printing
        `hit=yes` above a block saying ABSENT would be worse than printing neither.
        """
        placement = expected_placement(expected, retrieved)
        hit = retrieval_hit(expected, retrieved)

        assert (placement is None) == (hit is None)
        if placement is not None:
            assert placement.found == (hit is True)

    def test_same_file_wrong_page_and_right_page_wrong_file_are_both_misses(self):
        """Exact equality on BOTH fields survived the `_expected_pairs` extraction. A citation to
        the wrong page is wrong, not nearly right (the never-span guarantee, ADR 0019)."""
        expected = [ExpectedSource(file="Lecture 18 Cosmogony.pptx", page_or_slide=4)]

        same_file = expected_placement(expected, [chunk("Lecture 18 Cosmogony.pptx", 5)])
        same_page = expected_placement(expected, [chunk(LIVINGSTON, 4)])

        assert same_file.found is False
        assert same_page.found is False

    @pytest.mark.parametrize(
        "ranks", [(), (1,), (3,), (2, 4)], ids=["none", "first", "third", "twice"]
    )
    def test_rank_and_found_cannot_disagree_with_ranks(self, ranks):
        """`rank` and `found` are PROPERTIES over `ranks`, not stored fields - the same
        construction `EvalMetrics` uses for its rates, so no reported figure can disagree with
        the tally it came from."""
        placement = ExpectedPlacement(ranks=ranks, total=5, margin=None)

        assert placement.rank == (ranks[0] if ranks else None)
        assert placement.found is bool(ranks)

    def test_the_placement_object_is_frozen(self):
        """Frozen like every other object in this module: a diagnostic a caller can edit is a
        diagnostic that can disagree with the run it describes."""
        placement = expected_placement(Q005_EXPECTED, Q005_TOP_FIVE)

        with pytest.raises(dataclasses.FrozenInstanceError):
            placement.ranks = (1,)

    def test_expected_placement_is_absent_from_the_adr_0023_vector(self):
        """ISSUE #67'S "REPORTING ONLY" CLAIM, MADE MECHANICAL rather than asserted in a PR body.

        The diagnostic must not leak into the scored path. Three checks, because there are three
        ways it could: a field on the record the metrics are computed from, a new rate property,
        or a changed figure out of `compute_metrics` itself. ADR 0023 §3 fixes this vector; a
        rank-derived rate would be a new ranking signal smuggled in as a readout.
        """
        assert [f.name for f in dataclasses.fields(EvalRecord)] == ["expectation", "state", "hit"]

        rates = {
            name
            for name, attr in vars(EvalMetrics).items()
            if isinstance(attr, property) and not name.startswith("_")
        }
        assert rates == {
            "grounded_pass_rate",
            "correct_refusal_rate",
            "partial_rate",
            "integrity_flag_rate",
            "false_refusal_rate",
            "hallucination_rate",
            "refuse_integrity_flag_rate",
            "error_count",
            "error_rate",
            "retrieval_hit_rate",
        }

        metrics = compute_metrics(
            [
                EvalRecord("answer", GrounderState.GROUNDED, hit=True),
                EvalRecord("answer", GrounderState.PARTIAL, hit=True),
                EvalRecord("answer", GrounderState.REFUSAL, hit=False),
                EvalRecord("answer", GrounderState.ERROR, hit=None),
                EvalRecord("refuse", GrounderState.REFUSAL, hit=None),
                EvalRecord("refuse", GrounderState.GROUNDED, hit=None),
            ]
        )

        assert (metrics.answer.total, metrics.answer.scored) == (4, 3)
        assert (metrics.refuse.total, metrics.refuse.scored) == (2, 2)
        assert metrics.grounded_pass_rate == 1 / 3
        assert metrics.partial_rate == 1 / 3
        assert metrics.false_refusal_rate == 1 / 3
        assert metrics.correct_refusal_rate == 1 / 2
        assert metrics.hallucination_rate == 1 / 2
        assert (metrics.retrieval.applicable, metrics.retrieval.hits) == (3, 2)
        assert metrics.retrieval_hit_rate == 2 / 3
        assert metrics.error_count == 1
        assert metrics.error_rate == 1 / 6


# --- Issue #66: the attribution diagnostic ----------------------------------------------------

# The q008 RECONSTRUCTION. `eval/FINDINGS.md` (2026-07-27) elides two of the four sentences with
# `…`, so the model's real reply is UNRECOVERABLE without re-asking q008 — which costs money and
# is not authorized. This string is consistent with every fragment FINDINGS did record (sentences
# 1-2 verbatim; sentence 3's tail and sentence 4's middle rebuilt from the entailment spot-check
# in the same section), and it reproduces the probe's NUMBERS: 4 sentences, 3 uncited, {S1, S3}.
# Read it as a reconstruction, never as a captured artifact.
Q008_PROSE = (
    "One of the oldest and most widespread religious symbols, according to Charles H. Long, is "
    "the symbol of Mother Earth. This symbolism arises because of the homology between creation "
    "and woman; the woman bears children and experiences the mysteries of birth, growth, and "
    "change. The earth itself is likened to a woman, and the process of creation to birth "
    "[S1][S3]."
)


class TestSplitSentences:
    """The crude rule, and every misfire it is KNOWN to make, pinned as known.

    A misfire pinned by a test is a documented artifact; the same misfire undocumented is a defect
    waiting to be re-derived by whoever next reads a rate that looks a sentence off. None of these
    is a bug to fix: ADR 0015 ruled a smart splitter out of the ENFORCEMENT ladder, and issue #66
    mandates the crude one for measurement precisely because measuring cannot misfire into a
    false failure.
    """

    def test_empty_and_whitespace_prose_split_into_nothing(self):
        """`[]`, never `[""]`. An empty fragment is not a sentence, and one in the denominator
        would be a guaranteed-uncited row nobody wrote."""
        assert split_sentences("") == []
        assert split_sentences("   \n\t  \n ") == []

    def test_prose_with_no_terminal_punctuation_is_one_sentence(self):
        assert split_sentences("The earth is a woman [S1]") == ["The earth is a woman [S1]"]

    def test_a_terminator_plus_whitespace_splits(self):
        assert split_sentences("A. B.") == ["A.", "B."]

    @pytest.mark.parametrize("terminator", [".", "!", "?"])
    def test_all_three_terminators_split(self, terminator):
        """`!` and `?` are terminators too — an answer that asks a rhetorical question and then
        cites the answer must not read as one fused sentence."""
        assert split_sentences(f"First{terminator} Second.") == [f"First{terminator}", "Second."]

    def test_the_whitespace_class_spans_newlines(self):
        r"""`\s+` covers a newline, so a paragraph break after a terminator splits like a space.
        This is NOT a newline RULE — a newline with no terminator before it does not split, which
        is what fuses a heading into the block below it."""
        assert split_sentences("First one.\n\nSecond one.") == ["First one.", "Second one."]

    def test_a_heading_and_bullets_with_no_terminators_fuse_into_one_sentence(self):
        """THE BIGGEST UNDER-SPLIT, pinned deliberately. A markdown-ish block with no terminal
        punctuation is ONE crude sentence, so a single `[S#]` anywhere in it marks the whole block
        cited. Exactly the "lists, headings" misfire ADR 0015 named — here it costs a number, not
        a verdict."""
        block = "Cosmogony\n- creation from nothing [S1]\n- creation from chaos"

        assert split_sentences(block) == [block]

    def test_a_decimal_point_does_not_split(self):
        """The whitespace requirement is load-bearing: `[.!?]` alone would cut `0.85` in half."""
        assert split_sentences("The score is 0.85 [S1].") == ["The score is 0.85 [S1]."]

    def test_charles_h_long_splits_in_two(self):
        """THE q008 MISFIRE ITSELF, pinned as known-wrong. "Charles H. Long" is one name and the
        crude rule makes it two sentences — and the reference case this whole issue was filed on
        is built on top of that misfire. Recording it is more honest than tuning it away; a
        capital-letter rule would fix this row and break the numbered-list rows below."""
        assert split_sentences("According to Charles H. Long, it is a symbol.") == [
            "According to Charles H.",
            "Long, it is a symbol.",
        ]

    def test_a_numbered_list_turns_its_numbers_into_sentences(self):
        """`1.` and `2.` become one-token sentences, each necessarily uncited. A list of five
        labelled items can therefore report five extra uncited "sentences"."""
        assert split_sentences("Types [S1]. 1. From nothing. 2. From chaos.") == [
            "Types [S1].",
            "1.",
            "From nothing.",
            "2.",
            "From chaos.",
        ]

    def test_a_quoted_terminator_under_splits(self):
        """The opposite misfire from the two above: `"yes."` is followed by a `"` rather than
        whitespace, so the two sentences FUSE and one label covers both."""
        assert split_sentences('He said "yes." Then he left [S1].') == [
            'He said "yes." Then he left [S1].'
        ]

    def test_fragments_are_stripped_and_empties_dropped(self):
        assert split_sentences("  One.    \n\n   Two.   ") == ["One.", "Two."]


class TestMeasureAttribution:
    """One answer's prose -> its readout. `None` means UNMEASURED; `0.0` is a measurement."""

    def test_a_fully_cited_answer_has_a_measured_zero(self):
        """`0.0`, not `None`. A measured zero is a real and good result — every sentence carried a
        label — and it must be distinguishable from "there was nothing to measure"."""
        attribution = measure_attribution("One [S1]. Two [S2].")

        assert attribution is not None
        assert (attribution.total, attribution.uncited) == (2, 0)
        assert attribution.uncited_sentence_rate == 0.0

    def test_a_fully_uncited_answer_rates_one(self):
        attribution = measure_attribution("One thing. Another thing.")

        assert attribution.uncited_sentence_rate == 1.0

    def test_none_prose_is_none_not_a_zero(self):
        """REFUSAL and ERROR carry no prose by contract. `0.0` here would say "we measured, and
        everything was attributed" — dragging refusals in as free wins and making the rate IMPROVE
        the more the system refuses."""
        assert measure_attribution(None) is None

    def test_empty_and_whitespace_prose_are_none(self):
        assert measure_attribution("") is None
        assert measure_attribution("   \n  ") is None

    def test_a_label_mid_sentence_cites_it(self):
        attribution = measure_attribution("The earth [S1] is a woman.")

        assert attribution.uncited == 0

    def test_a_label_at_the_end_cites_it(self):
        """Position carries no meaning — presence anywhere in the sentence is the whole rule."""
        attribution = measure_attribution("The earth is a woman [S1].")

        assert attribution.uncited == 0

    def test_two_labels_in_one_sentence_are_one_cited_sentence_and_two_labels(self):
        """The sentence is cited ONCE — it is a sentence count, not a label count — while
        `labels_used` records both."""
        attribution = measure_attribution("The earth is a woman [S1][S3].")

        assert (attribution.total, attribution.uncited) == (1, 0)
        assert attribution.labels_used == ("[S1]", "[S3]")

    def test_no_labels_anywhere_leaves_labels_used_empty(self):
        attribution = measure_attribution("One. Two.")

        assert attribution.uncited_sentence_rate == 1.0
        assert attribution.labels_used == ()

    def test_a_dangling_label_still_counts_as_cited(self):
        """PRESENCE, NOT VALIDITY. `[S9]` against three sources is a dangling ordinal, and
        `_validate` already catches it as INTEGRITY_FLAGGED. Re-litigating validity here would
        make this a second writer for that fact."""
        attribution = measure_attribution("The earth is a woman [S9].")

        assert attribution.uncited == 0
        assert attribution.labels_used == ("[S9]",)

    @pytest.mark.parametrize("near_miss", ["[S1, S2]", "[Source 1]", "[s1]", "[ S1 ]"])
    def test_near_miss_label_forms_are_not_labels(self, near_miss):
        """The strict form is load-bearing in the other direction: the Grounder would not resolve
        any of these either, so a sentence carrying only one really does rest on nothing the
        citation spine can render."""
        attribution = measure_attribution(f"The earth is a woman {near_miss}.")

        assert attribution.uncited == 1
        assert attribution.labels_used == ()

    def test_a_two_digit_ordinal_is_a_label(self):
        """`\\d+`, not `\\d` — a top-k of 12 produces `[S12]`, and reading it as uncited would
        under-report exactly on the runs with the most sources."""
        attribution = measure_attribution("The earth is a woman [S12].")

        assert attribution.uncited == 0
        assert attribution.labels_used == ("[S12]",)

    def test_the_q008_reference_case(self):
        """THE ACCEPTANCE NUMBER: `sentences=4  uncited=3  labels_used=[S1][S3]`, reproducing the
        2026-07-27 probe in `eval/FINDINGS.md` and issue #66's own body.

        `Q008_PROSE` IS A RECONSTRUCTION, not a captured artifact — FINDINGS elides two sentences
        with `…` and re-asking q008 costs money. See the constant's comment. The reproduction
        claim is over the numbers and the label SET; the original probe printed a throwaway
        script's list repr (`['[S1]', '[S3]']`), which is not a format anything here promises.
        """
        attribution = measure_attribution(Q008_PROSE)

        assert attribution.total == 4
        assert attribution.uncited == 3
        assert attribution.labels_used == ("[S1]", "[S3]")
        assert attribution.uncited_sentence_rate == 0.75

    def test_a_label_after_the_period_becomes_its_own_sentence(self):
        """A KNOWN ARTIFACT, deliberately not mitigated. When the model writes `... woman. [S1]`
        instead of `... woman [S1].`, the labels split off into a fragment of their own: the
        prose sentence reads UNCITED and the label fragment reads CITED, so the count is one
        sentence high and the rate reports 50% where a human would say 0%.

        Re-attaching a label-only fragment to the sentence before it is step one of the smart
        splitter issue #66 rules out. How OFTEN the model writes it this way is unmeasured — the
        first live run settles it; until then this is an artifact, not a defect.
        """
        attribution = measure_attribution("The earth is a woman. [S1][S3]")

        assert attribution.total == 2
        assert attribution.uncited == 1
        assert [s.cited for s in attribution.sentences] == [False, True]

    def test_the_signature_cannot_see_a_state(self):
        """THE SIGNATURE IS THE INVARIANT, the same move `score_state` makes one class up. This
        takes prose and nothing else, so it CANNOT misread a state — which is the mechanical half
        of issue #66's "no answer's state changes because of it"."""
        assert list(inspect.signature(measure_attribution).parameters) == ["prose"]

    def test_the_label_regex_is_a_string_identical_twin_of_the_grounders(self):
        """The Grounder OWNS the `[S#]` vocabulary; this module keeps a twin rather than importing
        an underscore-private across a module boundary (`ask.py::_retrieval_error` refused that
        same import and documented why, and issue #66 must leave `src/gct/grounder/` untouched).

        A twin is only safe if drift is LOUD. If the Grounder ever widens its regex — which it may
        only do alongside a prompt change, per its own comment — this fires, and that is exactly
        when this metric's definition has to be revisited: a label the Grounder would resolve must
        count as a citation here too.
        """
        assert scoring._LABEL_RE.pattern == grounder_answer._LABEL_RE.pattern

    def test_the_objects_are_frozen(self):
        attribution = measure_attribution("One [S1].")

        with pytest.raises(dataclasses.FrozenInstanceError):
            attribution.sentences = ()
        with pytest.raises(dataclasses.FrozenInstanceError):
            attribution.sentences[0].text = "other"


class TestAggregateAttribution:
    """Pooling a run. MICRO, and `None` entries are unmeasured rather than zero."""

    def test_aggregation_is_micro_not_macro(self):
        """The two readings differ by a factor of two here, so they cannot be confused for a
        rounding difference: one answer of 1 uncited sentence and one of 3 fully-cited sentences
        pool to 1/4 = 0.25, while the mean of the per-answer rates (1.0 and 0.0) is 0.5.

        Micro is right for the same reason `GroundingCounts` keeps counts as the primitive: the
        question is "what share of the SENTENCES this bench asserted carried no label", and a
        macro mean would weight a one-sentence answer the same as a twelve-sentence one — the
        wrong unit for a rate whose subject is the individual claim.
        """
        one = measure_attribution("No label here.")
        three = measure_attribution("One [S1]. Two [S1]. Three [S2].")
        assert (one.uncited_sentence_rate, three.uncited_sentence_rate) == (1.0, 0.0)

        totals = aggregate_attribution([one, three])

        assert (totals.sentences, totals.uncited) == (4, 1)
        assert totals.uncited_sentence_rate == 0.25

    def test_none_entries_are_counted_as_unmeasured_and_contribute_no_sentences(self):
        measured = measure_attribution("One. Two [S1].")

        totals = aggregate_attribution([measured, None, None])

        assert (totals.answers_measured, totals.answers_unmeasured) == (1, 2)
        assert (totals.sentences, totals.uncited) == (2, 1)
        assert totals.uncited_sentence_rate == 0.5

    def test_an_all_none_run_reports_no_rate_rather_than_zero(self):
        """A refusal-only run measured NOTHING. `0.0` would read as "every sentence was cited"."""
        totals = aggregate_attribution([None, None, None])

        assert totals.uncited_sentence_rate is None
        assert (totals.answers_measured, totals.sentences) == (0, 0)

    def test_an_empty_run_is_zeros_and_no_rate(self):
        totals = aggregate_attribution([])

        assert totals == AttributionTotals(
            answers_measured=0, answers_unmeasured=0, sentences=0, uncited=0
        )
        assert totals.uncited_sentence_rate is None

    @pytest.mark.parametrize(
        "measurements",
        [
            [],
            [None],
            [measure_attribution("One [S1].")],
            [measure_attribution("One."), None, measure_attribution("Two [S1]. Three.")],
        ],
    )
    def test_measured_plus_unmeasured_always_equals_what_was_handed_in(self, measurements):
        """So a report can say how much of the suite it measured. On this corpus a third of the
        questions refuse by design, so the denominator is routinely short — a rate whose coverage
        is invisible is the "clean rate over a suite that quietly shrank" this module exists to
        prevent."""
        totals = aggregate_attribution(measurements)

        assert totals.answers_measured + totals.answers_unmeasured == len(measurements)


class TestAttributionIsNotTheVector:
    """ISSUE #66'S "REPORTED, NEVER ENFORCED" CLAIM, MADE MECHANICAL rather than asserted.

    NOT REPEATED HERE: that `EvalRecord`'s fields are exactly `("expectation", "state", "hit")`.
    `TestExpectedPlacement::test_expected_placement_is_absent_from_the_adr_0023_vector` already
    asserts it, and that check is not #67-specific — it is the same wall, and it fires for a
    prose field added for #66 exactly as it would for a rank field. A second copy of one fact is
    the drift this repo's CLAUDE.md is about; if that test ever moves, this comment is the
    pointer to follow.
    """

    def test_the_rate_is_not_on_the_metric_vector(self):
        """`EvalMetrics` IS the ADR 0023 vector by its own docstring, and §3 fixes it. A property
        added to that class is IN the vector by construction, and its denominator could not be
        `N_scored` anyway — this divides sentences by sentences, over a different population."""
        assert not hasattr(EvalMetrics, "uncited_sentence_rate")

        rates = {
            name
            for name, attr in vars(EvalMetrics).items()
            if isinstance(attr, property) and not name.startswith("_")
        }
        assert rates == {
            "grounded_pass_rate",
            "correct_refusal_rate",
            "partial_rate",
            "integrity_flag_rate",
            "false_refusal_rate",
            "hallucination_rate",
            "refuse_integrity_flag_rate",
            "error_count",
            "error_rate",
            "retrieval_hit_rate",
        }

    def test_the_metrics_are_identical_whatever_prose_those_answers_carried(self):
        """The strongest structural statement available: `compute_metrics` takes `EvalRecord`s,
        `EvalRecord` cannot carry prose, so two runs whose answers were worded completely
        differently — one fully cited, one fully uncited — produce the SAME rate vector while
        their attribution totals differ."""
        records = [
            EvalRecord("answer", GrounderState.GROUNDED, hit=True),
            EvalRecord("answer", GrounderState.GROUNDED, hit=True),
            EvalRecord("refuse", GrounderState.REFUSAL, hit=None),
        ]
        well_attributed = [
            measure_attribution("One [S1]. Two [S2]."),
            measure_attribution("Three [S1]."),
            None,
        ]
        badly_attributed = [
            measure_attribution("One. Two."),
            measure_attribution("Three."),
            None,
        ]

        metrics = compute_metrics(records)

        assert metrics.grounded_pass_rate == 1.0
        assert metrics.correct_refusal_rate == 1.0
        assert aggregate_attribution(well_attributed).uncited_sentence_rate == 0.0
        assert aggregate_attribution(badly_attributed).uncited_sentence_rate == 1.0
        # Same records, same vector — the attribution above cannot reach any of these figures.
        assert compute_metrics(records) == metrics


class ScriptedGeneration:
    """A `Generation`-shaped stub: replays `script` in order; RAISES any exception in it.

    Lives in this module rather than in a shared conftest for the same reason `BrokenEmbeddings`
    lives inside `test_ask.py`: `tests/gct/eval/` has no conftest of its own, and the grounder
    suite's `scripted` fixture is not visible from here (pytest exposes a conftest only to its own
    directory and below). Nothing here constructs a real provider client, so no test in this file
    takes a `live_*` fixture and none is paid.
    """

    def __init__(self, *script: str | Exception) -> None:
        self._script = list(script)

    @property
    def model_id(self) -> str:
        return "fake-gen-1"

    def generate(self, messages: Sequence[Message]) -> str:
        if not self._script:
            pytest.fail("generate() called more times than the test scripted replies")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def sources(n: int = 3) -> list[RetrievedChunk]:
    """`n` retrieved chunks in rank order — enough for `[S1]..[Sn]` to resolve."""
    return [
        RetrievedChunk(
            chunk_id=f"chunk-{i}",
            text=f"Source text number {i}.",
            file=f"lecture-{i}.pdf",
            page_or_slide=i,
            score=1.0 - i / 100,
        )
        for i in range(1, n + 1)
    ]


def grounded(*script: str | Exception) -> GrounderResult:
    """Drive the REAL `answer()` over a scripted reply. No network, no DB, no spend."""
    return grounder_answer.answer(
        "what is the symbol of Mother Earth?",
        sources(),
        "owner-1",
        generator=ScriptedGeneration(*script),
    )


class TestAttributionAcrossTheFiveStates:
    """The five states, through the REAL `answer()` — `None` vs `0.0` decided by PROSE, not state.

    Driving the real Grounder rather than hand-building `GrounderResult`s is the point in two
    places specifically: the coverage statement's exclusion belongs to `_parse`, and a hand-built
    result would let this file assert a fact it also constructed. A scripted stub reaches every
    branch deterministically and reaches none of them by paying for it.
    """

    def test_grounded_prose_is_measured(self):
        """The case issue #66 exists for: q008 scored GROUNDED with three of four sentences
        carrying no label, and nothing in V1 noticed."""
        result = grounded(
            "The earth is a mother [S1]. This arises from a homology. "
            "Creation is likened to birth.\nCOVERAGE: complete"
        )
        assert result.state is GrounderState.GROUNDED

        attribution = measure_attribution(result.answer_prose)

        assert (attribution.total, attribution.uncited) == (3, 2)

    def test_partial_prose_is_measured_by_the_same_rule(self):
        """PARTIAL has NEVER fired on the real corpus (ADR 0026), so this arm is covered BY
        CONSTRUCTION, not by observation. Said plainly rather than implied: a scripted PARTIAL
        proves the rule applies, not that the rule has ever been exercised in anger."""
        result = grounded("Chaos is one type [S2]. The rest is unclear.\nCOVERAGE: gaps: others")
        assert result.state is GrounderState.PARTIAL

        attribution = measure_attribution(result.answer_prose)

        assert (attribution.total, attribution.uncited) == (2, 1)

    def test_a_refusal_is_unmeasured_never_a_zero(self):
        """A refusal asserts nothing, so there is nothing to attribute. `0.0` would drag every
        honest decline in as a free win and make the rate improve as the product refuses more —
        `retrieval_hit`'s cannot-reach-1.0 argument, inverted."""
        result = grounded("COVERAGE: gaps: nothing on quantum chromodynamics")

        assert result.state is GrounderState.REFUSAL
        assert result.answer_prose is None
        assert measure_attribution(result.answer_prose) is None

    def test_an_error_is_unmeasured(self):
        """Nothing was generated. An infra fact is not an attribution fact (ADR 0016) — the same
        line `EvalMetrics` draws by excluding ERROR from N_scored."""
        result = grounded(TransientGenerationError("boom"), TransientGenerationError("again"))

        assert result.state is GrounderState.ERROR
        assert measure_attribution(result.answer_prose) is None

    def test_integrity_flagged_with_surviving_prose_is_measured(self):
        """ADR 0023 §1's independence principle, one layer down: retrieval counts on an ERRORed
        row when retrieval ran, "regardless of the final state". A flagged answer that still
        carries prose still ASSERTED those sentences, so they are attributable — and here every
        one of them carried a label, which is a MEASURED `0.0`, not a `None`."""
        reply = "The earth is a mother [S9]. It bears children [S1].\nCOVERAGE: complete"
        result = grounded(reply, reply)

        assert result.state is GrounderState.INTEGRITY_FLAGGED
        attribution = measure_attribution(result.answer_prose)

        assert (attribution.total, attribution.uncited) == (2, 0)
        assert attribution.uncited_sentence_rate == 0.0

    def test_integrity_flagged_with_no_prose_is_unmeasured(self):
        """THE STATE DOES NOT DECIDE — THE PROSE DOES. The mirror of the test above, in the same
        state: an unparseable coverage line leaves `_integrity_flagged` with `parsed.prose or
        None`, so there is nothing to attribute and the answer is unmeasured."""
        reply = "COVERAGE: bananas"
        result = grounded(reply, reply)

        assert result.state is GrounderState.INTEGRITY_FLAGGED
        assert result.answer_prose is None
        assert measure_attribution(result.answer_prose) is None

    def test_the_coverage_statement_never_enters_the_denominator(self):
        """EXCLUDED BY CONSTRUCTION, and pinned HERE rather than by a comment because the fact
        belongs to `grounder/answer.py::_parse` — it does `_COVERAGE_RE.sub("", raw).strip()`
        before `answer_prose` exists. Only a test at that seam notices if `_parse` ever stops
        cutting; a unit test in this module would be asserting its own fixture.

        The gap text lands in `coverage.gaps` instead, which is the OTHER, SATISFIED half of ADR
        0014's rule: the claims the answer correctly declined to assert. Counting them as uncited
        sentences would be exactly backwards.
        """
        result = grounded(
            "Chaos is one type [S2]. Nothing else is supported.\n"
            "COVERAGE: gaps: the Babylonian account; the Egyptian account"
        )

        assert result.coverage.gaps == ["the Babylonian account", "the Egyptian account"]
        attribution = measure_attribution(result.answer_prose)

        assert attribution.total == 2
        assert not any("COVERAGE" in s.text.upper() for s in attribution.sentences)
        assert not any("Babylonian" in s.text for s in attribution.sentences)

    def test_two_coverage_markers_are_both_cut(self):
        """`_parse` subs over EVERY marker, not just the one it read — an earlier version cut only
        the selected one and left internal protocol text on the trust surface. Two markers also
        flag the answer, which is the state this reply legitimately lands in; the point here is
        that neither marker reaches the denominator."""
        reply = "The earth is a mother [S1].\nCOVERAGE: complete\nCOVERAGE: gaps: everything else"
        result = grounded(reply, reply)

        assert result.state is GrounderState.INTEGRITY_FLAGGED
        attribution = measure_attribution(result.answer_prose)

        assert attribution.total == 1
        assert not any("COVERAGE" in s.text.upper() for s in attribution.sentences)


class TestSentenceCitationShape:
    """The leaf object, so `cited` cannot quietly become something other than label presence."""

    @pytest.mark.parametrize(
        ("labels", "cited"),
        [((), False), (("[S1]",), True), (("[S1]", "[S3]"), True)],
    )
    def test_cited_is_exactly_label_presence(self, labels, cited):
        assert SentenceCitation(text="whatever", labels=labels).cited is cited

    def test_the_measured_sentences_carry_the_text_they_were_split_from(self):
        attribution = measure_attribution("One [S1]. Two.")

        assert [s.text for s in attribution.sentences] == ["One [S1].", "Two."]
        assert isinstance(attribution, AnswerAttribution)
