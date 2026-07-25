"""Tests for the Grounder (issue #6) - pure, offline, no DB and no paid provider.

Organised by the thing being defended, not by function:

  - `TestEmptyRetrieval` - the short-circuit, including the "no generation call" half, which is
    the whole point of it (ADR 0016).
  - `TestLabeledContext` - honor-point ②: per-request ordinals, rank order, spine metadata, and
    that the `S# -> chunk_id` map stays SERVER-SIDE (ADR 0015).
  - `TestParse` - the text-parse layer on its own: marker forms, prose extraction, label reading.
  - `TestValidate` - the three-rung V1-structural ladder, and the rung that must NOT fire on an
    honest refusal.
  - `TestStates` - the decision table's happy rows: GROUNDED, PARTIAL, REFUSAL.
  - `TestRetryBudget` - the ONE shared budget: retry on structural failure, retry on transient
    error, and never more than `MAX_GENERATION_ATTEMPTS` calls.
  - `TestIntegrityFlagged` - every structural failure mode surviving the retry, showing prose
    with a flag and only the citations that actually resolved.
  - `TestProviderErrors` - error != refusal, the invariant the trust promise rests on.
"""

from __future__ import annotations

import pytest

from gct.grounder.answer import (
    MAX_GENERATION_ATTEMPTS,
    Coverage,
    GrounderState,
    _build_labeled_context,
    _parse,
    _validate,
    answer,
)
from gct.providers.base import TransientGenerationError

# A well-formed reply against a 2-chunk context: two cited claims + a complete coverage marker.
GOOD_REPLY = (
    "Gradient descent steps downhill [S1]. The step size is the learning rate [S2].\n"
    "COVERAGE: complete"
)

# The same, but declaring a gap - the PARTIAL shape.
PARTIAL_REPLY = (
    "Gradient descent steps downhill [S1].\n"
    "COVERAGE: gaps: the convergence proof; the exam's marking scheme"
)

# Empty prose + an enumerated gap list: the DEGENERATE refusal, i.e. the partial-support
# mechanic with an empty supported set (ADR 0008/0014) - one mechanism, not a second path.
REFUSAL_REPLY = "COVERAGE: gaps: anything about gradient descent"


class TestEmptyRetrieval:
    """Zero chunks -> canned REFUSAL, deterministically, with no model call (ADR 0016)."""

    def test_empty_retrieval_refuses_without_generating(self, scripted):
        """The load-bearing assertion is `call_count == 0`.

        Calling the model with an empty context would cost money and add a hallucination surface
        for a case we can answer deterministically - the exact alternative ADR 0016 rejected. The
        state alone would not catch a regression here: a model handed no sources would probably
        refuse too, so the suite would stay green while every empty class quietly paid for a
        call. Assert the absence of the call, not just the outcome.
        """
        generator = scripted()  # nothing scripted: any call at all raises

        result = answer("What is gradient descent?", [], "owner-1", generator=generator)

        assert generator.call_count == 0
        assert result.state is GrounderState.REFUSAL
        assert result.answer_prose is None
        assert result.citations == []
        assert result.error is None
        assert result.integrity.ok is True
        # Not "complete": the refusal must say the question went uncovered, or the UI has
        # nothing honest to render.
        assert result.coverage.complete is False
        assert result.coverage.gaps


