"""Queue - every statement that WRITES the `jobs` table lives here (issue #70).

WRITES, not touches: a caller may READ that table inside its own UPDATE, where an
answer this module returned would already be stale by the time it is used (#86).

This module RECORDS job state; it does not DECIDE it. The poll loop, the
retryable/terminal split, and every `files.status` transition after `queued`
belong to the worker (#71). That split is what keeps the two in disjoint files.

The `enqueue` / `claim` pair is ONE seam (ADR 0011): the boundary a V2 broker
swap drops in behind. Keep the shape swappable - no caller should need to know
the substrate is Postgres.

Every writer here REFUSES a connection that is already inside a transaction, rather
than silently degrading to a SAVEPOINT that publishes nothing (`gct.db.require_idle`;
ADR 0025, guarded per ADR 0027). This module adopted the guard first - measured at
zero firings, ADR 0027 §Adopted early in the queue module - and `index_file` followed
when #75 accepted the ADR, which is when the helper moved to `gct.db` so both modules
call one writer of the rule.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg

from gct.db import require_idle


@dataclass(frozen=True)
class Job:
    """One claimed unit of ingestion work - everything `ingest_file` needs, so the worker
    can call it without touching the DB in between.

    That last clause is the whole point of `staging_ref` being here rather than looked up.
    `ingest_file` takes a `path`, and the only record of it is `files.staging_ref`; a worker
    that had to fetch it would run a statement AFTER `claim` committed, and a bare SELECT
    reopens psycopg's implicit transaction exactly as a write does - putting the connection
    back INTRANS and degrading `index_file`'s transaction to a SAVEPOINT that publishes
    nothing (ADR 0025, and ADR 0027 §Context for the two tests this already cost). Carrying
    the value removes the worker's REASON to talk to the database between claim and ingest,
    which is a stronger guarantee than telling it not to.

    Wider than `ingestion-worker.md`'s `Job{ file_id, owner_id, class_id }` by four fields:
    `job_id` and `attempts` are the queue handle and the retry trail this module owns,
    `staging_ref` is the above, and `lease_token` is the proof of holding described below.
    The spec's §Interface/contract records the widening.

    `lease_token` is what makes "this lease is MINE" checkable. `claim` mints a fresh uuid per
    claim; the settle verbs match on it, so a worker whose lease expired and whose job was
    re-handed out is refused instead of overwriting the run that now holds it. Carry it back
    verbatim - it is a capability, not a value to inspect or reconstruct.
    """

    job_id: str
    file_id: str
    owner_id: str
    class_id: str
    attempts: int
    staging_ref: str
    lease_token: str


def enqueue(
    conn: psycopg.Connection,
    *,
    path: str | Path,
    owner_id: str,
    class_id: str,
) -> str:
    """Create the `files` row (queued) and its `jobs` row in ONE transaction; return file_id.

    With no API adapter in Slice 2, this is the only thing that can create the
    `queued` file row the worker later claims - which is exactly what #69's
    caller-supplied `file_id` exists to support.

    `staging_ref` is the file's ABSOLUTE PATH (decided 2026-08-03; confirmed when the stager
    arrived, #105). `gct.staging.stage` returns exactly the string a caller then passes here
    as `path`, so `enqueue` and `worker.process_one` (which opens `job.staging_ref` as a path)
    were both unchanged by Slice 3. It stops being a local path only at ADR 0010's V2 move to
    Object Storage, which is a V2 change.

    `path` yields TWO different column values: `filename` is the basename, because it is
    denormalized onto every chunk as the citation label (data-model.md §`chunks`) and a
    full path would render the uploader's home directory into every citation;
    `staging_ref` is the resolved absolute path, because that is what the worker opens.

    Both rows commit together or neither does. A committed `files` row with no `jobs` row
    is not a partial write anyone recovers from - it is a file stuck in `queued` that no
    worker will ever claim, and nothing in the system reports it.

    `path` NEED NOT EXIST, and that is deliberate rather than an oversight. `Path.resolve()`
    is non-strict, so a path to nothing enqueues cleanly and #71 fails it at parse - which is
    the designed route: ADR 0020 makes an unopenable file a TERMINAL failure carrying an
    actionable reason the student sees, and a reason on the file row is worth more to them
    than an exception thrown back at a caller with no UI. Checking here would also be a
    second writer for a fact the stager owns: `stage` returns only after the bytes are
    flushed, fsynced and renamed into place (#105), so a path it produced exists by
    construction, and the one case where a stat could disagree with it is ADR 0010's V2
    Object Storage, where `staging_ref` stops being a local path and the guard would go from
    useless to wrong. Recorded because "considered and routed to the parse-terminal path" and
    "nobody looked" are indistinguishable from the code alone.

    `class_id` IS PARSED AND THE CANONICAL SPELLING IS BOUND (#121), and this is now the shape
    EVERY id-taking boundary in the library takes - `class_exists`, `retrieve`, `index_file` and
    `ingest_file` for a caller's `class_id`, `get_file_status` for a `file_id` (#126: two sites
    bound raw, two caught too little of what the parser raises; all four now go through
    `gct.ids`). This function keeps its own copy of that parse, and `gct.ids`'s docstring is the
    writer of why.

    `get_file_status` is the writer of WHY - which spellings `uuid.UUID` accepts that Postgres's
    `::uuid` cast does not, and why validating the raw string and then binding THAT is not the
    same thing - and this is a cite, not a second copy of that argument.

    HOW LENIENT THIS ACTUALLY IS, said out loud because "the four spellings" under-states it and
    the guard test enumerates exactly four. `uuid.UUID` is not a spelling whitelist: it strips
    `urn:` and `uuid:` prefixes, strips surrounding braces, and removes hyphens WHEREVER they fall.
    So an id with a misplaced or extra hyphen - which Postgres's own cast refuses - now resolves to
    a real class here (measured on #121: six such forms accepted). That is the leniency this
    function chose, not an accident of it, and it does not widen who can reach what: the ownership
    check the upload route runs first (`class_exists`) parses with the same `uuid.UUID` and has
    always accepted these forms. The change is that the two calls now AGREE about an id instead of
    one saying yes and the other aborting, which was the whole defect.

    Lenient here, strict one layer up in the pipeline, and the asymmetry is about WHERE THE ID
    COMES FROM rather than about writers being stricter than readers. `ingest_file`
    (`gct/ingest/pipeline.py`) refuses a non-canonical `file_id` outright because the worker reads
    that id back out of the database, where it is canonical by construction, so any other spelling
    is an upstream bug. Everything that calls `enqueue` - a script, the Slice 3 upload route, the
    exit smoke - hands it a string a person typed. `ingest_file` applies BOTH rules on one call,
    which is the clearest statement of the criterion available: strict on `file_id`, lenient on
    `class_id`, the second added by #126 before the embedding run rather than after it.

    What is NOT inherited from those siblings is their REASON, and copying their sentence would
    make this docstring wrong. Theirs is a poisoned connection: they are readers with no
    transaction of their own, so a failed cast aborts the caller's implicit transaction and takes
    every later statement with it. This one has a transaction - the block below - and `require_idle`
    makes an IDLE connection the only kind that reaches it, so psycopg rolls that transaction back
    and leaves the connection IDLE. Measured on #121: the caller's next statement succeeds. The
    cost of letting the cast fail is narrower and later - one rejected write, surfacing as a
    psycopg `DataError` that names a Postgres type rather than anything a caller can act on, and
    for the upload route, after `stage` has already written the bytes.

    PRECONDITION ON `conn` (ADR 0025): it MUST NOT already be inside a transaction, or the
    block below degrades to a SAVEPOINT and this function returns having published nothing.
    Same precondition, same reason, as `index_file` - see its docstring.
    """
    require_idle(conn, "enqueue")
    try:
        canonical_class_id = str(uuid.UUID(class_id))
    except (ValueError, AttributeError, TypeError) as exc:
        # THREE exception types, not just `ValueError`, and the set is copied from `ingest_file`'s
        # guard (`gct/ingest/pipeline.py`) rather than invented: `uuid.UUID` reports a bad argument
        # in three different ways depending on what it was handed. A malformed STRING is the
        # `ValueError`; `None` is a `TypeError` ("one of the hex, bytes, ... arguments must be
        # given"); an `int`, a `list`, or an already-parsed `uuid.UUID` instance is an
        # `AttributeError` from the `.replace` call inside the parser. Catching only `ValueError`
        # let the last two escape as the parser's own message, which names `uuid.UUID`'s internals
        # and no remedy - the opposite of what this guard exists to do. Nothing shipped can reach
        # them today (the upload route's `class_id` is a Form field, so always a `str`), which is
        # what makes this a trap rather than a bug: it waits for the first caller that passes the
        # `uuid.UUID` it already has.
        raise ValueError(
            f"enqueue() requires a uuid class_id; got {class_id!r}. Pass the id `create_class` "
            "returned for the class this file belongs to - an id that is not a uuid cannot name "
            "any class, so nothing would be queued and the caller's staged bytes would be "
            "orphaned."
        ) from exc
    source = Path(path)

    with conn.transaction():
        # 1. The files row. `queued` is the status the student polls until the worker moves
        #    it (#71 owns every later transition). `file_id` is DB-generated and RETURNed
        #    rather than re-queried: a `select ... where filename = ...` is neither unique
        #    (the same file may be enqueued twice) nor scoped (it can cross owners, breaking
        #    F12), and this is one round trip instead of two.
        row = conn.execute(
            """
            insert into files (owner_id, class_id, filename, staging_ref, status)
            values (%(owner_id)s, %(class_id)s::uuid, %(filename)s, %(staging_ref)s, 'queued')
            returning file_id
            """,
            {
                "owner_id": owner_id,
                "class_id": canonical_class_id,
                "filename": source.name,
                "staging_ref": str(source.resolve()),
            },
        ).fetchone()
        file_id = str(row[0])

        # 2. The jobs row. `state`, `attempts` and the timestamps take their schema defaults
        #    (`queued`, 0, now()) because those are QUEUE MACHINERY: migrations/0001_init.sql
        #    owns them, and restating them here would be a second writer for values this
        #    module does not decide.
        #    `files.status` above is deliberately NOT the same case, and the rule is about
        #    machinery defaults only. That column is published domain truth - the thing the
        #    student polls - so naming it at the write site is worth the one line, and the
        #    duplication is safe in a way these are not: its legal values are pinned by a
        #    CHECK constraint AND by this module's own tests, so a schema change cannot drift
        #    past it quietly.
        #    `owner_id`/`class_id` are carried so `claim` can hand a worker everything
        #    `ingest_file` needs from one row - see `Job`.
        conn.execute(
            """
            insert into jobs (file_id, owner_id, class_id)
            values (%(file_id)s::uuid, %(owner_id)s, %(class_id)s::uuid)
            """,
            {"file_id": file_id, "owner_id": owner_id, "class_id": canonical_class_id},
        )

    return file_id


def claim(conn: psycopg.Connection, *, lease_seconds: int) -> Job | None:
    """Take the oldest queued job, or None. SELECT ... FOR UPDATE SKIP LOCKED (ADR 0011).

    Sets state='processing', bumps attempts, stamps leased_until, and mints a fresh
    `lease_token` - the proof of holding that `complete`/`fail`/`release` match on. A new
    token PER CLAIM is the point: after a reclaim the next claim's token differs, which is
    exactly what makes the previous holder's write refusable.

    COMMITS BEFORE RETURNING - that is the contract, not a detail (decided
    2026-08-11): both statements run inside this function's own `conn.transaction()`,
    so the lease is visible to every other worker the moment this returns, and `conn`
    is left OUTSIDE any transaction - the precondition `index_file` demands of a
    worker that ingests on the connection it claimed with (ADR 0025;
    `ingestion-worker.md` step 1). The rejected alternative - leaving the commit to
    the caller so a crash undoes the whole attempt - loses the `attempts` bump with
    it (a poison file then retries forever, its budget never spent) and holds the
    row lock through the entire ingest, where a hung worker parks the job somewhere
    neither another claimer nor `reclaim_expired` can see it.

    None means the queue is empty OR every queued row is mid-claim by a concurrent
    worker right now (SKIP LOCKED skips, never waits). Both are normal poll-loop
    outcomes, not errors - the worker hits them most ticks.

    The returned `attempts` COUNTS THIS CLAIM (the post-bump value): it answers
    "which attempt is this?", which is what #71 compares against its retry budget.

    `lease_seconds` is a parameter rather than a constant on purpose: no lease
    duration, backoff curve, or poll interval exists anywhere in the design corpus
    yet, and #71 owns picking them (epic #73, gap 1, resolved 2026-08-03).

    PRECONDITION ON `conn` (ADR 0025), same as `enqueue` and every writer here: it MUST
    NOT already be inside a transaction. The commit promised above is CONDITIONAL on it -
    hand this an open connection and the block below is a SAVEPOINT, so the lease is
    published to nobody, `conn` comes back still in a transaction, and a crash takes the
    `attempts` bump with it. All three guarantees fail together and silently. `require_idle`
    now refuses that call rather than letting it look like it worked; the docstring states
    it too, because the guard tells you THAT you are wrong and this tells you WHY.
    """
    require_idle(conn, "claim")
    with conn.transaction():
        # Both statements MUST share this transaction: the row lock FOR UPDATE takes
        # lives exactly as long as the transaction that took it, so a select in its
        # own transaction would release the lock before the update ran - two workers
        # could then claim the same job in the gap.
        # The join reaches `files` for ONE column - `staging_ref`, the path `ingest_file`
        # opens. It is here rather than in the worker so the worker runs no statement between
        # this commit and the ingest; see `Job`'s docstring for why that is load-bearing and
        # not a round-trip optimisation.
        # `for update OF J` is deliberate: an unqualified `for update` across a join locks the
        # matching `files` row too, and nothing here wants that row held - #71's `index_file`
        # upserts it moments later, on another connection under at-least-once.
        row = conn.execute(
            """
            select j.job_id::text, j.file_id::text, j.owner_id, j.class_id::text,
                   j.attempts, f.staging_ref
            from jobs j
            join files f using (file_id)
            where j.state = 'queued'
            order by j.created_at
            for update of j skip locked
            limit 1
            """
        ).fetchone()
        if row is None:
            return None
        job_id, file_id, owner_id, class_id, attempts, staging_ref = row

        # now() on the SERVER, not Python's clock: `reclaim_expired` compares
        # `leased_until < now()` on the server's clock, so stamping the lease from a
        # worker machine's clock would let clock drift expire leases early or late.
        # `gen_random_uuid()` on the SERVER for the same reason `now()` is: one source of
        # truth for a value the row is the authority on. RETURNING it rather than minting it
        # in Python keeps the token the row actually carries and the token the worker holds
        # the same value by construction, with no second write to get wrong.
        lease_token = conn.execute(
            """
            update jobs
            set state = 'processing',
                attempts = attempts + 1,
                lease_token = gen_random_uuid(),
                leased_until = now() + make_interval(secs => %(lease_seconds)s),
                updated_at = now()
            where job_id = %(job_id)s::uuid
            returning lease_token::text
            """,
            {"lease_seconds": lease_seconds, "job_id": job_id},
        ).fetchone()[0]

    return Job(
        job_id=job_id,
        file_id=file_id,
        owner_id=owner_id,
        class_id=class_id,
        attempts=attempts + 1,
        staging_ref=staging_ref,
        lease_token=lease_token,
    )


def _settle(
    conn: psycopg.Connection,
    *,
    verb: str,
    job_id: str,
    lease_token: str,
    assignments: str,
    params: dict[str, object],
    orphan_note: str,
) -> bool:
    """The shared body of `complete`/`fail`/`release`: one guarded write, one three-way answer.

    All three are the same move - "this worker is putting the job down" - and differ only in
    which state they put it down IN. That difference is the `assignments` fragment each verb
    passes; everything around it (the idle precondition, the lease guard, the existence
    re-query, the LookupError, the boolean) is one rule that must not be able to differ between
    them. It WAS three copies until #71 PR 2 added the third, at which point a guard change
    meant editing the same twenty lines in three places and getting all three right.

    `assignments` is interpolated into the statement rather than parameterized because SQL has
    no placeholder for a SET clause. It is safe by construction and must STAY so: every caller
    is inside this module and passes a literal. A caller-supplied fragment here would be
    injection - if a future verb ever needs a dynamic column, parameterize the VALUE and keep
    the column name literal.

    THE LEASE GUARD IS TWO CONDITIONS, and only together do they mean what the verbs claim.
    `state = 'processing'` says the job is somebody's in-flight work; `lease_token = ...` says
    that somebody is THIS caller. The state half alone is what shipped in #70/#71 PR 1, and it
    is not an ownership check: after a reclaim and a re-claim the row reads `processing` again,
    so a stalled worker's write passed a guard whose docstring promised it would not. Measured,
    not argued - the probe is `test_release_refuses_a_job_another_worker_now_holds`.

    The three-way answer, unchanged from when each verb spelled it out:
      - True  - this call is the one that settled the job;
      - False - the job exists but this worker no longer holds it: either it is no longer
                `processing`, or it is, under someone else's token. Both are "your lease is
                gone", routine under at-least-once, so it is a return value not an exception;
      - LookupError - no such `job_id`. A programming error: the caller believes something about
                a job that does not exist, and `orphan_note` says what that belief was.
    """
    require_idle(conn, verb)
    with conn.transaction():
        cursor = conn.execute(
            f"""
            update jobs
            set {assignments}, updated_at = now()
            where job_id = %(job_id)s::uuid
              and state = 'processing'
              and lease_token = %(lease_token)s::uuid
            """,
            {"job_id": job_id, "lease_token": lease_token, **params},
        )
        settled = cursor.rowcount == 1
        # Only asked when the update wrote nothing, and inside the same transaction so the
        # answer cannot be overtaken between the two statements.
        job_exists = (
            settled
            or conn.execute(
                "select 1 from jobs where job_id = %(job_id)s::uuid", {"job_id": job_id}
            ).fetchone()
            is not None
        )

    # Raised AFTER the block so the connection is left IDLE either way - the ADR 0025
    # contract this module's writers all keep.
    if not job_exists:
        raise LookupError(
            f"{verb}() called with job_id={job_id}, which names no row in `jobs`; {orphan_note}"
        )
    return settled


def complete(conn: psycopg.Connection, *, job_id: str, lease_token: str) -> bool:
    """Terminal success: state='done'. The worker calls this after ingest_file returns.

    Commits before returning, same contract as `claim` (its docstring has the
    argument). `leased_until` is deliberately left in place - `reclaim_expired`
    filters on state, so a stale lease on a terminal row is inert, and clearing it
    would erase the record of when the winning run held the job.

    ONLY THE CURRENT LEASEHOLDER MAY FINISH A JOB - `state = 'processing'` AND
    `lease_token`, together, in `_settle`. Without them, a worker whose lease expired and
    whose job was re-handed out can wake up and write a terminal state over the run that
    actually won. That is not hypothetical: `reclaim_expired`'s own docstring predicts this
    caller ("a stalled-but-alive worker keeps running and may finish after its job is
    re-handed out"), and the sequence claim -> reclaim -> re-claim -> the WINNER completes ->
    the zombie fails leaves the row `failed` for a file that is `ready`. Every argument in
    that sequence is correct; the guard is the only thing separating the two writers. It does
    NOT decide retryable-vs-terminal policy, which stays #71's - it only says a lease means
    what it says.

    THE STATE HALF ALONE DID NOT SAY THAT, which is why the token exists. `processing` is
    true of a job that has been re-handed out to someone else, so the sequence above passed
    the guard whenever the winner was still RUNNING rather than already finished - the
    version this docstring described, and the version the tests covered. `_settle` carries
    the mechanism; migration 0002 carries the argument.

    Returns False on a lost lease, raises `LookupError` on an unknown `job_id`, returns
    True when this call is the one that finished the job - the three-way split `_settle`
    owns and states, for all three verbs at once. That statement lives there and only
    there: this docstring used to carry its own copy naming ONE way to get False, and the
    lease token added a second the copy never learned about.
    """
    return _settle(
        conn,
        verb="complete",
        job_id=job_id,
        lease_token=lease_token,
        assignments="state = 'done'",
        params={},
        orphan_note="the caller believes a job finished that does not exist",
    )


def fail(conn: psycopg.Connection, *, job_id: str, lease_token: str, error: str) -> bool:
    """Terminal failure: state='failed' + last_error.

    `last_error` is free text - distinct from `files.failed_reason`, which is a
    closed CHECK set of five values that only #71 writes.

    Commits before returning, same contract as `claim`. `attempts` is untouched:
    the trail of how many tries this job burned is exactly what a human reading a
    failed row wants to see.

    Behind the same TWO-CONDITION lease guard as the other verbs - `state = 'processing'`
    AND `lease_token` - and this is the direction that actually costs something: a zombie's
    `fail` overwriting a genuine success is worse than a zombie's `complete` overwriting a
    genuine failure, because the second is at least true of some run. `_settle` owns the
    mechanism; `complete` carries why the state half alone was never an ownership check.

    Returns False on a lost lease, raises `LookupError` on an unknown `job_id`, returns
    True when this call is the one that failed the job - the same three-way split
    `_settle` makes for all three verbs. Kept symmetrical deliberately: a worker
    handles both outcomes in one place, and a split that differed between the success
    and failure paths would be read as meaningful.
    """
    return _settle(
        conn,
        verb="fail",
        job_id=job_id,
        lease_token=lease_token,
        assignments="state = 'failed', last_error = %(error)s",
        params={"error": error},
        orphan_note="the error message it carried has been discarded, not recorded",
    )


def release(conn: psycopg.Connection, *, job_id: str, lease_token: str, error: str) -> bool:
    """NON-terminal failure: state back to 'queued' so a later claim retries this job (#71).

    The third outcome `complete`/`fail` do not cover. Those two are the ends of a job's life;
    this one says "this attempt lost, the job has not". The worker calls it when the pipeline
    raises a TRANSIENT failure and the retry budget still has room - deciding WHICH failures
    are transient, and when the budget is spent, stays the worker's (see this module's opening
    lines: it records job state, it does not decide it).

    `attempts` is deliberately NOT decremented, and that is the whole point of the verb: the
    bump `claim` made is the retry trail the worker's budget counts against, so a release that
    gave it back would hand a poison file a fresh budget on every transient blip - the same
    failure `reclaim_expired` avoids by not resetting it. `last_error` IS overwritten, because
    the useful error is the one from the attempt that just failed.

    `leased_until` is cleared, unlike `complete`/`fail`, and the asymmetry is not an
    oversight: those leave a terminal row's lease in place because `reclaim_expired` filters
    on state and an inert stale lease is a record of when the winning run held the job. This
    row is going back to `queued`, where a leftover lease would be a live lie about a worker
    that no longer holds it.

    THE CALLER SERVES THE BACKOFF BEFORE CALLING THIS, not after. While the row is still
    `processing` the lease keeps every other worker off it, so the delay is enforced for the
    whole system; release first and the next poll tick - this worker's or another's - can
    re-claim it seconds later, which is ADR 0020's one genuinely wrong answer to a provider
    saying "slow down".

    Returns False on a lost lease, raises `LookupError` on an unknown `job_id`, returns True
    when this call is the one that requeued the job - the same three-way split, behind the same
    two-condition lease guard, that `complete` and `fail` make. THIS IS THE VERB THAT MOST
    NEEDS THE TOKEN HALF of it, because this is the one that puts a job back INTO CIRCULATION
    rather than ending it; migrations/0002_lease_token.sql carries what that costs.

    Commits before returning, same contract as `claim` (its docstring has the argument).
    """
    return _settle(
        conn,
        verb="release",
        job_id=job_id,
        lease_token=lease_token,
        assignments="state = 'queued', leased_until = null, last_error = %(error)s",
        params={"error": error},
        orphan_note="the job the caller believes it handed back does not exist",
    )


def renew_lease(
    conn: psycopg.Connection, *, job_id: str, lease_token: str, lease_seconds: int
) -> bool:
    """Push `leased_until` forward for a lease this caller STILL HOLDS. True if it moved.

    Not a settle verb: the other three put the job DOWN, and this one says "still mine, still
    working". It is the write behind `worker._LeaseHeartbeat`, and the reason `reclaim_expired`
    below can read a lapsed lease as "the worker is gone" rather than "the worker might merely be
    slow" (ADR 0028, the lease's meaning amended per ADR 0031).

    BEHIND `_settle`'s TWO CONDITIONS, VERBATIM - `state = 'processing'` AND `lease_token` -
    because every write that turns on who owns this job has to ask the ownership question the same
    way, and a fourth phrasing of it is a fourth thing to keep in step.

    The token half is more load-bearing here than in any settle verb, because this is the only
    write that hands a lease MORE LIFE. Without it, a worker whose job was reaped and re-handed out
    would pull the lease back from the run that now holds it - not a stale write landing on a
    settled row, but a dead claim resurrecting itself on a live one, and then beating indefinitely
    against a job someone else is ingesting. The state half is what stops a job settled mid-beat
    (`done`, `failed`, or handed back to `queued`) from being dragged back under a lease.

    `attempts` is untouched: a renewal is not an attempt, `claim` is the only verb that bumps that
    counter, and a heartbeat that spent the retry budget on staying alive would be the inverse of
    the defect ADR 0031 removes. `lease_token` is untouched for a different reason - the token
    identifies the CLAIM, not the beat, so minting a fresh one here would invalidate the very
    worker doing the renewing.

    NO `LookupError`, DELIBERATELY - the one place this shape departs from
    `complete`/`fail`/`release`, which raise on an orphaned `job_id`. EVERY answer that is not
    "renewed" comes back as False here, an unknown `job_id` included, and False is ROUTINE rather
    than an error. ADR 0031 §1 argues why; what the caller must DO with it is stop beating and
    settle nothing, because ADR 0030's publish guard is what refuses the doomed write.

    Server clock, for the reason `claim` stamps its lease from one: `reclaim_expired` compares
    `leased_until < now()` on the server, so a beat stamped from a worker machine's clock would
    renew into drift instead of out of it.

    PRECONDITION ON `conn` (ADR 0025, guarded per ADR 0027), same as every writer here. The
    heartbeat satisfies it by construction rather than by care - it opens its OWN connection and
    never borrows the worker's, which is inside `ingest_file`'s transaction for the whole window a
    beat could land in (`worker._LeaseHeartbeat`).
    """
    require_idle(conn, "renew_lease")
    with conn.transaction():
        cursor = conn.execute(
            """
            update jobs
            set leased_until = now() + make_interval(secs => %(lease_seconds)s),
                updated_at = now()
            where job_id = %(job_id)s::uuid
              and state = 'processing'
              and lease_token = %(lease_token)s::uuid
            """,
            {"lease_seconds": lease_seconds, "job_id": job_id, "lease_token": lease_token},
        )
    return cursor.rowcount == 1


def reclaim_expired(conn: psycopg.Connection) -> int:
    """The reaper: processing rows past leased_until go back to queued. Returns the count.

    Zero cleanup by design - a crashed run committed nothing, because the ingest
    transaction wraps only the final write, never the parse/embed work (ADR 0020).
    #71 calls this on the poll loop (ADR 0011 addendum).

    ONE statement on purpose: a select-then-update pair would need its own row
    locking to close the gap between reading a lease and resetting it; a single
    UPDATE is that whole move, atomically. Both WHERE conditions are load-bearing:
    - without `state = 'processing'`, terminal rows get resurrected - `complete`/
      `fail` never clear `leased_until`, so every done and failed job carries an
      expired lease forever and would requeue on every tick;
    - without `leased_until < now()`, a live lease is yanked from a healthy worker
      mid-job. (Same server clock `claim` stamped the lease with.)

    `lease_token` IS cleared, unlike `attempts`, and the two are opposites on purpose: the
    token is the current holder's proof and this statement is the act of taking it away, so
    leaving it would let the reclaimed worker keep settling the job. `attempts` is deliberately
    NOT reset - it is the retry trail #71 compares against its budget. A reclaim that zeroed it
    would hand a poison file a fresh budget after every crash.

    WHAT AN EXPIRED LEASE MEANS NARROWED WITH #95, and this statement is unchanged by it. A live
    worker now pushes its own `leased_until` forward through `renew_lease` while it works, so a
    lapsed lease no longer covers "the worker is merely slower than one lease" - it means the
    worker is gone, or wedged past the heartbeat's cap (ADR 0028, the lease's meaning amended per
    ADR 0031). The rows this reaps are a strict subset of the rows it reaped before; nothing it
    used to leave alone is newly at risk.

    Reclaiming is NOT killing: a stalled-but-alive worker keeps running and may
    finish after its job is re-handed out. That duplicate run is safe by design
    (at-least-once, ADR 0011), but HOW it is made safe changed with #92 and the
    distinction is load-bearing. It used to be ABSORBED - the reclaimed worker
    published and `index_file`'s all-or-nothing replace made the second write
    harmless (ADR 0020). Absorption is only harmless for the CHUNK set: the
    publish also flips `files.status` to `ready`, over whatever the winning run
    had written there, which is how a `ready` file came to carry a failure reason.
    So it is now DISCARDED instead - `worker.process_one` passes a lease predicate
    that `index_file` evaluates inside its transaction, and a reclaimed worker's
    publish is refused rather than replayed (ADR 0030). The work is thrown away;
    the row the winner wrote stands.

    Commits before returning, same contract as `claim` (its docstring has the
    argument). Returns how many rows moved; 0 is the normal tick, not an error.
    `jobs_state_lease_idx (state, leased_until)` exists precisely for this scan.
    """
    require_idle(conn, "reclaim_expired")
    with conn.transaction():
        cursor = conn.execute(
            """
            update jobs
            set state = 'queued',
                leased_until = null,
                lease_token = null,
                updated_at = now()
            where state = 'processing' and leased_until < now()
            """
        )
    return cursor.rowcount
