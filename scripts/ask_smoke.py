"""Slice 1 exit test — the smoke-suite runner (issue #8).

Proves the differentiator end to end, over REAL course materials and REAL models: every question
in `eval/questions.jsonl` goes through `gct.ask.ask()`, and the run passes only if at least one
in-corpus question came back GROUNDED (a cited answer) and at least one out-of-corpus question came
back REFUSAL (an honest decline). Everything else it prints — the ADR 0023 rate vector — is the
crude bench Spike Pass 1 ranks on, not a release gate (ADR 0023 §5).

A THIN peer caller (ADR 0009): this script wires providers, converges the corpus, loops, and
prints. Every METRIC it reports is computed in `gct` — the scoring table is `gct.eval.scoring`,
the five states are the Grounder's, the retrieval check is `retrieval_hit`, the rank/margin are
`expected_placement`, and the sentence split behind `uncited_sentence_rate` is
`measure_attribution`. If you find yourself adding a scoring rule here, it belongs one layer down,
where V3 can execute the same definition.

"Metric", not "judgment", and the narrowing is deliberate: the exit GATE below is computed here,
on purpose. It is issue #8's one-time acceptance ceremony, not an ADR 0023 measurement, and V3
will never re-execute it — pushing it into `gct` would enshrine a slice-exit ritual in the library
forever. Setup VALIDITY rules (is this run scoreable at all?) likewise belong here: they are facts
about a corpus and a directory, which the core has no access to and no opinion about.

Run (needs a migrated DB, `.env` secrets, and the corpus files present — this SPENDS MONEY):

    uv run python scripts/ask_smoke.py
    uv run python scripts/ask_smoke.py --suite smoke --k 5
    uv run python scripts/ask_smoke.py --only q005 --verbose    # a probe, not a bench run
    uv run python scripts/ask_smoke.py --only q005 --show-retrieved   # why it scored that way
    uv run python scripts/ask_smoke.py --chunk-size 400 --chunk-overlap 60 --owner nate-400-60

`--only` re-asks a hand-picked subset cheaply (issue #60's generation probe), `--verbose` prints
what the model actually wrote — annotated per sentence with whether it carried an `[S#]` label
(issue #66's attribution probe) — and `--show-retrieved` prints the top-k that was retrieved with
the expected source's rank and margin (issue #67's retrieval probe — the other, unconflated signal:
ADR 0023 §1's `hit` is an ANY-MATCH rule, so `hit=yes` says nothing about what outranked the
expected chunk). All three FLAGS are strictly additive: none changes what a default run prints, and
a default run's bytes are unchanged by any of them. Read that as the claim it is — it is about the
flags, not about the whole script. Issue #66's POOLED attribution block is not flag-gated at all:
`_print_attribution` runs on every full run, beside the rate vector, and does change a default
run's bytes relative to the run before #66. Only the per-sentence annotation rides `--verbose`.
Cheap assumes
the owner already HAS a converged corpus: convergence walks the corpus dir, never the question
list, so under a fresh `--owner` even `--only q005` pays the full ingest first.

A new chunk window needs a fresh `--owner`, as above: the corpus is converged PER OWNER and no
column records which window produced a chunk set, so a second window under an owner that already
has a corpus scores the first window's chunks. See `_print_census`, which prints the window for
exactly that reason.

Exit codes: 0 the gate passed, or a `--only` run completed (the gate does not run on a subset —
see `_print_filtered_notice`) · 1 the gate failed · 2 setup failed (no corpus, no questions, a
question naming a file the corpus does not have, an `--only` id no question carries, no API key,
no reachable database).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from uuid import uuid4

import psycopg
from openai import OpenAIError

from gct.ask import AskResult, ask
from gct.config import load_settings
from gct.db import connect
from gct.eval.questions import EXPECTATION_ANSWER, EXPECTATION_REFUSE, EvalQuestion, load_questions
from gct.eval.scoring import (
    AnswerAttribution,
    AttributionTotals,
    EvalMetrics,
    EvalRecord,
    ExpectedPlacement,
    aggregate_attribution,
    compute_metrics,
    expected_placement,
    measure_attribution,
    retrieval_hit,
    score_state,
)
from gct.grounder.answer import GrounderResult, GrounderState
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.ingest.pipeline import ingest_file
from gct.providers.base import Embeddings
from gct.providers.openai_provider import OpenAIEmbeddings, OpenAIGeneration
from gct.retriever.retrieve import DEFAULT_K, RetrievedChunk

DEFAULT_OWNER = "nate-dogfood"
DEFAULT_CORPUS_DIR = "data/dogfood/religion"
DEFAULT_QUESTIONS = "eval/questions.jsonl"
DEFAULT_SUITE = "smoke"

# The extensions `gct.ingest.parse` handles. Kept as data so the census and the ingest loop walk
# the SAME set — a corpus file the census counts but never ingests would read as a silent gap.
CORPUS_GLOBS = ("*.pdf", "*.pptx")

# What `data/dogfood/religion/manifest.md` says the default corpus parses to: 7+8+8 slides and
# 15+14 pages. PRINTED FOR THE EYE, never compared against: chunking re-packs parsed units into
# chunks (ingest/chunk.py), so chunks != units is the expected relationship, not a discrepancy.
# Stale-by-construction if the corpus changes — which is why nothing branches on it.
MANIFEST_PARSED_UNITS = 52
MANIFEST_FILES = 5

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_SETUP = 2


class SetupError(RuntimeError):
    """The bench cannot honestly run: no corpus, no questions, a missing expected file.

    Distinct from a failed gate. A gate failure is a RESULT (the system answered badly); this is
    the run never having been valid — scoring it would report a rate over a suite that quietly
    lost its ground truth. Exits 2 so a caller can tell "it ran and lost" from "it never ran".
    """


# --- setup: questions, class, corpus convergence ----------------------------------------------


def _resolve_class(conn: psycopg.Connection, owner_id: str, slug: str) -> str:
    """Find (or create) the `classes` row this suite's questions are scoped to; return its id.

    `classes` has no unique constraint on `(owner_id, name)`, so a duplicate is physically
    possible and is refused rather than resolved: picking either one would split the corpus across
    two class_ids, and every count printed below would then be over half a class.
    """
    rows = conn.execute(
        "select class_id from classes where owner_id = %(owner_id)s and name = %(name)s",
        {"owner_id": owner_id, "name": slug},
    ).fetchall()

    if len(rows) > 1:
        raise SetupError(
            f"owner {owner_id!r} has {len(rows)} classes named {slug!r} "
            f"({[str(r[0]) for r in rows]}) — the corpus is split; drop the extras first"
        )
    if rows:
        # str, not the psycopg UUID: every `gct` signature downstream types class_id as `str` and
        # casts `%s::uuid` at the SQL boundary.
        return str(rows[0][0])

    class_id = str(uuid4())
    with conn.transaction():
        conn.execute(
            """
            insert into classes (class_id, owner_id, name)
            values (%(class_id)s::uuid, %(owner_id)s, %(name)s)
            """,
            {"class_id": class_id, "owner_id": owner_id, "name": slug},
        )
    print(f"  created class {slug!r} for owner {owner_id!r} — class_id={class_id}")
    return class_id


def _ready_rows(
    conn: psycopg.Connection, owner_id: str, class_id: str, filename: str
) -> list[tuple[str, int]]:
    """Every `ready` files row for this filename in this class, as `(file_id, chunk_count)`.

    Keyed on FILENAME because that is the only identity the corpus directory and the DB share:
    `file_id` is minted fresh by each `ingest_file` call and is not derived from the path.
    Non-`ready` rows are excluded — `index_file` publishes `ready` inside the same transaction as
    the chunk insert (ADR 0020, publication conditional per ADR 0025 - satisfied here by the
    autocommit at wiring), so in Slice 1 a non-ready row means a file with no chunks, which must
    not count as ingested.
    """
    rows = conn.execute(
        """
        select f.file_id, count(c.chunk_id)
        from files f
        left join chunks c on c.file_id = f.file_id
        where f.owner_id = %(owner_id)s
          and f.class_id = %(class_id)s::uuid
          and f.filename = %(filename)s
          and f.status = 'ready'
        group by f.file_id
        order by f.created_at
        """,
        {"owner_id": owner_id, "class_id": class_id, "filename": filename},
    ).fetchall()
    return [(str(file_id), int(count)) for file_id, count in rows]


def _converge_corpus(
    conn: psycopg.Connection,
    *,
    owner_id: str,
    class_id: str,
    corpus_dir: Path,
    embedder: Embeddings,
    chunk_size: int,
    chunk_overlap: int,
) -> tuple[dict[str, int], int]:
    """Bring the class to "every corpus file ingested exactly once". Returns
    `(filename -> chunks, files actually ingested THIS run)`.

    The second element exists for `_print_census`: the requested window applies only to files this
    call actually ingests, so the census must not label already-`ready` chunks with it. Only this
    function can tell the two apart.

    PER FILE, not all-or-nothing, and that is the whole point. `ingest_file` mints a FRESH
    `file_id` on every call (pipeline.py), and `index_file`'s idempotency is delete-by-file_id —
    so it can only ever replace a set within one file_id, never across runs. A runner that
    ingested unconditionally would therefore ADD a complete duplicate chunk set every time it ran:
    the class doubles, retrieval fills its top-k with copies of one slide, and the census silently
    inflates. Convergence (check, then ingest only what is missing) is what makes this script safe
    to re-run, which a bench has to be.

    `chunk_size`/`chunk_overlap` are required rather than defaulted, for the same reason `embedder`
    is: they reach `ingest_file`, and the caller prints them (`_print_census`), so a default here
    would let the printed window and the ingested one drift apart. They apply only to files this
    call actually INGESTS — an already-`ready` file keeps whatever window produced it.
    """
    paths = sorted(
        {path for pattern in CORPUS_GLOBS for path in corpus_dir.glob(pattern)},
        key=lambda p: p.name,
    )
    if not paths:
        raise SetupError(
            f"no {' / '.join(CORPUS_GLOBS)} files in {corpus_dir} — the dogfood corpus is "
            "gitignored (manifest.md lists what to drop in)"
        )

    ready: dict[str, int] = {}
    ingested = 0
    for path in paths:
        rows = _ready_rows(conn, owner_id, class_id, path.name)

        if len(rows) > 1:
            # Not fatal, but never silent: N ready rows means the file was ingested N times, so
            # every chunk exists N times. Retrieval still works and still cites the right page —
            # it just spends its top-k budget on near-identical neighbours, and every count below
            # is multiplied. We do NOT auto-delete: dropping chunk sets is not a bench's call.
            total = sum(count for _, count in rows)
            print(f"  !! WARN  {path.name}: {len(rows)} ready `files` rows, {total} chunks total")
            print(f"           file_ids: {', '.join(file_id for file_id, _ in rows)}")
            print(
                "           this file was ingested more than once — its chunks are DUPLICATED, "
                "the census below is inflated,"
            )
            print(
                "           and retrieval's top-k will be crowded with copies. To fix: delete "
                "the extra file_ids' chunks rows,"
            )
            print("           then those files rows (chunks FK -> files, so chunks go first).")
            ready[path.name] = total
            continue

        if rows:
            _file_id, count = rows[0]
            print(f"  {path.name}: already ingested ({count} chunks)")
            ready[path.name] = count
            continue

        print(f"  {path.name}: ingesting (parse -> chunk -> embed -> index) ...", flush=True)
        file_id = ingest_file(
            path,
            owner_id,
            class_id,
            embedder=embedder,
            conn=conn,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        count = conn.execute(
            "select count(*) from chunks where file_id = %(file_id)s::uuid",
            {"file_id": file_id},
        ).fetchone()[0]
        print(f"  {path.name}: ingested — {count} chunks (file_id={file_id})")
        ready[path.name] = int(count)
        ingested += 1

    return ready, ingested


def _indexed_pages(
    conn: psycopg.Connection, *, owner_id: str, class_id: str
) -> set[tuple[str, int]]:
    """Every `(file, page_or_slide)` this class actually has an indexed chunk for.

    The exact pair `retrieval_hit` compares against, read back from the same scope retrieval will
    search. `page_or_slide` is `text` in the column and `int` everywhere else, so it converts here —
    the same boundary conversion `retriever.retrieve` does on read.
    """
    rows = conn.execute(
        """
        select distinct file, page_or_slide from chunks
        where owner_id = %(owner_id)s and class_id = %(class_id)s::uuid
        """,
        {"owner_id": owner_id, "class_id": class_id},
    ).fetchall()
    return {(file, int(page)) for file, page in rows}


def _require_expected_files(
    questions: list[EvalQuestion],
    ready: dict[str, int],
    indexed: set[tuple[str, int]],
) -> None:
    """Every source the suite cites must be in the corpus, `ready` — or the run is not scoreable.

    Hard failure, not a warning: `retrieval_hit` compares `(file, page_or_slide)` pairs, so a file
    that was never ingested scores as a MISS on every question that cites it — an honest-looking
    0% recall that is really a missing file. That is the one way this bench could report a system
    failure that never happened.
    """
    # An in-corpus question with NO expected sources is unscoreable on the retrieval signal, and
    # unscoreable SILENTLY: `retrieval_hit([], ...)` returns None (correct - that is the
    # out-of-corpus "not applicable" answer), `compute_metrics` drops None rows from the
    # denominator (correct), and the loop below iterates SOURCES, so a row carrying none
    # contributes nothing to check. Four correct behaviours compose into a hole: the row leaves
    # the recall denominator with no warning, and `retrieval_hit_rate` reports a clean rate over a
    # suite that quietly shrank - the exact failure `gct.eval.scoring`'s docstrings are built to
    # prevent, in the one place no guard covered. Caught here because it is a fact about the eval
    # FILE, which the loader deliberately does not judge (questions.py: "does not judge CONTENT").
    sourceless = [
        question.id
        for question in questions
        if question.expectation == EXPECTATION_ANSWER and not question.expected_sources
    ]
    if sourceless:
        raise SetupError(
            "in-corpus question(s) carry no 'expected_sources': "
            + ", ".join(repr(qid) for qid in sourceless)
            + " — they would leave the retrieval denominator silently, inflating hit rate"
        )

    expected = sorted(
        {source.file for question in questions for source in question.expected_sources}
    )
    missing = [name for name in expected if name not in ready]
    if missing:
        raise SetupError(
            "the suite expects sources from file(s) that are not ingested in this class: "
            + ", ".join(repr(name) for name in missing)
            + " — retrieval cannot hit a file the corpus does not have"
        )

    # And the PAGE, not just the file. `retrieval_hit` compares the whole `(file, page_or_slide)`
    # pair, so a page carrying no indexed chunk is exactly as unhittable as a missing file — the
    # check above just projects the pair down to its filename and throws the other half away.
    # This is the third face of one bug: the sourceless-row guard above, the missing-file guard
    # above it, and this all close the same hole — a clean-looking rate over ground truth that
    # cannot be hit — and the page is the half this corpus is actively ambiguous about, since
    # `Livingston Cosmogony.pdf` index-page 4 is PRINTED "200". Writing 200 there would post
    # `hit=no` on q007 with the report blaming retrieval.
    # It also catches a `ready` files row whose chunks were deleted (the WARN branch in
    # `_converge_corpus` above prints a two-step remediation; stop between the steps and you are in
    # exactly that state): such a file contributes no pages, so every source naming it lands here
    # rather than being scored as a retrieval regression.
    # The two cases differ in CONSEQUENCE, not in whether to stop, so the message names which one
    # you have. On a SINGLE-source row an unindexed page is a guaranteed permanent miss. On a
    # MULTI-source row the any-match rule means a surviving good entry still hits, so the metric
    # holds while the ground truth quietly weakens - worth stopping for, but an author who reads
    # "cannot be hit" and knows their run scored fine deserves to be told which of the two it is.
    unindexed = sorted(
        {
            (question.id, source.file, source.page_or_slide, len(question.expected_sources) > 1)
            for question in questions
            for source in question.expected_sources
            if (source.file, source.page_or_slide) not in indexed
        }
    )
    if unindexed:
        raise SetupError(
            "the suite expects source page(s) that carry no indexed chunk: "
            + ", ".join(
                f"{qid} -> {name!r} p.{page}"
                + (
                    " (multi-source row: any-match would still hit, so this weakens the ground "
                    "truth rather than the metric)"
                    if multi
                    else " (single-source row: a guaranteed permanent miss)"
                )
                for qid, name, page, multi in unindexed
            )
            + " — retrieval cannot hit a page the corpus does not have, and scoring that as a "
            "miss reports a retrieval failure that is really bad ground truth"
        )


def _print_census(
    conn: psycopg.Connection,
    *,
    owner_id: str,
    class_id: str,
    ready: dict[str, int],
    chunk_size: int,
    chunk_overlap: int,
    ingested: int,
) -> None:
    """One line of ground truth about what the questions below are actually being asked of.

    The chunk window prints HERE because nothing else can report it. `_converge_corpus` skips any
    file already `ready` for this owner, matching on FILENAME alone, and no column records which
    window produced a chunk set — so a second `--chunk-size`/`--chunk-overlap` under an owner that
    already has a corpus silently scores the FIRST window's chunks. That is why the window prints
    beside `ingested`, the count of files this run actually chunked at it: the REQUESTED window is
    a fact about the command line, and only the files ingested this run are known to carry it —
    labelling skipped files with it would state as stored fact something no column records. Vary
    the window under a fresh `--owner`.
    """
    total_chunks = conn.execute(
        """
        select count(*) from chunks
        where owner_id = %(owner_id)s and class_id = %(class_id)s::uuid
        """,
        {"owner_id": owner_id, "class_id": class_id},
    ).fetchone()[0]
    if ingested == len(ready):
        window_note = f"all ingested this run at chunk window {chunk_size}/{chunk_overlap} words"
    elif ingested == 0:
        window_note = (
            f"none ingested this run — requested window {chunk_size}/{chunk_overlap} words did "
            "not apply; stored chunks keep the window that made them"
        )
    else:
        window_note = (
            f"{ingested} of {len(ready)} file(s) ingested this run at the requested window "
            f"{chunk_size}/{chunk_overlap} words; the rest keep the window that made them"
        )
    print(
        f"  census: {len(ready)} file(s) ready — {sum(ready.values())} chunks across them, "
        f"{total_chunks} in the class, {window_note}"
    )
    print(
        f"  reference: manifest.md describes {MANIFEST_FILES} files / {MANIFEST_PARSED_UNITS} "
        "parsed units — chunks re-pack units, so these differ by design (not checked)"
    )


# --- report -----------------------------------------------------------------------------------


def _clip(text: str, limit: int = 80) -> str:
    """Flatten whitespace and cut to `limit` — a per-question line has to stay one line."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _detail(result: GrounderResult) -> str:
    """The one interesting field for THIS state — what a human scans the line for.

    Deliberately state-dependent: citations are the evidence on an answer, but on a REFUSAL the
    interesting fact is what the model said it could not cover, and on an ERROR it is the kind.
    """
    if result.state is GrounderState.ERROR and result.error is not None:
        return f"error: {result.error.kind} — {_clip(result.error.message)}"
    if result.state is GrounderState.INTEGRITY_FLAGGED:
        reasons = result.integrity.reasons
        first = _clip(reasons[0]) if reasons else "(no reason recorded)"
        return f"integrity: {first} ({len(reasons)} reason(s))"
    if result.state is GrounderState.REFUSAL:
        gaps = result.coverage.gaps
        return f"gaps: {_clip('; '.join(gaps))}" if gaps else "gaps: (none stated)"
    if not result.citations:
        return "citations: (none)"
    # Unique (file, page) in citation order — two labels can resolve to the same page, and the
    # line is about WHICH PAGES the answer rests on, not how many labels it used.
    seen: list[str] = []
    for citation in result.citations:
        rendered = f"{citation.file} p.{citation.page_or_slide}"
        if rendered not in seen:
            seen.append(rendered)
    return "citations: " + ", ".join(seen)


