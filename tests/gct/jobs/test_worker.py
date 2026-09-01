"""DB-backed tests for the worker: the happy path, the failure split, the reaper, the shutdown.

The first three are issue #71; the shutdown half is issue #82 and is now roughly half the file.

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

The shutdown half asks a different question from the other three, and it needs saying because
it changes what a test here has to do to be worth anything. The others ask what the worker does
with an INPUT - a good file, a corrupt one, a 429, an expired lease. This one asks where an
INTERRUPT LANDS, which is not an input at all: the guard in `process_one` claims to cover every
statement between `claim` committing and the settle verb returning, so it is only as good as its
worst-covered statement and a single test through `ingest_file` proves nothing about its edges.
The sweep near the bottom of this file is one case per landing point for that reason, and its
two subtle points - after `index_file` commits, and between `_bury`'s two writes - carry their
verdict in the case rather than in a comment somewhere else.

Interrupts are injected by stubbing whatever runs at the point under test, EXCEPT in
`test_both_signals_leave_the_job_in_the_same_state`, which delivers a real SIGINT and a real
SIGTERM through `signal.raise_signal` - so nothing about the handler-to-exception step is
simulated, and the exception lands between bytecodes rather than at a statement the test chose.
An expired lease is always FORCED (`_backdate_lease`), never waited out: the measured worst case
ingests 62x inside the real lease, so a test that waited would be waiting for something that
does not happen.

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
import signal
from pathlib import Path

import psycopg
import pytest
from reportlab.pdfgen import canvas

from gct.config import EMBEDDING_DIM
from gct.ingest import pipeline
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from gct.ingest.parse import ParseError
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
    re-driven here: this test is about what `process_one` DOES with the False - report it and
    move on. A handler that raised on it would turn the routine at-least-once outcome into a
    second traceback stacked on the interrupt.

    What it asserts is the fact BOTH causes share, not a diagnosis. Unlike the transient and
    terminal paths - where a `False` can only mean a lost lease, because the row is still
    `processing` when they run - the widened guard gives this call site a second cause: the
    sliver after a settle verb has already returned. So the message may only claim what both
    have in common, that this worker no longer holds the job.
    `…does_not_blame_another_worker` drives the other cause and pins that the lost-lease
    diagnosis is not asserted for it.
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

    assert any("no longer holds it" in record.message for record in caplog.records), (
        "a release the guard could not make must be reported, not swallowed"
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


def test_a_shutdown_after_the_job_was_already_settled_does_not_blame_another_worker(
    db, db_other, tmp_path, monkeypatch, caplog
):
    """`release` returning False has TWO causes since the guard was widened, not one.

    The guard spans everything after the claim, so it also covers the sliver AFTER a settle verb
    has already returned - `complete` has written `done`, or `_bury`'s `fail` has written
    `failed`, and the interrupt lands before `process_one` returns. `_settle` then refuses the
    shutdown release on its FIRST condition (`state = 'processing'`), which is correct and
    writes nothing.

    What is not correct is calling that "another worker owns it now". No other worker exists -
    V1 runs one (ADR 0011, and ADR 0028 §5's safety argument rests on it) - and this worker
    settled the job itself a microsecond earlier. That message is the diagnosis the OTHER settle
    paths make, where it is sound because their `False` can only mean a lost lease; the widening
    gave THIS call site a second cause its message never covered.

    It matters because ADR 0028 §Consequences makes these lines evidence: it spends a section on
    which log line may and may not be read as a signal about the lease number, and a WARNING
    asserting a concurrency event that cannot occur in V1 is a false one.

    The row is asserted through `db_other` to pin the other half - that the refused release
    wrote nothing and the terminal settle stands.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    real_complete = worker.complete

    def settle_then_interrupt(*args, **kwargs):
        # The real window: `complete` returns True, then the interrupt lands before
        # `process_one` can return - on the very `logger.info("done in ...")` call below it.
        real_complete(*args, **kwargs)
        raise KeyboardInterrupt("Ctrl-C landing just after the job was settled")

    monkeypatch.setattr(worker, "complete", settle_then_interrupt)
    source = write_pdf(tmp_path / "settled-then-stopped.pdf", ["a page that finished in time"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    with caplog.at_level("WARNING"):
        with pytest.raises(KeyboardInterrupt):
            tick(conn, FakeEmbeddings())

    state, _ = db_other.execute(
        "select state, attempts from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert state == "done", "the refused shutdown release must not disturb a settled job"

    assert not any("another worker owns it" in record.message for record in caplog.records), (
        "the shutdown release was refused because THIS worker had already settled the job, but "
        "the log blames a second worker - one that cannot exist in V1 (ADR 0011)"
    )


def test_no_interrupt_after_the_claim_can_strand_the_job(db, db_other, tmp_path, monkeypatch):
    """The guard's stated invariant: it covers the whole window in which the lease is held.

    The lease is held from the moment `claim` commits, so every statement after it has to be
    inside the guard - including the `claimed file` log line, which is real work (a handler can
    format, lock and write to a stream) and is exactly where a signal can land. Left outside,
    an interrupt there reproduces the whole defect issue #82 exists to close: the job keeps a
    live lease, `reclaim_expired`'s `leased_until < now()` refuses it, and nothing can claim it
    for up to `DEFAULT_LEASE_SECONDS`.

    Driving it through the logger is not a contrivance about logging - it is the only statement
    between the claim and the guard, so it is the only place this window can be demonstrated
    from. The assertion is about the window, not the log line.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True

    def interrupt_on_the_claim_log(*_args, **_kwargs):
        raise KeyboardInterrupt("SIGTERM landing on the claimed-file log line")

    monkeypatch.setattr(worker.logger, "info", interrupt_on_the_claim_log)
    source = write_pdf(tmp_path / "stranded-at-the-claim.pdf", ["a page nobody got to read"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    with pytest.raises(KeyboardInterrupt):
        tick(conn, FakeEmbeddings())

    state, leased_until = db_other.execute(
        "select state, leased_until from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (state, leased_until) == ("queued", None), (
        "an interrupt one statement after the claim strands the job under a live lease - the "
        "guard starts too late to cover the window it claims to cover"
    )
    requeued = claim(conn, lease_seconds=DEFAULT_LEASE_SECONDS)
    assert requeued is not None and requeued.file_id == file_id, (
        "nothing can claim the job until its lease elapses - the defect #82 exists to close"
    )


# ---------------------------------------------------------------------------
# Shutdown, part 2 (issue #82 hardening): WHERE the interrupt lands.
#
# The guard's claim is not "an interrupt inside `ingest_file` is handled" - it is
# that EVERY statement between `claim` committing and the settle verb returning is
# covered, because the lease is live for all of them. One test per landing point is
# what turns that from a comment into a checked property, and the two subtle points
# (after `index_file` commits; between `_bury`'s two writes) are where a plausible
# reading of the code gets the right answer for the wrong reason.
# ---------------------------------------------------------------------------


def _arm_after_claim(monkeypatch, conn, tmp_path):
    """(a) The very next statement after `claim` returned - the `claimed file` log line.

    The only statement that exists between the claim and the guard, so it is the only place
    this window can be driven from. `test_no_interrupt_after_the_claim_can_strand_the_job`
    pins the guard's OPENING BRACE from this same point; this sweep re-drives it because the
    sweep's assertion set is uniform across all seven points - a case that is exempted from
    the common assertions is a case nobody notices going quiet.
    """

    def interrupt_on_the_claim_log(*_args, **_kwargs):
        raise KeyboardInterrupt("Ctrl-C on the statement right after `claim` returned")

    monkeypatch.setattr(worker.logger, "info", interrupt_on_the_claim_log)
    return write_pdf(tmp_path / "at-the-claim.pdf", ["a page nobody got to read"]), FakeEmbeddings()


def _arm_during_the_processing_write(monkeypatch, conn, tmp_path):
    """(b) After the `processing` UPDATE has run, before its transaction commits.

    Wrapping `conn.execute` is the only way to get inside `with conn.transaction():` - and
    inside is the interesting half: psycopg's block rolls back on `BaseException`, so the
    UPDATE is DISCARDED and the file stays `queued`. That is the correct answer (the worker
    never got to work on it) and it is not the one a reader assumes, since the statement
    demonstrably ran.
    """
    real_execute = conn.execute

    def execute_then_interrupt(query, *args, **kwargs):
        result = real_execute(query, *args, **kwargs)
        if "set status = 'processing'" in str(query):
            raise KeyboardInterrupt("Ctrl-C between the `processing` UPDATE and its COMMIT")
        return result

    monkeypatch.setattr(conn, "execute", execute_then_interrupt)
    return write_pdf(tmp_path / "mid-write.pdf", ["a page mid-status-write"]), FakeEmbeddings()


def _arm_during_parse(monkeypatch, conn, tmp_path):
    """(c) Inside `parse_file`, i.e. the first real work the pipeline does.

    Patched on `gct.ingest.pipeline` rather than on the worker, so the interrupt lands where a
    real one would - several frames down, inside `compose`, with `ingest_file` mid-flight.
    """

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt("Ctrl-C while the parser was reading the file")

    monkeypatch.setattr(pipeline, "parse_file", interrupt)
    return write_pdf(tmp_path / "mid-parse.pdf", ["a page half-read"]), FakeEmbeddings()


class InterruptingEmbeddings(FakeEmbeddings):
    """An embedder that takes a Ctrl-C mid-batch - the longest window in the whole run.

    Deliberately a `KeyboardInterrupt` rather than `TransientEmbeddingError`: this is the
    operator stopping the worker during the paid call, not the provider refusing it, and the
    two take completely different paths out of `process_one`.
    """

    def embed(self, texts):
        self.calls.append(list(texts))
        raise KeyboardInterrupt("Ctrl-C mid-embed")


def _arm_during_embed(monkeypatch, conn, tmp_path):
    """(d) Inside the embedder - in production the longest-lived frame in the run."""
    return write_pdf(tmp_path / "mid-embed.pdf", ["a page half-embedded"]), InterruptingEmbeddings()


def _arm_after_index_before_complete(monkeypatch, conn, tmp_path):
    """(e) THE SUBTLE ONE: `index_file` has committed, `complete` has not run.

    WHAT THE CORRECT OUTCOME IS: the job goes back to `queued` with its lease cleared, and the
    file stays `ready` with its chunk set intact.

    WHY, on both halves:
      - THE JOB. `_release_on_shutdown` is called UNCONDITIONALLY, with no "did the pipeline
        finish?" branch (settled; `_release_on_shutdown`'s docstring carries the argument). So a
        finished-but-unsettled job is handed back and the next claim re-ingests it - a duplicate
        embedding bill. That is not a NEW cost class: today's reaper does exactly this to exactly
        this job fifteen minutes later, and ADR 0028 §Consequences already enumerates `ready` as
        a status a reaped job's file can be in. The shutdown release makes the same event happen
        sooner, not a different event.
      - THE FILE. It must NOT be demoted. `files.status` is a promise to the student and `ready`
        is the one direction that costs them something real to take back; the release verb
        touches `jobs` only, and the re-claim's `processing` write is guarded by
        `status <> 'ready'`. The chunk count is asserted for the same reason: a status string is
        something a bug can set with nothing behind it.

    Patching `complete` to raise WITHOUT calling through is what puts the interrupt in the
    window rather than after it - the sibling case, where `complete` did run first, is
    `test_a_shutdown_after_the_job_was_already_settled_does_not_blame_another_worker`.
    """

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt("Ctrl-C after `index_file` committed, before `complete` ran")

    monkeypatch.setattr(worker, "complete", interrupt)
    return write_pdf(tmp_path / "indexed-not-settled.pdf", ["a page that got all the way"]), (
        FakeEmbeddings()
    )


def _arm_during_the_backoff_sleep(monkeypatch, conn, tmp_path):
    """(f) Inside `time.sleep(delay)` - the worker is HOLDING the lease while it waits.

    The window matters more than its length suggests: the backoff is served before the transient
    `release` precisely so the delay binds every worker rather than just this one's next poll
    (`release`'s docstring), which means the lease is live for the whole sleep. If this window
    were outside the guard, stopping a worker during a retry backoff - a window up to
    BACKOFF_MAX_SECONDS wide, and the single most likely moment for an operator to give up on a
    worker that is visibly stuck - would strand the job for the full lease.

    It IS inside: the `except TransientEmbeddingError` block is nested inside the `try` the
    guard closes. Asserted here rather than read off the indentation.
    """

    def interrupt(_delay):
        raise KeyboardInterrupt("Ctrl-C while the worker slept off a 429")

    monkeypatch.setattr(worker.time, "sleep", interrupt)
    return (
        write_pdf(tmp_path / "mid-backoff.pdf", ["a page waiting out a 429"]),
        FakeEmbeddings(transient_failures=1),
    )


def _arm_inside_bury(monkeypatch, conn, tmp_path):
    """(g) THE OTHER SUBTLE ONE: inside `_bury`, after its `files` write, before its `jobs` write.

    WHAT THE CORRECT OUTCOME IS: the file reads `failed`/`unparseable` (committed - that write
    is its own transaction), and the job goes back to `queued`, claimable at once.

    WHY THAT IS RIGHT RATHER THAN A HALF-WRITTEN MESS. `_bury` writes `files` FIRST on purpose,
    and its docstring picks that order by asking what a crash between the two leaves behind:
    files-then-jobs leaves a `failed` file under a job that is still claimable, so a later
    attempt overwrites both and the row self-heals; jobs-then-files leaves a terminally-`failed`
    job and a file stranded in `processing` forever. This interrupt lands in exactly that window,
    so the shutdown release is not fighting the ordering - it is delivering the recovery the
    ordering was chosen to buy, without the fifteen-minute lease wait.

    The `failed -> processing` transition that the recovery needs is legal by construction: the
    re-claim's status write guards on `status <> 'ready'`, and `process_one`'s comment records
    that permitting `failed` here is load-bearing for exactly this case. The test drives the
    recovery to its end rather than stopping at the requeue, because "self-healing" is a claim
    about the NEXT attempt, not about this row.
    """

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt("Ctrl-C inside `_bury`, between the files write and the jobs write")

    monkeypatch.setattr(worker, "fail", interrupt)
    return write_corrupt_pdf(tmp_path / "mid-bury.pdf"), FakeEmbeddings()


# (arm, the `files.status` this point leaves behind, its failed_reason, whether chunks survive).
# The file status is the axis that legitimately DIFFERS between landing points - before the
# status write it is still `queued`, after it `processing`, after `index_file` `ready`, and
# inside `_bury` `failed`. Everything about the JOB is identical at all seven, which is the
# property being swept.
INTERRUPT_POINTS = [
    pytest.param(_arm_after_claim, "queued", None, False, id="a-right-after-the-claim"),
    pytest.param(
        _arm_during_the_processing_write, "queued", None, False, id="b-during-the-status-write"
    ),
    pytest.param(_arm_during_parse, "processing", None, False, id="c-during-parse"),
    pytest.param(_arm_during_embed, "processing", None, False, id="d-during-embed"),
    pytest.param(
        _arm_after_index_before_complete, "ready", None, True, id="e-after-index-before-complete"
    ),
    pytest.param(
        _arm_during_the_backoff_sleep, "processing", None, False, id="f-during-the-backoff-sleep"
    ),
    pytest.param(
        _arm_inside_bury, "failed", "unparseable", False, id="g-inside-bury-between-its-two-writes"
    ),
]


@pytest.mark.parametrize("arm,file_status,failed_reason,chunks_survive", INTERRUPT_POINTS)
def test_an_interrupt_anywhere_between_claim_and_settle_hands_the_job_back(
    db, db_other, tmp_path, monkeypatch, arm, file_status, failed_reason, chunks_survive
):
    """Seven landing points, one outcome for the JOB: `queued`, lease cleared, claimable now.

    The guard is a claim about a WINDOW, not about a call site, so it is only as good as its
    worst-covered statement. One test that interrupts `ingest_file` proves the middle of the
    window and says nothing about its edges - and the edges are where the interesting failures
    are: before the status write, after the index commit, and between `_bury`'s two writes.

    Every case asserts the same four durable facts about `jobs`, through `db_other` because they
    are claims about what other connections can see:
      - `state = 'queued'` - handed back, not stranded;
      - `leased_until is null` - only `release` clears it, so this also proves the settle verb
        was `release` and not something that merely looked like it;
      - `attempts = 1` - a shutdown costs ONE attempt, the same as a crash or a caught 429
        (ADR 0028 §1); there is no special case to invent and none was;
      - `last_error` names the shutdown, so an operator can tell a stopped worker from a 429.

    And then the consequence the issue exists for: a fresh `claim` at the FULL default lease
    returns this same job at attempt 2. The default is passed explicitly - shortening it here
    would test something easier than the thing that was broken.

    `files.status` is the one axis that legitimately varies, so it is a parameter rather than an
    assertion dropped for being inconvenient. `lease_token` is deliberately not asserted: it
    survives the release, measured inert (see the note in
    `test_an_interrupt_mid_ingest_leaves_the_job_claimable_at_once`).
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    source, embedder = arm(monkeypatch, conn, tmp_path)
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    with pytest.raises(KeyboardInterrupt):
        tick(conn, embedder)

    state, attempts, leased_until, last_error = db_other.execute(
        "select state, attempts, leased_until, last_error from jobs where file_id = %s::uuid",
        (file_id,),
    ).fetchone()
    assert (state, attempts, leased_until) == ("queued", 1, None), (
        "an interrupt at this point leaves the job stranded under a live lease - the guard does "
        "not actually cover the whole window between the claim and the settle"
    )
    assert last_error is not None and last_error.startswith("shutdown:"), (
        "the row must say WHY it came back, or a stopped worker is indistinguishable from a 429"
    )

    status, reason = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (status, reason) == (file_status, failed_reason)

    (chunk_count,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (chunk_count > 0) is chunks_survive, (
        "an interrupted run publishes chunks only if `index_file` had already committed "
        "(ADR 0020 §2/§3) - and when it had, the shutdown release must not delete them"
    )

    requeued = claim(conn, lease_seconds=DEFAULT_LEASE_SECONDS)
    assert requeued is not None, (
        "nothing can claim the job until its lease elapses - the defect #82 exists to close"
    )
    assert requeued.file_id == file_id
    assert requeued.attempts == 2, "the retry trail is preserved, not given back (ADR 0028 §1)"


def test_an_interrupt_inside_bury_self_heals_on_the_next_attempt(
    db, db_other, tmp_path, monkeypatch
):
    """Case (g) driven to its END: the half-buried row converges, it does not stay half-buried.

    The sweep above proves the requeue. What it cannot prove is the thing that makes `_bury`'s
    files-then-jobs ordering the right choice - that a LATER attempt overwrites both axes. The
    row this leaves behind is genuinely inconsistent for a moment (`files.status = 'failed'`
    under a `queued` job), and "self-healing" is a claim about the next claim, not about that
    moment.

    So: interrupt inside `_bury`, then run the tick that follows. The `processing` write flips
    `failed -> processing` - legal, guarded only on `ready`, and load-bearing rather than
    accidental (`process_one`'s comment says so) - the corrupt file fails terminally again, and
    both axes end where they belong.

    THIS TEST DOES NOT PIN THAT GUARD'S WIDTH, and an earlier version of this docstring claimed
    it did ("tighten that guard to exclude `failed` and this test is what reddens" - measured
    false). Both of its attempts are corrupt, so the row converges on `('failed',
    'unparseable')` whether or not the middle transition happened at all: a guard excluding
    `failed` simply leaves the row where `_bury` already put it, and every assertion still
    holds. What this test pins is the JOBS axis recovering - `queued -> failed` on the second
    attempt - which is the half `_bury`'s ordering exists for.
    The guard's width IS pinned, by the four #24 tests that start from `_half_bury` and read the
    row mid-flight: `test_the_claim_clears_the_previous_attempts_failed_reason`,
    `test_a_retry_that_succeeds_leaves_no_failed_reason_on_the_ready_file`,
    `test_a_terminal_failure_after_a_cleared_reason_writes_the_new_reason_not_the_old` and
    `test_a_transient_failure_after_a_half_bury_leaves_processing_with_no_reason`. They redden
    on that mutation because they distinguish the intermediate state, which this one cannot.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    real_fail = worker.fail
    interrupt_armed = {"on": True}

    def fail_or_interrupt(*args, **kwargs):
        if interrupt_armed["on"]:
            raise KeyboardInterrupt("Ctrl-C inside `_bury`")
        return real_fail(*args, **kwargs)

    monkeypatch.setattr(worker, "fail", fail_or_interrupt)
    source = write_corrupt_pdf(tmp_path / "half-buried.pdf")
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    with pytest.raises(KeyboardInterrupt):
        tick(conn, FakeEmbeddings())

    # The genuinely inconsistent moment, asserted rather than assumed: a `failed` file under a
    # job that is claimable again.
    assert db_other.execute(
        """
        select f.status, j.state from files f join jobs j using (file_id)
        where f.file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone() == ("failed", "queued")

    interrupt_armed["on"] = False
    assert tick(conn, FakeEmbeddings()) is True

    status, reason, state, attempts = db_other.execute(
        """
        select f.status, f.failed_reason, j.state, j.attempts
        from files f join jobs j using (file_id) where f.file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert (status, reason, state) == ("failed", "unparseable", "failed"), (
        "the attempt after a half-bury must settle BOTH axes - that recovery is the whole "
        "reason `_bury` writes `files` before `jobs`"
    )
    assert attempts == 2, "the second bury spent one more attempt, exactly as a re-claim does"


def test_an_interrupt_while_the_worker_holds_no_job_touches_nothing(
    db, db_other, tmp_path, monkeypatch
):
    """The idle worker (case 2): `claim` returned None, so there is nothing to hand back.

    `process_one` returns False BEFORE the guard opens, which is correct and load-bearing: the
    guard needs a `Job` to release, and on an empty tick there is none. What has to be true is
    that the shutdown path does not misfire on the last job this worker happened to see, or on
    somebody else's in-flight row.

    So the scenario has a row that a misfiring handler could plausibly damage: another worker
    holds a live lease on it, which is exactly the row a "release whatever we last had" bug would
    hand back. `release` and `_release_on_shutdown` are both spied, because "nothing was written"
    and "the write was never attempted" are different claims and only the second rules out a
    handler that fired and was refused by the lease guard for unrelated reasons.

    The interrupt lands in `run`'s poll sleep - the real idle window, and the only statement an
    idle worker spends any time in.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    source = write_pdf(tmp_path / "someone-elses.pdf", ["a page another worker is holding"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    holder = claim(conn, lease_seconds=3600)
    assert holder is not None, "the queue must be empty for OUR worker but not for the database"

    shutdown_calls: list[object] = []
    release_calls: list[object] = []
    monkeypatch.setattr(worker, "_release_on_shutdown", lambda *a, **k: shutdown_calls.append(a))
    monkeypatch.setattr(worker, "release", lambda *a, **k: release_calls.append(k))

    def interrupt_the_nap(_delay):
        raise KeyboardInterrupt("Ctrl-C on an idle worker")

    monkeypatch.setattr(worker.time, "sleep", interrupt_the_nap)

    with pytest.raises(KeyboardInterrupt) as excinfo:
        worker.run(
            conn,
            embedder=FakeEmbeddings(),
            chunk_size=CHUNK_SIZE_WORDS,
            chunk_overlap=CHUNK_OVERLAP_WORDS,
        )

    assert str(excinfo.value) == "Ctrl-C on an idle worker", (
        "the interrupt must arrive at the operator unchanged - not wrapped, and not replaced by "
        "whatever a misfiring shutdown handler raised on its way out"
    )
    assert excinfo.value.__context__ is None, "nothing else raised while the interrupt unwound"
    assert shutdown_calls == [], "the shutdown handler fired with no job in hand"
    assert release_calls == [], "an idle worker tried to hand back a job it did not hold"

    state, attempts, leased_until, token = db_other.execute(
        """
        select state, attempts, leased_until, lease_token::text
        from jobs where file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert (state, attempts, leased_until is None, token) == (
        "processing",
        1,
        False,
        holder.lease_token,
    ), "an idle worker's shutdown disturbed a row it never claimed"


def _one_shot(exc, *, fallback=None):
    """A stub that raises `exc` on its FIRST call and delegates afterwards.

    One-shot rather than permanent because the `db` fixture's teardown uses the same connection:
    a `rollback` stubbed to raise forever would poison the teardown and bury the test's real
    result under an unrelated error. Which failure the shutdown path saw is the assertion; how
    long the stub survives is not.
    """
    fired = {"yet": False}

    def stub(*args, **kwargs):
        if not fired["yet"]:
            fired["yet"] = True
            raise exc
        return fallback(*args, **kwargs) if fallback is not None else None

    return stub


@pytest.mark.parametrize(
    "where,error",
    [
        pytest.param("release", RuntimeError("something nobody predicted"), id="release-raises"),
        pytest.param(
            "release",
            psycopg.OperationalError("server closed the connection unexpectedly"),
            id="release-raises-psycopg",
        ),
        pytest.param(
            "rollback",
            psycopg.OperationalError("the connection is closed"),
            id="the-rollback-before-it-raises",
        ),
    ],
)
def test_a_failing_shutdown_release_is_logged_and_never_masks_the_interrupt(
    db, db_other, tmp_path, monkeypatch, caplog, where, error
):
    """Case 3: the handler's own write fails - and a Ctrl-C still exits as a Ctrl-C.

    The database being unreachable is not an exotic input here; it is one of the very things
    that gets a worker stopped, so "the release cannot run" is a likely shutdown, not a rare one.
    Three failure sites, because they are three different lines of the handler:
      - `release` raising something GENERIC, which the `except Exception` has to catch on shape
        rather than on type;
      - `release` raising a psycopg error - the realistic cause;
      - `conn.rollback()` raising, which is the FIRST statement in the handler and the reason
        that call is inside the `try` rather than above it (its docstring says so; without this
        case that placement is unpinned and moving the rollback out goes green).

    What must be true in all three:
      - the ORIGINAL `KeyboardInterrupt` is what propagates. A psycopg traceback in its place
        tells the operator the wrong story about why their worker stopped.
      - `__context__` is clean. The failure is caught and logged INSIDE the handler, so it never
        becomes the interrupt's context - a Ctrl-C must not print "during handling of the above
        exception" with a database error above it. That is the assertion that makes "never
        surfaces as a psycopg traceback" checkable rather than aspirational.
      - the failure is LOGGED, at WARNING, WITH the traceback (`exc_info`) and with what the
        operator now has to expect - the row keeps its lease until the reaper collects it.
        Swallowing it silently would make a stranded job look like a handled one.
      - the row is UNTOUCHED: still `processing`, lease still live. That fallback is precisely
        the pre-#82 behaviour, which is what makes this handler an optimisation of an already
        safe path rather than a correctness dependency.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker, "ingest_file", _interrupt)
    if where == "release":
        monkeypatch.setattr(worker, "release", _one_shot(error))
    else:
        monkeypatch.setattr(conn, "rollback", _one_shot(error, fallback=conn.rollback))
    source = write_pdf(tmp_path / "release-failed.pdf", ["a page nobody could hand back"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    with caplog.at_level("WARNING", logger="gct.jobs.worker"):
        with pytest.raises(KeyboardInterrupt) as excinfo:
            tick(conn, FakeEmbeddings())

    assert excinfo.value.__context__ is None and excinfo.value.__cause__ is None, (
        "the failed release became the interrupt's context - the operator's Ctrl-C now prints a "
        "database traceback underneath it and reads as a DB fault"
    )

    reported = [r for r in caplog.records if "could not requeue on shutdown" in r.message]
    assert len(reported) == 1, "a release that could not run must be reported, not swallowed"
    assert reported[0].levelname == "WARNING"
    assert reported[0].exc_info is not None, (
        "the traceback is the only record of WHICH failure stopped the requeue"
    )
    assert reported[0].exc_info[1] is error, "the log reported a different failure than the one hit"
    assert "until its lease expires" in reported[0].getMessage(), (
        "the consequence - the job now waits out its lease - has to be said, not inferred "
        "from silence"
    )

    state, leased_until, last_error = db_other.execute(
        "select state, leased_until, last_error from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (state, leased_until is None, last_error) == ("processing", False, None), (
        "a failed release must write nothing at all - a half-written row would be worse than "
        "the lease wait it falls back to"
    )


def test_a_second_interrupt_during_the_handler_wins_and_carries_the_first(
    db, db_other, tmp_path, monkeypatch
):
    """Case 4: the operator hits Ctrl-C again while the handler is still writing. Pinned as-is.

    THE VERDICT, measured: the SECOND interrupt propagates, the first survives on its
    `__context__`, and the release never happens - so the row keeps its live lease and the
    reaper is the backstop, exactly as it was before #82.

    That is a deliberate consequence of one word: `_release_on_shutdown` catches `Exception`,
    not `BaseException`. A second `KeyboardInterrupt` is not "the release failed" - it is the
    operator insisting, and a handler that swallowed it to finish its own write would be
    ignoring the more recent instruction to stop. The cost is real and bounded: the job waits
    out its lease. The alternative - catching `BaseException` here - would make a doubled Ctrl-C
    unable to stop a worker whose database is hanging, which is worse.

    Both halves are asserted because either alone is satisfiable by a bug: that the SECOND is
    what comes out (a handler swallowing it would surface the first), and that the FIRST is not
    lost (`__context__`, which is what a traceback prints and what `run`'s caller can inspect).
    """
    conn, owner_id, class_id = db
    conn.autocommit = True

    def first_ctrl_c(*_args, **_kwargs):
        raise KeyboardInterrupt("first Ctrl-C, mid-ingest")

    def second_ctrl_c(*_args, **_kwargs):
        raise KeyboardInterrupt("second Ctrl-C, during the requeue")

    monkeypatch.setattr(worker, "ingest_file", first_ctrl_c)
    monkeypatch.setattr(worker, "release", second_ctrl_c)
    source = write_pdf(tmp_path / "twice-stopped.pdf", ["a page interrupted twice"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    with pytest.raises(KeyboardInterrupt) as excinfo:
        tick(conn, FakeEmbeddings())

    assert str(excinfo.value) == "second Ctrl-C, during the requeue", (
        "the second interrupt was swallowed - the handler kept working after the operator told "
        "it to stop again, which is what `except Exception` (not `BaseException`) refuses to do"
    )
    assert isinstance(excinfo.value.__context__, KeyboardInterrupt)
    assert str(excinfo.value.__context__) == "first Ctrl-C, mid-ingest", (
        "the interrupt that started the shutdown must not be lost - it is the one that explains "
        "why the worker was unwinding at all"
    )

    state, leased_until, last_error = db_other.execute(
        "select state, leased_until, last_error from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (state, leased_until is None, last_error) == ("processing", False, None), (
        "the accepted cost of insisting: the requeue did not finish, so the job waits out its "
        "lease and the reaper collects it - the pre-#82 fallback, not a half-written row"
    )


def test_a_second_interrupt_after_the_requeue_committed_still_leaves_it_committed(
    db, db_other, tmp_path, monkeypatch
):
    """Case 4, the other half: the second Ctrl-C lands AFTER `release` returned True.

    The window is the handler's own `logger.warning` - a real statement (a handler formats,
    locks, and writes to a stream) and the last thing standing between a committed requeue and
    the re-raise. The distinction is worth pinning because the previous test's outcome could be
    misread as "a doubled Ctrl-C costs you the requeue": it costs you the requeue only if it
    beats it. `release` commits before returning (queue.py's contract), so once it has, no
    later interrupt can take it back.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    real_release = worker.release

    def release_then_second_ctrl_c(*args, **kwargs):
        real_release(*args, **kwargs)
        raise KeyboardInterrupt("second Ctrl-C, after the requeue was already committed")

    monkeypatch.setattr(worker, "ingest_file", _interrupt)
    monkeypatch.setattr(worker, "release", release_then_second_ctrl_c)
    source = write_pdf(tmp_path / "twice-stopped-late.pdf", ["a page requeued just in time"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    with pytest.raises(KeyboardInterrupt) as excinfo:
        tick(conn, FakeEmbeddings())

    assert str(excinfo.value).startswith("second Ctrl-C"), (
        "the interrupt that arrived last is still the one that propagates"
    )
    assert isinstance(excinfo.value.__context__, KeyboardInterrupt)

    state, attempts, leased_until = db_other.execute(
        "select state, attempts, leased_until from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (state, attempts, leased_until) == ("queued", 1, None), (
        "`release` commits before returning, so a second interrupt after it cannot un-requeue "
        "the job - only one that beats it can"
    )


@pytest.fixture
def restore_signal_dispositions():
    """Hand SIGINT and SIGTERM back to the pytest process after a test that raises them for real.

    Real delivery is the point of the test that uses this - `signal.raise_signal` goes through
    the interpreter's actual dispositions, so nothing about the handler-to-exception step is
    simulated - and the price is that the test genuinely mutates the process.
    """
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _sigterm_as_keyboard_interrupt(signum, _frame):
    """The disposition `scripts/worker.py` installs for SIGTERM, mirrored for a library test.

    Mirrored rather than imported: `scripts/` is deliberately not a package (ADR 0009), and the
    fact that the SCRIPT installs exactly this is owned where it lives - `test_worker_script.py`
    asserts both that `_interrupt` raises `KeyboardInterrupt` carrying the signal number and
    that `main` registers it against the interpreter. What this file owns is the half that needs
    a database: given the signal really does arrive as that interrupt, the worker's durable
    outcome is the same for both signals.
    """
    raise KeyboardInterrupt(f"signal {signum}")


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM], ids=["sigint", "sigterm"])
def test_both_signals_leave_the_job_in_the_same_state(
    db, db_other, tmp_path, monkeypatch, restore_signal_dispositions, signum
):
    """SIGINT and SIGTERM are one shutdown path, not two - proven on the row, not on the type.

    The whole design of the SIGTERM wiring is that it produces the unwind Ctrl-C already
    produces, so that there is exactly one shutdown path to reason about and to test. That claim
    is only worth as much as its observable consequence, so this asserts the DURABLE outcome for
    both signals and requires it to be identical, field for field.

    Delivered with `signal.raise_signal` rather than by raising an exception directly, which is
    the part no other test in this file covers: a real signal is handled between bytecodes in
    the main thread, so it lands at an arbitrary statement inside `ingest_file` rather than at a
    point the test chose. If either disposition failed to convert the signal into an exception,
    the stub's own `AssertionError` - not a passing test - is what comes out.

    One `db` fixture per signal (parametrized, not looped) is load-bearing: the released job
    from the first signal would still be the OLDEST queued row, so a loop's second `claim`
    would pick it up again and quietly assert the same job twice. Measured while writing this.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    signal.signal(signal.SIGTERM, _sigterm_as_keyboard_interrupt)

    def deliver_the_signal(*_args, **_kwargs):
        signal.raise_signal(signum)
        # CPython runs the handler between bytecodes, not inside `raise_signal` itself, so the
        # exception appears a beat later - here, rather than at some unrelated statement.
        for _ in range(100):
            pass
        raise AssertionError(f"signal {signum} never became an exception - nothing was tested")

    monkeypatch.setattr(worker, "ingest_file", deliver_the_signal)
    source = write_pdf(tmp_path / "signalled.pdf", ["a page stopped by a real signal"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    with pytest.raises(KeyboardInterrupt):
        tick(conn, FakeEmbeddings())

    outcome = db_other.execute(
        """
        select j.state, j.attempts, j.leased_until, f.status
        from files f join jobs j using (file_id) where f.file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert outcome == ("queued", 1, None, "processing"), (
        f"signal {signum} left the job somewhere the other signal does not - the two are "
        "supposed to be one shutdown path, and a second path is a second thing to get wrong"
    )


def test_a_zombie_worker_unwinding_after_a_reap_writes_nothing_and_blames_nobody(
    db, db_other, tmp_path, monkeypatch, caplog
):
    """Case 6: the lease expired, the reaper requeued, another worker re-claimed - THEN Ctrl-C.

    The zombie sequence at the `process_one` level, which is where it has consequences the queue
    cannot see. `reclaim_expired`'s docstring warns about exactly this caller: reclaiming is not
    killing, so a stalled-but-alive worker keeps running and reaches its settle verb long after
    its job was handed to someone else. #82 adds a NEW settle call on that path - the shutdown
    release - and it is the most dangerous verb to get wrong, because it puts a job back into
    circulation rather than ending it. A shutdown release that landed here would drag a job
    another worker is actively ingesting back to `queued` and pay the embedding bill twice.

    It does not, and the guard that stops it is `_settle`'s `state='processing' AND
    lease_token = ours`. That refusal is already driven on a real reap-and-re-claim by
    `test_release_refuses_a_job_another_worker_now_holds` (tests/gct/jobs/test_queue.py:845) -
    cited, not duplicated. What is unproven until here is what `process_one` DOES with the
    `False`: report it, write nothing, and let the interrupt out.

    THE LOG IS HALF THE TEST. The message may not say "another worker owns it now" - that is the
    diagnosis the OTHER settle paths make, and it is sound for them because their `False` can
    only mean a lost lease. The shutdown guard spans a wider window, so its `False` has a second
    cause: this worker settling the job itself a microsecond earlier. V1 runs ONE worker
    (ADR 0011, and ADR 0028 §5's safety argument rests on it), so a WARNING asserting a
    concurrency event is a false line in a log that ADR 0028 §Consequences reads as evidence.
    The shipped message names both causes and diagnoses neither, which is the only honest thing
    it can say.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    source = write_pdf(tmp_path / "zombie-shutdown.pdf", ["a page two workers both wanted"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    holder: dict[str, object] = {}
    real_release = worker.release
    release_answers: list[bool] = []

    def spy_release(*args, **kwargs):
        answer = real_release(*args, **kwargs)
        release_answers.append(answer)
        return answer

    def stall_until_the_job_is_someone_elses(*_args, **kwargs):
        # The zombie is mid-ingest. Its lease elapses (forced, not waited out: a legitimate
        # ingest finishes ~62x inside the real lease, so waiting is not an option a test has),
        # the reaper on some later tick requeues the row, and the next claim - worker B - takes
        # it with a fresh token. Only THEN does the zombie notice it was told to stop.
        worker_conn = kwargs["conn"]
        worker_conn.execute(
            "update jobs set leased_until = now() - interval '1 hour' where file_id = %s::uuid",
            (file_id,),
        )
        assert reclaim_expired(worker_conn) == 1
        holder["job"] = claim(worker_conn, lease_seconds=3600)
        raise KeyboardInterrupt("Ctrl-C, long after this worker stopped owning the job")

    monkeypatch.setattr(worker, "ingest_file", stall_until_the_job_is_someone_elses)
    monkeypatch.setattr(worker, "release", spy_release)

    with caplog.at_level("WARNING", logger="gct.jobs.worker"):
        with pytest.raises(KeyboardInterrupt):
            tick(conn, FakeEmbeddings())

    assert release_answers == [False], (
        "the shutdown release must be REFUSED for a job this worker no longer holds - a True "
        "here means it dragged another worker's in-flight job back into the claimable pool"
    )

    state, attempts, leased_until, token, last_error = db_other.execute(
        """
        select state, attempts, leased_until, lease_token::text, last_error
        from jobs where file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert (state, attempts, leased_until is None) == ("processing", 2, False)
    assert token == holder["job"].lease_token, "the new holder's proof of holding was overwritten"
    assert last_error is None, (
        "a refused release still wrote its shutdown message over the new holder's row"
    )

    lines = [r.getMessage() for r in caplog.records]
    assert any("no longer holds it" in line for line in lines), (
        "a release the guard refused must be reported, not swallowed"
    )
    assert not any("another worker owns it" in line for line in lines), (
        "the shutdown release's `False` has two causes and this message diagnoses one of them - "
        "and the one it picks is a concurrency event V1 cannot have (ADR 0011)"
    )


def test_a_shutdown_costs_exactly_one_attempt_however_the_job_comes_back(
    db, db_other, tmp_path, monkeypatch
):
    """Case 7: shutdown -> reclaim -> shutdown spends three attempts, one per claim. Never two.

    `attempts` is the durable retry budget, and its whole design is that ONE claim costs ONE
    attempt no matter how that claim ends (ADR 0028 §1) - a caught 429, a crash with no handler,
    or an operator stopping the worker. A shutdown that decremented it (an "it never really ran"
    refund) would hand a file that kills the worker on every start an unbounded budget; one that
    double-counted would bury a healthy file early.

    Three ticks, and the middle one is the point: it takes the OTHER route back into the queue.
    Its shutdown release fails, so the row stays `processing` under a live lease and the REAPER
    is what requeues it - the path that runs when no handler gets to run at all (SIGKILL, OOM,
    power loss). Both routes have to cost the same, or the budget means something different
    depending on how the worker died.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    real_release = worker.release
    release_fails = {"on": False}

    def release_or_die(*args, **kwargs):
        if release_fails["on"]:
            raise psycopg.OperationalError("the database went away with the worker")
        return real_release(*args, **kwargs)

    monkeypatch.setattr(worker, "ingest_file", _interrupt)
    monkeypatch.setattr(worker, "release", release_or_die)
    source = write_pdf(tmp_path / "attempts.pdf", ["a page stopped over and over"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    def attempts_now():
        return db_other.execute(
            "select attempts from jobs where file_id = %s::uuid", (file_id,)
        ).fetchone()[0]

    # 1. A clean shutdown: the handler hands the job back itself.
    with pytest.raises(KeyboardInterrupt):
        tick(conn, FakeEmbeddings())
    assert attempts_now() == 1

    # 2. A shutdown whose release could not run - the row keeps its lease, and the reaper is
    #    what returns it. The attempt is still spent, and spent exactly once.
    release_fails["on"] = True
    with pytest.raises(KeyboardInterrupt):
        tick(conn, FakeEmbeddings())
    assert attempts_now() == 2, "the failed requeue must not change what the claim already cost"
    _backdate_lease(conn, file_id)
    assert reclaim_expired(conn) == 1
    assert attempts_now() == 2, (
        "the reaper deliberately does not reset `attempts` - a reclaim that granted a fresh "
        "budget would let a poison file be retried forever"
    )

    # 3. A third claim, stopped the same way as the first. Three claims, three attempts.
    release_fails["on"] = False
    with pytest.raises(KeyboardInterrupt):
        tick(conn, FakeEmbeddings())
    state, attempts = db_other.execute(
        "select state, attempts from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (state, attempts) == ("queued", 3), (
        "three claims must cost three attempts - a shutdown is not a special case, which is "
        "exactly why the budget still bounds a worker that is being restarted in a loop"
    )


def test_a_shutdown_never_demotes_a_file_that_is_already_ready(db, db_other, tmp_path, monkeypatch):
    """Case 8: `files.status` is untouched by the shutdown release - including from `ready`.

    Two facts share one test because they are one rule. `release` writes `jobs` and nothing
    else, so whatever the file said before a shutdown it still says after: `processing` stays
    `processing` (mid-flight, still true, and the student learns nothing from a flicker), and
    `ready` stays `ready`.

    `ready` is the half that costs something to get wrong, and it is reachable: the file here
    was genuinely published by an earlier run whose `complete` never landed, the reaper requeued
    the job, and this claim is the at-least-once redelivery. The re-claim's `processing` write
    is refused by its `status <> 'ready'` guard, and then the interrupt arrives. If either the
    guard or the release touched `files`, a student watching a finished file would see it fall
    back to `processing` because somebody restarted a worker.

    The chunk count is asserted alongside, because a status column is a promise and the chunks
    are whether it was kept.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    source = write_pdf(tmp_path / "already-ready.pdf", ["a page an earlier run published"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    # The run that WON: it claimed, ingested and published `ready` - and then stalled without
    # settling, so the job never went `done`.
    winner = claim(conn, lease_seconds=3600)
    assert winner is not None
    ingest_file(
        winner.staging_ref,
        winner.owner_id,
        winner.class_id,
        file_id=winner.file_id,
        embedder=FakeEmbeddings(),
        conn=conn,
        chunk_size=CHUNK_SIZE_WORDS,
        chunk_overlap=CHUNK_OVERLAP_WORDS,
    )
    published = db_other.execute(
        """
        select f.status, (select count(*) from chunks c where c.file_id = f.file_id)
        from files f where f.file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert published[0] == "ready" and published[1] > 0, "the scenario needs a really-ready file"

    _backdate_lease(conn, file_id)
    assert reclaim_expired(conn) == 1

    # The redelivery, stopped mid-ingest.
    monkeypatch.setattr(worker, "ingest_file", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        tick(conn, FakeEmbeddings())

    status, failed_reason, chunk_count, state, leased_until, attempts = db_other.execute(
        """
        select f.status, f.failed_reason,
               (select count(*) from chunks c where c.file_id = f.file_id),
               j.state, j.leased_until, j.attempts
        from files f join jobs j using (file_id) where f.file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert (status, failed_reason) == ("ready", None), (
        "a stopped worker un-published a file the student could already query - the one "
        "direction `files.status` is not allowed to move"
    )
    assert chunk_count == published[1], "the shutdown path must not touch the published corpus"
    assert (state, leased_until, attempts) == ("queued", None, 2), (
        "the job half still has to be handed back - `files` being untouched is not an excuse "
        "for stranding the row"
    )


# --- The claim clears the previous attempt's `failed_reason` (issue #24) -----------------------
#
# `files.failed_reason` is the actionable message F3 surfaces to the student (ADR 0020 §1), and
# `_bury` is its only WRITER: it sets the column and nothing ever unset it. That asymmetry is the
# defect. `_bury` commits `files` before `jobs`, so the `jobs` half can fail to settle - a crash
# between the two writes, or `fail` returning False because the lease expired and the reaper
# already requeued (logged and continued, no crash at all). The job stays claimable, and the next
# claim's `processing` write flips `failed -> processing` over a row still wearing the last
# attempt's reason.
#
# These tests exercise BOTH shapes that reaches, because they cost the student different things:
#   - a file mid-flight advertising a failure it is no longer failing (A1, A5) - the wide case,
#     needing no input change at all, and lasting the whole backoff;
#   - a PUBLISHED, queryable file advertising one (A2) - which additionally needs the input to
#     have changed between attempts, and does: `staging_ref` is a plain absolute path
#     (`enqueue`'s docstring), bytes can arrive late or be replaced, and a raised ceiling
#     re-admits a file that was `too_long`.
#
# Every assertion goes through `db_other`, and here that is not ceremony: the whole subject is
# whether the clear COMMITTED at the claim, independent of whatever the attempt goes on to do.


def _half_bury(conn, monkeypatch, *, source, owner_id, class_id) -> str:
    """Drive `source` to the genuinely half-buried row and DISARM: `files` `failed`, job claimable.

    The recipe `test_an_interrupt_inside_bury_self_heals_on_the_next_attempt` established -
    interrupt `_bury` between its `files` write (already committed, its own transaction) and its
    `jobs` write. Only the arming is shared; each caller drives its own second attempt, because
    what the SECOND attempt does is the axis these tests vary.

    `worker.fail` is restored to the real one before returning, so the caller's tick settles
    normally. Returns the `file_id`.
    """
    real_fail = worker.fail
    armed = {"on": True}

    def fail_or_interrupt(*args, **kwargs):
        if armed["on"]:
            raise KeyboardInterrupt(
                "Ctrl-C inside `_bury`, between its files write and its jobs write"
            )
        return real_fail(*args, **kwargs)

    monkeypatch.setattr(worker, "fail", fail_or_interrupt)
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    with pytest.raises(KeyboardInterrupt):
        tick(conn, FakeEmbeddings())
    armed["on"] = False
    return file_id


def _file_row(db_other, file_id: str) -> tuple:
    return db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()


def test_the_claim_clears_the_previous_attempts_failed_reason(db, db_other, tmp_path, monkeypatch):
    """The clear COMMITS at the claim, on its own - not as a side effect of a later success.

    Half-bury, then make the second attempt fail transiently rather than succeed. If the reason
    were being cleared anywhere downstream - in `index_file`'s upsert, say - this row would still
    read `unparseable`, because nothing downstream ran: the ingest raised before the index
    transaction ever opened (ADR 0020 §3). `('processing', None)` therefore isolates the
    `processing` write as the thing that did it.

    The half-buried row is asserted first rather than assumed: it is the precondition the whole
    file is about, and a recipe that silently stopped producing it would make every test below
    green for the wrong reason.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_corrupt_pdf(tmp_path / "half-buried.pdf")
    file_id = _half_bury(conn, monkeypatch, source=source, owner_id=owner_id, class_id=class_id)

    assert _file_row(db_other, file_id) == ("failed", "unparseable"), (
        "the precondition never formed - this test is not measuring what it claims to"
    )
    assert db_other.execute(
        "select state from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone() == ("queued",), "a half-buried job must still be claimable, or nothing retries it"

    def transient(*_args, **_kwargs):
        raise TransientEmbeddingError("simulated provider 429 - this attempt settles nothing")

    monkeypatch.setattr(worker, "ingest_file", transient)
    assert tick(conn, FakeEmbeddings()) is True

    assert _file_row(db_other, file_id) == ("processing", None), (
        "a file the worker is actively working on is still advertising the LAST attempt's "
        "failure - and for the whole backoff, not an instant"
    )


def test_a_retry_that_succeeds_leaves_no_failed_reason_on_the_ready_file(
    db, db_other, tmp_path, monkeypatch
):
    """The acceptance criterion of #24: `('ready', <a reason>)` must be unreachable.

    Half-bury a corrupt file, then replace the bytes AT THE SAME `staging_ref` with a parseable
    PDF and let the same job's next attempt run. That input change is the ordinary case, not a
    contrivance: `enqueue` records that `path` need not exist and routes a missing or unreadable
    file to terminal `unparseable`, and `staging_ref` is a plain absolute path that Slice 3's
    stager writes asynchronously. Bytes arriving late, bytes being replaced, and a raised
    `MAX_INGEST_WORDS` all reach exactly this sequence.

    Asserts the chunk set as well as the columns, because `('ready', None)` on a file with no
    chunks would be a different bug wearing this test's green.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_corrupt_pdf(tmp_path / "arrived-late.pdf")
    file_id = _half_bury(conn, monkeypatch, source=source, owner_id=owner_id, class_id=class_id)
    assert _file_row(db_other, file_id) == ("failed", "unparseable")

    write_pdf(source, ["the real lecture text finally arrived at the staging path"])
    assert tick(conn, FakeEmbeddings()) is True

    status, reason, state, chunk_count = db_other.execute(
        """
        select f.status, f.failed_reason, j.state,
               (select count(*) from chunks c where c.file_id = f.file_id)
        from files f join jobs j using (file_id) where f.file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert (status, reason) == ("ready", None), (
        "a published, queryable file is telling the student it failed to parse - the exact "
        "contradiction #24 exists to remove"
    )
    assert state == "done", "the successful retry must settle the job, not just the file"
    assert chunk_count > 0, "`ready` means the full chunk set is committed and queryable"


def test_the_claim_does_not_clear_a_reason_on_a_file_that_is_already_ready(
    db, db_other, tmp_path, monkeypatch
):
    """The clear is GOVERNED by `status <> 'ready'`, because it rides that one UPDATE.

    THE SUBJECT HERE IS THE SQL'S SHAPE, AND THE ROW IS SET DIRECTLY RATHER THAN EARNED.
    `('ready', 'unparseable')` is what #24 removes from the SEQUENTIAL path - one attempt after
    another, which is every other test in this file - and #86's lease guard on `_bury` removed
    the out-of-order path that was still producing it, so NO sequence of ticks earns this row
    now. That is what makes setting it directly the only way to reach the statement under test,
    rather than a shortcut past a sequence that exists (the section at the bottom of this file
    drives the out-of-order interleaving itself).
    What the test pins is that the clear cannot be lifted out into a second, unguarded
    `update files set failed_reason = null`: that version wipes the reason off a `ready` row,
    which is a row a zombie whose lease expired is entitled to be about to bury, and it widens
    the window between the two writes for no gain. Split the statement and this goes red; leave
    it riding the guarded UPDATE and the row is untouched.

    The rest of the tick is the ordinary terminal path: the file is corrupt, so `_bury` runs and
    is itself stopped by its own identical `status <> 'ready'` guard. Both guards being present
    is what the last assertion reads.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_corrupt_pdf(tmp_path / "already-published.pdf")
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    conn.execute(
        "update files set status = 'ready', failed_reason = 'unparseable' where file_id = %s::uuid",
        (file_id,),
    )

    assert tick(conn, FakeEmbeddings()) is True

    assert _file_row(db_other, file_id) == ("ready", "unparseable"), (
        "the clear reached a `ready` row - it is not riding the guarded UPDATE any more"
    )
    (state,) = db_other.execute(
        "select state from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert state == "failed", "the jobs axis still records what this attempt did"


def test_a_terminal_failure_after_a_cleared_reason_writes_the_new_reason_not_the_old(
    db, db_other, tmp_path, monkeypatch
):
    """The reason a student reads names THIS attempt, even when both attempts failed.

    Half-bury with `unparseable`, then make the second attempt fail terminally for a DIFFERENT
    cause. Without the clear the two are indistinguishable in the row - the old value would still
    be there and `_bury` would simply overwrite it with the new one, so this test would pass on
    the broken code too. It does not: the interesting half is the ORDER (clear at the claim, new
    reason at the bury), and the assertion that isolates it is the intermediate one - after the
    clear and before the bury, the row must already be clean.

    `ingest_file` is stubbed rather than fed a second corrupt fixture because the point is the
    reason CHANGING; `empty` is a real member of the terminal taxonomy `parse.py` raises.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_corrupt_pdf(tmp_path / "fails-twice.pdf")
    file_id = _half_bury(conn, monkeypatch, source=source, owner_id=owner_id, class_id=class_id)
    assert _file_row(db_other, file_id) == ("failed", "unparseable")

    seen: list[tuple] = []

    def empty_after_looking(*_args, **_kwargs):
        # Read the row from the SECOND connection at the one moment that distinguishes the two
        # implementations: the claim has committed, the bury has not run.
        seen.append(_file_row(db_other, file_id))
        raise ParseError("empty", "the file parsed but contained no extractable text")

    monkeypatch.setattr(worker, "ingest_file", empty_after_looking)
    assert tick(conn, FakeEmbeddings()) is True

    assert seen == [("processing", None)], (
        "between the claim and the bury the row still carried the PREVIOUS attempt's reason"
    )
    assert _file_row(db_other, file_id) == ("failed", "empty"), (
        "the student must be told what went wrong THIS time"
    )


def test_a_transient_failure_after_a_half_bury_leaves_processing_with_no_reason(
    db, db_other, tmp_path, monkeypatch
):
    """Sequence A end to end - the WIDE shape, which needs no input change and no success.

    A half-buried file whose next attempt hits a 429 sits at `('processing', <old reason>)` for
    the whole backoff on the broken code, and the student is looking at a spinner captioned with
    a failure. Nothing exotic reaches it: a lost lease is the routine at-least-once outcome, and
    `_bury` logs-and-continues when `fail` returns False rather than crashing.

    Distinct from A1 in what raises: this one goes through the REAL pipeline and a real
    `TransientEmbeddingError` from the embedder, so the classification, the served backoff and
    the release are all the shipped ones rather than a stub of them.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    slept: list[float] = []
    monkeypatch.setattr(worker.time, "sleep", slept.append)
    source = write_corrupt_pdf(tmp_path / "then-a-429.pdf")
    file_id = _half_bury(conn, monkeypatch, source=source, owner_id=owner_id, class_id=class_id)
    assert _file_row(db_other, file_id) == ("failed", "unparseable")

    write_pdf(source, ["a page the provider refuses exactly once"])
    embedder = FakeEmbeddings(transient_failures=1)
    assert tick(conn, embedder) is True

    assert _file_row(db_other, file_id) == ("processing", None), (
        "the file is mid-flight, so `processing` is right and a reason is a lie - the student "
        "sees the last attempt's failure captioning a run that is still going"
    )
    state, attempts = db_other.execute(
        "select state, attempts from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert (state, attempts) == ("queued", 2), "a transient failure requeues, it does not bury"
    assert len(embedder.calls) == 1, "the attempt really did reach the provider and get refused"
    assert slept == [backoff_seconds(2)], "the transient path served its backoff as usual"


def test_the_diagnostic_trail_survives_the_clear(db, db_other, tmp_path, monkeypatch):
    """Clearing the STUDENT-facing message destroys no OPERATIONAL history - different axes.

    `files.failed_reason` is a closed set shown to a student and must describe the current
    attempt. `jobs.last_error` is free text kept for an operator and is a trail: it records what
    the previous attempt did, and a published file is not a reason to forget it. The two are
    deliberately different columns on different tables (`fail`'s docstring), and this test is
    what goes red if a future edit widens the clear into "reset the failure state".

    A2's sequence, asserted from the other side. `files.failed_reason` is deliberately NOT
    re-asserted here: A2 owns that claim, and repeating it would make this test go red for A2's
    reason as well as its own, which is exactly the ambiguity a second assertion buys. What is
    asserted is that the file did reach `ready` (so the trail survived a SUCCESS, not a failure)
    and that `last_error` is byte-for-byte what attempt 1 left. `complete` does not touch
    `last_error` - only `fail` and `release` write it - so the trail survives the success too.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_corrupt_pdf(tmp_path / "keeps-its-trail.pdf")
    file_id = _half_bury(conn, monkeypatch, source=source, owner_id=owner_id, class_id=class_id)
    first_error = db_other.execute(
        "select last_error from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    assert first_error is not None and first_error.startswith("shutdown:")

    write_pdf(source, ["the real lecture text"])
    assert tick(conn, FakeEmbeddings()) is True

    status, state, last_error = db_other.execute(
        """
        select f.status, j.state, j.last_error
        from files f join jobs j using (file_id) where f.file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert (status, state) == ("ready", "done"), (
        "the retry has to have SUCCEEDED for this to mean anything"
    )
    assert last_error == first_error, (
        "the operator's record of attempt 1 was collateral damage of clearing the student's "
        "message - they are separate axes and only one of them describes 'now'"
    )


def test_a_budget_exhausted_bury_is_not_undone_by_a_later_claim(
    db, db_other, tmp_path, monkeypatch
):
    """The budget check runs BEFORE the `processing` write, so an exhausted job never clears.

    The end state alone cannot pin this - `_bury` writes `transient_exhausted` either way, so a
    `processing` write moved above the budget check leaves every status column identical. What
    distinguishes them is whether that statement RAN AT ALL, so the tick's SQL is recorded and
    the absence of the `processing` UPDATE is the assertion. Move the write above the check and
    this goes red while nothing else in the suite notices.

    Why it matters rather than being a tidiness point: the exhausted claim exists only to write
    the reason. Passing the row through `processing` first would flip a `failed` file back to
    mid-flight and clear the reason it is about to re-set - a visible flicker in the student's UI
    for a file that is finished, and one that lasts as long as the bury takes.

    Set up from a HALF-BURY rather than a plain doomed run so there is genuinely a reason present
    for the clear to have wiped; `max_attempts=1` makes the re-claim the exhausting one.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_corrupt_pdf(tmp_path / "out-of-budget.pdf")
    file_id = _half_bury(conn, monkeypatch, source=source, owner_id=owner_id, class_id=class_id)
    assert _file_row(db_other, file_id) == ("failed", "unparseable")

    statements: list[str] = []
    real_execute = conn.execute

    def recording_execute(query, *args, **kwargs):
        statements.append(str(query))
        return real_execute(query, *args, **kwargs)

    monkeypatch.setattr(conn, "execute", recording_execute)
    assert tick(conn, FakeEmbeddings(), max_attempts=1) is True

    assert not any("set status = 'processing'" in sql for sql in statements), (
        "the exhausted claim ran the `processing` write - the budget check is no longer first, "
        "so a finished file flickers back to mid-flight with its reason cleared"
    )
    assert _file_row(db_other, file_id) == ("failed", "transient_exhausted"), (
        "the file must end on the reason the budget check wrote"
    )
    (state,) = db_other.execute(
        "select state from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert state == "failed", "an exhausted job must not stay claimable"


def test_the_worker_publishes_a_files_row_that_agrees_with_its_chunks(
    db, db_other, tmp_path, monkeypatch
):
    """Both halves of #24 on the PRODUCTION path: `enqueue` -> `process_one` -> a consistent row.

    `test_index.py` proves the refresh at the seam, with hand-built `PreparedChunk`s. This proves
    it survives the real derivation chain, which is where a plausible-looking refresh goes wrong:
    the worker passes `job.staging_ref` (an ABSOLUTE path) as the file to ingest and
    `job.filename`/`owner_id`/`class_id` as the scope, while every chunk's `file` comes from
    `pipeline.py`'s `Path(path).name`. Derive the `files.filename` from the wrong one of those
    two and the citation label the student reads becomes the uploader's home directory - green in
    every single-column assertion, wrong in the only comparison that matters.

    So the assertion is the JOIN, not a list of expected literals: the `files` row and every one
    of its chunks must agree, whatever the values turn out to be. Plus `failed_reason is null` -
    the other half of #24, on the path a student actually travels.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_pdf(tmp_path / "week-3-lecture.pdf", ["page one text", "page two text"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    assert tick(conn, FakeEmbeddings()) is True

    status, reason, f_owner, f_class, f_name = db_other.execute(
        """
        select status, failed_reason, owner_id, class_id::text, filename
        from files where file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert (status, reason) == ("ready", None)

    scopes = db_other.execute(
        "select distinct owner_id, class_id::text, file from chunks where file_id = %s::uuid",
        (file_id,),
    ).fetchall()
    assert scopes == [(f_owner, f_class, f_name)], (
        "the published `files` row and the chunk set it describes disagree - retrieval filters "
        "chunks on (owner_id, class_id) and cites `chunks.file`, so a status read and an answer "
        "would be describing different files"
    )
    assert f_name == source.name, (
        "`files.filename` must be the basename the citation label uses, not the staging path - "
        "a full path here renders the uploader's home directory into every citation"
    )


# --- A dead lease writes NOTHING through `_bury` (issue #86) -----------------------------------
#
# SCOPED TO `_bury`, and the name says so on purpose. Issue #86's acceptance is written as "writes
# nothing to `files`", which is broader than what any test here pins: two other `files` writes are
# reachable by a worker whose lease has expired and neither reads a lease - `index_file`'s upsert
# (reachable, see `_bury`'s docstring) and `process_one`'s `processing` write, guarded only by
# `status <> 'ready'`. A test named for the broad sentence would report a coverage it does not
# have.
#
# `_bury` writes both axes, and until #86 only the `jobs` half read the lease. So the two halves
# of one function disagreed about who owned the job: `fail` refused a zombie whose lease had
# expired, and the `files` write went through anyway. `status <> 'ready'` does not cover that -
# the window it leaves open is precisely the one where the winner is `processing`, which is where
# a zombie stalled long enough to lose its lease is most likely to wake up.
#
# WHY THE FIRST TEST DRIVES THE INTERLEAVING INSTEAD OF SETTING A ROW. Every other guard in this
# file is a property of one statement, so a directly-set row reaches it honestly. This one is a
# property of an ORDER - claim, `processing`, bury, publish - and the end state it forbids,
# `('ready', <a reason>)`, is reachable from several orders that are NOT this bug (#24 closed the
# sequential one). Setting the row would pin the end state while proving nothing about the path,
# which is what let this survive: the suite passed identically with and without the guard.
# So the winner runs through the REAL `process_one` and the zombie's bury is injected at the one
# instant that matters, between the `processing` write and the `index_file` commit.
# The pull request carries a runnable script printing the same five steps, for a reader who
# would rather watch it than read one assertion.
#
# The other two pin the guard's SHAPE, which is a genuine fork rather than a detail: a condition
# that is too strict strands a file as surely as one that is too loose publishes a lie.


def test_a_zombie_whose_lease_expired_writes_nothing_through_bury(
    db, db_other, tmp_path, monkeypatch
):
    """The at-least-once interleaving that produced `('ready', 'unparseable')` (issue #86).

    Worker A claims and stalls. Its lease expires and the reaper requeues the job. Worker B - the
    winner - claims, writes `processing`, and starts a real ingest. A wakes up mid-ingest and runs
    its terminal path against a lease it no longer holds. B then publishes.

    Only A's wake-up is injected; everything else is the production path. B claims through
    `process_one`'s own `claim`, writes `processing` through its own statement, publishes through
    the real `index_file` and settles through the real `complete` - so nothing about the ORDER
    under test is arranged by the test, which is the whole point of driving it rather than staging
    the row. A's bury is the REAL `_bury` with A's real `Job`, dead token and all.

    Three assertions, and the middle one is the defect: the row must be `('processing', None)`
    both before and after A's bury. The end state alone is not enough - `process_one`'s claim-time
    clear (#24) would scrub a reason written BEFORE B's `processing` write, so a test that only
    read the end could go green on the wrong guard entirely.

    All reads go through `db_other`, including the two inside the hook: they are claims about what
    A COMMITTED, and A shares `conn` with B here, so `conn` would see A's write whether or not the
    guard let it through.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    source = write_pdf(tmp_path / "the-winner-publishes-this.pdf", ["the real lecture text"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    zombie = claim(conn, lease_seconds=3600)
    assert zombie is not None
    _backdate_lease(conn, file_id)
    assert reclaim_expired(conn) == 1, "the scenario needs the job genuinely handed back"

    observed: list[tuple] = []
    real_ingest = worker.ingest_file

    def bury_from_the_zombie_then_ingest(*args, **kwargs):
        """Worker A wakes up between B's `processing` write and B's `index_file` commit."""
        observed.append(_file_row(db_other, file_id))
        worker._bury(conn, zombie, reason="unparseable", error="a zombie's dead-lease bury")
        observed.append(_file_row(db_other, file_id))
        return real_ingest(*args, **kwargs)

    monkeypatch.setattr(worker, "ingest_file", bury_from_the_zombie_then_ingest)
    assert tick(conn, FakeEmbeddings()) is True

    before, after = observed
    assert before == ("processing", None), (
        "the zombie did not wake up in the window this test is about - the winner had not "
        "written `processing` yet, so any guard at all would have looked sufficient"
    )
    assert after == ("processing", None), (
        "the zombie's `_bury` wrote to `files` on a lease it no longer holds - the `jobs` half "
        "refused the same call, which is the asymmetry #86 is"
    )

    status, reason, state, chunk_count = db_other.execute(
        """
        select f.status, f.failed_reason, j.state,
               (select count(*) from chunks c where c.file_id = f.file_id)
        from files f join jobs j using (file_id) where f.file_id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    assert (status, reason) == ("ready", None), (
        "a queryable file is carrying a failure reason - the forbidden end state, reached by "
        "the one path #24's claim-time clear cannot see"
    )
    assert state == "done", "the winner settled its own job; the zombie's `fail` was refused"
    assert chunk_count > 0, "`ready` means the full chunk set is committed and queryable"


def test_a_bury_still_holding_its_claim_writes_both_halves_past_the_lease_deadline(
    db, db_other, tmp_path, monkeypatch
):
    """The guard is OWNERSHIP, not the clock - `leased_until > now()` would strand this file.

    An ingest can outrun its own lease with nothing behind it to reap the job: V1 runs one worker
    (ADR 0011), and `reclaim_expired` only fires on a poll tick that has not happened yet. The job
    is then still `processing` under THIS worker's token, with `leased_until` in the past, and
    `fail` settles it happily - its guard reads state and token and never reads the clock.

    So a files half that tested `leased_until > now()` would refuse the call `fail` accepts, and
    leave the row `processing` under a terminally `failed` job: nothing will ever claim it again
    and the student gets no reason. That is the exact stranding `_bury`'s files-before-jobs order
    exists to avoid, reintroduced by the guard meant to make it safer. Matching `fail`'s two
    conditions verbatim is what keeps the two halves agreeing.

    Driven, not staged: the lease is backdated from INSIDE the ingest, which is what "the ingest
    outran the lease" means, and the terminal failure is the real `ParseError` the worker's
    `except` clause is written against.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    source = write_pdf(tmp_path / "slower-than-its-lease.pdf", ["a page that took too long"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)

    def outrun_the_lease_then_fail_terminally(*_args, **_kwargs):
        _backdate_lease(conn, file_id)
        raise ParseError("unparseable", "the parse finished long after the lease did")

    monkeypatch.setattr(worker, "ingest_file", outrun_the_lease_then_fail_terminally)
    assert tick(conn, FakeEmbeddings()) is True

    assert _file_row(db_other, file_id) == ("failed", "unparseable"), (
        "the file is stranded mid-flight with no reason for the student, while its job is "
        "terminally failed - the guard is reading the clock instead of the ownership"
    )
    state, lease_is_expired = db_other.execute(
        "select state, leased_until < now() from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert state == "failed", "`fail` settles a job past its lease deadline; both halves must"
    assert lease_is_expired, (
        "the lease never actually expired, so this test would pass without the distinction it "
        "exists to draw - compared in the SERVER's clock, the one the lease is stamped in"
    )


def test_a_bury_on_a_job_that_is_no_longer_processing_writes_nothing(db, db_other, tmp_path):
    """The STATE half of the guard, which the token half cannot stand in for.

    The two conditions mean ownership only together, the same argument `_settle`'s docstring
    makes about the guard this one copies. `complete` and `fail` leave the winning run's
    `lease_token` on the settled row deliberately - it records who finished the job - so a token
    match alone says nothing about whether the job is still anybody's in-flight work. Drop
    `state = 'processing'` and a bury reaching an already-settled job writes `failed` over it.

    The row is set by really claiming and really completing, so the token on it is the one the
    settle verb chose to keep rather than one the test invented. The file is left `queued`, never
    `ready`, so the monotonic guard is out of the way and only the lease guard can refuse.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    source = write_pdf(tmp_path / "already-settled.pdf", ["a page whose job is finished"])
    file_id = enqueue(conn, path=source, owner_id=owner_id, class_id=class_id)
    job = claim(conn, lease_seconds=3600)
    assert job is not None
    assert complete(conn, job_id=job.job_id, lease_token=job.lease_token) is True
    assert db_other.execute(
        "select lease_token::text from jobs where file_id = %s::uuid", (file_id,)
    ).fetchone() == (job.lease_token,), (
        "the settled row lost its token, so the state half is not the only thing refusing "
        "below and this test proves less than it claims"
    )

    worker._bury(conn, job, reason="unparseable", error="a bury reaching a settled job")

    assert _file_row(db_other, file_id) == ("queued", None), (
        "a bury wrote `failed` over a job that is no longer processing - a token match on a "
        "settled row is not ownership"
    )
