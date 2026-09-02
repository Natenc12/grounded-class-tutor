"""Worker - polls `jobs`, claims one, runs the unchanged `ingest_file`, drives `files.status`.

The other half of the split `queue.py` declares in its first lines: that module RECORDS job
state, this one DECIDES it. The poll loop, the retryable/terminal split (issue #71 PR 2), and
every `files.status` transition after `queued` live here.

Status ownership - who writes which `files.status` transition (issue #71):

    | transition                | writer                                                     |
    |---------------------------|------------------------------------------------------------|
    | -> queued                 | `enqueue` (#70)                                            |
    | -> processing             | THIS module, on claim - the first-ever writer, and the     |
    |                           | only UN-writer of `failed_reason` (#24)                    |
    | -> ready                  | `index_file`, inside the index transaction (ADR 0020 §3)   |
    | -> failed + failed_reason | THIS module - terminal input, or budget exhausted          |

`jobs.state` is the OTHER axis - claim/lease/retry machinery - and `queue.py` owns every
statement that WRITES that table. Exactly one statement here READS it: `_bury`'s lease guard,
which has to ask "do I still hold this job?" inside the `files` UPDATE rather than before it,
or the answer is stale by the time it is used (#86; the argument is in `_bury`'s docstring).

The retryable/terminal split (ADR 0020 §1) is this module's, and it is a two-line rule:
`ParseError` is bad INPUT - straight to `failed(reason)`, retry budget untouched, because a
corrupt file is exactly as corrupt on the next attempt. `TransientEmbeddingError` is bad LUCK -
back off, hand the job back to `queued` via `release`, and let a later claim retry it. Anything
else propagates and crashes the worker, deliberately: an unclassified exception is a bug we do
not want swallowed into a `failed_reason` that names the wrong cause. A DB error is in that
class, which DEVIATES from ADR 0020 §1's original taxonomy and is now ratified (ADR 0020 §1,
DB-blip class amended per ADR 0028). The durable budget bounds the resulting crash loop, and
`run`'s reaper is what makes that true: it moves the crashed job out of `processing` so the next
claim can spend an attempt on it.

WHY THE BUDGET IS COUNTED IN THE DATABASE, not in a `for` loop around the ingest. `jobs.attempts`
survives the worker process; a loop counter does not. The failure that most needs bounding is the
one where the worker DIES mid-job - a crash, an OOM, a `kill` - and no in-process handler ever
runs. Those attempts still have to count, or a poison file is re-handed out forever by the reaper
(`claim`: "a poison file then retries forever, its budget never spent"; `reclaim_expired`:
"`attempts` is deliberately NOT reset - it is the retry trail #71 compares against its budget").
One durable counter bounds transient failures and crash loops with one rule.

WHY THE CHECK IS HERE AND NOT INSIDE `claim`. An over-budget job still has to be CLAIMED to be
buried: burying it means writing `files.status = failed` + `failed_reason`, a table `queue.py` is
not allowed to touch, so a `claim` that refused to hand the job over would leave the student with
a file stuck mid-flight and no reason for it. It is also policy, which that module declares it
does not own; and `claim` is the seam a V2 broker swaps in behind (ADR 0011), where the universal
shape is exactly this - the substrate hands you a delivery count, the consumer decides
dead-lettering. `claim` returning the post-bump `attempts` is what makes that split work.

Runs as a separate OS process via `scripts/worker.py`: ADR 0011's addendum is explicit that
"in-process" means no broker, NOT inside an API event loop; the script is a thin peer caller
over this module (ADR 0009).

Connection contract (ADR 0025, guarded per ADR 0027): every entry point here needs a
connection that is idle between statements. Every writer on the worker path commits
itself inside its own `conn.transaction()` (`claim`/`complete` in queue.py, the status
write here, `index_file`), so a plain connection works end to end. `scripts/worker.py`
still sets `conn.autocommit = True` at wiring as defense in depth: it protects the next
statement someone adds OUTSIDE a transaction block from silently opening the implicit
transaction this module no longer contains.

ONE EXCEPTION, AND IT IS DELIBERATELY NOT ON THAT CONNECTION: `_LeaseHeartbeat` opens its own
through `gct.db.connect()` for the length of each beat, so a worker briefly holds TWO connections
while it ingests. It has to - the worker's is inside `ingest_file`'s transaction for that whole
window, which is where the contract above stops being satisfiable (ADR 0031 §2 argues why sharing
the connection fails, and the class lists the rules that follow).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import psycopg

from gct.db import connect
from gct.ingest.index import PublishRefused
from gct.ingest.parse import ParseError
from gct.ingest.pipeline import ingest_file
from gct.jobs.queue import Job, claim, complete, fail, reclaim_expired, release, renew_lease
from gct.providers.base import Embeddings, TransientEmbeddingError

logger = logging.getLogger(__name__)

# The four numbers, ratified by ADR 0028 - which also records that NOTHING HAS MEASURED any of
# them. They are chosen to be safe-if-wrong in the direction that costs least; the ADR names that
# direction per number, and the evidence that would move each one - THREE of which this module
# emits; the poll number has no in-process signal by construction, because a slow poll costs
# latency a student feels and the worker cannot see. Ratified is not measured: change these when
# the evidence says to, not on taste.
# The lease errs LONG on purpose: too short reclaims a job a healthy worker is still ingesting
# and pays the embedding bill twice ONCE A SECOND WORKER EXISTS - on today's single worker the
# reaper cannot run while `process_one` holds the loop, so a short lease shows up as a cut
# backoff instead (ADR 0028 §1). Too long only delays reclaim after a genuine crash - which is
# now the SIGKILL-shaped subset of them: `_release_on_shutdown` hands the job back on any death
# that unwinds the stack, so this number is paid only when no handler gets to run at all
# (ADR 0028 §Consequences, shutdown-release bullet).
DEFAULT_LEASE_SECONDS = 15 * 60
DEFAULT_POLL_SECONDS = 2.0
# How many attempts actually RUN before the job is buried as `transient_exhausted`. Counted in
# `jobs.attempts`, so a crash costs an attempt exactly as a caught 429 does - see the module
# docstring for why that is the point rather than an approximation. A doomed job is always
# claimed once MORE than this, to be buried: burying writes `files`, a table `queue.py` may not
# touch, so only a worker holding the job can do it (ADR 0028 §1). That claim buys no embed.
DEFAULT_MAX_ATTEMPTS = 5
# Backoff between attempts: doubling, and capped. The cap is what keeps the delay a delay - an
# uncapped curve reaches hours by the fifth attempt for a provider blip that cleared in seconds.
# Both sit far under DEFAULT_LEASE_SECONDS on purpose: the worker serves the backoff while still
# HOLDING the lease (see `process_one`), so a backoff that could outlast the lease would let the
# reaper hand the job to someone else mid-wait, which is the double-embed the lease exists to
# prevent. ADR 0011 named that mechanism and deferred its value; the value is ADR 0028 §1.
# That relationship is ENFORCED in `process_one`, not just intended here -
# `lease_seconds` is a parameter, so these two constants cannot see the number they must stay
# under, and a caller passing a short lease would otherwise break the rule silently.
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0
# The two HEARTBEAT numbers. ADR 0031 §5 ratifies both WITHOUT having measured either - the same
# stance ADR 0028 took on its four - and states per number the safe-if-wrong direction and the
# evidence that would move it. That argument lives there and is not copied here.
#
# How often a LIVE worker pushes its lease forward. A FRACTION, not an absolute interval, so a
# caller that shortens `lease_seconds` shortens the beat with it and the two cannot drift apart.
DEFAULT_HEARTBEAT_FRACTION = 0.25
# The cap on how long ONE run may keep beating: FOUR LEASES, and it must stay written as four
# leases. A heartbeat cannot tell WORKING from WEDGED - both are a live process holding a lease -
# so the beats stop here and the lease is allowed to lapse on schedule, which is what hands a
# wedged worker back to the reaper and back under the ordinary attempts budget (ADR 0031 §4). The
# safe-if-wrong argument is ADR 0031 §5, and what erring long actually costs a wedged run is that
# ADR's §Consequences - read those rather than a copy of them here.
# DERIVED, NOT A SECOND NUMBER THAT HAPPENS TO AGREE. ADR 0031 §5 argues this cap in units of the
# lease, so a literal 3600 sitting beside a `lease_seconds` someone retunes would leave the ADR's
# claim true only by coincidence, and the first person to shorten the lease would falsify it
# silently. Multiplying is what makes the two unable to drift. Effective value: 3600s.
DEFAULT_HEARTBEAT_MAX_SECONDS = 4 * DEFAULT_LEASE_SECONDS
# A floor under the DERIVED beat interval, so a pathological `heartbeat_fraction` (zero, or
# negative) cannot turn the beat loop into a spin. Not a knob: it guards a misconfiguration rather
# than expressing a policy, and it is small enough that the sub-second leases the tests drive
# still beat several times.
_MIN_HEARTBEAT_INTERVAL_SECONDS = 0.05
# How long `_LeaseHeartbeat.__exit__` waits for its thread. Bounded rather than unbounded because
# the thread may be inside a beat against a database that has stopped answering, and by then the
# ingest is already over - a shutdown must not hang on a lease it is about to stop needing.
_HEARTBEAT_JOIN_SECONDS = 5.0


def heartbeat_interval(lease_seconds: float, fraction: float) -> float:
    """The derived beat interval: `fraction` of the lease, floored (ADR 0031 §5).

    A function rather than an expression because TWO places need the answer and only one of them
    computes anything: `_LeaseHeartbeat` beats at it, and `run`'s startup line reports it. A line
    whose entire job is to REPORT a number must not be a second derivation of that number - the two
    copies agree until someone edits one, and the failure is a startup banner that reads true and
    describes a beat the worker is not running. One writer, two readers.
    """
    return max(lease_seconds * fraction, _MIN_HEARTBEAT_INTERVAL_SECONDS)


def backoff_seconds(attempts: int) -> float:
    """Delay before the attempt AFTER `attempts` - exponential, capped (ADR 0020).

    `attempts` is the post-bump value `claim` returns, so the first failure (attempts=1) waits
    `BACKOFF_BASE_SECONDS` and each later one doubles until the cap. Retrying a provider that
    just said "slow down" with no delay at all is the one answer ADR 0020 calls genuinely wrong;
    the curve is the cheapest thing that is not that.

    No jitter, deliberately. Jitter exists to de-synchronise a fleet of clients retrying in
    lockstep; V1 is ONE in-process poll worker (ADR 0011) with nothing to de-synchronise from,
    so it would add a moving part with no failure to prevent. Revisit with worker concurrency
    (`ingestion-worker.md` §Open/deferred), not before.
    """
    return min(BACKOFF_BASE_SECONDS * 2 ** (attempts - 1), BACKOFF_MAX_SECONDS)


def served_backoff(
    attempts: int, *, max_attempts: int, lease_seconds: int, elapsed: float
) -> tuple[float, float]:
    """`(wanted, served)` - the curve's answer, and what this worker can actually afford.

    Split from `backoff_seconds` because they answer different questions and only one of them
    is the delay: the curve is a WANT, and the lease is the budget. Returning both is what lets
    the caller say "cut from X to Y" - a cut is evidence the lease number is wrong, and a
    function returning only the served value would make that unreportable.

    Three things bound the wait, and each is a different failure if it is missing:
      - THE CURVE - retrying a provider that just said "slow down" with no delay at all is the
        one answer ADR 0020 calls genuinely wrong;
      - THE BUDGET - the sleep is served while the lease is HELD (that is the point: it holds
        every worker off the row, not just this one's next poll). A delay outlasting the lease
        lets the reaper hand the job to a second worker mid-wait, and two runs embed one file.
        `BACKOFF_MAX_SECONDS` sits far under `DEFAULT_LEASE_SECONDS`, but `lease_seconds` is a
        PARAMETER and the curve is module constants - they cannot see each other, so the rule
        held only by coincidence of the defaults until it was enforced here (ADR 0028 §2).
        Measured at `lease_seconds=5`, where the unclamped curve slept 16s under a 5s lease;
      - THE LAST ATTEMPT - no wait at all, because the next claim's budget check refuses the
        job and the delay would buy a retry that never comes. A READER of `max_attempts`, not
        a second decider: the outcome is still settled by `process_one`'s one check.

    HALVED, not merely fitted, and `elapsed` is why: the caller measures it from just before
    the `processing` write, a hair AFTER `claim` stamped the lease, so `lease_seconds - elapsed`
    slightly OVER-estimates what is left. The margin absorbs that, the `release` write that
    follows the sleep, and clock skew against the server whose clock the lease is stamped in.
    """
    wanted = backoff_seconds(attempts) if attempts < max_attempts else 0.0
    return wanted, min(wanted, max(0.0, lease_seconds - elapsed) / 2)


def _publish_guard(job: Job) -> Callable[[psycopg.Connection], bool]:
    """Build the predicate `index_file` asks before publishing: is this job still OURS? (#92)

    THE THIRD INTERLEAVING, and the one neither #24 nor #86 is aimed at (`_bury`'s docstring
    partitions all three). A worker stalls in ingest, its lease is reaped, a second worker claims,
    exhausts the budget and burys WITH A LIVE LEASE - so #86's guard passes exactly as designed -
    and the first worker then publishes `ready` over the reason. `index_file` was the only write to
    `files` that read no lease; this closes that, and the invariant in
    `components/ingestion-worker.md` holds for the worker path as of ADR 0030.

    THE CONDITIONS ARE `_settle`'s TWO, VERBATIM, for the reason `_bury`'s `EXISTS` copies them:
    every write that settles this job must agree about who owns it, and a third phrasing of the
    ownership question is a third thing to keep in step. `state = 'processing'` AND our
    `lease_token`; nothing about the clock, which `_bury`'s docstring explains at length.

    `FOR UPDATE` IS THE WHOLE MECHANISM AND NOT AN OPTIMISATION - WITHOUT IT THIS NARROWS THE RACE
    INSTEAD OF CLOSING IT. This is a check-then-act: the SELECT is one statement and the publish is
    the next, and under READ COMMITTED the publish gets a fresh snapshot. A plain SELECT can
    therefore answer "still ours", have the reaper requeue the job and a second worker claim and
    bury in the gap, and the publish still lands - the same defect, in a window microseconds wide
    instead of ingest-wide. `FOR UPDATE` takes a row lock held to COMMIT, and the lease can only
    move through `reclaim_expired`, an UPDATE of that row, which now blocks until this
    transaction ends. So the answer cannot go stale between being given and being relied on.
    `_bury` solves the identical problem the other way, by folding its check into the UPDATE's
    own WHERE - unavailable here, because the write is in `index.py` and giving that statement a
    `jobs` conjunct is the seam crossing ADR 0030 §3 declined.

    NO DEADLOCK AGAINST `_bury`, and it is worth stating because the two lock the same two rows in
    OPPOSITE ORDER - this takes `jobs` then `files`, `_bury` writes `files` then `jobs`. A cycle
    needs one transaction holding both, and `_bury` has none: its `files` write and its `fail()`
    are separate transactions on purpose (its docstring argues the order from what a crash between
    them leaves behind), so the `files` lock is released before the `jobs` lock is asked for.
    Whoever makes those two writes one transaction reintroduces the cycle, and should read this.

    Returns the predicate rather than taking `conn` directly: `index_file` supplies the connection,
    and it must be THAT one - the check is worthless on any other, since the lock would not be held
    by the transaction doing the writing (`index_file` step 0).
    """

    def still_ours(conn: psycopg.Connection) -> bool:
        return (
            conn.execute(
                """
                select 1 from jobs
                where job_id = %(job_id)s::uuid
                  and state = 'processing'
                  and lease_token = %(lease_token)s::uuid
                for update
                """,
                {"job_id": job.job_id, "lease_token": job.lease_token},
            ).fetchone()
            is not None
        )

    return still_ours


def _bury(conn: psycopg.Connection, job: Job, *, reason: str, error: str) -> None:
    """Terminal failure on BOTH axes: `files.status='failed'` + `failed_reason`, then `jobs`.

    The two writes are one event and always travel together, which is why they are one
    function rather than two calls a future handler could get half-right.

    `reason` goes into `files.failed_reason`, a CHECK-constrained closed set (`migrations/`
    owns the members, and has already widened them once - ADR 0020, terminal set extended per
    ADR 0029; the count is derived from the schema, never stored here) and is passed through
    UNTRANSLATED from `ParseError.reason`, which is drawn from the same taxonomy for exactly
    this reason (ADR 0020; `parse.py`'s docstring promises the pass-through). `error` is the
    free-text `jobs.last_error` - the diagnostic detail the closed set cannot carry.

    THE FILES ROW GOES FIRST, and the order is chosen for what a crash between the two writes
    leaves behind. Files-then-jobs: the file reads `failed` while the job is still `processing`
    under a lease, so the reaper requeues it and a later attempt overwrites both - self-healing.
    Jobs-then-files: the job is terminally `failed`, nothing will ever claim it again, and the
    file is stranded in `processing` forever with no reason for the student. Only one of those
    two recovers on its own.

    THE FILES WRITE IS GUARDED TWICE, and the two guards answer different questions. Both are
    about the same at-least-once fact - this worker may be a zombie whose lease was reaped and
    re-handed out while it was stalled (ADR 0011) - but they catch it at different moments.

    `status <> 'ready'` keeps the student-facing status monotonic, the same guard and the same
    argument as the `processing` write in `process_one`: flipping a queryable file to `failed`
    is the one direction that costs the student something real.

    The `EXISTS` is the LEASE guard, and it is `_settle`'s two conditions verbatim
    (`state = 'processing'` AND `lease_token` = ours) so that the two halves of this one function
    agree about who owns the job. They did not: `fail` refused a dead lease while this write went
    through regardless, so a zombie could stamp a reason on a row the winning run was mid-way
    through publishing - `processing`, not `ready`, so the monotonic guard saw nothing wrong - and
    `index_file` then republished it `ready` with the reason still attached (#86). Matching `fail`
    exactly is what makes a lost-lease bury write NOTHING rather than half of an event whose two
    writes "always travel together". It READS `jobs` from this module, which the ownership rule
    at the top of this file permits and the module docstring now says out loud: `queue.py` owns
    every statement that WRITES that table, and this is the one place outside it that reads one.
    Asking `queue.py` for the answer first and then writing on it would be a check-then-act with
    a reclaim-sized gap in the middle; inside the UPDATE the two are one statement.

    NOT `leased_until > now()`, which is stricter and would strand a file. An ingest that OUTRUNS
    its lease with nothing behind it to reap the job still settles that job through `fail`, whose
    guard does not read the clock; a files half that refused the same call would leave the row in
    `processing` under a terminally `failed` job - the exact stranding this function's write ORDER
    was chosen to avoid, reintroduced by the guard meant to make it safer.

    TOGETHER WITH THE CLAIM'S CLEAR (#24) THIS CLOSES TWO OF THE THREE INTERLEAVINGS THAT REACH
    `('ready', <a reason>)`, which is #24's acceptance and did not hold on #24 alone. The winner's
    `claim` strictly precedes its `processing` write and invalidates this worker's token, so a bury
    whose `EXISTS` still passes is one the winner has not claimed past yet - and that later claim's
    `processing` write clears the reason. Neither guard closes the other's: #24 covers
    bury-then-claim, this covers claim-then-bury.

    THE PARTITION IS BY WHERE THE BURY FALLS RELATIVE TO THE PUBLISHER'S CLAIM, not by whose
    lease is alive. The end state needs a bury (the only writer that SETS the reason) with no
    `processing` write (the only writer that CLEARS it) between it and the publish. Either the
    publisher claimed AFTER the bury - #24's clear removes the reason on the way past - or it was
    already past its claim, and then the burier either LOST its lease (#86, this guard) or still
    HELD it (#92, closed by `_publish_guard` above rather than by anything in this function).

    #92 WAS THE ONE NEITHER GUARD IS AIMED AT, AND IT IS NOT CLOSED HERE. A slow worker's lease
    expires, the reaper requeues, a second worker claims and burys with a live lease - this guard
    passes, correctly - and the first worker then publishes through `index_file`, which read no
    lease at all. The winner burys, the zombie publishes, and the reason survived under `ready`.
    Demonstrated by execution, not argued - and the same script produced the same final row on the
    commit BEFORE this guard, so it was a residual this function never covered rather than one it
    introduced. It is now refused at the PUBLISH instead: `_publish_guard` hands `index_file` this
    same ownership question to ask inside its own transaction (ADR 0030), which is what a bury
    holding a live lease cannot be asked to do anything about.

    THE CLAIM-THEN-BURY HALF RESTS ON ONE `jobs` ROW PER `file_id`, WHICH `enqueue` GUARANTEES AND
    THE SCHEMA DOES NOT. The argument above turns on the winner's claim invalidating *this* worker's
    token - true only while both runs contend for the same job row. Given two live jobs for one
    file, a zombie's `EXISTS` is satisfied by its own row while the other run publishes `ready`,
    and the forbidden state returns. No input reaches that today: `queue.py`'s insert is the only
    one in the repo and `enqueue` mints a fresh `files` row beside it. Whoever gives a file a
    second live job - an upload path that re-enqueues an existing `file_id` is the obvious way -
    breaks this claim, and should read it before deciding they haven't.

    A lost lease is reported, not raised: it is the routine at-least-once outcome (`fail`'s
    docstring), and now both halves report the same thing rather than disagreeing.
    """
    with conn.transaction():
        conn.execute(
            """
            update files
            set status = 'failed',
                failed_reason = %(reason)s,
                updated_at = now()
            where file_id = %(file_id)s::uuid
              and status <> 'ready'
              and exists (
                  select 1 from jobs
                  where job_id = %(job_id)s::uuid
                    and state = 'processing'
                    and lease_token = %(lease_token)s::uuid
              )
            """,
            {
                "reason": reason,
                "file_id": job.file_id,
                "job_id": job.job_id,
                "lease_token": job.lease_token,
            },
        )

    if not fail(conn, job_id=job.job_id, lease_token=job.lease_token, error=error):
        logger.warning(
            "job %s: lease lost before it could be failed - another worker owns it now",
            job.job_id,
        )


def _release_on_shutdown(conn: psycopg.Connection, job: Job, exc: BaseException) -> None:
    """Hand the in-flight job back to `queued` while `exc` unwinds `process_one` (issue #82).

    WHY `release` AND NOT `complete`/`fail`: it is the only settle verb that CLEARS
    `leased_until`. The other two leave a terminal row's stale lease in place deliberately -
    the reaper filters on state, so it is inert, and it records when the winning run held the
    job. This row is going back to `queued`, where a leftover lease would be a live lie about a
    worker that no longer holds it (`release`'s docstring). That lie is the whole defect: a
    lease stamped up to `DEFAULT_LEASE_SECONDS` ahead does not match `reclaim_expired`'s
    `leased_until < now()`, so nothing - not even the worker that restarts a second later - can
    claim the job until the lease elapses.

    `attempts` is left alone, which is what makes this NOT a special case: a shutdown costs one
    attempt exactly as a crash or a caught 429 does (ADR 0028 §1), so the durable budget bounds
    a restart loop the same way it bounds everything else.

    CALLED UNCONDITIONALLY, with no "did the pipeline finish?" branch. If the interrupt landed
    in the millisecond between `index_file`'s commit and `complete`, the file is already `ready`
    and this sends a finished job back for a full re-ingest - a duplicate embedding bill. That
    is not a new cost class: today's reaper does exactly the same thing to exactly that job
    fifteen minutes later (ADR 0028 §Consequences enumerates `ready` as reachable under a
    reaped job). Branching on the pipeline's return would buy that bill back at the price of
    threading a "did it return" flag through the guard and a second settle path to test, and
    was declined for this reason rather than overlooked.

    NO LEASE GUARD OF ITS OWN, because `_settle` already has the one that matters:
    `state='processing' AND lease_token = ours`. A zombie whose lease expired, was reaped and
    was re-claimed by another worker gets `False` and writes nothing -
    `test_release_refuses_a_job_another_worker_now_holds` (tests/gct/jobs/test_queue.py) drives
    that on a real reap-and-re-claim. So `False` here is a REPORT, not an error.

    BUT IT IS NOT THE SAME REPORT the other settle paths make, and the message must not borrow
    theirs. On `complete`/`fail`/the transient `release`, a `False` can only mean a lost lease:
    the job is still `processing` when they run, so the only condition that can refuse them is
    the token half. The widened guard gives this call site a SECOND cause - it also spans the
    sliver after a settle verb has already returned, where the row reads `done`/`failed`/
    `queued` and `_settle`'s FIRST condition refuses the release. Nothing is wrong there and
    nothing is written; what would be wrong is reporting it as "another worker owns it now",
    which names a concurrency event V1 cannot have (ADR 0011: one worker; ADR 0028 §5's safety
    argument rests on it) about a job THIS worker settled a microsecond earlier. ADR 0028
    §Consequences reads these lines as evidence, so a false one is not cosmetic. The message
    below therefore states the fact both causes share - this worker no longer holds the job -
    and names both rather than diagnosing the wrong one.

    `conn.rollback()` FIRST. `release` calls `gct.db.require_idle` and raises on a connection
    left inside a transaction (ADR 0025, guarded per ADR 0027). psycopg's `transaction()` block
    rolls itself back on `BaseException`, so an interrupt inside `ingest_file` already leaves
    the connection IDLE - but a bare statement outside any transaction block leaves a PLAIN
    connection INTRANS, and `run` accepts a plain connection by contract (this module's
    connection-contract paragraph). Rolling back makes IDLE unconditional; it is a no-op on an
    already-idle connection under either wiring (measured), and it is inside the `try` because
    on a closed connection it is the statement that raises.

    THE RELEASE MUST NOT MASK `exc`. A DB error is one of the very causes of the shutdown, so
    "the DB is gone" is a likely reason this write fails - and a Ctrl-C that surfaced as a
    psycopg traceback would tell the operator the wrong story. On failure the fallback is
    precisely the pre-#82 behavior: the row keeps its live lease and the reaper collects it when
    that lease expires. This handler is an optimization of a path that was already safe (nothing
    was committed - ADR 0020 §2/§3), never a correctness dependency.

    `Exception` rather than `BaseException` on that catch, deliberately: a SECOND interrupt
    arriving during the release is not "the release failed", it is the operator insisting, and
    swallowing it would be the one thing worse than losing the original - which survives as
    `__context__` either way, and the worker exits regardless.

    WHAT THIS CANNOT COVER, plainly: `SIGKILL`, an OOM kill, and a hard power loss. No handler
    runs, nothing writes the row, and the 15-minute lease is paid in full. This narrows the
    trigger set; it does not empty it.
    """
    try:
        conn.rollback()
        if release(
            conn,
            job_id=job.job_id,
            lease_token=job.lease_token,
            error=f"shutdown: {type(exc).__name__}: {exc}",
        ):
            logger.warning(
                "job %s: requeued on shutdown (%s) - claimable at once, not after its lease",
                job.job_id,
                type(exc).__name__,
            )
        else:
            logger.warning(
                "job %s: nothing to requeue on shutdown - this worker no longer holds it "
                "(it settled just before the interrupt, or its lease was reaped and re-claimed)",
                job.job_id,
            )
    except Exception:
        # Reported at WARNING with the traceback, not raised: `exc` is what the operator asked
        # about and it is about to re-raise. The consequence is worth saying out loud in the log
        # rather than leaving to be inferred from silence.
        logger.warning(
            "job %s: could not requeue on shutdown - it stays `processing` until its lease "
            "expires and the reaper collects it",
            job.job_id,
            exc_info=True,
        )


class _LeaseHeartbeat:
    """Keeps a LIVE worker's lease fresh while it ingests, on ITS OWN CONNECTION (ADR 0031 §2).

    A context manager wrapping the `ingest_file` call and nothing else. On entry a daemon thread
    starts beating `renew_lease` at a fraction of the lease; on exit the thread is stopped and
    joined. It settles nothing, reads nothing back into the ingest, and touches no state the
    pipeline can see.

    FOUR RULES THIS CLASS MUST KEEP, each argued in ADR 0031 §2 and §4 and deliberately not
    re-argued here - an argument with two writers is an argument that drifts:
      - ITS OWN CONNECTION, NEVER THE WORKER'S. The worker's is inside `ingest_file`'s transaction
        for the whole window a beat could land in, where a renewal silently publishes nothing
        (ADR 0025) or is loudly refused (ADR 0027) depending on a race.
      - FROM `gct.db.connect()`, never from a DSN reconstructed off another connection.
      - ONE CONNECTION PER BEAT, opened and closed inside the beat rather than held for the ingest.
      - A BEAT FAILURE IS LOGGED AND SWALLOWED, and no answer this thread receives settles
        anything. The doomed write a lost lease must actually stop is refused by ADR 0030's
        publish guard, inside `index_file`'s own transaction.

    Two facts about this code that no ADR carries:

    IT NEVER ENTERS `ingest_file`, WHICH IS WHAT KEEPS THE PM-4 SEAM INTACT (ADR 0020). The
    pipeline's signature, body and transaction are untouched by the heartbeat. An edit that gave a
    beat anything to do inside the pipeline would break that seam without changing a line under
    `src/gct/ingest/`, which is exactly the kind of break a reviewer of that directory cannot see.

    A DAEMON THREAD, so a beat blocked on an unresponsive database cannot keep a dying process
    alive. That is also why `__exit__` joins with a timeout rather than waiting forever.
    """

    def __init__(
        self,
        *,
        job: Job,
        lease_seconds: int,
        fraction: float = DEFAULT_HEARTBEAT_FRACTION,
        max_seconds: float = DEFAULT_HEARTBEAT_MAX_SECONDS,
    ) -> None:
        self._job = job
        self._lease_seconds = lease_seconds
        self._interval = heartbeat_interval(lease_seconds, fraction)
        self._max_seconds = max_seconds
        self._stop = threading.Event()
        self._beats = 0
        self._thread = threading.Thread(
            target=self._beat_until_stopped,
            name=f"lease-heartbeat-{job.job_id}",
            daemon=True,
        )

    @property
    def beats(self) -> int:
        """How many renewals actually reached the database. Read after `__exit__` has joined.

        Exists so a test can assert the MECHANISM ran rather than infer it from an outcome the
        `lease_seconds` alone could have produced.
        """
        return self._beats

    def __enter__(self) -> _LeaseHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self._stop.set()
        self._thread.join(timeout=_HEARTBEAT_JOIN_SECONDS)
        # False: this manager never swallows an exception. Every classification `process_one`
        # makes - terminal, transient, refused publish, shutdown - happens in the handlers around
        # this block, and a heartbeat that ate one would silently delete a `failed_reason` the
        # student was owed.
        return False

    def _beat_until_stopped(self) -> None:
        # `Event.wait` rather than `time.sleep`, and BEFORE the first beat rather than after:
        # waiting first means a job that finishes inside one interval - the overwhelming majority
        # of them - costs zero extra statements, and `wait` returning True is a stop request the
        # loop honours immediately instead of after a full interval of sleeping.
        deadline = time.monotonic() + self._max_seconds
        while not self._stop.wait(self._interval):
            if time.monotonic() >= deadline:
                # The cap. Stop beating and let the lease lapse on schedule: from here the job is
                # the reaper's, and the attempts budget bounds it exactly as it does without any
                # heartbeat at all. WARNING because a run reaching this is abnormal - it is either
                # wedged or slower than anyone has measured, and both are things to look at.
                logger.warning(
                    "job %s: lease heartbeat stopped at its %.0fs cap - the lease will lapse and "
                    "the reaper may hand this job to another worker while this one is still "
                    "running (its publish is then refused, ADR 0030)",
                    self._job.job_id,
                    self._max_seconds,
                )
                return
            try:
                beat_conn = connect()
                try:
                    held = renew_lease(
                        beat_conn,
                        job_id=self._job.job_id,
                        lease_token=self._job.lease_token,
                        lease_seconds=self._lease_seconds,
                    )
                finally:
                    beat_conn.close()
            except Exception:
                # Swallowed on purpose - see the class docstring. Counted as no beat, so the next
                # interval tries again; the fraction is what buys the margin to fail here.
                logger.warning(
                    "job %s: lease heartbeat failed to renew - retrying at the next beat",
                    self._job.job_id,
                    exc_info=True,
                )
                continue
            self._beats += 1
            if not held:
                # Someone else owns this job now (`renew_lease` returns False rather than
                # raising - it is the routine at-least-once outcome). Stop beating and settle
                # nothing: the ingest still running below will have its publish refused.
                logger.warning(
                    "job %s: lease heartbeat found the lease is no longer ours - stopping",
                    self._job.job_id,
                )
                return


def process_one(
    conn: psycopg.Connection,
    *,
    embedder: Embeddings,
    chunk_size: int,
    chunk_overlap: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    heartbeat_fraction: float = DEFAULT_HEARTBEAT_FRACTION,
    heartbeat_max_seconds: float = DEFAULT_HEARTBEAT_MAX_SECONDS,
) -> bool:
    """One tick of work: claim a job, ingest its file, and settle it - done, failed, or requeued.

    Returns False when the queue has nothing claimable (the normal idle tick - `run` sleeps),
    True when this call processed a job.

    The sequence, and why the order is the contract:
      1. `claim(conn, lease_seconds=...)` - commits before returning (queue.py's contract), so
         the connection comes back idle and the lease is already visible to other workers.
      2. Write `files.status = 'processing'` for the claimed file - this module's own SQL, the
         one worker-owned statement (see the ownership table above). Wrapped in its own
         `conn.transaction()` so it commits under EITHER wiring - autocommit or plain - and
         leaves the connection idle for step 3, the same shape as every `queue.py` writer.
      3. `ingest_file(job.staging_ref, ..., file_id=job.file_id, embedder=..., conn=conn,
         chunk_size=..., chunk_overlap=...)` - the UNCHANGED Slice 1 pipeline (PM-4 seam:
         wrap, do not rewrite). It publishes `ready` itself, inside the index transaction.
         Wrapped in `_LeaseHeartbeat`, which keeps this worker's lease alive from a separate
         thread on a separate connection while the pipeline runs (ADR 0031).
      4. `complete(conn, job_id=..., lease_token=job.lease_token)` - False means the lease
         was lost to another writer (`claim` handed the token out for exactly this check);
         handled rather than ignored, because `run`'s reaper is what creates the writer that
         takes it.

    Steps 2-4 only run if the job still HAS a budget: `job.attempts > max_attempts` is checked
    first, before the `processing` write, so an exhausted job is buried without a status
    flicker and without buying an embed it will never use. The module docstring carries why
    the counter is `jobs.attempts` and why this check is here rather than inside `claim`.

    Three ways a tick that claimed something can end, all returning True - the caller only asks
    "was there work", never "did it succeed":
      - `complete` - the pipeline returned;
      - `_bury` - terminal input (`ParseError`), or the budget was already spent;
      - `release` - transient failure with budget left, after the backoff is served.

    Returns True for a failed job on purpose: `run` sleeps only on an EMPTY tick, and a queue
    with a failing job in it is not idle - the next job should be claimed immediately.

    `chunk_size` / `chunk_overlap` are REQUIRED and forwarded verbatim to `ingest_file` -
    never hardcoded here. They are provisional spike parameters (ADR 0019) and Spike Pass 2
    still has a live chunking axis (ADR 0026); same discipline as `ask_smoke._converge_corpus`.

    An exception that is NEITHER `ParseError` nor `TransientEmbeddingError` still propagates,
    and the worker still crashes - unchanged, and deliberate. Classifying an unknown exception
    would mean guessing a `failed_reason` from the closed set, and the wrong reason shown to a
    student is worse than none. Crash-mid-processing committed nothing (ADR 0020), so there is
    zero cleanup, and the durable budget bounds the loop instead of the handler doing it.

    WHAT THE UNWIND NOW DOES ON ITS WAY OUT (issue #82). Everything after the claim runs under
    `_release_on_shutdown`, which hands the job back to `queued` before re-raising - so a Ctrl-C,
    a SIGTERM the caller routed onto this unwind, or an unclassified exception leaves the job
    CLAIMABLE AT ONCE rather than stranded under a live lease the reaper is written to refuse.
    It adds a cleanup write and changes nothing else: the exception still propagates and the
    worker still crashes. `run`'s reaper remains the backstop, and is the only recourse when no
    handler runs at all - SIGKILL, OOM, power loss.

    A DB error takes that path too, which reads as a deviation from ADR 0020 §1's list and is
    now the ratified answer (ADR 0020 §1, DB-blip class amended per ADR 0028). The ADR carries
    the argument and the revisit condition; it is not restated here.
    """

    job = claim(conn, lease_seconds=lease_seconds)

    if job is None:
        return False

    # THE SHUTDOWN GUARD (issue #82). Everything below runs while THIS worker holds the
    # lease, and `job.lease_token` - the proof needed to give it back - is a local of this
    # function and of nothing else. `scripts/worker.py` therefore cannot implement this
    # (ADR 0009: it may only route a signal onto the same unwind); the decision is here.
    #
    # `BaseException`, not `Exception`, and that is the entire point: `KeyboardInterrupt`
    # and `SystemExit` do not derive from `Exception`, and they are the two the guard exists
    # for. It changes NO control flow - `_release_on_shutdown` writes and returns, and the
    # `raise` re-raises the original - so ADR 0028 §4's stance stands unchanged: an
    # unclassified exception still crashes the worker, it just stops stranding its job first.
    #
    # IT OPENS ON THE STATEMENT AFTER THE `None` CHECK, not after the log line below, and the
    # boundary is the invariant rather than a tidiness preference: the lease is live from the
    # moment `claim` commits, so ANY statement outside this block is a window in which an
    # interrupt strands the job for the full lease - the exact defect this guard closes. The
    # log call is not a free statement; a handler formats, locks and writes to a stream, and a
    # signal can land in it. Pinned by `test_no_interrupt_after_the_claim_can_strand_the_job`,
    # which drives an interrupt through that one line: move the `try` back below it and the
    # job comes back `processing` under a live lease.
    try:
        # Deliberately nothing logged on the empty tick above: at a 2s poll that is 30 lines a
        # minute of "nothing happened", which buries the lines that matter.
        logger.info(
            "job %s: claimed file %s (attempt %s/%s)",
            job.job_id,
            job.file_id,
            job.attempts,
            max_attempts,
        )

        # The budget check, before ANY work and before the `processing` write. `attempts` counts
        # this claim (queue.py), so `>` rather than `>=`: at `attempts == max_attempts` this IS the
        # last permitted attempt and it gets to run. `transient_exhausted` is the only reason in the
        # closed set that means "we gave up after retrying", and it is written even when the
        # attempts were burned by CRASHES rather than caught 429s - the taxonomy has no reason for
        # "kept dying" (ADR 0020 §Open: a richer taxonomy is a clean V2 extension), and `last_error`
        # carries the detail that distinguishes them.
        if job.attempts > max_attempts:
            logger.warning(
                "job %s: retry budget spent (%s attempts) - failing file %s",
                job.job_id,
                max_attempts,
                job.file_id,
            )
            _bury(
                conn,
                job,
                reason="transient_exhausted",
                error=f"retry budget spent: {max_attempts} attempts made, none succeeded",
            )
            return True

        # Timed from the claim, not the ingest, because the LEASE covers claim->complete: this
        # duration is the evidence ADR 0028's lease number will be RE-chosen from, and `attempts`
        # above is the same for the retry budget. The ADR ratified both without measuring either,
        # and named these two log lines as what would move them.
        started = time.perf_counter()

        # Keyed on `file_id`, the primary key - not `staging_ref`, which is nullable and carries
        # no uniqueness constraint. `updated_at` is bumped by hand: `files` has no trigger.
        #
        # `status <> 'ready'` keeps the student-facing status monotonic. Under at-least-once a job
        # can be reclaimed a SECOND time after an earlier worker's `index_file` already published
        # `ready` - that worker's `complete` returned False, so the job never went `done`. Without
        # the guard, this claim flips a finished file back to `processing` in the UI.
        # `failed -> processing` IS permitted here, and that is load-bearing rather than an
        # accident. The retry path does not need it - a retryable failure never writes `failed` at
        # all, it leaves the file `processing` and requeues the job - but `_bury` does: it writes
        # `files` BEFORE `jobs` precisely so that a crash between the two leaves a `failed` file
        # under a job that is still claimable. The reaper requeues it, this line writes `processing`
        # back over `failed`, and a later attempt settles both axes. Tightening this guard to
        # exclude `failed` would strand exactly that file forever, which is the recovery `_bury`'s
        # write order was chosen to buy. Only `ready` is protected, because only `ready` is a
        # promise already made to the student.
        # (An earlier version of this comment called the transition UNREACHABLE, reasoning that a
        # `failed` file always has a terminally-failed job. That is true only when `_bury` completed
        # BOTH its writes - the crash window between them is the whole point of its ordering, and
        # `update files set status='processing' ... and status <> 'ready'` writes 1 row against a
        # `failed` row when you run it.)
        # Pinned by `test_a_terminal_failure_cannot_unpublish_a_file_that_is_already_ready`: drop
        # this guard and that test goes red. It reaches this line without going through the reaper
        # at all - it publishes `ready` directly, which is what a won run leaves behind however it
        # got there - and without the guard the file lands on `processing` here and `failed` in
        # `_bury`, whose own identical guard then sees `processing` rather than `ready`. So the two
        # guards are not redundant: this one is what keeps the other one's precondition true.
        #
        # `conn.transaction()` rather than a bare execute: a bare statement only commits if the
        # caller wired autocommit, and on a plain connection it would sit unpublished in the
        # implicit transaction - then `index_file` refuses the INTRANS connection AFTER the embed
        # was paid (ADR 0025). The block commits under either wiring, like every `queue.py`
        # writer. No `require_idle` here on purpose: `claim` just committed and nothing runs
        # between it and this statement, so the connection cannot be anything but idle.
        #
        # `failed_reason = null` RIDES THIS STATEMENT (issue #24). The column is the actionable
        # message F3 surfaces to the student (ADR 0020 §1), and it is written by exactly one
        # place - `_bury`, which sets it and never unsets it. Without the clear, a reason
        # outlives the attempt that produced it: `_bury` commits `files` before `jobs`, so the
        # `jobs` half can fail to settle (a crash between the two writes, or `fail` returning
        # False because the lease expired and the reaper already requeued - logged and
        # continued, no crash at all), the job stays claimable, and this line then flips
        # `failed -> processing` over a row still carrying `unparseable`. The student sees a
        # file mid-flight advertising the LAST attempt's failure, and if that attempt then
        # succeeds against changed bytes at `staging_ref`, a published `ready` file wearing it.
        #
        # WHY HERE AND NOT IN `index_file`'s `DO UPDATE`, which is the obvious other home and
        # was rejected: `_bury` is the only writer that SETS this column, so the worker owns
        # both halves of the fact, and `index_file` is deliberately pure of job/queue/retry
        # machinery (the PM-4 seam, ADR 0020). Clearing it there would make the publisher a
        # second writer for a column it knows nothing about. `index_file` therefore leaves
        # `failed_reason` alone on purpose - pinned by
        # `test_index_file_does_not_clear_failed_reason`.
        #
        # ONE STATEMENT, NOT TWO, and that is load-bearing: riding inside this UPDATE puts the
        # clear under the same `status <> 'ready'` guard. A separate unguarded
        # `update files set failed_reason = null` would wipe the reason off a `ready` file a
        # zombie is about to bury, and would widen the crash window between the two writes.
        #
        # WHAT THIS CLEAR CANNOT DO ALONE, and what does it alongside (#86). This statement
        # closes the SEQUENTIAL path - a bury, then a later claim - and nothing that happens
        # HERE can close the out-of-order one: a zombie whose lease expired reaching `_bury`
        # AFTER this write and BEFORE the `index_file` commit stamps `('failed', reason)` on the
        # row this run then republishes as `ready`, which is a bury this line has already run
        # past. #86 closed that end instead, with a lease guard on `_bury`'s files write - the
        # move this comment named, and NOT a second writer for `failed_reason` in `index_file`.
        # `('ready', <a reason>)` needed a THIRD guard, and it is not in this module's writes at
        # all: a bury that legitimately HOLDS its lease, followed by a publish from a worker whose
        # lease expired, reached it through `index_file`, which read no lease. `_publish_guard`
        # closed that by making the publish ask the same ownership question inside its own
        # transaction (#92, ADR 0030) - see `_bury`'s docstring for how the three partition.
        # `test_the_claim_does_not_clear_a_reason_on_a_file_that_is_already_ready` still sets its
        # row directly, and the reason is CONTROL - the subject there is this statement's shape.
        # Make no unreachability claim about that row on its behalf: two versions of its docstring
        # have made one and both were demonstrated false. Read it rather than restating it.
        with conn.transaction():
            conn.execute(
                """
                update files
                set status = 'processing',
                    failed_reason = null,
                    updated_at = now()
                where file_id = %(file_id)s::uuid
                  and status <> 'ready'
                """,
                {"file_id": job.file_id},
            )

        try:
            # THE HEARTBEAT WRAPS THE INGEST AND NOTHING ELSE (ADR 0031 §3), and where this `with`
            # ENDS is the load-bearing half of that: the BACKOFF below must stay outside it. Widen
            # this block to cover the retry path and a retry curve extends its own deadline, which
            # is the one thing `served_backoff`'s lease bound exists to stop - and ADR 0028 §2
            # stands unchanged only for as long as this boundary holds.
            with _LeaseHeartbeat(
                job=job,
                lease_seconds=lease_seconds,
                fraction=heartbeat_fraction,
                max_seconds=heartbeat_max_seconds,
            ):
                ingest_file(
                    job.staging_ref,
                    job.owner_id,
                    job.class_id,
                    file_id=job.file_id,
                    embedder=embedder,
                    conn=conn,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    publish_guard=_publish_guard(job),
                )
        except PublishRefused:
            # NOT A FAILURE OF THIS FILE, AND SO NOT A BURY (#92, ADR 0030). The guard refused
            # because the lease moved while the parse/chunk/embed above was running: the reaper
            # requeued this job and another worker owns it now. The work is finished and correct
            # and is thrown away, which is the routine at-least-once outcome ADR 0011 already
            # accepts - what changed is only that the duplicate run is now DISCARDED at the
            # publish rather than absorbed by it (`queue.reclaim_expired`, corrected).
            #
            # WRITE NOTHING AND SETTLE NOTHING. Both halves matter and for the same reason: this
            # job is not ours. `_bury` would be refused by its own lease guard, and `complete`
            # by `_settle`'s - so calling either would only produce a second "lease lost" log
            # for a fact this branch already knows. The owner settles it.
            #
            # `True` because a tick DID happen - the return says "there was work", not "the work
            # succeeded" (`process_one`'s docstring). Returning False would idle a poller that
            # has a full queue in front of it.
            logger.warning(
                "job %s: lease lost during ingest after %.1fs (lease was %ss) - "
                "publish refused, nothing written; another worker owns this job",
                job.job_id,
                time.perf_counter() - started,
                lease_seconds,
            )
            return True
        except ParseError as exc:
            # TERMINAL: bad input (ADR 0020 §1). Zero retries - the file is exactly as corrupt on
            # the next attempt, and the budget exists for bad luck, not bad bytes. `exc.reason` is
            # already one of `files.failed_reason`'s legal values, so it passes straight through.
            # Note this DID cost one `attempts` bump, from the claim; that is the trail of what
            # happened, not a retry spent - nothing will claim this job again.
            logger.warning(
                "job %s: terminal failure (%s) after %.1fs - %s",
                job.job_id,
                exc.reason,
                time.perf_counter() - started,
                exc,
            )
            _bury(conn, job, reason=exc.reason, error=f"{exc.reason}: {exc}")
            return True
        except TransientEmbeddingError as exc:
            # TRANSIENT: bad luck, and a NARROWER class than ADR 0020 §1 first drew - a DB blip is
            # not in it (ADR 0020 §1, DB-blip class amended per ADR 0028). Nothing was committed -
            # the index transaction never opened (ADR 0020 §3) - so the job goes back to `queued`
            # with no cleanup at all, and
            # `files.status` stays `processing`, which is still TRUE: the file is mid-flight and the
            # student has nothing new to learn from a status flicker. Only budget exhaustion moves
            # it to `failed`.
            #
            # THE BACKOFF IS SERVED BEFORE THE RELEASE, while this worker still holds the lease, so
            # the delay applies to every worker and not just to this one's next poll (see
            # `release`'s docstring). How long it is - the curve, the lease budget, and the
            # last-attempt zero - is `served_backoff`'s, which carries the argument for all three.
            #
            # ACCEPTED COST: this worker is blocked for the delay, so one flaky file holds up every
            # other queued file for up to BACKOFF_MAX_SECONDS. That is a real head-of-line block,
            # and it is affordable only because V1 is one worker serving one user (ADR 0011) with a
            # cap measured in seconds. Worker concurrency (`ingestion-worker.md` §Open/deferred) is
            # what makes it stop being affordable - at which point the delay wants to live in the
            # database (a `visible_after` column claim filters on) rather than in a sleeping
            # process. Recorded so that change is a decision someone makes, not a bug someone finds.
            elapsed = time.perf_counter() - started
            wanted, delay = served_backoff(
                job.attempts,
                max_attempts=max_attempts,
                lease_seconds=lease_seconds,
                elapsed=elapsed,
            )
            # The outcomes say OPPOSITE things and must not share a message - and WHICH ONE THIS IS
            # COMES FROM THE BUDGET, NEVER FROM `delay`. A zero delay has two causes and only one of
            # them is "this was the last attempt": `served_backoff` also clamps a perfectly ordinary
            # retry's wait to zero when the lease has no room left for it. Inferring the bury from
            # `delay == 0` therefore announced "no retry left, the next claim buries it" over runs
            # that went on to be requeued, retried and published `ready` - an edge case before
            # ADR 0031 and a ROUTINE one after it, because a renewed lease makes `elapsed >
            # lease_seconds` an everyday fact about a slow file rather than a symptom.
            # `job.attempts` and `max_attempts` are the actual deciders (the budget check above uses
            # exactly this comparison); ask them, and let `delay` say only how long the wait is.
            retry_left = job.attempts < max_attempts
            if not retry_left:
                outcome = "no retry left, the next claim buries it"
            elif delay:
                outcome = f"retrying in {delay:.0f}s"
            else:
                outcome = "retrying with no backoff - the lease left no room for one"
            logger.warning(
                "job %s: transient failure on attempt %s/%s after %.1fs - %s - %s",
                job.job_id,
                job.attempts,
                max_attempts,
                elapsed,
                outcome,
                exc,
            )
            if delay < wanted:
                # Reported rather than absorbed: a cut backoff means this attempt ate a lease that
                # was meant to cover it, which is evidence the lease number is wrong - exactly the
                # measurement ADR 0028 §2 says would move it. Silently clamping would swallow it.
                logger.warning(
                    "job %s: backoff cut from %.0fs to %.0fs - the %ss lease could not cover it",
                    job.job_id,
                    wanted,
                    delay,
                    lease_seconds,
                )
            if delay:
                time.sleep(delay)
            if not release(
                conn, job_id=job.job_id, lease_token=job.lease_token, error=f"transient: {exc}"
            ):
                logger.warning(
                    "job %s: lease lost before it could be requeued - another worker owns it now",
                    job.job_id,
                )
            return True

        elapsed = time.perf_counter() - started
        if complete(conn, job_id=job.job_id, lease_token=job.lease_token):
            logger.info("job %s: done in %.1fs", job.job_id, elapsed)
        else:
            # The lease was too short for this file - the one signal that says so. `elapsed`
            # against `lease_seconds` is the whole diagnosis.
            logger.warning(
                "job %s: lease lost after %.1fs (lease was %ss) - another worker owns it now",
                job.job_id,
                elapsed,
                lease_seconds,
            )

        return True
    except BaseException as exc:
        _release_on_shutdown(conn, job, exc)
        raise


def run(
    conn: psycopg.Connection,
    *,
    embedder: Embeddings,
    chunk_size: int,
    chunk_overlap: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    heartbeat_fraction: float = DEFAULT_HEARTBEAT_FRACTION,
    heartbeat_max_seconds: float = DEFAULT_HEARTBEAT_MAX_SECONDS,
) -> None:
    """The poll loop: reap, then `process_one`, sleeping `poll_seconds` after each empty tick.

    A tick that DID process a job polls again immediately - a non-empty queue means there is
    likely more work, and sleeping between real jobs just adds latency the student sees.

    THE REAPER RUNS ON EVERY TICK, BEFORE THE CLAIM (ADR 0011 addendum; cadence, and the
    argument for it, ADR 0028 §5). Every tick rather than only the idle one, and ahead of the
    claim rather than after it - both are the ADR's reasoning, not restated here.

    What the ADR cannot say, because it is a fact about this file: the reaper lives in `run`
    rather than in `process_one` because a dozen tests call `process_one` directly, and a reap
    inside it would silently collect the expired leases those tests construct. §5's safety
    argument also rests on a property of THIS loop - single-threaded, holding a lease only
    between `claim` and its settle verb, both of them inside `process_one` along with the
    backoff sleep - so an edit that makes this loop concurrent invalidates the ADR, not just
    this comment.

    A reap of ZERO is not logged. At a 2s poll that is 30 lines a minute of "nothing happened",
    the same argument that keeps the empty tick in `process_one` silent. A non-zero reap IS
    logged at WARNING because a reclaim is abnormal - it means a worker died without settling
    its job - and because it must survive a caller that configures no logging at all: Python's
    unconfigured root drops INFO. `scripts/worker.py` does not, which is why the INFO lines in
    `process_one` are worth emitting; a library cannot assume that wiring.

    It is NOT evidence that the lease is too short (ADR 0028 §Consequences): a single worker
    that overruns its lease still completes, because the settle verbs guard on state and token
    and never on `leased_until`. A reap means something died - and since ADR 0031 that reading
    holds against a SECOND worker too, where it did not before: a live run now renews its own
    lease, so a reaped job is one whose worker stopped beating (dead, or wedged past the
    heartbeat cap) rather than one that was merely slow.

    A raising reaper crashes the loop, on purpose and without a handler - the module's stance on
    every unclassified exception, ratified for a DB error specifically by ADR 0028 §4. Catching
    here would quietly contradict it.
    """

    logger.info(
        "worker started: poll %.1fs, lease %ss renewed every %.0fs up to %.0fs, budget %s attempts",
        poll_seconds,
        lease_seconds,
        heartbeat_interval(lease_seconds, heartbeat_fraction),
        heartbeat_max_seconds,
        max_attempts,
    )
    while True:
        reclaimed = reclaim_expired(conn)
        if reclaimed:
            logger.warning(
                "reaper: %s job(s) whose lease had elapsed are claimable again - a worker died "
                "in a way that ran no handler (SIGKILL, OOM, power loss); a stopped or crashed "
                "one hands its own job back (issue #82). Their files keep whatever status that "
                "run reached until some later claim moves them",
                reclaimed,
            )
        if not process_one(
            conn,
            embedder=embedder,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
            heartbeat_fraction=heartbeat_fraction,
            heartbeat_max_seconds=heartbeat_max_seconds,
        ):
            time.sleep(poll_seconds)
