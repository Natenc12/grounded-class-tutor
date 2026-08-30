"""The ingest input ceiling reached the way production reaches it: `enqueue` -> `process_one`
(issue #43; ADR 0020, terminal set extended per ADR 0029).

Every other ceiling test calls `compose`/`ingest_file` directly with an explicit `max_words`.
That is the right level for the guard's own semantics, and it leaves two facts unmeasured, both
of which are the ones a student actually experiences:

  1. **The shipped 250,000 is the number in force.** `process_one` calls `ingest_file` WITHOUT
     `max_words`, so production is measured against `ingest_file`'s DEFAULT. The rest of the
     suite pins that default on the SIGNATURE (`test_default_ceiling_is_the_config_constant`) -
     an identity check, not a behavioral one. Here the file is genuinely 250,001 words and no
     `max_words` is passed anywhere, so the assertion is that the real constant refuses a real
     file through the real call chain.
  2. **The reason survives the whole path.** `too_long` is raised in `pipeline.py`, carried by
     `ParseError.reason`, written untranslated by `worker._bury`, and stored in a column whose
     CHECK was widened by `migrations/0003_failed_reason_too_long.sql`. Four components, one
     value, no translation step - and until this file, nothing exercised more than two of them at
     a time. ADR 0029 says "no worker change"; that is a claim about behavior, and this is where
     it is measured rather than reasoned.

WHY IT LIVES IN `tests/gct/ingest/` rather than beside the worker's own suite: the subject is the
ceiling, which is `ingest/`'s. `tests/gct/jobs/test_worker.py` owns the claim/lease/retry
machinery and takes its own local stubs; this file imports `gct.jobs` as a CALLER and asserts
nothing about the queue that is not downstream of the ceiling firing. It also inherits
`tests/gct/ingest/conftest.py`'s file factories and embedder stubs, which is the other half of the
reason - a copy of `pptx_factory` next to the worker suite would be a second writer for what a
"real" PPTX is.

PPTX, not PDF, for the oversized fixture, and the difference is not cosmetic. Measured
2026-08-30: a 250,001-word deck builds in 0.10s and parses in 0.01s, while the same words as a PDF
cost 0.34s to build and **2.53s** to parse through pypdf - more than half of the entire `not live`
suite's runtime, for a fixture whose only job is to be too long.

Every assertion about what was PUBLISHED goes through `db_other`, a second connection: a
connection sees its own uncommitted work, so reading back through `db` would hold whether or not
anything committed (ADR 0025, and `db_other`'s docstring).
"""

from __future__ import annotations

import pytest

from gct.config import MAX_INGEST_WORDS
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.jobs import worker
from gct.jobs.queue import enqueue
from gct.jobs.worker import process_one

# The same 260,000-character, zero-space Chinese text `test_pipeline.py` pins the blind spot with.
# Duplicated rather than imported: the two files are asserting different things about it, and a
# shared constant would make the second test's fixture change silently when the first one's does.
_CJK_TEXT = "宇宙论是关于宇宙起源的理论" * 20_000


def tick(conn, embedder, **overrides):
    """One `process_one` at the default chunk window; `overrides` is what a test is varying.

    Same helper, same reason, as `tests/gct/jobs/test_worker.py`'s: `process_one` REQUIRES the
    window and forwards it verbatim to `ingest_file` (ADR 0019/0026 - never hardcoded inside the
    worker), which is a contract worth keeping and pure noise at every call site here.

    `max_words` is conspicuously absent from the signature and must stay absent: `process_one`
    does not take one, and the entire point of this file is that the ceiling in force on the
    production path is `ingest_file`'s own default.
    """
    return process_one(
        conn,
        embedder=embedder,
        **{
            "chunk_size": CHUNK_SIZE_WORDS,
            "chunk_overlap": CHUNK_OVERLAP_WORDS,
            **overrides,
        },
    )