class TestLabeledContext:
    """② The labeled context and the server-side map (ADR 0015)."""

    def test_blocks_are_rank_ordered_ordinals_with_spine_metadata(self, chunks):
        context, sources = _build_labeled_context(chunks(2))

        assert context == (
            "[S1] (lecture-1.pdf, p.1)\nSource text number 1.\n\n"
            "[S2] (lecture-2.pdf, p.2)\nSource text number 2."
        )
        # Ordinals are per-REQUEST and follow retrieval rank, so S1 is always the top hit.
        assert sources[1].chunk_id == "chunk-1"
        assert sources[2].chunk_id == "chunk-2"

    def test_chunk_ids_never_reach_the_model(self, chunks, scripted):
        """The prompt carries ordinals and provenance - never a `chunk_id`.

        This is why ADR 0015 chose ordinals over DB ids: a label the model never saw is a label
        it cannot fabricate and we cannot be tricked into trusting. If a `chunk_id` ever appears
        in the prompt, a model could echo one back and the resolution step would have to decide
        whether to believe it. It must never have to.
        """
        retrieved = chunks(2)
        generator = scripted(GOOD_REPLY)

        answer("q", retrieved, "owner-1", generator=generator)

        prompt = "\n".join(m["content"] for m in generator.calls[0])
        assert "[S1]" in prompt and "[S2]" in prompt
        assert "lecture-1.pdf" in prompt and "Source text number 1." in prompt
        for chunk in retrieved:
            assert chunk.chunk_id not in prompt

    def test_prompt_states_the_three_contract_rules(self, chunks, scripted):
        """The PROSE is provisional (a spike deliverable); these three demands are not.

        ADR 0013/0014/0015 require the prompt to ask for: cite only valid [S#], no outside
        knowledge, always emit the coverage marker. This test is deliberately loose about
        WORDING so prompt tuning doesn't fight it, and strict about the contract surviving that
        tuning - specifically, that the marker syntax the prompt teaches is the one the parser
        reads.
        """
        generator = scripted(GOOD_REPLY)

        answer("q", chunks(2), "owner-1", generator=generator)

        system = generator.calls[0][0]["content"]
        assert "[S1]" in system  # the label form it must cite with
        assert "COVERAGE: complete" in system  # the marker form `_COVERAGE_RE` parses
        assert "COVERAGE: gaps:" in system
        assert "outside" in system.lower()  # the no-outside-knowledge rule