def _print_question_line(question: EvalQuestion, result: AskResult, record: EvalRecord) -> None:
    hit = {True: "yes", False: "no", None: "n/a"}[record.hit]
    outcome = score_state(record.state, record.expectation).value
    print(
        f"  {question.id:<5} {question.expectation:<7} {record.state.value:<18} "
        f"hit={hit:<4} outcome={outcome:<9} {_detail(result.result)}",
        flush=True,
    )


def _retrieved_row(rank: int, chunk: RetrievedChunk, expected: bool) -> str:
    """One row of the `--show-retrieved` table: rank, score, provenance, expected marker.

    FILENAMES ARE NEVER CLIPPED, unlike the gap and error text on the summary line, which
    `_detail` clips at 80 chars. The file+page pair IS the payload here: it is the exact pair
    `retrieval_hit` compares and the exact pair a citation names, so a truncated
    `Lecture 20 Evolutionist-Crea...` would defeat the only purpose the block has — telling the
    reader WHICH slide outranked which. A long name is allowed to make the line wide; clipping it
    would make the line wrong.

    Scores print at 4 decimals to match `eval/FINDINGS.md`, so a row pasted from a run is directly
    comparable with the measurements recorded there.
    """
    marker = "   <-- expected" if expected else ""
    return f"          {rank:>4}  {chunk.score:.4f}  {chunk.file} p.{chunk.page_or_slide}{marker}"


