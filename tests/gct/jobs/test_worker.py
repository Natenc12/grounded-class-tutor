"""DB-backed tests for the worker: the happy path, the failure split, and the reaper (issue #71).

Same discipline as `test_queue.py`: real Postgres via the `db` fixture, and every assertion
about what the worker PUBLISHED goes through `db_other` - a second connection. A connection
sees its own uncommitted work, so reading back through `db` would hold whether or not anything
was committed (ADR 0025); the durability assertion IS the point of the happy-path test, not a
flourish on it.

The failure-split half exercises ADR 0020 §1 from both sides (ADR 0020 §1, DB-blip class
amended per ADR 0028 - a blip is not in the transient class). Its stubs are chosen so a failure
costs nothing and is exact: `FakeEmbeddings(transient_failures=N)` raises the
provider-agnostic `TransientEmbeddingError` on cue, and a corrupt PDF makes `parse_file`
raise the real `ParseError` rather than a mock of one - the terminal path's whole contract is
that `exc.reason` is already a legal `files.failed_reason`, and a stubbed exception would let that
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

import psycopg
import pytest
from reportlab.pdfgen import canvas

from gct.config import EMBEDDING_DIM
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.ingest.pipeline import ingest_file
from gct.jobs import worker
from gct.jobs.queue import claim, complete, enqueue, reclaim_expired
from gct.jobs.worker import (
    DEFAULT_LEASE_SECONDS,
    backoff_seconds,
    process_one,
    served_backoff,
)
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


def tick(conn, embedder, **overrides):
    """One `process_one` at the default chunk window; `overrides` is what a test is varying.

    The window is REQUIRED by `process_one` and forwarded verbatim - never hardcoded inside it
    (ADR 0019/0026). That requirement is a contract worth keeping, and repeating it at ten call
    sites is what buried the one kwarg each test is actually about (`max_attempts=2`,
    `lease_seconds=5`). Passing it here keeps the contract and makes the deviation the only
    thing visible where it matters.

    `test_chunk_window_reaches_ingest` deliberately does NOT use this - the whole point of that
    test is that both values are passed explicitly, so routing it through a helper that supplies
    them would test the helper.
    """
    return process_one(
        conn,
        embedder=embedder,
        chunk_size=CHUNK_SIZE_WORDS,
        chunk_overlap=CHUNK_OVERLAP_WORDS,
        **overrides,
    )


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

    processed = tick(conn, FakeEmbeddings())

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

    processed = tick(conn, FakeEmbeddings())

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

    processed = tick(conn, FakeEmbeddings())

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
    assert tick(conn, FakeEmbeddings())

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


class Stop(Exception):
    """Sentinel - the only way out of `run`'s `while True`.

    Shared by every loop test below. `run` takes no `max_ticks` knob on purpose: that would put
    a branch in production code which only tests ever take, so the loop is exited by making a
    stubbed collaborator raise once its script runs out.
    """


def _script_the_loop(monkeypatch, ticks, *, reclaims=None):
    """Stub out both of the loop's collaborators and record what it did, in order.

    Returns `(events, slept)`. `events` interleaves `("reap", conn)` and `("tick", conn)` so a
    test can assert not just THAT the reaper ran but that it ran BEFORE the claim - the ordering
    is the decision (ADR 0028 §5), and a test that only counted calls would pass a loop that
    reaped afterwards.

    Both stubs record the connection they were handed. `run` is driven with a SENTINEL object
    rather than `None` so "passed the wrong thing" is a distinguishable failure: a stub that
    accepts anything would survive the mutation `reclaim_expired(conn)` -> `reclaim_expired(x)`.

    `time.sleep` is stubbed because the scripts below contain empty ticks and the real
    `DEFAULT_POLL_SECONDS` is 2s - the delays are the assertion, never something a test waits
    out (the same rule the backoff tests keep).
    """
    events: list[tuple[str, object]] = []
    slept: list[float] = []
    reclaims = iter(reclaims if reclaims is not None else [])
    remaining = iter(ticks)

    def fake_reclaim(conn):
        events.append(("reap", conn))
        return next(reclaims, 0)

    def fake_process_one(conn, **kwargs):
        events.append(("tick", conn))
        try:
            return next(remaining)
        except StopIteration:
            raise Stop from None

    monkeypatch.setattr(worker, "reclaim_expired", fake_reclaim)
    monkeypatch.setattr(worker, "process_one", fake_process_one)
    monkeypatch.setattr(worker.time, "sleep", slept.append)
    return events, slept


def test_run_sleeps_only_on_empty_ticks(monkeypatch):
    """The loop naps on an idle tick and polls straight through a productive one.

    This pins the bug the loop shipped with in review: forwarding nothing and sleeping after
    EVERY tick, which adds `poll_seconds` of dead time to every job in a busy queue.
    """
    _events, slept = _script_the_loop(monkeypatch, [True, True, False, True])

    with pytest.raises(Stop):
        worker.run(object(), embedder=None, chunk_size=1, chunk_overlap=0, poll_seconds=0.25)

    # Four ticks, exactly one of them empty - so exactly one sleep, at the configured interval.
    assert slept == [0.25]


def test_the_reaper_runs_before_the_claim_on_every_tick(monkeypatch):
    """The reaper is on the poll loop, every iteration, ahead of `process_one` (ADR 0028 §5).

    Three properties, and each fails a different plausible wiring:
      - EVERY tick, not only the idle one. Reaping only when the queue looks empty leaves a
        crashed job stranded behind every queued file, with the student's status frozen -
        reclaim latency unbounded under exactly the load that makes it matter.
      - BEFORE `process_one`, so a job stranded by the previous crash is claimable by THIS tick
        rather than the next.
      - handed the worker's OWN connection. Asserted against a sentinel because a recording stub
        accepts any argument, so passing the wrong object would otherwise go green.

    Deliberately a pure test: `reclaim_expired` has its own DB coverage in `test_queue.py`, and
    what is unproven until here is the WIRING, which needs no database to state.
    """
    conn = object()
    # Two scripted ticks - one productive, one empty - then a third that raises out of the loop.
    events, _slept = _script_the_loop(monkeypatch, [True, False])

    with pytest.raises(Stop):
        worker.run(conn, embedder=None, chunk_size=1, chunk_overlap=0, poll_seconds=0)

    assert events == [
        ("reap", conn),
        ("tick", conn),  # a PRODUCTIVE tick still got reaped first
        ("reap", conn),
        ("tick", conn),  # ... and so did the EMPTY one that followed it
        ("reap", conn),
        ("tick", conn),  # ... and the reap happens before the claim that ends the loop
    ], "the reaper must run once per tick, before the claim, on the worker's own connection"


def test_the_reaper_is_silent_unless_it_actually_reclaimed_something(monkeypatch, caplog):
    """Zero reclaims logs nothing; a non-zero reclaim logs once, at WARNING.

    The silence is the load-bearing half. At a 2s poll a line per reap is 30 a minute of
    "nothing happened", which buries the lines that matter - the same argument that keeps
    `process_one`'s empty tick silent. The WARNING is the other half: a reclaim means a worker
    died without settling its job, which is abnormal, and the level has to survive a caller
    that configures no logging - Python's unconfigured root drops INFO.
    """
    _events, _slept = _script_the_loop(monkeypatch, [False, False], reclaims=[0, 3])

    with caplog.at_level("WARNING", logger="gct.jobs.worker"):
        with pytest.raises(Stop):
            worker.run(object(), embedder=None, chunk_size=1, chunk_overlap=0, poll_seconds=0)

    reaper_lines = [r for r in caplog.records if r.message.startswith("reaper:")]
    assert len(reaper_lines) == 1, "only the tick that reclaimed something may log"
    assert "3 job(s)" in reaper_lines[0].getMessage()
    assert reaper_lines[0].levelname == "WARNING"


def test_a_raising_reaper_stops_the_loop(monkeypatch):
    """No handler around the reaper - a DB error crashes the worker (ADR 0028 §4).

    Pins a rule that is otherwise invisible: the module's stance is that an unclassified
    exception propagates rather than being guessed into a `failed_reason`, and ADR 0028 ratifies
    that a DB error is in that class. Without this test a future defensive `except Exception`
    around the reap - the exact thing the ADR argues against - goes in green.
    """

    def boom(conn):
        raise psycopg.OperationalError("connection lost")

    def unreached(conn, **kwargs):
        raise Stop  # so a SWALLOWED reaper error fails as `Stop`, not as an incidental
        # AttributeError from the sentinel connection reaching a real `require_idle`

    monkeypatch.setattr(worker, "reclaim_expired", boom)
    monkeypatch.setattr(worker, "process_one", unreached)

    with pytest.raises(psycopg.OperationalError):
        worker.run(object(), embedder=None, chunk_size=1, chunk_overlap=0)


def test_backoff_doubles_and_is_capped(monkeypatch):
    """The curve, with no DB and no worker - a pure function, pinned as one (ADR 0020).

    Two properties, and the second is the one that bites in production. Doubling is what makes
    the delay respond to a provider that is genuinely down rather than briefly busy. THE CAP is
    what keeps the delay a delay: uncapped, attempt 10 waits over seventeen minutes, which
    would outlast the lease and let the reaper hand the job to another worker mid-wait - two
    workers embedding the same file, the exact double-charge the lease number exists to stop.

    Asserted against the constants rather than literals: ADR 0028 ratified the numbers without
    MEASURING them and names the log lines that would move them, so a test written in literals
    would go red on a tuning change that broke nothing.
    """
    base = worker.BACKOFF_BASE_SECONDS
    cap = worker.BACKOFF_MAX_SECONDS

    assert backoff_seconds(1) == base
    assert backoff_seconds(2) == base * 2
    assert backoff_seconds(3) == base * 4
    assert all(backoff_seconds(n) <= cap for n in range(1, 50))
    assert backoff_seconds(49) == cap, "an uncapped curve would sail past the lease"


def test_served_backoff_never_exceeds_the_curve_or_the_remaining_lease():
    """The clamp, as a pure function — no DB, no worker, no fake 429 (issue #71).

    `backoff_seconds` answers "how long does the curve want?"; this answers "how long can this
    worker afford?", and only the second is the delay. Pinned here rather than only through
    `test_the_backoff_never_outlasts_the_lease_it_is_served_under`, which needs Postgres and
    four `process_one` ticks to observe two numbers — the arithmetic deserves the same cheap,
    exact coverage the curve already has.

    Asserted against the constants, never literals: ADR 0028 ratified the four numbers without
    measuring them, so a test in literals would go red on a tuning change that broke nothing.
    """
    plenty = DEFAULT_LEASE_SECONDS

    # A lease with room to spare: the curve gets exactly what it asked for.
    wanted, served = served_backoff(1, max_attempts=5, lease_seconds=plenty, elapsed=0.0)
    assert (wanted, served) == (backoff_seconds(1), backoff_seconds(1))

    # The last permitted attempt wants nothing - the next claim's budget check refuses the job,
    # so a wait would buy a retry that never comes.
    assert served_backoff(5, max_attempts=5, lease_seconds=plenty, elapsed=0.0) == (0.0, 0.0)

    # A lease too short for the curve: `wanted` still reports what was asked, so the caller can
    # say "cut from X to Y" - a function returning only the served value makes that unloggable.
    wanted, served = served_backoff(3, max_attempts=5, lease_seconds=4, elapsed=0.0)
    assert wanted == backoff_seconds(3)
    assert served == 2.0, "the wait must fit inside the lease it is served under, halved"

    # The attempt already ate the whole lease: no sleep at all, and never a negative one.
    assert served_backoff(2, max_attempts=5, lease_seconds=10, elapsed=99.0) == (
        backoff_seconds(2),
        0.0,
    )

    # The invariant, swept: served is never above the curve, never above half the remaining
    # lease, and never negative - the three ways this could be wrong at once.
    for attempts in range(1, 8):
        for lease in (1, 5, 60, DEFAULT_LEASE_SECONDS):
            for elapsed in (0.0, 0.5, 3.0, 1e6):
                wanted, served = served_backoff(
                    attempts, max_attempts=5, lease_seconds=lease, elapsed=elapsed
                )
                assert 0.0 <= served <= wanted
                assert served <= max(0.0, lease - elapsed) / 2


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

    assert tick(conn, embedder) is True, (
        "a failed job is still work - the tick must not read as idle"
    )

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

    assert tick(conn, embedder) is True

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
        assert tick(conn, embedder) is True

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
        assert tick(conn, embedder, max_attempts=2) is True

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
    wired into the poll loop by `run`, and this test is about the WRITE's guard, not about the
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

    assert tick(conn, FakeEmbeddings()) is True

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
    `lease_seconds`, never a literal - ADR 0028 ratified the four numbers without measuring
    them, so a test in literals would go red on a tuning change that broke nothing.

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
        tick(conn, embedder, lease_seconds=lease_seconds)

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


def _backdate_lease(conn, file_id: str) -> None:
    """Push a claimed job's lease an hour into the past - a worker that died mid-job.

    Expiry is simulated by editing the timestamp rather than claiming with a tiny
    `lease_seconds` and sleeping: no wall-clock wait, and the lease is unambiguously expired
    rather than racing the test's own speed. Commits, so `reclaim_expired` starts outside a
    transaction (its contract; ADR 0025).

    Duplicated from `test_queue.py` rather than imported: pytest does not expose a conftest
    sideways, and this file already carries its own stubs for that reason (see the module
    docstring). Two lines of SQL is the cheaper duplication.
    """
    conn.execute(
        "update jobs set leased_until = now() - interval '1 hour' where file_id = %s::uuid",
        (file_id,),
    )
    conn.commit()


def test_a_reclaimed_job_reruns_and_leaves_one_chunk_set(db, db_other, tmp_path):
    """At-least-once redelivery leaves ONE full chunk set, not two (ADR 0020 §2, issue #71).

    The acceptance criterion the failure-split PR could not reach, because reaching it needs a
    reclaim. The scenario is the one `reclaim_expired`'s docstring warns about and no test had
    yet driven end to end through the WORKER: reclaiming is not killing, so a stalled-but-alive
    worker keeps running and its job gets re-handed out while it is still holding results.

    Worker A claims and ingests - it has genuinely published `ready` and a full chunk set - but
    is slow, and its lease expires before it can call `complete`. The reaper requeues the job.
    Worker B claims the SAME job and re-runs the whole pipeline from scratch.

    What must be true afterwards is the invariant the differentiator rests on: the file has
    exactly ONE chunk set. `index_file`'s delete-then-insert is what makes the duplicate run
    free of consequence rather than merely survivable - a Retriever reading a doubled corpus
    would rank the same passage twice and the Grounder would cite it twice. Every assertion goes
    through `db_other`, because a doubled set would be just as invisible to the connection that
    wrote it as a published one is.

    NOTE ON WHAT THIS DOES AND DOES NOT COVER: it drives `reclaim_expired` and `process_one`
    directly, never `run`, so it proves the IDEMPOTENCY acceptance box and passes identically
    with or without the reaper being wired into the poll loop. The wiring is covered by
    `test_the_reaper_runs_before_the_claim_on_every_tick`. Two different claims, two tests.
    """
    conn, owner_id, class_id = db
    embedder = FakeEmbeddings()
    pdf = write_pdf(tmp_path / "lecture-4.pdf", ["reclaimed page one", "reclaimed page two"])
    file_id = enqueue(conn, path=pdf, owner_id=owner_id, class_id=class_id)

    # Worker A: claims, does the whole job, publishes - and then stalls before settling it.
    stalled = claim(conn, lease_seconds=3600)
    assert stalled is not None
    ingest_file(
        stalled.staging_ref,
        stalled.owner_id,
        stalled.class_id,
        file_id=stalled.file_id,
        embedder=embedder,
        conn=conn,
        chunk_size=CHUNK_SIZE_WORDS,
        chunk_overlap=CHUNK_OVERLAP_WORDS,
    )
    (published,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert published > 0, "the scenario needs A to have really published something to double"

    # A's lease expires; the reaper hands the job back. `attempts` is deliberately NOT reset,
    # so B's claim is attempt 2 - well inside the default budget, which is what lets it run.
    _backdate_lease(conn, file_id)
    assert reclaim_expired(conn) == 1
    # Read through `db_other` because it is the reclaim's durable effect, not a return value.
    # Asserted here rather than left to the `complete` at the bottom: by then B has moved the
    # job to `done`, so the guard's STATE half alone refuses A and the token clearing goes
    # unpinned - dropping `lease_token = null` from `reclaim_expired` leaves this whole file
    # green without it.
    (token_after_reclaim,) = db_other.execute(
        "select lease_token from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert token_after_reclaim is None, "the lease and its proof must die together"

    # Worker B: the full tick, from a clean claim through to `complete`.
    assert tick(conn, embedder) is True

    status, chunk_count, state, attempts = db_other.execute(
        """
        select f.status, (select count(*) from chunks c where c.file_id = f.file_id),
               j.state, j.attempts
        from files f join jobs j using (file_id)
        where f.file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()

    assert len(embedder.calls) == 2, (
        "B must RE-RUN the pipeline, not inherit A's chunks - without this the test passes "
        "for a worker that claimed and completed while skipping the ingest entirely, which "
        "is what `FakeEmbeddings.calls` exists to catch (was an embed actually bought?)"
    )
    assert chunk_count == published, (
        "the re-run must REPLACE the chunk set, not add to it - a doubled corpus is the "
        "at-least-once failure ADR 0020's delete-then-insert exists to make impossible"
    )
    assert status == "ready", "B republished the file; the student never saw it leave ready"
    assert state == "done"
    assert attempts == 2, "the reclaim preserves the retry trail rather than granting a fresh one"

    # A finally wakes up. Its token died with its lease, so the run that won is safe from it.
    assert complete(conn, job_id=stalled.job_id, lease_token=stalled.lease_token) is False, (
        "a stalled worker must not be able to settle a job that was re-handed out"
    )


def test_run_reaps_a_stranded_job_on_a_real_connection(db, db_other, tmp_path, monkeypatch):
    """The one thing the stubbed loop tests cannot prove: `run`'s reap works on a REAL conn.

    Every other loop test stubs `reclaim_expired`, so the call site `run` now owns is never
    driven against Postgres - and that call site has a connection precondition. `reclaim_expired`
    calls `require_idle` and RAISES on a connection left inside a transaction (ADR 0025, guarded
    per ADR 0027), so a loop that reaped at the wrong moment would not return a wrong answer, it
    would crash the worker on the first tick. Stubbing the reaper everywhere would leave that
    entirely uncovered while looking thorough.

    Driven on a PLAIN connection - no autocommit - deliberately. `scripts/worker.py` wires
    autocommit as defense in depth, so testing under it would prove the wiring rather than the
    loop; the contract is that every writer commits inside its own `conn.transaction()` and
    leaves the connection idle for the next one.

    `process_one` is stubbed to end the loop after one tick, so what runs here is the real
    reaper against a real stranded job and nothing else.
    """
    conn, owner_id, class_id = db
    pdf = write_pdf(tmp_path / "stranded.pdf", ["a page the dead worker never finished"])
    file_id = enqueue(conn, path=pdf, owner_id=owner_id, class_id=class_id)

    # A worker claims the job and dies: the row stays `processing` under a lease nothing settles.
    stranded = claim(conn, lease_seconds=3600)
    assert stranded is not None
    _backdate_lease(conn, file_id)

    ticks = iter([Stop])

    def stop_after_one_tick(conn, **kwargs):
        raise next(ticks)

    monkeypatch.setattr(worker, "process_one", stop_after_one_tick)
    with pytest.raises(Stop):
        worker.run(conn, embedder=None, chunk_size=1, chunk_overlap=0)

    state, leased_until, token = db_other.execute(
        "select state, leased_until, lease_token from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (state, leased_until, token) == ("queued", None, None), (
        "the loop's reaper must return a stranded job to the queue, on a real connection, "
        "with the lease and its proof both cleared"
    )
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE, (
        "the reap must leave the connection idle for the claim that follows it (ADR 0025)"
    )


# ---------------------------------------------------------------------------
# Shutdown (issue #82): a worker killed mid-ingest hands its job back rather
# than stranding it under a lease nothing can reach for up to 15 minutes.
# ---------------------------------------------------------------------------


def _interrupt(*_args, **_kwargs):
    """Stand in for a Ctrl-C landing anywhere inside the pipeline.

    `KeyboardInterrupt` rather than a custom exception because it is the ONE class the guard
    must not miss: it derives from `BaseException`, not `Exception`, so a handler written as
    `except Exception` would let it straight past and leave the job stranded - the exact
    behavior this issue is about. Testing with an ordinary exception would pass against that
    broken narrower catch.
    """
    raise KeyboardInterrupt("Ctrl-C mid-ingest")


def test_an_interrupt_mid_ingest_leaves_the_job_claimable_at_once(
    db, db_other, tmp_path, monkeypatch
):
    """The claim issue #82 makes: killed mid-ingest, the job is claimable NOW - no lease wait.

    Two halves, and the second is the one that fails without the shutdown release:
      - the ROW - `queued` with `leased_until` null (only `release` clears the lease;
        `complete`/`fail` deliberately leave a terminal row's stale lease in place), and
        `attempts` still 1 - a shutdown costs one attempt exactly as a crash or a caught 429
        does, so there is no special case to invent (ADR 0028 §1);
      - the CONSEQUENCE - a fresh `claim` returns this same job at attempt 2. Before this fix
        that call returns `None` for up to `DEFAULT_LEASE_SECONDS`, because `reclaim_expired`'s
        `leased_until < now()` is false against a live lease, by design.

    The `KeyboardInterrupt` must still come out. Swallowing it would contradict ADR 0028 §4 -
    the worker crashes on an unclassified exception on purpose - so `pytest.raises` here is
    half the assertion, not scaffolding around it. Every durable claim is read through
    `db_other`: through `conn` they hold whether or not the release was ever committed.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker, "ingest_file", _interrupt)
    source = write_pdf(tmp_path / "interrupted.pdf", ["a page the operator never let finish"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    with pytest.raises(KeyboardInterrupt):
        tick(conn, FakeEmbeddings())

    state, attempts, leased_until, last_error = db_other.execute(
        """
        select state, attempts, leased_until, last_error
        from jobs where file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert (state, attempts, leased_until) == ("queued", 1, None)
    assert last_error and "shutdown" in last_error, (
        "the row must say WHY it came back, or a stopped worker is indistinguishable from a 429"
    )

    # NOT asserted: `lease_token is null`. Issue #82's acceptance text expects it, and `release`
    # does not do it - it clears `leased_until` only, which `reclaim_expired` (which DOES null
    # the token) makes look like an oversight. Measured, it is inert rather than wrong: with the
    # row back on `queued`, `_settle`'s FIRST condition (`state = 'processing'`) already refuses
    # every zombie the token would have refused, and the next `claim` mints a fresh token over
    # it. Left alone on purpose - `release` is shared with the transient-retry path, its
    # docstring enumerates what it clears and argues the asymmetry, and changing it is a
    # decision about that verb rather than about shutdown.

    # `files.status` is deliberately untouched - the same thing the transient-failure release
    # does, and still true: the file is mid-flight and a later claim rewrites it. A worker that
    # is stopped and never restarted leaves it `processing` regardless of this fix (issue #82,
    # explicitly out of scope).
    status, failed_reason = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (status, failed_reason) == ("processing", None)

    (chunk_count,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert chunk_count == 0, "an interrupted run commits nothing (ADR 0020 §2/§3)"

    # THE assertion this issue exists for. Note the default lease is passed explicitly: the
    # point is that a full 15-minute lease is no obstacle, so shortening it here would test
    # something easier than the thing that was broken.
    requeued = claim(conn, lease_seconds=DEFAULT_LEASE_SECONDS)
    assert requeued is not None, (
        "the interrupted job is still stranded under its own live lease - nothing can claim it"
    )
    assert requeued.file_id == file_id
    assert requeued.attempts == 2, "the retry trail is preserved, not given back (ADR 0028 §1)"


def test_a_raising_release_does_not_mask_the_interrupt_that_triggered_it(
    db, db_other, tmp_path, monkeypatch
):
    """The DB is gone - one of the very causes of the shutdown - and the release cannot run.

    What must come out is the `KeyboardInterrupt`, not a psycopg traceback: turning a Ctrl-C
    into an error about the database tells the operator the wrong story, and it would hide the
    original from `run`'s caller. The fallback is then exactly the pre-#82 behavior - the row
    stays `processing` under its live lease and the reaper collects it when the lease expires -
    which is why this handler is an optimization of a path that was already safe, never a
    correctness dependency.

    Asserted through `db_other`, because "nothing was written" is a claim about what other
    connections can see.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True

    def db_is_gone(*_args, **_kwargs):
        raise psycopg.OperationalError("the connection is closed")

    monkeypatch.setattr(worker, "ingest_file", _interrupt)
    monkeypatch.setattr(worker, "release", db_is_gone)
    source = write_pdf(tmp_path / "db-gone.pdf", ["a page nobody could hand back"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    with pytest.raises(KeyboardInterrupt):
        tick(conn, FakeEmbeddings())

    state, leased_until = db_other.execute(
        "select state, leased_until from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert state == "processing", "a failed release must not half-write the row"
    assert leased_until is not None, (
        "the fallback IS the 15-minute lease; a cleared lease here would mean the release "
        "partly succeeded"
    )


def test_a_lost_lease_on_shutdown_is_logged_not_raised(db, tmp_path, monkeypatch, caplog):
    """`release` returning False is the zombie case, and it is a report - never an error.

    The guard that produces the False is `_settle`'s `state='processing' AND lease_token = ours`
    and it is already covered, on a real reap-and-re-claim, by
    `test_release_refuses_a_job_another_worker_now_holds` (tests/gct/jobs/test_queue.py). Not
    re-driven here: this test is about what `process_one` DOES with the False, which is the
    same thing it already does on the transient and terminal paths - log the lost lease and
    move on. A handler that raised on it would turn the routine at-least-once outcome into a
    second traceback stacked on the interrupt.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker, "ingest_file", _interrupt)
    monkeypatch.setattr(worker, "release", lambda *_a, **_k: False)
    source = write_pdf(tmp_path / "zombie.pdf", ["a page a second worker already owns"])
    enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    with caplog.at_level("WARNING"):
        with pytest.raises(KeyboardInterrupt):
            tick(conn, FakeEmbeddings())

    assert any("lease lost" in record.message for record in caplog.records), (
        "a lost lease on shutdown must be reported, the way every other settle path reports it"
    )


def test_the_shutdown_release_works_on_a_plain_connection_left_mid_transaction(
    db, db_other, tmp_path, monkeypatch
):
    """`require_idle` must not be what stops the release (ADR 0025, guarded per ADR 0027).

    `run` accepts a PLAIN connection by contract - `scripts/worker.py`'s autocommit is defense in
    depth, not a requirement (see `test_processing_status_publishes_on_a_plain_connection`). On
    such a connection a bare statement outside any transaction block opens psycopg's implicit
    transaction and leaves it INTRANS, and `release` calls `require_idle`, which RAISES on that.
    The handler would then swallow its own `RuntimeError` and requeue nothing - correct only
    under the shipped wiring, and silently broken under the contract.

    The stub reproduces exactly that: a bare `select 1` and then the interrupt, which is the
    shape of any unclassified exception raised after the pipeline touched the connection
    directly. `conn.rollback()` in the handler is what makes IDLE unconditional; drop it and
    this test is the only one that reddens.
    """
    conn, owner_id, class_id = db  # deliberately NOT autocommit - that is the whole point

    def bare_statement_then_interrupt(*_args, **kwargs):
        kwargs["conn"].execute("select 1")
        raise KeyboardInterrupt("Ctrl-C with the connection left INTRANS")

    monkeypatch.setattr(worker, "ingest_file", bare_statement_then_interrupt)
    source = write_pdf(tmp_path / "plain-conn-shutdown.pdf", ["a page on a plain connection"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    with pytest.raises(KeyboardInterrupt):
        tick(conn, FakeEmbeddings())

    state, leased_until = db_other.execute(
        "select state, leased_until from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (state, leased_until) == ("queued", None), (
        "the shutdown release refused its own connection - `require_idle` fired instead of the "
        "requeue, so the job is stranded under a live lease on the contract's own wiring"
    )
