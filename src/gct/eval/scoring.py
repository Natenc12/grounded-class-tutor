"""Eval scoring - ADR 0023's two-signal rule as executable definitions (issue #8).

This module IS the ADR, in code. ADR 0023's forward-shape section is explicit about why it lives
in the core rather than in a script: "The rule lives in the core as a pure `state -> outcome` map
+ a retrieval-hit check against `expected_sources`, so V3 is the *same* definitions executed."
The V1 human eyeballing the smoke suite and the V3 harness must read ONE definition; a second
copy in a runner is how two "identical" rules quietly stop agreeing.

The two signals stay UNCONFLATED (ADR 0023 §1), which is why there are two functions and not one
verdict:
  - **Retrieval** - was an `expected_sources` chunk in the retrieved top-k? The lever that
    chunking / k / embedder spikes tune (crude N1 proxy). Misses surface here REGARDLESS of the
    final state, so they cannot hide behind a PARTIAL.
  - **Grounding** - the Grounder `state`. The lever that generation / prompt spikes tune (crude
    N2/N3 proxies).

THE ONE THING THIS FILE MUST NEVER DO: score on `coverage`. The Grounder can legitimately return
a REFUSAL whose `coverage` reads `complete` - `_decide` in `grounder/answer.py` documents that
incoherent combination and deliberately reports the model's statement unrewritten so telemetry
sees what it actually said. This module is the first consumer that could misread it (issue #8's
inherited warning from #6/PR #35). `score_state` therefore takes the state and the expectation
and NOTHING ELSE: it is not merely that we choose not to look at coverage, it is that coverage is
not in scope to look at. Pinned by `test_refusal_with_complete_coverage_scores_on_state`.

What this is NOT (ADR 0023 §5, recorded against erosion): a release gate, a quality verdict, or
an approximation of V3. It is a crude bench for RANKING bake-offs. A good `grounded_pass_rate`
means one spike beat another on this suite - never that the system is faithful (V1 checks
structure, not entailment; ADR 0014).

ONE thing here is NOT a metric: `expected_placement` (issue #67). It is a DIAGNOSTIC - the rank
and margin behind a single `hit`. It joins no rate, enters no record, and aggregates over
nothing, so ADR 0023 §3's vector is exactly what it was. It lives here anyway, for the reason
directly above: rank and margin are facts about a `RetrievedChunk[]` list, with three genuine
definitional choices apiece (see the function), and computing them in a runner would be the
second copy this module exists to not have. REJECTED ALTERNATIVE, recorded because nothing else
carries it: compute them inline in `scripts/ask_smoke.py`'s printer. It is a cheaper diff and
matches issue #67's own `Touches:` line, and nothing in V1 would ever notice the divergence -
which is precisely the failure mode ADR 0017 (clamped per ADR 0024) names about its own seam:
load-bearing exactly because nothing in V1 reads it, so a wrong choice hides until V3. It is also
why the runner's own docstring says a scoring rule found there "belongs one layer down".
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from gct.eval.questions import EXPECTATION_ANSWER, EXPECTATION_REFUSE, ExpectedSource
from gct.grounder.answer import GrounderState
from gct.retriever.retrieve import RetrievedChunk


class Outcome(str, Enum):
    """What one question's grounding state is worth (ADR 0023 §2).

    Subclasses `str` for the same reason `GrounderState` does: an outcome serializes straight
    into a JSON run report without a custom encoder.

    FOUR values, not two, and the extra pair is the entire decision:
      - `TRACKED` is PARTIAL's bucket. It is neither success nor failure - it counts in the
        denominator but not in the pass numerator (§2/§3). ADR 0023 §5 says outright that any
        future edit folding TRACKED into PASS or FAIL "destroys the exact signal this decision
        exists to preserve", and that its awkwardness as a third thing IS the point.
      - `EXCLUDED` is ERROR's. An infra failure is not a corpus verdict (ADR 0016), so it leaves
        the denominator entirely - and gets counted separately, because a FAIL->ERROR escape
        hatch has to stay visible (§3).
    """

    PASS = "PASS"
    FAIL = "FAIL"
    TRACKED = "TRACKED"
    EXCLUDED = "EXCLUDED"


# ADR 0023 §2's table, transcribed. Laid out as expectation -> state -> outcome so it can be read
# against the markdown row by row; keep it that way, so a future edit to the ADR is a visibly
# one-to-one edit here. Both columns are total over the five states - there is no default and no
# fallthrough, because an unmapped state should be a loud KeyError, not a quiet PASS.
_SCORE_TABLE: dict[str, dict[GrounderState, Outcome]] = {
    # in-corpus: we expect a cited answer
    EXPECTATION_ANSWER: {
        GrounderState.GROUNDED: Outcome.PASS,  # pass-rate numerator
        GrounderState.PARTIAL: Outcome.TRACKED,  # in N_scored, NOT in the numerator
        GrounderState.REFUSAL: Outcome.FAIL,  # false refusal (N3)
        GrounderState.INTEGRITY_FLAGGED: Outcome.FAIL,  # structural defect
        GrounderState.ERROR: Outcome.EXCLUDED,  # infra, not a corpus verdict
    },
    # out-of-corpus: we expect an honest decline
    EXPECTATION_REFUSE: {
        GrounderState.GROUNDED: Outcome.FAIL,  # hallucinated
        GrounderState.PARTIAL: Outcome.FAIL,  # asserted when it should refuse
        GrounderState.REFUSAL: Outcome.PASS,
        GrounderState.INTEGRITY_FLAGGED: Outcome.FAIL,
        GrounderState.ERROR: Outcome.EXCLUDED,
    },
}


def score_state(state: GrounderState, expectation: str) -> Outcome:
    """ADR 0023 §2: one Grounder state, scored against what the question expected.

    THE SIGNATURE IS THE INVARIANT. Two parameters, and neither is a `GrounderResult`: a REFUSAL
    carrying `coverage.complete=True` is a shape the shipped Grounder genuinely emits (see
    `_decide`), and scoring it on coverage would report a refusal as a covered answer. Passing
    the whole result and "just reading `.state`" would work today and rot the first time someone
    reaches for another field. Do not widen this signature.

    `expectation` is the raw string from the eval file (ADR 0021 §3), not an enum, so a row scores
    exactly as written - no rehydration step between the file and the verdict where a value could
    be normalised into something the file never said. Unknown values raise rather than defaulting;
    the loader rejects them first, so reaching this is a programmatic caller's bug.

    Because `GrounderState` subclasses `str`, a state replayed from a JSON run report scores
    without rehydrating the enum - `score_state("PARTIAL", "answer")` is the same lookup. That
    is deliberate: V3 re-scoring a stored run must not need V1's objects.
    """
    try:
        row = _SCORE_TABLE[expectation]
    except KeyError:
        raise ValueError(
            f"unknown expectation {expectation!r}; must be one of "
            f"{sorted(_SCORE_TABLE)} (ADR 0021 §3)"
        ) from None
    try:
        return row[state]
    except KeyError:
        raise ValueError(
            f"unknown grounder state {state!r}; ADR 0023 §2 maps exactly "
            f"{[s.value for s in GrounderState]}"
        ) from None


def retrieval_hit(
    expected_sources: Sequence[ExpectedSource], retrieved: Sequence[RetrievedChunk]
) -> bool | None:
    """ADR 0023 §1's retrieval signal: did the top-k contain an expected source? (crude N1.)

    Returns `None` - not `False` - when `expected_sources` is empty. ADR 0023 §1 defines this
    signal PER IN-CORPUS QUESTION, and `expected_sources` is "in-corpus only" (ADR 0021 §3), so
    an out-of-corpus row has no expectation to check: the signal is NOT APPLICABLE, which is a
    different fact from "we looked and missed". Returning `False` there would drag the four
    refuse questions into the recall denominator as guaranteed misses and cap the hit rate at
    8/12 no matter how good retrieval got - a metric that cannot reach 1.0 stops being usable for
    ranking, which is the one job it has.

    ANY-MATCH, not all-match: True iff at least one expected source equals at least one retrieved
    chunk on `(file, page_or_slide)`. This is the settled reading of §1's "was an
    `expected_sources` chunk in the retrieved top-k" - singular, and phrased as membership.

    All-match was considered and REJECTED, on two grounds. (1) It silently couples the metric to
    k: a row carrying MULTIPLE expected sources would have its score depend on the top-k budget in
    a way a single-source row does not - the spikes tune k, and a metric that moves with the lever
    it is measuring cannot rank it. (2) A missed second leg is not lost signal: the answer is then
    supported in part, which surfaces as PARTIAL in the grounding signal. Two signals already cover
    the case, and this is precisely §1's promise that retrieval misses "surface here regardless of
    the final state" while completeness lives in the other column.

    How many multi-source rows exist is deliberately NOT recorded here - derive it if you need it
    (rows where `len(expected_sources) > 1` in eval/questions.jsonl). The argument never needed a
    count; a count would need maintaining.

    Equality is EXACT on both fields. Same file, wrong page is a miss - a citation to the wrong
    page is wrong, not approximately right (the never-span honesty guarantee, ADR 0019).
    """
    if not expected_sources:
        return None

    # Set membership over the provenance pair - `ExpectedSource` and `RetrievedChunk` share the
    # field names and types exactly, which is what makes this comparison legible rather than a
    # mapping step. Chunk ids are NOT compared: the eval file names a page a human can verify,
    # and chunk ids are re-minted by every re-ingest, so a chunk-id rule would go stale the first
    # time the corpus was re-indexed with unchanged content.
    pairs = _expected_pairs(expected_sources)
    return any((chunk.file, chunk.page_or_slide) in pairs for chunk in retrieved)


def _expected_pairs(expected_sources: Sequence[ExpectedSource]) -> frozenset[tuple[str, int]]:
    """The `(file, page_or_slide)` set both retrieval readouts match on - ONE definition of
    "this chunk is an expected source", shared by `retrieval_hit` and `expected_placement`.

    Extracted rather than duplicated for the reason this module's docstring gives about the
    V1/V3 split: a second copy of the match rule is how two "identical" rules quietly stop
    agreeing. Exact equality on BOTH fields stays `retrieval_hit`'s rule and its argument
    (never-span, ADR 0019); this helper only names it.
    """
    return frozenset((source.file, source.page_or_slide) for source in expected_sources)


@dataclass(frozen=True)
class ExpectedPlacement:
    """WHERE the expected source landed in one question's top-k, and by how much (issue #67).

    A DIAGNOSTIC, not a metric, and the distinction is structural rather than stylistic: this
    object is deliberately absent from `EvalRecord`, `GroundingCounts`, `RetrievalCounts` and
    `EvalMetrics`. ADR 0023 §3 fixes the rate vector, nothing here aggregates over a run, and
    adding a rank-derived rate would be a new ranking signal smuggled in as a readout. Pinned by
    `test_expected_placement_is_absent_from_the_adr_0023_vector`.

    It exists because ADR 0023 §1's retrieval signal is an ANY-MATCH membership rule (see
    `retrieval_hit`): `hit=yes` says an expected chunk is SOMEWHERE in the top-k and says nothing
    about what outranks it. `eval/FINDINGS.md` (2026-08-01) is where that bit: q005 scored a clean
    `hit=yes` while four of its five retrieved blocks were a different author arguing the opposite
    case, and finding that out took a separate hand-written probe. This is that probe, in the
    core - here rather than in the runner for the same reason `retrieval_hit` is (module
    docstring).

    `ranks` carries EVERY matching position, not just the best one. Anchoring on the best rank
    alone and printing "rank 2" would repeat this issue's own sin: one summary number hiding what
    the top-k actually contained. `rank` and `found` are properties derived from it - the same
    construction `EvalMetrics` uses for its rates, so no reported figure can disagree with the
    tally it came from.

    `margin` is `None` when there is no row below the best rank, and `0.0` on a tie. The two are
    NOT interchangeable (`_rate` carries the general argument): `None` is "nothing was measured",
    `0.0` is a measurement.
    """

    ranks: tuple[int, ...]
    total: int
    margin: float | None

    @property
    def rank(self) -> int | None:
        """The best (lowest) matching rank, 1-based - or `None` when nothing matched.

        `None` here means "we looked in the top-k and the expected source was not there", never
        "not applicable": that second fact is the `None` `expected_placement` itself returns.
        """
        return self.ranks[0] if self.ranks else None

    @property
    def found(self) -> bool:
        """Whether any expected source appeared at all - the same bit `retrieval_hit` reports."""
        return bool(self.ranks)


def expected_placement(
    expected_sources: Sequence[ExpectedSource], retrieved: Sequence[RetrievedChunk]
) -> ExpectedPlacement | None:
    """Where the expected source(s) landed in this top-k, and the margin over the next result.

    Returns `None` - not an empty placement - when `expected_sources` is empty, mirroring
    `retrieval_hit`'s tri-state for the identical reason (ADR 0021 §3): an out-of-corpus row has
    NO EXPECTATION, which is a different fact from "we looked and missed". Inside the returned
    object, `rank is None` means the second thing. The two `None`s are never the same claim, and
    a caller that coalesces them reports a miss the suite never scored.

    RANK IS POSITION IN THE SEQUENCE AS HANDED, 1-based, never a re-sort. `AskResult.retrieved`
    is contractually "the EXACT top-k list handed to the Grounder, in rank order", so sorting
    here would report a rank the Grounder never saw. A list that is not score-ordered therefore
    yields a NEGATIVE margin, reported signed rather than clamped - that is a real upstream
    defect and worth surfacing, not smoothing.

    MARGIN IS `score[rank] - score[rank+1]`: the expected row's score minus the row IMMEDIATELY
    BELOW it. Headroom, not deficit. ADR 0026 §2 ("Not a margin") fixes the reading - it records
    that q007's expected page "ranks first" while what ranked second was never captured, so the
    margin was unmeasured; the deficit reading (`score[1] - score[rank]`) is undefined at rank 1,
    the one case that ADR wants a margin for.

    `None` at the last row (there is nothing below it), `0.0` on a tie. The distinction is
    load-bearing - the argument `_rate` carries about an empty denominator, applied one layer
    down: a computed zero must never be indistinguishable from a measurement never made, and
    `+0.0000` reads as "tied with the next row".

    TIES HAVE NO STABLE ORDER. `retrieve()`'s SQL is `order by distance asc` with no tiebreak, so
    a `0.0` margin means "the rank boundary here is arbitrary" - it may swap between runs - not
    "this row barely won".

    The margin is a difference of two normalized similarities (ADR 0017, clamped per ADR 0024),
    so it lies in [0,1] for a score-ordered list. It is comparable WITHIN ONE QUERY ONLY: ADR
    0017 rejected batch-relative normalization precisely because corpus-relative scores are not
    comparable across queries. Do not rank questions by margin.

    `k` is not a parameter and must not become one: this is defined over the sequence it is
    handed, so `total` is `len(retrieved)`. `retrieve()` already returns <= k, so a short list is
    normal; a longer one is an upstream contract violation that still renders rather than raising.

    NEVER RAISES, for any input shape - an empty list, a single row, duplicates, unsorted scores.
    It is a readout printed beside a bench run; a diagnostic that can abort the run it is
    describing is worse than no diagnostic.
    """
    if not expected_sources:
        return None

    pairs = _expected_pairs(expected_sources)
    rows = list(retrieved)
    ranks = tuple(
        position
        for position, chunk in enumerate(rows, start=1)
        if (chunk.file, chunk.page_or_slide) in pairs
    )

    margin: float | None = None
    if ranks:
        best = ranks[0]
        # `best < len(rows)` is the last-row guard, and it is what makes `None` mean "undefined"
        # rather than "zero": at the last rank there is no `rows[best]` to subtract.
        if best < len(rows):
            margin = rows[best - 1].score - rows[best].score

    return ExpectedPlacement(ranks=ranks, total=len(rows), margin=margin)


@dataclass(frozen=True)
class EvalRecord:
    """One question's observed run - the minimum a metric needs, and nothing more.

    Deliberately NOT a `GrounderResult` plus an `EvalQuestion`: this is the seam where scoring
    stops being able to see `coverage` at all (see the module docstring). Whoever builds these
    records reads `result.state` once, in the runner, in the open.

    `hit` is the tri-state `retrieval_hit` returns - `None` means "not applicable to this
    question", never "missed". It defaults to `None` so a refuse row reads as
    `EvalRecord("refuse", state)` without a filler argument that would look like a measurement.
    """

    expectation: str
    state: GrounderState
    hit: bool | None = None


@dataclass(frozen=True)
class GroundingCounts:
    """The raw state tally for ONE expectation type, plus its denominator.

    Counts, not rates, are the primitive here - the rates on `EvalMetrics` are computed from
    these. That ordering is not stylistic: the suite is 8 in-corpus and 4 out-of-corpus questions,
    so a single question moves the primary rate by 12.5 or 25 points, and a report that prints
    "75%" without "6/8" invites reading noise as a result. ADR 0023 §4 makes the same point at the
    other end - ranking is distribution-based, not a single scalar, so the distribution has to be
    printable.
    """

    total: int  # every record of this expectation, ERROR included
    scored: int  # N_scored = the non-EXCLUDED ones (ADR 0023 §2) - every rate's denominator
    grounded: int
    partial: int
    refusal: int
    integrity_flagged: int
    error: int

    @property
    def hallucinated(self) -> int:
        """GROUNDED + PARTIAL - meaningful on the REFUSE side, where both mean "asserted".

        The FAIL bucket on a refuse question is not one failure mode: INTEGRITY_FLAGGED is a
        structural defect, while GROUNDED or PARTIAL means the model answered a question its
        corpus does not cover - it made something up. That is the failure this product exists to
        prevent, and a single `fail_rate` would bury it among format slips. Kept as a count so
        "1 of 4" reads as the small, alarming number it is.
        """
        return self.grounded + self.partial


@dataclass(frozen=True)
class RetrievalCounts:
    """The retrieval signal, over the in-corpus questions where it applies (ADR 0023 §1).

    `applicable` counts rows with a non-`None` hit, which is what keeps the refuse questions out
    of the denominator (see `retrieval_hit`). Note what is NOT filtered here: the state. A row
    that ended in ERROR still counts if retrieval ran, because §1 says retrieval misses surface
    "regardless of the final state" - excluding them would make the two signals depend on each
    other, which is the conflation the split exists to prevent. A row whose retrieval never
    happened carries `hit=None` and drops out on THAT basis, not on its state.
    """

    applicable: int
    hits: int


def _rate(numerator: int, denominator: int) -> float | None:
    """`numerator / denominator`, or `None` when there is nothing to divide.

    `None`, never `0.0`. An empty suite has no pass rate; reporting one as 0% makes "we ran
    nothing" indistinguishable from "we ran everything and failed" - and the second is the one
    that should stop a bake-off. Callers must render `None` as "n/a" and not coalesce it.
    """
    return numerator / denominator if denominator else None


@dataclass(frozen=True)
class EvalMetrics:
    """ADR 0023 §3's metric vector: primaries, secondaries over the SAME denominator, health.

    Rates are properties over the stored counts, so no rate can disagree with the tally it came
    from. Every rate is `float | None` - see `_rate`.
    """

    answer: GroundingCounts
    refuse: GroundingCounts
    retrieval: RetrievalCounts

    # --- Primary: what a bake-off ranks on (ADR 0023 §3) ---------------------------------------
    @property
    def grounded_pass_rate(self) -> float | None:
        """GROUNDED / N_scored, in-corpus. The crude V1 proxy for N2."""
        return _rate(self.answer.grounded, self.answer.scored)

    @property
    def correct_refusal_rate(self) -> float | None:
        """REFUSAL / N_scored, out-of-corpus. The crude V1 proxy for N3."""
        return _rate(self.refuse.refusal, self.refuse.scored)

    # --- Secondary: same denominator, deliberately (ADR 0023 §3) --------------------------------
    # The shared denominator IS the anti-gaming property, not a convenience. If PARTIAL were
    # dropped from N_scored, trading a FAIL for a PARTIAL would shrink the denominator and RAISE
    # the pass rate - rewarding the trade. Keeping it in leaves `grounded_pass_rate` flat while
    # `partial_rate` moves, so the shift is observable rather than buried (the ADR's own 70/20/10
    # vs 70/0/30 example, pinned by `test_fail_to_partial_trade_leaves_primary_flat`).
    @property
    def partial_rate(self) -> float | None:
        """The tracked bucket, in-corpus. Not a pass, not a fail - watch it move."""
        return _rate(self.answer.partial, self.answer.scored)

    @property
    def integrity_flag_rate(self) -> float | None:
        """Structural defects, in-corpus. A high value means the PROMPT needs tuning."""
        return _rate(self.answer.integrity_flagged, self.answer.scored)

    @property
    def false_refusal_rate(self) -> float | None:
        """REFUSAL on an in-corpus question - the product's most annoying failure (N3)."""
        return _rate(self.answer.refusal, self.answer.scored)

    @property
    def hallucination_rate(self) -> float | None:
        """GROUNDED-or-PARTIAL on an out-of-corpus question: it answered anyway.

        The refuse side's FAIL decomposition. `correct_refusal_rate` alone cannot distinguish a
        run that flagged four malformed refusals from one that invented four answers, and only
        the second breaks the trust promise.
        """
        return _rate(self.refuse.hallucinated, self.refuse.scored)

    @property
    def refuse_integrity_flag_rate(self) -> float | None:
        """The other half of the refuse-side FAIL bucket - a defect, not a fabrication."""
        return _rate(self.refuse.integrity_flagged, self.refuse.scored)

    # --- Health: excluded from ranking, never from the report (ADR 0023 §3) ---------------------
    @property
    def error_count(self) -> int:
        """Every ERROR, both expectation types. Excluded from ranking, LOUD in the report.

        ADR 0023 §3: ERROR leaves the denominator because infra is not a corpus verdict, but "a
        FAIL->ERROR escape hatch is visible" only if this is printed beside the rates. Many
        ERRORs is itself a red flag - and, worse, silently shrinks N_scored, so a run with 6 of 8
        in-corpus questions erroring can post a perfect 2/2 pass rate. The per-type counts on
        `answer.error` / `refuse.error` say which side lost its denominator.
        """
        return self.answer.error + self.refuse.error

    @property
    def error_rate(self) -> float | None:
        """ERRORs over ALL records - the only rate whose denominator INCLUDES the excluded rows.

        It has to be: a rate of ERRORs over non-ERRORs would be 0 exactly when everything errored.
        """
        total = self.answer.total + self.refuse.total
        return _rate(self.error_count, total)

    # --- Retrieval: the other signal (ADR 0023 §1) ----------------------------------------------
    @property
    def retrieval_hit_rate(self) -> float | None:
        """Crude recall@k proxy (N1), over in-corpus questions only."""
        return _rate(self.retrieval.hits, self.retrieval.applicable)