def _placement_line(placement: ExpectedPlacement | None) -> str:
    """The sentence `hit=yes` structurally cannot say: where the expected source landed.

    Every MEASURED figure in it comes from `expected_placement` (ADR 0009); the only arithmetic
    here is `rank + 1`, which names the row the margin was measured against rather than measuring
    anything. The three `None`-shaped cases are deliberately worded so a reader cannot confuse
    them, because they are three different facts:

      - `placement is None` — the row is out-of-corpus and carries no `expected_sources` at all,
        so there is NOTHING TO LOOK FOR (ADR 0021 §3). Not a miss.
      - `not placement.found` — we looked at the whole top-k and it was not there. A measured
        miss, and it prints NO margin number: there is no rank to have a margin from, and a `0.0`
        there would be a figure nobody measured.
      - `placement.margin is None` — found, at the last row, so nothing ranks below it. Also not
        `0.0`, for the reason `_pct` gives about rates: a computed zero must never look like a
        measurement that was never made. `+0.0000` is reserved for the tie, where it IS one.
    """
    if placement is None:
        return (
            "          expected source: n/a — an out-of-corpus row carries no expected_sources "
            "(ADR 0021 §3)"
        )
    if not placement.found:
        return (
            f"          expected source: ABSENT from the top-{placement.total} — "
            "no rank, no margin (hit=no)"
        )

    rank = placement.rank
    # Extras are NAMED, never summarised away. A bare "rank 2" on a source that also sits at rank
    # 4 hides composition, which is the exact failure this whole block exists to fix.
    extras = placement.ranks[1:]
    if len(extras) == 1:
        also = f" (also at rank {extras[0]})"
    elif extras:
        also = f" (also at ranks {', '.join(str(r) for r in extras)})"
    else:
        also = ""

    if placement.margin is None:
        tail = "margin n/a (last row — nothing ranked below it)"
    elif placement.margin == 0.0:
        # A MEASURED zero, and the note says what it means: `retrieve()` orders by distance with
        # no tiebreak, so which of two tied rows came first is arbitrary and may swap between runs.
        tail = (
            f"margin over rank {rank + 1} = {placement.margin:+.4f} "
            "(tied — the rank boundary here is arbitrary)"
        )
    else:
        # Signed, never clamped: a negative margin means the list was handed to us out of score
        # order, which is an upstream contract violation worth seeing rather than smoothing.
        tail = f"margin over rank {rank + 1} = {placement.margin:+.4f}"

    return f"          expected source: rank {rank} of {placement.total}{also}, {tail}"