class TestParse:
    """④ Text-parse: prose, `[S#]` ordinals, and the coverage marker (ADR 0013)."""

    def test_parses_complete_marker_and_strips_it_from_prose(self):
        parsed = _parse("The answer is 4 [S1].\nCOVERAGE: complete")

        assert parsed.prose == "The answer is 4 [S1]."
        assert parsed.ordinals == [1]
        assert parsed.coverage == Coverage(complete=True, gaps=[])

    def test_parses_gap_list_on_semicolons(self):
        parsed = _parse("Partly [S2].\nCOVERAGE: gaps: the proof ; the exam date")

        assert parsed.coverage == Coverage(complete=False, gaps=["the proof", "the exam date"])

    @pytest.mark.parametrize(
        "raw",
        [
            "Claim [S1].",  # marker missing entirely
            "Claim [S1].\nCOVERAGE: mostly complete",  # unrecognised body
            "Claim [S1].\nCOVERAGE:",  # marker with nothing after it
            "Claim [S1].\nCOVERAGE: gaps:",  # incomplete without naming a gap
            "Claim [S1].\nCoverage is complete.",  # prose about coverage, not a marker
        ],
    )
    def test_unparseable_marker_yields_none_not_a_guess(self, raw):
        """None, never an optimistic `complete=True`.

        This is the fail-safe invariant at its smallest scale (ADR 0014): silently reading an
        unreadable marker as full coverage would claim grounding we cannot confirm, which is the
        one failure the whole box exists to prevent. `gaps:` with nothing after it is malformed
        rather than "no gaps" - it declares the answer incomplete while naming nothing.
        """
        assert _parse(raw).coverage is None

    def test_labels_come_from_prose_only_not_from_the_gap_list(self):
        """A gap that mentions `[S3]` must not count as a citation.

        The coverage statement describes what is MISSING; reading its labels as citations would
        let a declared gap masquerade as support and turn a PARTIAL into a false GROUNDED. The
        prose assertion is not decoration - the marker line has to be GONE from the text the
        student reads, and an ordinals-only assertion would pass while it leaked.
        """
        raw = (
            "Only this part is supported [S1].\nCOVERAGE: gaps: nothing in [S3] addresses the rest"
        )
        parsed = _parse(raw)

        assert parsed.prose == "Only this part is supported [S1]."
        assert parsed.ordinals == [1]
        assert parsed.coverage == Coverage(
            complete=False, gaps=["nothing in [S3] addresses the rest"]
        )

    def test_every_marker_line_is_cut_from_the_prose(self):
        """No `COVERAGE:` line survives into `answer_prose`, however many there are.

        Cutting only the marker we READ left the others in the prose - internal protocol text
        rendered onto the trust surface, which is the one place it must never appear.
        """
        parsed = _parse(
            "COVERAGE: complete\nActually only this is supported [S1].\nCOVERAGE: gaps: the rest"
        )

        assert parsed.prose == "Actually only this is supported [S1]."
        assert "COVERAGE" not in parsed.prose
        assert parsed.marker_count == 2

    def test_more_than_one_marker_refuses_to_pick_a_winner(self):
        """Two markers -> coverage is None, so validation fails and the retry ladder runs.

        Picking a winner is a guess, and the guess that reads best (`complete`) is the unsafe
        one - see the end-to-end test in TestIntegrityFlagged for what that used to produce.
        """
        parsed = _parse("Claim [S1].\nCOVERAGE: gaps: nothing on Navaho\nCOVERAGE: complete")

        assert parsed.coverage is None
        assert parsed.marker_count == 2

    def test_marker_is_case_and_whitespace_tolerant(self):
        """A formatting slip is not a grounding failure and should not cost a retry."""
        assert _parse("X [S1].\n  coverage:   Complete  ").coverage == Coverage(True, [])

    @pytest.mark.parametrize(
        "marker",
        [
            # the whole line wrapped
            "**COVERAGE: complete**",
            "__COVERAGE: complete__",
            "  **COVERAGE:  complete**  ",
            "*COVERAGE: complete*",
            # the KEYWORD wrapped - both places the closing delimiter can fall
            "**COVERAGE:** complete",
            "**COVERAGE**: complete",
            "__COVERAGE:__ complete",
            "*COVERAGE:* complete",
            "  **COVERAGE:**   Complete  ",
            # the body wrapped on its own
            "COVERAGE: **complete**",
            "**COVERAGE**: **complete**",
        ],
    )
    def test_bolded_marker_is_accepted(self, marker):
        """Bolding a trailing protocol line is the slip chat models actually make.

        Same principle as the case/whitespace tolerance above, applied where it will really bite:
        an INTEGRITY_FLAGGED answer scores as a FAIL under ADR 0023, so a purely cosmetic habit
        could turn #8's whole smoke suite red and read as a grounding problem. The contract is
        untouched - keyword and body grammar are still required exactly, and the `[S#]` regex
        stays strict, because there a generous match would hide drift instead of absorbing it.

        A model that decides to bold this line picks among these forms arbitrarily, so accepting
        only the whole-line one would still fail for a purely cosmetic reason - which is why the
        keyword-wrapped forms are here rather than left to be discovered by #8 going red.
        """
        assert _parse(f"X [S1].\n{marker}").coverage == Coverage(complete=True, gaps=[])

    @pytest.mark.parametrize(
        "marker",
        [
            "**COVERAGE: gaps: the proof; the exam date**",
            "**COVERAGE:** gaps: the proof; the exam date",
            "**COVERAGE**: gaps: the proof; the exam date",
        ],
    )
    def test_bolded_gap_list_is_accepted_without_swallowing_the_body(self, marker):
        parsed = _parse(f"X [S1].\n{marker}")

        assert parsed.coverage == Coverage(complete=False, gaps=["the proof", "the exam date"])

    def test_a_body_ending_in_an_emphasis_char_is_not_truncated(self):
        """The trailing slot is anchored, so it can only eat emphasis at the very end of the line.

        Without the `$` anchor a non-greedy body would stop at the first `_` it could hand to that
        slot, and `foo_bar` would silently become `foo` - a gap statement quietly rewritten.
        """
        parsed = _parse("X [S1].\nCOVERAGE: gaps: nothing on foo_bar; the exam")

        assert parsed.coverage == Coverage(complete=False, gaps=["nothing on foo_bar", "the exam"])

    @pytest.mark.parametrize(
        "marker",
        [
            "**COVERAGE:** mostly",  # body grammar is still exact
            "- COVERAGE: complete",  # a list item, not a bare final line
            "## COVERAGE: complete",  # a heading, ditto
            "**COVERAGE:** **complete**",  # two emphasis openers back to back
        ],
    )
    def test_the_tolerance_stops_at_presentation(self, marker):
        """Where the line is drawn, pinned so that moving it has to be deliberate.

        We absorb emphasis around the keyword, the body, or the whole line. We do NOT absorb a
        changed body grammar (that is the contract), a bullet or heading (those change what the
        line IS in markdown, and the contract asks for a bare final line), or arbitrary runs of
        emphasis - past that point we would be stripping markdown soup and would stop noticing
        real format drift, which is the signal the strictness exists to preserve.

        If #8's smoke suite shows the model actually emitting one of these, widen the regex and
        delete the case - but do it on evidence, the way `grounder.md` makes marker syntax a
        spike deliverable, not on a guess.
        """
        assert _parse(f"X [S1].\n{marker}").coverage is None

    def test_repeated_label_is_kept_as_written(self):
        """`_parse` reports what the model wrote; de-duplication belongs to resolution."""
        assert _parse("A [S1]. B [S1]. C [S2].\nCOVERAGE: complete").ordinals == [1, 1, 2]


