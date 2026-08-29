"""DB-backed tests for the worker: the happy path (PR 1) and the failure split (PR 2, issue #71).

Same discipline as `test_queue.py`: real Postgres via the `db` fixture, and every assertion
about what the worker PUBLISHED goes through `db_other` - a second connection. A connection
sees its own uncommitted work, so reading back through `db` would hold whether or not anything
was committed (ADR 0025); the durability assertion IS the point of the happy-path test, not a
flourish on it.

The PR 2 half exercises ADR 0020 §1 from both sides. Its stubs are chosen so a failure costs
nothing and is exact: `FakeEmbeddings(transient_failures=N)` raises the provider-agnostic
`TransientEmbeddingError` on cue, and a corrupt PDF makes `parse_file` raise the real
`ParseError` rather than a mock of one - the terminal path's whole contract is that
`exc.reason` is already a legal `files.failed_reason`, and a stubbed exception would let that
pass-through drift. `time.sleep` is stubbed everywhere the backoff runs: the delays are the
assertion, never something a test waits out.

`FakeEmbeddings` and `write_pdf` are LOCAL on purpose: the root conftest records that the
ingest factories "deliberately stayed put" in `tests/gct/ingest/`, pytest does not expose a
conftest sideways, and `test_ask_smoke.py` set the precedent of a suite outside `ingest/`
carrying its own minimal stubs shaped to what IT asserts. Here that shape is: real enough for
`parse_file` to parse (the worker actually opens the file - `test_queue`'s bytes-stub is not
enough), corrupt enough for it to refuse (the terminal path), and an embedder that can fail on
demand and count what it was asked to do.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from gct.config import EMBEDDING_DIM
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.jobs import worker
from gct.jobs.queue import enqueue
from gct.jobs.worker import backoff_seconds, process_one
from gct.providers.base import TransientEmbeddingError


class FakeEmbeddings:
    """Deterministic, free, and LOUD about how often it was called.

    `calls` records every batch handed to `embed`, and it is how the PR 2 tests ask the
    question the status columns cannot answer: "was an embed actually BOUGHT?". A terminal
    parse failure must leave it empty, and the claim that buries an exhausted job must not
    add to it - both are money, and both would be invisible in `files.status`.

    `transient_failures` makes the first N calls raise `TransientEmbeddingError`, the
    provider-agnostic "try again" type real adapters re-raise (`providers/base.py`). The call
    is RECORDED BEFORE it raises on purpose: a failed attempt still cost a provider round trip,
    so `len(calls)` counts attempts made rather than attempts that worked.
    """

    model_id = "fake-embed-3"

    def __init__(self, transient_failures: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._failures_left = transient_failures

    def embed(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        if self._failures_left > 0:
            self._failures_left -= 1
            raise TransientEmbeddingError("simulated provider 429 - slow down")
        # Distinct-but-arbitrary vectors; nothing here asserts on ranking (same stance as
        # test_ask_smoke's stub).
        return [[float((hash(t) % 97) + 1)] + [0.0] * (EMBEDDING_DIM - 1) for t in texts]


def write_corrupt_pdf(path: Path) -> Path:
    """A file that claims to be a PDF and is not - the terminal `unparseable` case.

    The header is real so nothing rejects it on the extension alone; the body is not, so pypdf
    raises and `parse_file` converts it to `ParseError("unparseable", ...)`. Written as bytes
    rather than mocked so the reason travelling into `files.failed_reason` is the one the real
    parser produces.
    """
    path.write_bytes(b"%PDF-1.7\nthis is not a PDF at all\n")
    return path


def write_pdf(path: Path, page_texts: list[str]) -> Path:
    """A real, parseable PDF - one page per entry in `page_texts`."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    for text in page_texts:
        c.drawString(72, 700, text)
        c.showPage()
    c.save()
    path.write_bytes(buf.getvalue())
    return path