def _print_retrieved(question: EvalQuestion, result: AskResult) -> None:
    """`--show-retrieved` (issue #67): the top-k this answer was actually built from.

    STRICTLY ADDITIVE — the same bargain `_print_verbose` makes, and for the same reason: it
    prints AFTER `_print_question_line` and touches nothing above, so a default run's output stays
    byte-identical and a probe run can be diffed against the run it is probing.

    WHY THIS EXISTS. `retrieval_hit` is an ANY-MATCH membership rule (ADR 0023 §1), so `hit=yes`
    says the expected chunk is SOMEWHERE in the top-k and nothing about what outranks it. q005
    scored a clean `hit=yes` off a top-5 whose other four blocks were a different author arguing
    the opposite case (`eval/FINDINGS.md`, 2026-08-01) — a fact that took a separate hand-written
    probe to discover. This is that probe, kept.

    NO METRIC IS COMPUTED HERE (ADR 0009). Margin and the multi-position list are all
    `expected_placement`'s, one layer down where V3 executes the same definition. What this
    function does compute is presentation: the top-k size, and the display rank per row, which it
    enumerates OFF THE HANDED ORDER — a re-sort here would print a rank the Grounder never saw and
    move the marker onto the wrong row, so a test pins that order.

    Two shapes print no table at all, and the distinction between them is the same one
    `AskResult.retrieval_ran` exists to carry:
      - retrieval never ran (the two retrieval-side ERROR paths) — there is no top-k to print, and
        fabricating one, even an empty one, would report a measurement that never happened. This
        mirrors how the summary line already suppresses `hit` to `n/a` on those rows.
      - retrieval ran and returned nothing — an empty or un-ingested class (retriever.md), which
        is a MEASURED miss, said in those words rather than as "nothing matched".

    An out-of-corpus row still gets its table. Its composition is exactly `FINDINGS.md`'s
    "attractor slides" observation, and printing it is free.
    """
    if not result.retrieval_ran:
        print(
            "        retrieved: retrieval never ran (retrieval-side ERROR) — no top-k exists "
            "to print"
        )
        return

    if not result.retrieved:
        print(
            '        retrieved 0 chunks — an empty/un-ingested class, never "nothing matched" '
            "(retriever.md)"
        )
        return

    placement = expected_placement(question.expected_sources, result.retrieved)
    # EVERY matching row is marked, not just the best one — a single marker on a source that
    # appears twice would say the top-k contained it once.
    marked = set(placement.ranks) if placement is not None else set()

    total = len(result.retrieved)
    print(f"        retrieved top-{total}:")
    print("          rank   score  source")
    for rank, chunk in enumerate(result.retrieved, start=1):
        print(_retrieved_row(rank, chunk, rank in marked))
    print(_placement_line(placement))