class TestValidate:
    """⑤ The V1-structural ladder - three rungs, and no fourth (ADR 0015)."""

    def test_valid_answer_has_no_reasons(self):
        assert _validate(_parse(GOOD_REPLY), n_sources=2) == []

    def test_dangling_label_is_caught(self):
        reasons = _validate(_parse("Claim [S5].\nCOVERAGE: complete"), n_sources=2)

        assert len(reasons) == 1
        assert "[S5]" in reasons[0]

    def test_zero_citation_prose_is_caught_coarsely(self):
        reasons = _validate(_parse("Gradient descent goes downhill.\nCOVERAGE: complete"), 2)

        assert len(reasons) == 1
        assert "no citation" in reasons[0]

    def test_double_marker_is_caught_and_named_as_such(self):
        """Reported as "two markers", not as "missing" - they are opposite problems, and the
        telemetry is the only place the difference is visible."""
        raw = "Claim [S1].\nCOVERAGE: gaps: nothing on Navaho\nCOVERAGE: complete"

        reasons = _validate(_parse(raw), n_sources=2)

        assert len(reasons) == 1
        assert "2 coverage markers" in reasons[0]
        assert "missing" not in reasons[0]

    def test_zero_citation_guard_does_not_fire_on_an_honest_refusal(self):
        """Empty prose + gaps is the mechanism WORKING, not failing.

        The guard is scoped to NON-EMPTY prose for exactly this reason. Drop that condition and
        every honest decline becomes an integrity flag - which would drive the false-refusal rate
        (N3, bar <=0.10) straight through the ceiling while looking like a tightened check.
        """
        assert _validate(_parse(REFUSAL_REPLY), n_sources=2) == []

    def test_per_claim_citation_is_deliberately_not_checked(self):
        """An uncited sentence beside a cited one passes V1 - by design, not by omission.

        Catching this needs claim segmentation, which text-parse V1 cannot do honestly; it is V3
        judge-based faithfulness (ADR 0015). Pinned as a test so the V1/V3 line is visible in the
        suite: if someone later "fixes" this, they are moving the line and owe the spec an edit.
        """
        raw = "First claim [S1]. Second claim, uncited.\nCOVERAGE: complete"

        assert _validate(_parse(raw), n_sources=2) == []