def test_a_file_over_the_shipped_ceiling_fails_terminally_through_the_worker(
    pptx_factory, counting_embedder, db, db_other, monkeypatch
):
    """queued -> processing -> failed(`too_long`), zero retries, zero chunks, zero paid calls.

    The acceptance shape of issue #43, measured end to end at the REAL 250,000-word ceiling with
    no `max_words` passed by anyone. Six assertions, each a different bug:

      - `files.status/failed_reason == ('failed', 'too_long')` - the student is told something
        actionable, in the identifier ADR 0029 §3 settled. Checked for the SPECIFIC value: a
        worker that mapped every terminal cause onto one reason satisfies a non-null check and
        tells the student nothing.
      - `jobs.state == 'failed'` - TERMINAL, not requeued. A file is exactly as long on the next
        attempt, so the retry budget must not be spent on it (ADR 0020 §1).
      - `attempts == 1` - and this is NOT the same claim as "no retries". The claim already
        bumped the counter, because a claim happened; 1 is the trail of what occurred, and any
        value above it would mean something handed the job out again.
      - a second `tick` returns False - the direct proof of terminality, where `state == 'failed'`
        is the indirect one. Nothing is claimable, so no later worker rediscovers this file.
      - `counting_embedder.calls == []` - the assertion no status column can make. The ceiling's
        entire value is its POSITION above the pipeline's one paid call (ADR 0029 §3); a guard
        moved below `embed` sets every column on this list correctly and still buys the run.
      - zero `chunks` - no partial index is ever visible, the Slice 2 exit property.

    `sleep` is stubbed and asserted empty for the same reason the corrupt-file test in the worker
    suite does it: a terminal failure must not serve a backoff for a retry that is never coming.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    slept: list[float] = []
    monkeypatch.setattr(worker.time, "sleep", slept.append)
    # One word past the shipped ceiling - derived from the constant, never a literal 250_001, so
    # the fixture follows the config if the config ever moves.
    oversized = " ".join(f"w{i}" for i in range(MAX_INGEST_WORDS + 1))
    source = pptx_factory("a-whole-textbook.pptx", [oversized])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    assert tick(conn, counting_embedder) is True, (
        "a failed job is still work - the tick must not read as idle"
    )

    published = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert published == ("failed", "too_long")

    state, attempts, last_error = db_other.execute(
        "select state, attempts, last_error from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert state == "failed", "a file over the ceiling must not stay claimable"
    assert attempts == 1, "the claim happened once and nothing retried it"
    assert last_error and "too_long" in last_error
    assert str(MAX_INGEST_WORDS) in last_error, (
        "the operator's only record of WHY is this string - it must name the ceiling in force"
    )

    assert tick(conn, counting_embedder) is False, "nothing is left to claim - terminal means over"

    assert counting_embedder.calls == [], "an over-ceiling file must cost nothing at the provider"
    assert slept == [], "a terminal failure must not serve a retry backoff"
    (chunk_count,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert chunk_count == 0


def test_the_ceiling_refusal_is_the_only_thing_that_stopped_it(
    pptx_factory, counting_embedder, db, db_other, monkeypatch
):
    """The control for the test above: the SAME deck, one word short of the ceiling, ingests.

    Without it, that test is satisfied by any pipeline that fails on a large PPTX for any reason
    at all - a python-pptx limit, a Postgres parameter cap, an out-of-memory kill - and would keep
    passing after the ceiling was deleted, as long as something else broke first. The two fixtures
    differ by exactly one word, so the ceiling is the only variable between a `too_long` and a
    `ready`.

    It is also the only place the ACCEPT side is measured at production scale: everything else in
    the suite accepts small files, and `test_a_file_at_the_ceiling_ingests_completely_and_is
    _queryable` accepts a small one at a lowered knob. Deliberately asserts `ready` and a chunk
    count rather than retrieval - the read path is that test's subject, and re-asserting it here
    would make this a second writer for it.

    THE WIDE CHUNK WINDOW IS LOAD-BEARING TWICE OVER, so do not "restore the default" here.
      - It is a second assertion, not a shortcut. ADR 0029 §1 chose words over chunk count
        precisely because the chunk window is a provisional knob (ADR 0019) that Spike Pass 2 is
        still moving, and a chunk-based ceiling would drift ~70% when it does. A file accepted at
        a window 1,000x the default is that independence, measured: the ceiling counts
        `parse_file`'s output, and nothing about chunking can reach it.
      - It is also what makes the test affordable. Measured 2026-08-30: at the default 250/40
        window this deck becomes 1,191 chunks and 1,191 pgvector row inserts, and the test costs
        **7.1s** - more than the entire `not live` suite. At one chunk it costs ~0.3s. The parse
        and the ceiling check are byte-identical between the two; only the row count changes, and
        row count is `test_index.py`'s subject, not this file's.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    just_under = " ".join(f"w{i}" for i in range(MAX_INGEST_WORDS - 1))
    source = pptx_factory("a-slightly-smaller-textbook.pptx", [just_under])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    assert tick(conn, counting_embedder, chunk_size=MAX_INGEST_WORDS, chunk_overlap=0) is True

    published = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert published == ("ready", None), (
        "one word under the ceiling is not over it - the guard is `>`, not `>=`"
    )
    (chunk_count,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert chunk_count == 1, "one window that wide is one chunk - see the docstring"
    assert len(counting_embedder.calls) == 1, "the accepted file DID reach the provider, once"


def test_a_space_free_file_slips_the_ceiling_and_crashes_the_worker_unclassified(
    pptx_factory, token_limit_embedder, db, db_other, monkeypatch
):
    """PINNED, NOT FIXED - the end-to-end verdict on the ceiling's blind spot, and it is LOUD.

    `test_the_word_ceiling_does_not_see_space_free_scripts` establishes that 260,000 characters of
    Chinese count as one word and sail past a 250,000-word ceiling. The question that decides
    whether that is a nuisance or a trust bug is what happens NEXT, in production, to the file and
    to the student - and this is the only place it can be answered by execution rather than by
    reading.

    THE VERDICT: loud. The single 260,000-character chunk is refused by the provider; that refusal
    is neither `ParseError` nor `TransientEmbeddingError`, so `process_one` does not classify it,
    the exception propagates, and the worker crashes exactly as it is designed to on an
    unclassified error (ADR 0020 §1, DB-blip class amended per ADR 0028 - the same "we do not
    guess a `failed_reason`" rule). Nothing is committed: the index transaction never opened, so
    there is no partial set, no `ready`, and nothing that can ever be mis-cited. The trust
    property holds.

    WHAT IT COSTS, stated because "loud" is not "free":
      - one embedding request IS bought and refused - `calls` is 1, not 0. The ceiling's promise
        to fire before the paid call does not reach this input.
      - the file is left `processing`, not `failed`. The student sees a spinner, not a reason,
        until the reaper requeues and the durable budget grinds the job down to
        `transient_exhausted` - which is the WRONG reason for this cause, and is the reason the
        taxonomy has (ADR 0020 §Open records that a richer taxonomy is a V2 extension).
      - so the failure is loud to an OPERATOR (a crashing worker, a stack trace) and quiet to the
        STUDENT (a long spinner, then a misleading reason).

    None of that is a defect introduced by #43 - the pipeline behaves identically for any input
    one chunk too large for one request, and the blind spot is inherited from ADR 0019's
    whitespace-word strategy. It is recorded here so the fix has a target: a `tiktoken` ceiling
    (ADR 0029's parked upgrade) turns this whole path into a clean `too_long` before any call, and
    when it does, this test goes red. That red is the notification.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = pptx_factory("cosmogony-zh.pptx", [_CJK_TEXT])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    with pytest.raises(token_limit_embedder.Error):
        tick(conn, token_limit_embedder)

    assert len(token_limit_embedder.calls) == 1, "the ceiling did not fire - the provider was paid"

    published = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert published == ("processing", None), (
        "no reason is guessed for an unclassified error - the file is left mid-flight, which "
        "is the honest state and NOT an actionable one"
    )
    state, attempts = db_other.execute(
        "select state, attempts from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (state, attempts) == ("processing", 1), (
        "the crash left the lease held; the reaper is what reclaims it (issue #71)"
    )
    (chunk_count,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert chunk_count == 0, "loud, not silent: nothing was indexed, so nothing can be mis-cited"