def _print_verbose(result: GrounderResult) -> None:
    """`--verbose`: the fields the one-line scan format structurally cannot carry.

    STRICTLY ADDITIVE, and that is the requirement rather than a nicety: it prints AFTER
    `_print_question_line` and touches nothing above, so a default run's output stays
    byte-identical and a probe run can be diffed against the run it is probing.

    The first two are the ones a generation probe (issue #60) is actually looking at.
    `answer_prose` is never printed anywhere else — `_detail` reports citations, gaps, integrity
    or error, never the prose the model wrote. And gaps reach the summary line only on REFUSAL,
    clipped at 80 chars by `_clip` — on PARTIAL, `_detail`'s citations branch wins and the gaps
    do not appear there at all; the gap WORDING is the evidence compared across repeated asks,
    so this is the one place it prints whole.

    The third is issue #66's per-sentence ATTRIBUTION, printed between them because that is what
    it annotates: which sentences of the prose above carried an `[S#]` label, and which are the
    ones that escaped both halves of ADR 0014's rule (labelled, or declared in the gaps below).

    NO DEDICATED FLAG for it, and the rejection is worth recording: a `--show-attribution`
    symmetric with `--show-retrieved` would print the same sentences twice whenever both flags
    were on, because this block IS an annotation of the prose block `--verbose` already prints.
    `--show-retrieved` earns its own flag for the opposite reason — it prints something
    `--verbose` does not contain (ADR 0023 §1's other, unconflated signal).

    No prose means no block — not an empty one and not the string "None". REFUSAL and ERROR carry
    no prose by contract (`GrounderResult`), so on a refusal-heavy run this prints the gaps alone,
    and `measure_attribution` returns `None` on exactly those rows for the same reason.
    """
    # Truthiness, not `is not None`: it covers the contractual None and an empty-string prose in
    # one branch, and both mean the same thing here — there is nothing to show.
    if result.answer_prose:
        print("        answer_prose:")
        for line in result.answer_prose.splitlines():
            # `rstrip` so a blank paragraph break does not emit a trailing-whitespace line: this
            # output gets diffed run-against-run, and invisible whitespace is noise in that diff.
            print(f"          | {line}".rstrip())

    # Computed HERE rather than threaded in, and computing it twice per answer (once in `main`'s
    # loop for the pooled total, once here) is deliberate: ONE definition in `gct` called twice is
    # ADR 0009's shape, while widening this signature would ripple into three existing tests for
    # no gain. `measure_attribution` reads prose and nothing else, so this cannot disagree with
    # the pooled figure printed below.
    attribution = measure_attribution(result.answer_prose)
    if attribution is not None:
        print("        attribution (crude sentence split — telemetry, not a verdict):")
        for sentence in attribution.sentences:
            marker = "CITED  " if sentence.cited else "UNCITED"
            # Flattened, never CLIPPED. A heading with no terminator FUSES into the sentence after
            # it (`split_sentences`), so a "sentence" here can legitimately contain newlines — and
            # clipping would hide the exact fusion this line exists to make visible. `rstrip` for
            # the same run-against-run diff-noise reason as the prose block above.
            print(f"          [{marker}] {' '.join(sentence.text.split())}".rstrip())
        # The label SET, rendered in the citation spine's own vocabulary. The 2026-07-27 probe
        # printed a Python list repr (`['[S1]', '[S3]']`); the reproduction claim is over the
        # numbers and the set, not the byte format of a throwaway script.
        labels = "".join(attribution.labels_used) or "(none)"
        print(
            f"          sentences={attribution.total}  uncited={attribution.uncited}  "
            f"rate={_pct(attribution.uncited_sentence_rate)}  labels_used={labels}"
        )

    # `coverage` is non-None on every state (see `_error` / `_empty_retrieval_refusal`), so this
    # needs no state switch — an ERROR simply carries no gaps and prints nothing.
    gaps = result.coverage.gaps
    if gaps:
        print(f"        gaps ({len(gaps)}, un-truncated):")
        for gap in gaps:
            print(f"          - {gap}")


def _pct(rate: float | None) -> str:
    """A rate, or `n/a` — NEVER 0%. `_rate` returns None for an empty denominator, and rendering
    that as 0% would make "we scored nothing" look like "we scored everything and failed"."""
    return "  n/a" if rate is None else f"{rate * 100:5.1f}%"


def _rate_line(label: str, rate: float | None, numerator: int, denominator: int) -> None:
    """Every rate prints beside the counts it came from (ADR 0023 §3 / GroundingCounts).

    The suite is 8 in-corpus and 4 out-of-corpus, so one question moves a rate by 12.5 or 25
    points. A bare "75%" invites reading that noise as a result; "75.0%  6/8" does not.
    """
    print(f"    {label:<26} {_pct(rate)}   {numerator}/{denominator}")


def _print_summary(metrics: EvalMetrics) -> None:
    answer, refuse = metrics.answer, metrics.refuse
    print("\nSummary — ADR 0023 rate vector (crude spike-ranking bench, NOT a quality verdict):")

    print(
        f"  in-corpus (expectation=answer)   N_scored={answer.scored} "
        f"of {answer.total} asked, {answer.error} excluded as ERROR"
    )
    _rate_line("grounded_pass_rate", metrics.grounded_pass_rate, answer.grounded, answer.scored)
    _rate_line("partial_rate", metrics.partial_rate, answer.partial, answer.scored)
    _rate_line("false_refusal_rate", metrics.false_refusal_rate, answer.refusal, answer.scored)
    _rate_line(
        "integrity_flag_rate", metrics.integrity_flag_rate, answer.integrity_flagged, answer.scored
    )

    print(
        f"  out-of-corpus (expectation=refuse)   N_scored={refuse.scored} "
        f"of {refuse.total} asked, {refuse.error} excluded as ERROR"
    )
    _rate_line("correct_refusal_rate", metrics.correct_refusal_rate, refuse.refusal, refuse.scored)
    _rate_line("hallucination_rate", metrics.hallucination_rate, refuse.hallucinated, refuse.scored)
    _rate_line(
        "refuse_integrity_flag_rate",
        metrics.refuse_integrity_flag_rate,
        refuse.integrity_flagged,
        refuse.scored,
    )

    print("  retrieval (in-corpus only — the other, unconflated signal):")
    _rate_line(
        "retrieval_hit_rate",
        metrics.retrieval_hit_rate,
        metrics.retrieval.hits,
        metrics.retrieval.applicable,
    )
    # The denominator is MEASURED rows, not in-corpus rows: a retrieval-side ERROR sets
    # `retrieval_ran=False`, the row drops out of `applicable`, and `7/7` then prints identically
    # to a full `8/8` over a different suite. The error count above cannot disambiguate it — a
    # GENERATION-side ERROR leaves `retrieval_ran` True, so it shrinks N_scored but NOT this
    # denominator, and a reader seeing `error_count=1` beside `7/7` cannot tell which one lost the
    # row. Say the shortfall out loud instead; a rate over a suite that quietly shrank is the one
    # failure this whole bench is built not to have.
    if metrics.retrieval.applicable < metrics.answer.total:
        short = metrics.answer.total - metrics.retrieval.applicable
        print(
            f"      ^^ measured over {metrics.retrieval.applicable} of {metrics.answer.total} "
            f"in-corpus question(s) — {short} never ran retrieval (retrieval-side ERROR), so this "
            "rate is NOT comparable to a full-suite run"
        )

    # ALWAYS printed on a bench run, including at zero. ERROR leaves N_scored, so errors silently
    # shrink every denominator above — a run where 6 of 8 in-corpus questions errored can post a
    # perfect 2/2. The absence of this line is exactly what would hide that FAIL->ERROR escape
    # hatch. A `--only` run never reaches this function; `_print_filtered_notice` prints an
    # aggregate ERROR count instead — conditional rather than at-zero, because a filtered run's
    # per-question lines already name each ERROR (see its docstring for the different bargain).
    print("  health (excluded from ranking, never from the report):")
    _rate_line(
        "error_count / error_rate",
        metrics.error_rate,
        metrics.error_count,
        answer.total + refuse.total,
    )