class TestStates:
    """⑧ The decision table's rows that a validated answer can reach."""

    def test_grounded_resolves_citations_through_the_server_side_map(self, chunks, scripted):
        result = answer("q", chunks(2), "owner-1", generator=scripted(GOOD_REPLY))

        assert result.state is GrounderState.GROUNDED
        assert result.answer_prose == (
            "Gradient descent steps downhill [S1]. The step size is the learning rate [S2]."
        )
        assert result.coverage == Coverage(complete=True, gaps=[])
        assert result.integrity.ok is True
        assert result.error is None
        # Provenance comes from OUR map, field by field - this is honor-point ③.
        assert [(c.label, c.file, c.page_or_slide, c.chunk_id) for c in result.citations] == [
            ("S1", "lecture-1.pdf", 1, "chunk-1"),
            ("S2", "lecture-2.pdf", 2, "chunk-2"),
        ]

    def test_partial_keeps_the_supported_prose_and_the_gaps(self, chunks, scripted):
        result = answer("q", chunks(2), "owner-1", generator=scripted(PARTIAL_REPLY))

        assert result.state is GrounderState.PARTIAL
        assert result.answer_prose == "Gradient descent steps downhill [S1]."
        assert result.citations[0].chunk_id == "chunk-1"
        assert result.coverage.complete is False
        assert result.coverage.gaps == ["the convergence proof", "the exam's marking scheme"]
        assert result.integrity.ok is True

    def test_empty_supported_set_is_a_refusal(self, chunks, scripted):
        """Chunks came back, the model could support nothing with them -> REFUSAL.

        Note this is a refusal reached WITH a non-empty corpus, which is the case that matters:
        in V1 the Retriever has no score floor, so a non-empty class returns k chunks for EVERY
        question. The entire refusal guarantee for out-of-corpus questions rests on this branch.
        """
        result = answer("q", chunks(2), "owner-1", generator=scripted(REFUSAL_REPLY))

        assert result.state is GrounderState.REFUSAL
        assert result.answer_prose is None
        assert result.citations == []
        assert result.coverage.gaps == ["anything about gradient descent"]
        assert result.error is None

    def test_repeated_label_resolves_to_one_citation(self, chunks, scripted):
        reply = "A [S2]. B [S2]. C [S1].\nCOVERAGE: complete"

        result = answer("q", chunks(2), "owner-1", generator=scripted(reply))

        # De-duplicated, and ordered by ordinal (= retrieval rank), not by first mention - so the
        # citation list reads in the same order as the context we handed in.
        assert [c.label for c in result.citations] == ["S1", "S2"]

    def test_states_are_plain_strings_for_the_eval_runner(self):
        """`GrounderState` subclasses `str`, so ADR 0023's state->outcome map and the API adapter
        can read the value with no custom encoder."""
        assert GrounderState.GROUNDED == "GROUNDED"


class TestRetryBudget:
    """⑥ ONE shared budget: at most `MAX_GENERATION_ATTEMPTS` calls per ask (ADR 0015/0016)."""

    def test_structural_failure_retries_once_and_can_recover(self, chunks, scripted):
        generator = scripted("Claim [S5].\nCOVERAGE: complete", GOOD_REPLY)

        result = answer("q", chunks(2), "owner-1", generator=generator)

        assert generator.call_count == 2
        assert result.state is GrounderState.GROUNDED
        assert result.integrity.ok is True  # the first attempt's failure is not held against it

    def test_transient_provider_error_retries_once_and_can_recover(self, chunks, scripted):
        generator = scripted(TransientGenerationError("429 rate limited"), GOOD_REPLY)

        result = answer("q", chunks(2), "owner-1", generator=generator)

        assert generator.call_count == 2
        assert result.state is GrounderState.GROUNDED

    def test_retry_resends_the_identical_context(self, chunks, scripted):
        """Both attempts must see the same ordinals, or the citations resolve against a map that
        no longer matches the prompt the model actually answered."""
        generator = scripted(TransientGenerationError("timeout"), GOOD_REPLY)

        answer("q", chunks(2), "owner-1", generator=generator)

        assert generator.calls[0] == generator.calls[1]

    def test_budget_is_shared_not_per_failure_kind(self, chunks, scripted):
        """A transient error THEN a structural failure exhausts the budget - it does not earn a
        third call. Separate budgets would multiply attempts and blow the latency bound (N5),
        which is precisely what ADR 0016 rejected."""
        generator = scripted(TransientGenerationError("timeout"), "Claim [S9].\nCOVERAGE: complete")

        result = answer("q", chunks(2), "owner-1", generator=generator)

        assert generator.call_count == MAX_GENERATION_ATTEMPTS == 2
        assert result.state is GrounderState.INTEGRITY_FLAGGED

    def test_never_exceeds_the_budget(self, chunks, scripted):
        """Two failing replies are scripted; a third call would run off the end and fail here."""
        generator = scripted(
            "no labels here.\nCOVERAGE: complete", "still no labels.\nCOVERAGE: complete"
        )

        result = answer("q", chunks(2), "owner-1", generator=generator)

        assert generator.call_count == MAX_GENERATION_ATTEMPTS
        assert result.state is GrounderState.INTEGRITY_FLAGGED

    def test_over_budget_is_not_swallowed_by_the_broad_except(self, chunks, scripted):
        """The test above only bites if the stub's guard can escape `answer()`. This proves it.

        `answer()` catches broad `Exception` around `generate()` on purpose - the thin provider
        seam can't enumerate a provider's error types (ADR 0013). An over-budget guard raising a
        plain Exception would therefore be caught and re-reported as `state=ERROR`, and a
        3-attempts-per-ask regression would arrive disguised as a provider outage. The stub uses
        `pytest.fail`, whose `Failed` derives from BaseException and passes straight through.
        """
        one_short = scripted("no labels here.\nCOVERAGE: complete")  # budget is 2, script is 1

        with pytest.raises(pytest.fail.Exception):
            answer("q", chunks(2), "owner-1", generator=one_short)