def test_happy_path_publishes_through_a_second_connection(db, db_other, tmp_path):
    """queued -> processing -> ready for a real file, proven on a connection the worker never had.

    This is the acceptance item issue #71 bolds - the ADR 0025 durability test. Assert through
    `db_other`, ALL of it:
      - files.status == 'ready' (and failed_reason is null)
      - jobs.state == 'done'
      - a non-zero chunk count for this file_id
    A same-connection read passes even when the savepoint bug is present, so any assertion
    made through `db` here proves nothing about publication.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True  # matches the shipped wiring; not required for correctness -
    # every writer commits itself (see the plain-connection test below)
    source = write_pdf(tmp_path / "lecture-3.pdf", ["alpha page one words", "beta page two words"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    processed = process_one(
        conn,
        embedder=FakeEmbeddings(),
        chunk_size=CHUNK_SIZE_WORDS,
        chunk_overlap=CHUNK_OVERLAP_WORDS,
    )

    assert processed is True

    # All three reads go through `db_other`. Through `conn` they would pass even if the
    # savepoint bug (ADR 0025) meant nothing was ever published - see the fixture's docstring.
    status, failed_reason = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (status, failed_reason) == ("ready", None)

    (state,) = db_other.execute(
        "select state from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert state == "done"

    # Both status columns above are strings a bug could set with no content behind them; this
    # is the one assertion that the ingest actually produced rows.
    (chunk_count,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert chunk_count > 0


def test_processing_status_publishes_on_a_plain_connection(db, db_other, tmp_path):
    """The whole worker path commits itself - autocommit at wiring is defense in depth.

    Regression for the bare-execute version of the `processing` write (fixed on this
    branch): on a connection WITHOUT autocommit it sat unpublished in psycopg's implicit
    transaction, and `index_file` then refused the INTRANS connection - after the embed
    was already paid (ADR 0025). Wrapped in its own `conn.transaction()`, the path now
    works on a plain connection; asserting through `db_other` is what proves publication,
    same argument as the happy-path test above.
    """
    conn, owner_id, class_id = db  # deliberately NOT switched to autocommit - that is the point
    source = write_pdf(tmp_path / "plain-conn.pdf", ["plain connection page words"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    processed = process_one(
        conn,
        embedder=FakeEmbeddings(),
        chunk_size=CHUNK_SIZE_WORDS,
        chunk_overlap=CHUNK_OVERLAP_WORDS,
    )

    assert processed is True
    status, state = db_other.execute(
        """
        select f.status, j.state
        from files f
        join jobs j using (file_id)
        where f.file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert (status, state) == ("ready", "done")


def test_empty_queue_is_a_normal_tick(db):
    """Nothing enqueued: process_one returns False and raises nothing - the loop's idle case."""
    conn, _owner_id, _class_id = db
    conn.autocommit = True

    processed = process_one(
        conn,
        embedder=FakeEmbeddings(),
        chunk_size=CHUNK_SIZE_WORDS,
        chunk_overlap=CHUNK_OVERLAP_WORDS,
    )

    assert processed is False