def _print_attribution(totals: AttributionTotals) -> None:
    """Issue #66's `uncited_sentence_rate`, printed BESIDE the ADR 0023 vector — never inside it.

    A SEPARATE FUNCTION, called by `main()` after `_print_summary`, on purpose: `_print_summary`'s
    body is left byte-identical, so "the vector block did not change" is a one-line diff audit
    rather than a claim in a PR body. The layout satisfies the issue's "printed with the rest of
    the vector" — a contiguous block is what printed-with means — while the boundary the
    diagnostics banner in `gct/eval/scoring.py` argues for stays structural.

    NO ARITHMETIC HERE (ADR 0009). Every figure is `AttributionTotals`'; this function chooses
    wording. `_rate_line` and `_pct` are reused unchanged, so an unmeasured run renders `n/a`
    exactly like every other rate — never `0.0%`, which would say the bench measured every
    sentence and found them all cited.

    THE `^^` LINE PRINTS UNCONDITIONALLY, unlike the retrieval-shortfall line just above it in
    `_print_summary`, and the divergence is deliberate. For retrieval a shortfall is an ANOMALY
    (a retrieval-side ERROR ate a row), so a line that appears only then carries information. Here
    a shortfall is the NORM — every honest refusal removes an answer from the denominator, and
    this suite is a third refuse questions by design — so a conditional line would fire on nearly
    every run and mean nothing, while an unconditional one keeps the denominator on screen where
    a reader comparing two runs can see it moved.
    """
    print("  attribution telemetry (reported, never enforced — NOT part of the ADR 0023 vector):")
    _rate_line(
        "uncited_sentence_rate",
        totals.uncited_sentence_rate,
        totals.uncited,
        totals.sentences,
    )
    answers = totals.answers_measured + totals.answers_unmeasured
    print(
        f"      ^^ measured over {totals.answers_measured} of {answers} answer(s) — "
        f"{totals.answers_unmeasured} carried no prose to measure (REFUSAL and ERROR\n"
        "         carry none by contract). The sentence split is CRUDE: its misfires are a "
        "reporting\n"
        "         artifact, not a verdict (ADR 0015 keeps per-claim presence out of the "
        "enforcement\n"
        "         ladder; ADR 0014 is the rule being measured)."
    )


def _print_filtered_notice(
    questions: list[EvalQuestion], suite_total: int, records: list[EvalRecord]
) -> None:
    """What a `--only` run prints INSTEAD of the rate vector and the gate.

    Both suppressions are the same rule this file already enforces everywhere else. `_print_summary`
    reports rates over `N_scored`; on a hand-picked subset those are rates over a suite that shrank
    — the one failure `_require_expected_files`, `_pct` and the retrieval shortfall warning each
    exist to prevent — except here the shrinking was ASKED FOR, so the honest report is to name it
    rather than to refuse the run.

    The gate goes with them for a different reason: it requires >=1 in-corpus GROUNDED AND >=1
    out-of-corpus REFUSAL, which a subset can be structurally incapable of containing. Running it
    on `--only q005` would print FAIL and exit 1 — "the system answered badly" — for a run that
    answered exactly what it was asked. So the subset run exits EXIT_OK and says why, out loud;
    setup failures still exit 2, since those are about validity, not results.

    One aggregate survives the suppression: an ERROR count, printed only when something errored.
    That is a summary convenience, not the closing of a hidden signal — every errored ask already
    prints its own line above (state ERROR, outcome=EXCLUDED, the error kind and message); the
    aggregate keeps a multi-question probe from ending on a quiet closing block while an earlier
    line errored. Deliberately CONDITIONAL, unlike `_print_summary`'s always-printed health line:
    a probe with no errors needs no aggregate, and the line's presence-on-error is pinned by
    tests rather than by an at-zero convention.
    """
    print(
        f"\nFILTERED RUN — {len(questions)} of {suite_total} question(s) in the suite: "
        + ", ".join(question.id for question in questions)
    )
    print(
        "  No ADR 0023 rate vector: rates over a hand-picked subset are not comparable to a "
        "full-suite run."
    )
    print(
        "  No exit gate either: it needs >=1 in-corpus GROUNDED and >=1 out-of-corpus REFUSAL, "
        "which a subset need not contain."
    )
    # Suppressed for the SAME reason as the vector, one line up: a sentence rate pooled over a
    # hand-picked subset is a rate over a suite that shrank. The per-answer block still prints
    # under `--verbose` — that one is a probe readout, not an aggregate, so it cannot mislead
    # about coverage.
    print(
        "  No pooled uncited_sentence_rate: the same argument — a rate over hand-picked "
        "answers is not comparable either (--verbose still annotates each answer)."
    )
    errored = sum(1 for record in records if record.state is GrounderState.ERROR)
    if errored:
        # Both numbers derive from `records` — the asks that actually ran — so the line cannot
        # disagree with itself if a caller ever hands it fewer records than questions.
        print(
            f"  !! {errored} of {len(records)} question(s) came back ERROR — a health count "
            "(ADR 0023 §3's error signal, counted rather than rated because rates stay "
            "suppressed here). This run measured less than it asked."
        )
    print("  The per-question lines above are this run's whole result.")


def _print_gate(records: list[EvalRecord], metrics: EvalMetrics) -> bool:
    """Issue #8's acceptance criterion, read minimally, plus the counts behind the demo claim."""
    grounded = sum(
        1
        for r in records
        if r.expectation == EXPECTATION_ANSWER and r.state is GrounderState.GROUNDED
    )
    refused = sum(
        1
        for r in records
        if r.expectation == EXPECTATION_REFUSE and r.state is GrounderState.REFUSAL
    )
    passed = grounded >= 1 and refused >= 1

    answer, refuse = metrics.answer, metrics.refuse
    # The split is printed INSIDE the fused count, never just the sum. A run of 1 GROUNDED + 7
    # PARTIAL reads "8/8" and then "PASS" — literally accurate and completely misleading, since
    # PARTIAL is the TRACKED bucket that is explicitly not a pass (ADR 0023 §2). §5 names folding
    # TRACKED into PASS as the erosion it exists to prevent, and the last line is what a human
    # actually skims.
    print(
        f"\n  cited answers (GROUNDED+PARTIAL): {answer.grounded + answer.partial}/{answer.total}"
        f" ({answer.grounded} GROUNDED + {answer.partial} PARTIAL)"
        f"  ·  honest refusals: {refuse.refusal}/{refuse.total}"
    )
    verdict = "PASS" if passed else "FAIL"
    print(
        f"{verdict} — exit gate (>=1 in-corpus GROUNDED, >=1 out-of-corpus REFUSAL): "
        f"GROUNDED={grounded}, REFUSAL={refused}"
    )
    return passed