class TestIntegrityFlagged:
    """Structural failure surviving the retry -> flag-and-show, never a clean answer (ADR 0015)."""

    def test_dangling_label_twice_flags_and_keeps_only_resolvable_citations(self, chunks, scripted):
        """The prose is shown, S1 still resolves, and [S5] is named in the reasons - not dropped
        silently. A dropped-but-unreported label would leave a claim looking supported by a
        citation the reader never sees is missing."""
        reply = "Real claim [S1]. Invented claim [S5].\nCOVERAGE: complete"
        generator = scripted(reply, reply)

        result = answer("q", chunks(2), "owner-1", generator=generator)

        assert result.state is GrounderState.INTEGRITY_FLAGGED
        assert result.answer_prose == "Real claim [S1]. Invented claim [S5]."
        assert [c.label for c in result.citations] == ["S1"]
        assert result.integrity.ok is False
        assert "[S5]" in result.integrity.reasons[0]
        assert result.error is None  # a model defect is NOT a transport failure

    def test_zero_citation_prose_twice_flags(self, chunks, scripted):
        reply = "Gradient descent goes downhill.\nCOVERAGE: complete"

        result = answer("q", chunks(2), "owner-1", generator=scripted(reply, reply))

        assert result.state is GrounderState.INTEGRITY_FLAGGED
        assert result.citations == []
        assert result.integrity.ok is False

    def test_missing_coverage_marker_twice_flags_and_defaults_to_incomplete(self, chunks, scripted):
        """With no trustworthy coverage statement, `complete=False` is the only safe default -
        the alternative is presenting an unverified answer as fully covering the question."""
        reply = "A claim [S1]."

        result = answer("q", chunks(2), "owner-1", generator=scripted(reply, reply))

        assert result.state is GrounderState.INTEGRITY_FLAGGED
        assert result.coverage == Coverage(complete=False, gaps=[])
        assert "coverage marker" in result.integrity.reasons[0]

    def test_malformed_coverage_marker_twice_flags(self, chunks, scripted):
        reply = "A claim [S1].\nCOVERAGE: mostly"

        result = answer("q", chunks(2), "owner-1", generator=scripted(reply, reply))

        assert result.state is GrounderState.INTEGRITY_FLAGGED
        assert result.integrity.ok is False

    def test_disagreeing_double_marker_never_resolves_to_a_clean_grounded(self, chunks, scripted):
        """The round-1 review's regression, pinned end to end (q003 provenance).

        This exact reply used to come back GROUNDED, with `COVERAGE: gaps: nothing on Navaho`
        still sitting in the prose the student reads - an answer whose own coverage statement
        enumerated a gap, presented as fully grounded, carrying protocol text on the trust
        surface. Two failures from one parse bug, and both are asserted here: the state, and the
        prose.
        """
        reply = (
            "Cosmogony is the emergence of world order [S1].\n"
            "COVERAGE: gaps: nothing on Navaho\n"
            "COVERAGE: complete"
        )

        result = answer("q", chunks(2), "owner-1", generator=scripted(reply, reply))

        assert result.state is GrounderState.INTEGRITY_FLAGGED
        assert result.answer_prose == "Cosmogony is the emergence of world order [S1]."
        assert "COVERAGE" not in result.answer_prose
        # No trustworthy statement about coverage survived, so we claim none.
        assert result.coverage == Coverage(complete=False, gaps=[])
        assert "2 coverage markers" in result.integrity.reasons[0]

    def test_a_marker_restated_mid_answer_is_flagged_not_silently_partial(self, chunks, scripted):
        """The variant `_parse`'s own docstring predicted: a model that restates the format
        before using it. It leaked the same way, just landing on PARTIAL instead of GROUNDED."""
        reply = (
            "COVERAGE: complete\n"
            "Actually, only this part is supported [S1].\n"
            "COVERAGE: gaps: the rest"
        )

        result = answer("q", chunks(2), "owner-1", generator=scripted(reply, reply))

        assert result.state is GrounderState.INTEGRITY_FLAGGED
        assert result.answer_prose == "Actually, only this part is supported [S1]."

    def test_a_structural_failure_never_lands_on_grounded(self, chunks, scripted):
        """The fail-safe invariant, stated as its own test (ADR 0014).

        Every one of these replies is broken in a different way and each carries a `COVERAGE:
        complete` that a careless implementation would read as "fully grounded". None may.
        """
        for reply in (
            "Claim [S7].\nCOVERAGE: complete",
            "Claim with no label at all.\nCOVERAGE: complete",
            "Claim [S1].",
            "Claim [S1].\nCOVERAGE: somewhat",
        ):
            result = answer("q", chunks(2), "owner-1", generator=scripted(reply, reply))
            assert result.state is not GrounderState.GROUNDED, reply
            assert result.state is GrounderState.INTEGRITY_FLAGGED, reply