def test_chunk_window_reaches_ingest(db, db_other, tmp_path):
    """The chunk window is forwarded verbatim, never hardcoded (ADR 0019; issue #71 bolds this).

    Same proof shape as test_pipeline / test_ask_smoke: process two files under two DIFFERENT
    windows - one at the module defaults, one chosen so the same text must chunk differently
    (small `chunk_size`, page text long enough to split) - and assert their chunk counts
    differ through `db_other`. A worker that silently ignored the parameters goes red here
    because both runs would land on the default count.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    # IDENTICAL text in both files - it is the control. Differing chunk counts prove the window
    # reached the chunker only if nothing else about the input differed.
    many_words = " ".join(f"word{i}" for i in range(60))
    first = write_pdf(tmp_path / "default-window.pdf", [many_words])
    second = write_pdf(tmp_path / "small-window.pdf", [many_words])

    # Enqueued and processed ONE AT A TIME rather than both up front. `process_one` takes no
    # file - it claims whatever `claim` picks, `order by created_at`. Enqueuing both first would
    # leave the pairing of file to window resting on two `now()` stamps landing in the intended
    # order; with one claimable job at a time it rests on nothing.
    first_id = enqueue(conn, path=first, owner_id=owner_id, class_id=class_id)
    assert process_one(
        conn,
        embedder=FakeEmbeddings(),
        chunk_size=CHUNK_SIZE_WORDS,
        chunk_overlap=CHUNK_OVERLAP_WORDS,
    )

    # 60 words fits whole inside the 250-word default, so the run above is one chunk; a 20-word
    # window has to split the same text. Any pair that forces different counts would do.
    second_id = enqueue(conn, path=second, owner_id=owner_id, class_id=class_id)
    assert process_one(
        conn,
        embedder=FakeEmbeddings(),
        chunk_size=20,
        chunk_overlap=5,
    )

    (default_chunks,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (first_id,)
    ).fetchone()
    (small_chunks,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (second_id,)
    ).fetchone()

    # Both non-zero first: a worker that ingested nothing at all would satisfy `!=` on 0 vs 0
    # only by accident, but it would satisfy plenty of other broken shapes, and "0 != 1" is not
    # the fact this test is about.
    assert default_chunks > 0
    assert small_chunks > 0
    assert default_chunks != small_chunks


def test_run_sleeps_only_on_empty_ticks(monkeypatch):
    """The loop naps on an idle tick and polls straight through a productive one.

    No DB and no `run` parameter added for testability: `while True` is exited by making the
    stubbed `process_one` raise once its script runs out. The alternative - a `max_ticks` knob
    on `run` - would put a branch in production code that only tests ever take.

    This pins the bug the loop shipped with in review: forwarding nothing and sleeping after
    EVERY tick, which adds `poll_seconds` of dead time to every job in a busy queue.
    """

    class Stop(Exception):
        """Sentinel - the only way out of the loop."""

    ticks = iter([True, True, False, True])
    slept: list[float] = []

    def fake_process_one(conn, **kwargs):
        try:
            return next(ticks)
        except StopIteration:
            raise Stop from None

    monkeypatch.setattr(worker, "process_one", fake_process_one)
    monkeypatch.setattr(worker.time, "sleep", slept.append)

    with pytest.raises(Stop):
        worker.run(None, embedder=None, chunk_size=1, chunk_overlap=0, poll_seconds=0.25)

    # Four ticks, exactly one of them empty - so exactly one sleep, at the configured interval.
    assert slept == [0.25]


def test_backoff_doubles_and_is_capped(monkeypatch):
    """The curve, with no DB and no worker - a pure function, pinned as one (ADR 0020).

    Two properties, and the second is the one that bites in production. Doubling is what makes
    the delay respond to a provider that is genuinely down rather than briefly busy. THE CAP is
    what keeps the delay a delay: uncapped, attempt 10 waits over seventeen minutes, which
    would outlast the lease and let the reaper hand the job to another worker mid-wait - two
    workers embedding the same file, the exact double-charge the lease number exists to stop.

    Asserted against the constants rather than literals: the numbers are provisional until the
    four-numbers ADR (PR 3) ratifies them, and a test written in literals would go red on a
    tuning change that broke nothing.
    """
    base = worker.BACKOFF_BASE_SECONDS
    cap = worker.BACKOFF_MAX_SECONDS

    assert backoff_seconds(1) == base
    assert backoff_seconds(2) == base * 2
    assert backoff_seconds(3) == base * 4
    assert all(backoff_seconds(n) <= cap for n in range(1, 50))
    assert backoff_seconds(49) == cap, "an uncapped curve would sail past the lease"


def test_a_corrupt_file_fails_terminally_and_buys_no_embed(db, db_other, tmp_path, monkeypatch):
    """queued -> processing -> failed(unparseable), with ZERO retries spent (acceptance, #71).

    "No retries spent" is not the same claim as "attempts == 0", and the difference is worth
    stating: the claim already bumped `attempts` to 1, because a claim happened. What must be
    true is that the job is TERMINAL - `state = failed`, never back to `queued` - so nothing
    will ever hand it out again. A corrupt file is exactly as corrupt on the next attempt; the
    budget exists for bad luck, not bad bytes (ADR 0020 §1).

    `embedder.calls == []` is the assertion the status columns cannot make. Parse comes before
    embed, so a terminal file must cost nothing at the provider - and a worker that caught
    `ParseError` in the wrong place (say, around the whole tick, after a retry loop) would set
    every status column here correctly while having paid for N embeds. `sleep` is asserted
    empty for the same reason: a terminal failure must not serve a backoff for a retry that is
    never coming.

    `failed_reason` is checked for the SPECIFIC value, not merely non-null. The whole point of
    the terminal taxonomy is that the student is told something actionable (ADR 0020), and
    `parse.py` promises `ParseError.reason` passes through untranslated - a worker that mapped
    everything to one reason would satisfy a non-null check and tell the student nothing.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    slept: list[float] = []
    monkeypatch.setattr(worker.time, "sleep", slept.append)
    embedder = FakeEmbeddings()
    source = write_corrupt_pdf(tmp_path / "shredded.pdf")
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    assert (
        process_one(
            conn,
            embedder=embedder,
            chunk_size=CHUNK_SIZE_WORDS,
            chunk_overlap=CHUNK_OVERLAP_WORDS,
        )
        is True
    ), "a failed job is still work - the tick must not read as idle"

    status, failed_reason = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (status, failed_reason) == ("failed", "unparseable")

    state, attempts, last_error = db_other.execute(
        "select state, attempts, last_error from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert state == "failed", "a terminal failure must not leave the job claimable"
    assert attempts == 1, "the claim happened once and nothing retried it"
    assert last_error and "unparseable" in last_error

    assert embedder.calls == [], "a terminal file must cost nothing at the provider"
    assert slept == [], "a terminal failure must not serve a retry backoff"
    (chunk_count,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert chunk_count == 0


def test_a_transient_failure_backs_off_then_requeues_the_job(db, db_other, tmp_path, monkeypatch):
    """Bad luck, not bad input: the job goes back to `queued` and the file stays mid-flight.

    Four things, and each is a different bug:
      - `jobs.state = queued` with the lease cleared - a later claim can pick it up;
      - `files.status` STAYS `processing` - which is still true, and it is why no new status
        transition was needed for the retry path. Only budget exhaustion moves a file to
        `failed` (ADR 0020: "land on failed(transient_exhausted) ONLY when the budget is
        spent"). A worker that flipped the file to `failed` on every 429 would tell the student
        the upload died while it was in fact still being retried;
      - nothing committed - zero chunks, because the index transaction never opened (ADR 0020
        §3), which is what makes retry-from-scratch safe;
      - the backoff was SERVED - one sleep, of exactly `backoff_seconds(1)`. Retrying instantly
        after a provider says "slow down" is ADR 0020's one genuinely wrong answer, and it is
        invisible in every status column above.

    The order the backoff is served in cannot be read off `slept` alone, so the state is: the
    sleep happens while the job is still `processing` under this worker's lease, so the delay
    applies to every worker rather than only to this one's next poll (`release`'s docstring).
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    slept: list[float] = []
    monkeypatch.setattr(worker.time, "sleep", slept.append)
    embedder = FakeEmbeddings(transient_failures=1)
    source = write_pdf(tmp_path / "flaky.pdf", ["some words the provider choked on"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    assert (
        process_one(
            conn,
            embedder=embedder,
            chunk_size=CHUNK_SIZE_WORDS,
            chunk_overlap=CHUNK_OVERLAP_WORDS,
        )
        is True
    )

    state, attempts, leased_until, last_error = db_other.execute(
        "select state, attempts, leased_until, last_error from jobs where file_id = %s::uuid",
        (file_id,),
    ).fetchone()
    assert (state, attempts, leased_until) == ("queued", 1, None)
    assert last_error and "429" in last_error, "the provider's own words are the diagnosis"

    status, failed_reason = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (status, failed_reason) == ("processing", None), (
        "a retryable failure must not tell the student the upload died"
    )

    (chunk_count,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert chunk_count == 0, "a failed attempt must commit nothing (ADR 0020 §3)"
    assert slept == [backoff_seconds(1)], "the retry was not backed off"


def test_a_retry_after_a_transient_failure_publishes_normally(db, db_other, tmp_path, monkeypatch):
    """The requeue is worth nothing unless a later claim actually finishes the job.

    This is the half the previous test cannot see: it proves the row `release` left behind is
    genuinely claimable and that a second attempt reaches `ready` - i.e. that the transient
    path is a RETRY and not a slower way to lose a file. Two embed calls, one file, one full
    chunk set: the all-or-nothing replace means the failed attempt left nothing to collide
    with (ADR 0020 §2).
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    embedder = FakeEmbeddings(transient_failures=1)
    source = write_pdf(tmp_path / "second-time-lucky.pdf", ["alpha words", "beta words"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    for _ in range(2):
        assert (
            process_one(
                conn,
                embedder=embedder,
                chunk_size=CHUNK_SIZE_WORDS,
                chunk_overlap=CHUNK_OVERLAP_WORDS,
            )
            is True
        )

    status, failed_reason = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (status, failed_reason) == ("ready", None)

    state, attempts = db_other.execute(
        "select state, attempts from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (state, attempts) == ("done", 2), "the second claim must be the one that finished it"
    assert len(embedder.calls) == 2, "the retry re-embeds from scratch (ADR 0020 §2)"

    (chunk_count,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert chunk_count > 0


def test_the_budget_runs_out_and_the_file_fails_transient_exhausted(
    db, db_other, tmp_path, monkeypatch
):
    """queued -> processing -> failed(transient_exhausted) once the budget is spent (acceptance).

    Run with `max_attempts=2` so the whole life of a doomed job fits in three ticks, and read
    what each one had to do:
      1. attempt 1 fails -> backoff `backoff_seconds(1)` -> requeued;
      2. attempt 2 fails -> NO backoff, because this was the last permitted attempt and the
         delay would buy a retry the budget check is about to refuse -> requeued;
      3. attempts == 3 > 2 -> buried, having claimed the job purely in order to bury it.

    `len(embedder.calls) == 2` is the sharp assertion: it says the budget bounded the number of
    PAID attempts at exactly `max_attempts`, and that tick 3 - which had to claim the job to
    write `failed_reason` at all, since `queue.py` may not touch `files` - spent nothing doing
    it. A budget that was checked one tick late would look identical in every status column and
    cost one extra embedding run per doomed file.

    `slept == [backoff_seconds(1)]` pins the same boundary from the delay side.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    slept: list[float] = []
    monkeypatch.setattr(worker.time, "sleep", slept.append)
    embedder = FakeEmbeddings(transient_failures=99)
    source = write_pdf(tmp_path / "doomed.pdf", ["words the provider never accepts"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    for _ in range(3):
        assert (
            process_one(
                conn,
                embedder=embedder,
                chunk_size=CHUNK_SIZE_WORDS,
                chunk_overlap=CHUNK_OVERLAP_WORDS,
                max_attempts=2,
            )
            is True
        )

    status, failed_reason = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (status, failed_reason) == ("failed", "transient_exhausted")

    state, attempts = db_other.execute(
        "select state, attempts from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert state == "failed", "an exhausted job must not stay claimable"
    assert attempts == 3, "two paid attempts plus the claim that buried it"

    assert len(embedder.calls) == 2, "the budget must bound PAID attempts, not just claims"
    assert slept == [backoff_seconds(1)], "the last permitted attempt must not wait for nothing"
    (chunk_count,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert chunk_count == 0


def test_a_terminal_failure_cannot_unpublish_a_file_that_is_already_ready(
    db, db_other, tmp_path, monkeypatch
):
    """The monotonic guard on the failure write - the direction that costs the student something.

    Reachable under at-least-once: worker A claims and stalls, the reaper requeues, worker B
    claims and publishes `ready`, then A wakes up and fails. Nothing stops A from running the
    failure handler - `complete`/`fail`/`release`'s lease guards protect the JOBS axis, and
    `files` is a table `queue.py` is not allowed to touch, so the guard has to live in the
    worker. Without it, a queryable file flips to `failed` in the student's UI while its chunks
    sit there answering questions.

    The `ready` row is set directly rather than by staging a real reclaim: `reclaim_expired` is
    wired into the poll loop in PR 3, and this test is about the WRITE's guard, not about the
    sequence that reaches it. A published `ready` file is a published `ready` file however it
    got there. The sibling assertion is that the jobs axis still records the failure - the
    guard suppresses the student-facing lie, not the operational trail.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_corrupt_pdf(tmp_path / "already-published.pdf")
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    # The run that won, standing in for worker B's `index_file`.
    conn.execute("update files set status = 'ready' where file_id = %s::uuid", (file_id,))

    assert (
        process_one(
            conn,
            embedder=FakeEmbeddings(),
            chunk_size=CHUNK_SIZE_WORDS,
            chunk_overlap=CHUNK_OVERLAP_WORDS,
        )
        is True
    )

    status, failed_reason = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (status, failed_reason) == ("ready", None), (
        "a zombie must not unpublish a file another run already made queryable"
    )
    (state,) = db_other.execute(
        "select state from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert state == "failed", "the jobs axis still records what this attempt did"


def test_the_backoff_never_outlasts_the_lease_it_is_served_under(
    db, db_other, tmp_path, monkeypatch
):
    """The sleep is bounded by the lease this worker actually holds, not by the module constants.

    The rule is stated where the constants live - both sit far under `DEFAULT_LEASE_SECONDS`
    because the sleep is served while the lease is HELD, so a delay outlasting it lets the
    reaper hand the job to a second worker mid-wait and two runs embed one file. But
    `lease_seconds` is a PARAMETER and the curve is module constants: they cannot see each
    other, so the relationship held only by coincidence of the defaults. At `lease_seconds=5`
    the uncapped curve slept 2s, 4s, 8s, then 16s - three times the lease it was holding.

    Driven at a 5-second lease precisely because the defaults CANNOT fail this: 60s under a
    900s lease passes whether or not the clamp exists, so a test written at the defaults would
    be green on both sides of the fix and prove nothing. The assertion is against
    `lease_seconds`, never a literal - the four numbers are provisional until PR 3's ADR
    (ADR 0020), and a test in literals would go red on a tuning change that broke nothing.

    Both halves are asserted. The bound alone would pass a worker that never slept at all -
    which is ADR 0020's one genuinely wrong answer to a provider saying "slow down" - so the
    first tick's delay is pinned to the full curve value it was always allowed to serve.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    lease_seconds = 5
    slept: list[float] = []
    monkeypatch.setattr(worker.time, "sleep", slept.append)
    embedder = FakeEmbeddings(transient_failures=99)
    source = write_pdf(tmp_path / "flaky-under-a-short-lease.pdf", ["words"])
    enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    for _ in range(4):
        process_one(
            conn,
            embedder=embedder,
            chunk_size=CHUNK_SIZE_WORDS,
            chunk_overlap=CHUNK_OVERLAP_WORDS,
            lease_seconds=lease_seconds,
        )

    assert slept, "the transient path served no backoff at all"
    assert max(slept) <= lease_seconds / 2, (
        f"the worker slept {max(slept)}s while holding a {lease_seconds}s lease - the reaper "
        "can hand the job to a second worker mid-wait, and both embed the same file"
    )
    assert slept[0] == backoff_seconds(1), (
        "the first delay fits inside the lease and must still be the full curve value - "
        "clamping something that already fit would turn a bound into a blanket cut"
    )
    assert any(d < backoff_seconds(n + 1) for n, d in enumerate(slept)), (
        "nothing was actually clamped, so this test would pass without the fix"
    )
