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
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from gct.eval.questions import ExpectedSource
from gct.eval.scoring import (
    EvalMetrics,
    EvalRecord,
    ExpectedPlacement,
    Outcome,
    compute_metrics,
    expected_placement,
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