# --- wiring -----------------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--owner", default=DEFAULT_OWNER, help="owner_id every row is scoped to")
    parser.add_argument(
        "--corpus-dir", default=DEFAULT_CORPUS_DIR, help="directory of .pdf/.pptx to converge"
    )
    parser.add_argument("--questions", default=DEFAULT_QUESTIONS, help="the eval JSONL (ADR 0021)")
    parser.add_argument("--suite", default=DEFAULT_SUITE, help="suite name to filter on")
    parser.add_argument(
        "--only",
        default=None,
        metavar="ID[,ID...]",
        help="restrict the run to these question ids (e.g. q005,q009), applied after --suite; "
        "suppresses the rate vector and the exit gate",
    )
    # Issue #66's per-sentence attribution rides THIS flag rather than gaining one of its own: it
    # annotates the prose block `--verbose` already prints, so a dedicated flag (symmetric with
    # `--show-retrieved` below) would print the same sentences twice whenever both were on. The
    # help text names it, because a flag description that no longer describes the flag is exactly
    # the drift CLAUDE.md's "cite ADRs, don't re-argue them" section is about.
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="also print each answer's full prose and un-truncated gap list, and its per-sentence "
        "attribution (which sentences carried an [S#] label), after its summary line (the summary "
        "line itself is unchanged)",
    )
    # Its OWN flag, not a second meaning for `--verbose`. `--verbose` is the GENERATION probe
    # (issue #60: what the model actually wrote); this is the RETRIEVAL probe (issue #67: what it
    # was given). ADR 0023 §1 keeps those two signals unconflated at the metric layer, and fusing
    # their flags would re-conflate them at the reporting layer — you could not look at retrieval
    # composition without a wall of model prose. Named for the field it prints
    # (`AskResult.retrieved`): `--topk` was rejected as visually colliding with `--k`, which SETS
    # k and takes a value, and `--retrieval` as ambiguous with ADR 0023's retrieval signal.
    parser.add_argument(
        "--show-retrieved",
        action="store_true",
        help="also print the retrieved top-k (rank, score, file, page/slide) per question, with "
        "the expected source's rank and its margin over the next result (the default report is "
        "unchanged)",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=DEFAULT_K,
        help=f"retriever top-k (default {DEFAULT_K}, the retriever's own DEFAULT_K)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=CHUNK_SIZE_WORDS,
        help=(
            f"chunk window in words (default {CHUNK_SIZE_WORDS}, the chunker's own "
            "CHUNK_SIZE_WORDS)"
        ),
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=CHUNK_OVERLAP_WORDS,
        help=(
            f"chunk overlap in words (default {CHUNK_OVERLAP_WORDS}, the chunker's own "
            "CHUNK_OVERLAP_WORDS)"
        ),
    )
    args = parser.parse_args()
    # Range-checked HERE, before the DB opens or a single file is embedded. `retrieve()` rejects
    # k < 1 correctly and `ask()` correctly declines to convert caller bugs into a tidy ERROR - so
    # without this the raise lands mid-loop, AFTER the corpus was paid for, and exits 1: the code
    # this script documents as "the gate failed". A typo must not be indistinguishable from a
    # system that answered badly. `parser.error` exits 2, matching EXIT_SETUP.
    if args.k < 1:
        parser.error(f"--k must be >= 1, got {args.k}")
    # The `--k` precedent above, one stage earlier and cheaper to get wrong: an invalid window
    # reaches `chunk_units`, whose guard raises mid-ingest — after every file before it in the
    # corpus was already parsed, embedded and PAID FOR — and exits 1.
    if not (0 <= args.chunk_overlap < args.chunk_size):
        parser.error(
            "--chunk-overlap must be in [0, --chunk-size), got "
            f"--chunk-overlap {args.chunk_overlap}, --chunk-size {args.chunk_size}"
        )
    return args


def _load(path: str, suite: str) -> list[EvalQuestion]:
    """Load + suite-filter, converting a bad file into a setup failure rather than a traceback."""
    try:
        questions = load_questions(path, suite=suite)
    except (OSError, ValueError) as err:
        raise SetupError(f"cannot read {path}: {err}") from err
    if not questions:
        raise SetupError(f"no questions in suite {suite!r} in {path}")
    return questions


def _select(questions: list[EvalQuestion], only: str | None) -> list[EvalQuestion]:
    """`--only`: keep just the named ids, AFTER the suite filter — so `--suite smoke --only q005`
    means "q005, if smoke has it", and an id that smoke does not carry is an error rather than a
    silently empty run.

    An unknown id is a SetupError for the same reason a missing expected file is one: a typo must
    never quietly run a smaller suite than intended. Here that failure would be especially cheap to
    miss — `--only q05` matching nothing would ask ZERO questions and print a tidy report of them.

    Suite order is preserved rather than command-line order: the per-question lines are meant to be
    diffed against the same lines from a full run, and reordering them would break that comparison
    for no gain.
    """
    if only is None:
        return questions

    wanted = [qid.strip() for qid in only.split(",") if qid.strip()]
    if not wanted:
        raise SetupError(f"--only {only!r} names no question ids")

    available = {question.id for question in questions}
    unknown = [qid for qid in wanted if qid not in available]
    if unknown:
        raise SetupError(
            "--only names question id(s) that this suite does not contain: "
            + ", ".join(repr(qid) for qid in unknown)
            + f" — the suite has {', '.join(sorted(available))}"
        )

    keep = set(wanted)
    return [question for question in questions if question.id in keep]