class TestProviderErrors:
    """Transport failures -> ERROR, never REFUSAL (ADR 0016)."""

    def test_persistent_transient_error_is_an_error_not_a_refusal(self, chunks, scripted):
        """The invariant the trust promise rests on: an outage is not a corpus judgment.

        Reporting "your materials don't cover this" because OpenAI returned a 503 would be a
        dishonest conflation - and it would also poison the N3 refusal metric with events that
        have nothing to do with grounding.
        """
        generator = scripted(TransientGenerationError("503"), TransientGenerationError("503 again"))

        result = answer("q", chunks(2), "owner-1", generator=generator)

        assert generator.call_count == MAX_GENERATION_ATTEMPTS
        assert result.state is GrounderState.ERROR
        assert result.state is not GrounderState.REFUSAL
        assert result.answer_prose is None
        assert result.citations == []
        assert result.error is not None
        assert "503 again" in result.error.message
        # No claim is made about what the corpus covers - we never got an answer to judge.
        assert result.coverage == Coverage(complete=False, gaps=[])

    def test_terminal_provider_error_ends_the_ask_without_burning_the_retry(self, chunks, scripted):
        """A bad key or malformed request will fail identically on a second call, so retrying it
        only adds latency - the same reasoning the OpenAI adapter uses when it declines to
        classify terminal errors as transient. It still becomes an ERROR, not an exception
        escaping into `ask()`."""
        generator = scripted(RuntimeError("invalid api key"), GOOD_REPLY)

        result = answer("q", chunks(2), "owner-1", generator=generator)

        assert generator.call_count == 1
        assert result.state is GrounderState.ERROR
        assert "invalid api key" in result.error.message
        assert "RuntimeError" in result.error.message

    def test_model_failures_are_returned_never_raised(self, chunks, scripted):
        """No provider failure escapes as an exception. A caller forced to use try/except to
        learn the corpus didn't cover something will eventually forget to, and the fallback
        would be worse than the honest decline this box exists to give."""
        generator = scripted(TransientGenerationError("x"), TransientGenerationError("y"))

        result = answer("q", chunks(1), "owner-1", generator=generator)  # must not raise

        assert result.state is GrounderState.ERROR