def _counts(states: Counter[GrounderState], expectation: str, total: int) -> GroundingCounts:
    """Tally -> `GroundingCounts`, with N_scored DERIVED from the scoring map.

    `scored` is computed by asking `score_state` which states are EXCLUDED, not by testing
    `state is ERROR`. The two agree today; only the first still agrees if ADR 0023 ever excludes
    a second state. One definition of the denominator, in the table, where the ADR put it.
    """
    excluded = sum(
        count
        for state, count in states.items()
        if score_state(state, expectation) is Outcome.EXCLUDED
    )
    return GroundingCounts(
        total=total,
        scored=total - excluded,
        grounded=states[GrounderState.GROUNDED],
        partial=states[GrounderState.PARTIAL],
        refusal=states[GrounderState.REFUSAL],
        integrity_flagged=states[GrounderState.INTEGRITY_FLAGGED],
        error=states[GrounderState.ERROR],
    )


def compute_metrics(records: Sequence[EvalRecord]) -> EvalMetrics:
    """Score a whole run: ADR 0023 §3's rate vector, with the counts it was computed from.

    Every record is passed through `score_state` on the way in - not for its verdict (the tallies
    are per-state), but because that is the one place `expectation` is validated and the one
    place EXCLUSION is defined. A record with an unrecognised expectation raises here rather than
    being dropped into neither bucket, which would silently shrink a denominator: the failure
    mode this whole module is built to avoid is a rate that looks fine over a set that quietly
    lost members.

    An empty `records` is legal and returns all-zero counts with `None` rates - "we scored
    nothing" said honestly, rather than a 0% that reads like a result.
    """
    per_expectation: dict[str, Counter[GrounderState]] = {
        EXPECTATION_ANSWER: Counter(),
        EXPECTATION_REFUSE: Counter(),
    }
    totals: Counter[str] = Counter()
    applicable = 0
    hits = 0

    for record in records:
        # Validates `expectation` (and the state) at the door. The returned outcome is not needed
        # here - the per-state tally carries strictly more information than the four buckets.
        score_state(record.state, record.expectation)
        per_expectation[record.expectation][record.state] += 1
        totals[record.expectation] += 1

        # Retrieval is scored only where it APPLIES: in-corpus, and with a hit actually measured.
        # `is not None` rather than truthiness - `hit=False` is a measured miss and must count in
        # the denominator, which is exactly the row a `if record.hit:` bug would drop, inflating
        # the hit rate toward 1.0 by discarding the misses.
        if record.expectation == EXPECTATION_ANSWER and record.hit is not None:
            applicable += 1
            hits += int(record.hit)

    return EvalMetrics(
        answer=_counts(
            per_expectation[EXPECTATION_ANSWER], EXPECTATION_ANSWER, totals[EXPECTATION_ANSWER]
        ),
        refuse=_counts(
            per_expectation[EXPECTATION_REFUSE], EXPECTATION_REFUSE, totals[EXPECTATION_REFUSE]
        ),
        retrieval=RetrievalCounts(applicable=applicable, hits=hits),
    )