def main() -> int:
    args = _parse_args()
    # The chunk window is part of the run's identity, not decoration: a corpus is converged per
    # owner and carries no record of the window that produced it (see `_print_census`), so the
    # header and the census are the only two places a run says which window it was asked for.
    print(
        f"Slice 1 smoke — suite={args.suite!r} k={args.k} "
        f"chunk={args.chunk_size}/{args.chunk_overlap} owner={args.owner!r}"
    )

    try:
        questions = _load(args.questions, args.suite)
        suite_total = len(questions)
        questions = _select(questions, args.only)

        # The class check runs over the SELECTED rows, and the setup guards below likewise: a
        # subset run only has to be scoreable in the questions it actually asks, and failing it on
        # a missing source for a question it will never ask would make `--only` unusable on a
        # corpus that is mid-move — which is exactly when a probe gets run.
        slugs = {question.class_slug for question in questions}
        if len(slugs) > 1:
            # V1 scope: one ask is scoped to one class, so one run is one class. A multi-class
            # suite would need a class_id per question and a per-class census - not a loop tweak.
            raise SetupError(f"suite spans {len(slugs)} classes {sorted(slugs)}; V1 runs one")
        slug = slugs.pop()

        corpus_dir = Path(args.corpus_dir)
        if not corpus_dir.is_dir():
            raise SetupError(f"corpus dir {corpus_dir} does not exist")

        # Built ONCE, for the whole run: one OpenAI client, one connection. The embedder in
        # particular must be the same object on both sides of the seam - ingest STAMPS
        # `embedding_model_id` from it and the Retriever ASSERTS the stamp against it (ADR 0018).
        #
        # Wiring failures are SETUP failures (SetupError's "it never ran"), so they convert HERE
        # and exit 2 - unconverted they escape `except SetupError` below and exit 1 with a
        # traceback, reporting "the system answered badly" for a run that never validly started.
        # The key check is explicit because absence never raises: `load_settings()` defaults a
        # missing OPENAI_API_KEY to "", which the client ACCEPTS at construction - the failure
        # would otherwise surface as an AuthenticationError at the first PAID call, mid-ingest.
        # (A key that is present but wrong still fails there, as exit 1; converting that would
        # mean wrapping paid calls, which the question loop below deliberately refuses to do.)
        if not load_settings().openai_api_key:
            raise SetupError("OPENAI_API_KEY is empty — set it in .env (CLAUDE.md, Local dev)")

        try:
            embedder = OpenAIEmbeddings()
            generator = OpenAIGeneration()
        except OpenAIError as err:
            raise SetupError(f"cannot construct the OpenAI providers: {err}") from err

        try:
            conn = connect()
        except psycopg.OperationalError as err:
            raise SetupError(
                f"cannot connect to Postgres (DATABASE_URL — is the server up?): {err}"
            ) from err

        with conn:
            # AUTOCOMMIT, and it is load-bearing — not a style choice.
            #
            # psycopg opens an implicit transaction on the first statement, so WITHOUT this the
            # very first read (`_resolve_class`'s SELECT) leaves the connection INTRANS for the
            # rest of the run. `index_file`'s `with conn.transaction()` then degrades from
            # BEGIN/COMMIT to a mere SAVEPOINT: nothing is durable until this block exits cleanly,
            # so ANY later raise — a missing expected file, a bad key on question 1, Ctrl-C —
            # rolls back every embedding the run already PAID FOR. That silently downgrades ADR
            # 0020's per-file atomicity to per-run and falsifies `_converge_corpus`'s promise that
            # this script is safe to re-run.
            #
            # Autocommit fixes it at the mechanism rather than per call site: the implicit
            # transaction never opens, so `index_file`'s block is the real top-level transaction
            # ADR 0020 §3 describes (still atomic — a failure inside it still rolls back its own
            # writes). A trailing `conn.commit()` after each ingest would also work, but leaves the
            # hazard live for the next writer who forgets one.
            #
            # This line IS how this script satisfies `index_file`'s caller precondition — ADR 0025,
            # which amends ADR 0020's unconditional publication claim and names this script's
            # autocommit as the way it is met. Do not remove it without reading that ADR.
            #
            # It also keeps the connection IDLE across the question loop below. ADR 0020 §3 warns
            # in bold against holding a transaction open across API round-trips; without this the
            # read path reopens one at `_print_census` and holds it through every paid embed and
            # generation.
            conn.autocommit = True

            print(f"\nSetup — class {slug!r}, corpus {corpus_dir}:")
            class_id = _resolve_class(conn, args.owner, slug)
            ready, ingested = _converge_corpus(
                conn,
                owner_id=args.owner,
                class_id=class_id,
                corpus_dir=corpus_dir,
                embedder=embedder,
                chunk_size=args.chunk_size,
                chunk_overlap=args.chunk_overlap,
            )
            _require_expected_files(
                questions,
                ready,
                _indexed_pages(conn, owner_id=args.owner, class_id=class_id),
            )
            _print_census(
                conn,
                owner_id=args.owner,
                class_id=class_id,
                ready=ready,
                chunk_size=args.chunk_size,
                chunk_overlap=args.chunk_overlap,
                ingested=ingested,
            )

            # The tiny closure the loop drives: providers, connection and scope are wired once
            # here, so the loop below reads as "ask each question" and nothing else.
            def ask_one(question: str) -> AskResult:
                return ask(
                    conn,
                    question,
                    args.owner,
                    class_id,
                    embedder=embedder,
                    generator=generator,
                    k=args.k,
                )

            print(f"\nAsking {len(questions)} question(s):")
            records: list[EvalRecord] = []
            # A SECOND PARALLEL LIST, named here rather than hidden. `EvalRecord` deliberately
            # cannot carry prose (its own docstring: "the minimum a metric needs, and nothing
            # more"), so `compute_metrics` structurally cannot see attribution — which is the
            # mechanism behind issue #66's "reported, never enforced" claim, not merely a
            # convention. The price of that guarantee is this list, and it is one `.append` in a
            # loop that already appends.
            attributions: list[AnswerAttribution | None] = []
            for question in questions:
                # NOT wrapped in try/except. A raise out of ask() is a TERMINAL provider error or
                # our own bug (ask.py's error boundary converts exactly two retrieval-side
                # exceptions and lets everything else through). Logging it and continuing would
                # drop the question from the denominator and report a clean rate over a suite that
                # quietly shrank - the one failure this whole bench is built not to have.
                result = ask_one(question.question)

                # `result.result.state` is read HERE, once, in the open - and nothing else from
                # the GrounderResult crosses into scoring. That is the R6 warning made physical:
                # a REFUSAL can carry `coverage.complete=True` (grounder/answer.py `_decide`
                # documents the incoherent combination), so anything scoring on coverage would
                # read a refusal as a covered answer. EvalRecord cannot see coverage at all.
                record = EvalRecord(
                    expectation=question.expectation,
                    state=result.result.state,
                    # Tri-state, and BOTH sources of `None` are needed. `retrieval_hit` supplies
                    # one (a refuse row has no expected_sources - nothing to check). `ask()`
                    # supplies the other via `retrieval_ran`: on the two retrieval-side ERROR
                    # paths no top-k was ever produced, and scoring that as a miss would report a
                    # recall failure that never happened. Without this branch every such row lands
                    # as `hit=False` - a MEASURED miss - and a run degraded by an outage posts
                    # `retrieval_hit_rate 0%`, indistinguishable from a genuine retrieval
                    # regression (ADR 0023 §1 defines the signal over questions where retrieval
                    # actually ran).
                    hit=(
                        retrieval_hit(question.expected_sources, result.retrieved)
                        if result.retrieval_ran
                        else None
                    ),
                )
                records.append(record)
                # Prose, and nothing else, crosses into the attribution readout — the same
                # narrowness `record` above has about coverage. A REFUSAL or ERROR carries no
                # prose by contract, so this appends `None`: unmeasured, never a measured zero.
                attributions.append(measure_attribution(result.result.answer_prose))
                _print_question_line(question, result, record)
                # Retrieval BEFORE generation, matching the order the pipeline actually ran in: a
                # reader following "why did it answer that" reads the evidence it was handed, then
                # what it wrote. Both blocks are strictly additive, so under `--verbose` alone the
                # emitted bytes are unchanged from before this flag existed.
                if args.show_retrieved:
                    _print_retrieved(question, result)
                if args.verbose:
                    _print_verbose(result.result)

            # A filtered run reports what it asked and stops; a full run is the bench, and gets the
            # rate vector and the gate. `_print_filtered_notice` carries why the subset gets
            # neither. Keyed on the FLAG, not on whether the subset came out smaller: `--only`
            # naming every id in the suite today is still a probe, and one question added to the
            # eval file tomorrow must not silently turn that same command back into a bench run.
            if args.only is not None:
                _print_filtered_notice(questions, suite_total, records)
                return EXIT_OK

            metrics = compute_metrics(records)
            _print_summary(metrics)
            # Beside the vector, after it, and BEFORE the gate — which is unchanged and still
            # called with exactly `(records, metrics)`. Nothing computed on this line can reach
            # the verdict on the next one.
            _print_attribution(aggregate_attribution(attributions))
            return EXIT_OK if _print_gate(records, metrics) else EXIT_GATE_FAILED

    except SetupError as err:
        print(f"\nSETUP FAILED — {err}")
        return EXIT_SETUP


if __name__ == "__main__":
    raise SystemExit(main())
