# 0031. A slow file is not a failing file — the lease is renewed by a live worker, under a cap

- **Date:** 2026-09-02
- **Status:** accepted — amends **ADR 0028** (its §1 lease row: the *value* is unchanged at 15
  minutes, but what the number MEANS narrows — it is no longer the outer bound on one attempt, it
  is how long a SILENT worker may hold a job before it is presumed dead, and its safe-if-wrong
  direction changes with it. The table also gains two rows, so "the four numbers" is now six. §1's
  budget, backoff and poll numbers, §2's lease bound on the backoff, §3's accepted head-of-line
  cost, §4's DB-blip class and §5's reaper cadence all stand unchanged — §2 deliberately so, see
  §3 below)

> ADR 0020's own status line is NOT extended by this ADR, and that is a finding rather than an
> omission. §1's retryable/terminal split never classified a lease overrun at all: the budget was
> being spent by *machinery*, not by either class, which is why removing that spend needs no
> change to the taxonomy. ADR 0030's publish-entitlement guard is likewise untouched and is
> **relied on** — see §4.

## Context

`claim` bumps `jobs.attempts`, and `reclaim_expired` deliberately does not reset it: that pairing
is what bounds a poison file and a crash loop with one durable counter, and both docstrings argue
for it. It has a consequence neither of them noticed. An ingest that merely **outlasts its lease**
is reaped mid-flight, and once ADR 0030 landed its publish is *refused* rather than absorbed — so
the attempt ends having bought a full parse/chunk/embed run, written nothing, and spent one of
five attempts. Nothing distinguishes that from a genuine transient failure, because the counter it
spends is the same counter.

Measured on `main` before this ADR: a 6 s ingest under a 2 s lease is reclaimed mid-flight, its
publish refused, `chunks = 0`, one attempt gone. A file that is **always** slower than its lease is
therefore buried as `transient_exhausted` after six cycles, having paid for the ingest six times
and never once failed. The student is told their file could not be processed after repeated
attempts. That is a false statement about their file, which makes it a trust defect and not a
throughput one — the product is the honesty of the answer (`vision.md`), and this is the same class
of harm as a `ready` file wearing a failure reason (ADR 0030).

The defect needs a **second reaper** to be reachable, which is why nothing caught it: ADR 0028 §5
puts the reaper on the same thread as `process_one`, so V1's single worker cannot reap its own
in-flight job, and ADR 0028 §Consequences records that a lease overrun therefore shows up as a cut
backoff rather than as a reap. `claim`'s `SKIP LOCKED` exists for the deployment where that stops
being true, and ADR 0028 §1 chooses the lease *for* that deployment. So the defect is latent on
today's shape and live on the shape the numbers were chosen for.

The root cause is that **an expired lease was overloaded**. It meant both "the worker is gone" and
"the worker is slower than one lease", and only the first deserves a reclaim. The lease had no way
to tell them apart because a lease is a promise made once, at claim, by a worker that did not yet
know how long its file would take.

## Decision

### 1. A live worker renews its own lease

`queue.renew_lease(conn, *, job_id, lease_token, lease_seconds) -> bool` pushes `leased_until`
forward, behind **`_settle`'s two conditions verbatim** — `state = 'processing'` AND
`lease_token`. The token half is more load-bearing here than in any settle verb, because this is
the only write that grants a lease *more life*: without it a worker whose job was already
re-handed out would renew onto the run that now holds it, a dead claim extending a live one
indefinitely. `attempts` and `lease_token` are untouched; a renewal is not a claim.

It returns `False` rather than raising for **every** answer that is not "renewed", including an
unknown `job_id` — the one place the shape departs from `complete`/`fail`/`release`. Those raise
`LookupError` on an orphan because a caller believing in a job that does not exist is a
programming error. The caller here is a background beat whose only correct response to anything
but `True` is to stop beating, so splitting the answers would buy a traceback across a thread
boundary that no one can act on.

An expired lease consequently means **the worker is gone**, not "the worker might merely be slow".
`reclaim_expired`'s statement is unchanged, and the rows it takes are a strict subset of the rows
it took before.

### 2. The beats run on their own connection, from `gct.db.connect()`

`worker._LeaseHeartbeat` is a daemon thread wrapping the `ingest_file` call. It **must not** use
the worker's connection: that connection is inside `ingest_file`'s transaction for the whole
window a beat could land in, where `renew_lease`'s `conn.transaction()` degrades to a SAVEPOINT
that publishes nothing while returning `True` (ADR 0025) — the lease would silently stop being
renewed at the exact moment the mechanism exists to renew it. `require_idle` turns that into a
loud refusal (ADR 0027), but only if the beat lands while psycopg has the connection marked
INTRANS, so sharing the connection leaves a race deciding whether the bug is loud or silent.
Neither outcome is survivable, and neither is a thing to be careful about: a separate connection
removes the shared object rather than the temptation. It is also what keeps the PM-4 seam intact —
the thread never enters `ingest_file`, whose signature, body and transaction are unchanged.

The connection comes from **`gct.db.connect()`**, not from a DSN derived from the worker's
connection. `conn.info.dsn` **redacts the password**, so a heartbeat that rebuilt its DSN from it
would beat correctly on every passwordless local box and fail to authenticate on the first
password-authenticated deployment — beating never, lapsing every lease, and reintroducing this
ADR's defect in exactly the environment no test covers (V2 is Supabase, ADR 0006). One connection
is opened and closed **per beat**, so a worker holds a second connection for the length of one
tiny `UPDATE` rather than for the length of an ingest, and a beat recovers on its own from a
database restart that would have killed a cached handle.

### 3. It wraps the ingest and nothing else

Everything else between the claim and the settle verb is a single fast statement. The exception is
the **backoff**, which is deliberately outside: it is served while still holding the lease so that
the delay binds every worker and not just this one's next poll, and `served_backoff` bounds it by
the lease for that reason (ADR 0028 §2). Beating through the backoff would let a retry curve
extend its own deadline, which is the one thing that bound exists to stop. ADR 0028 §2 therefore
stands unchanged **because** the heartbeat stops at this boundary, not by coincidence.

### 4. The beats stop at a cap, and settle nothing

A beat proves the process is **alive**; it cannot prove the process is making **progress**. Wedged
and working are indistinguishable from the outside, so the beats stop after a bounded total and
the lease is allowed to lapse on schedule. That returns a wedged worker to the reaper and to the
ordinary attempts budget — the same bound a run with no heartbeat at all has always had. The cap
is why this is a heartbeat and not simply a longer lease.

Two things the heartbeat explicitly does **not** do. It never settles a job: when a beat finds the
lease gone it stops, and **ADR 0030's publish guard** is what refuses the doomed write, from inside
`index_file`'s own transaction where the answer cannot go stale before it is used. And a beat
failure is logged and swallowed — nothing this thread does may kill the worker or the ingest, and
the fraction below is what buys the margin to fail.

### 5. The two numbers

| number | value | constant / parameter | safe-if-wrong direction |
|---|---|---|---|
| **beat interval** | ¼ of the lease | `DEFAULT_HEARTBEAT_FRACTION` / `heartbeat_fraction` | errs **short**. A fraction rather than an absolute interval, so a caller that shortens `lease_seconds` shortens the beat with it and the two cannot drift apart. At a quarter, **three consecutive beats must fail** before the lease lapses, so a DB blip mid-ingest is not an expiry. Too frequent costs one tiny `UPDATE` per interval, which is nothing against local Postgres serving one user; too infrequent lets a blip lapse a live worker's lease, which is this ADR's defect returning. |
| **heartbeat cap** | 1 hour | `DEFAULT_HEARTBEAT_MAX_SECONDS` / `heartbeat_max_seconds` | errs **long**. Too short reintroduces the defect for every file slower than the cap — the expensive direction. Too long only delays recovery from a wedge that V1 cannot recover from anyway: one worker means the wedged process **is** the poller, so no reaper is running to collect what the cap gives up (ADR 0011). One hour is four leases, and far longer than any input under the ADR 0029 word ceiling has taken. |

**Both are parameters, not only constants** — `process_one` and `run` take them and forward them,
the same discipline `lease_seconds` and `max_attempts` already have, so the tests can drive the
mechanism at sub-second scale without a module-global to reset.

**NOTHING HAS MEASURED EITHER**, and this ADR ratifies them anyway on ADR 0028's own terms: chosen
safe-if-wrong, with the evidence that would move them named. That evidence is the worker's
existing duration lines — `done in %.1fs`, and the `lease lost after %.1fs` warning — which is the
same evidence ADR 0028 §Consequences named for the lease itself. Ratified is not measured.

## Alternatives considered

- **A `lease_overruns` counter** — a second column, bumped by the reaper, so a job reclaimed while
  its worker was still alive does not spend an `attempts`. **This was the issue's own first
  candidate, and it was measured before it was rejected**, which is why it is recorded here: no
  other document carries it, and it reads plausible right up to the point where you run it. It does
  not clear the acceptance line at all. Separating the counters changes *only how the failure is
  counted*, not what happens: the reaper still takes the job mid-ingest, the publish is still
  refused (ADR 0030), and the finished parse/chunk/embed is still thrown away on **every** cycle —
  `chunks = 0`, exactly as before. What it buys is more cycles of that. "Buried after 6 attempts"
  becomes "buried after 22 attempts and roughly 4x the compute", and the file still never becomes
  queryable, because nothing in the design ever lets an ingest outlive one lease. It also costs a
  migration and a rewrite of seven tests that deliberately pin `attempts` surviving a reclaim —
  paying a schema change and the loss of pinned invariants to make a wrong outcome slower.
- **A longer lease** — the cheapest-looking answer, and it moves the threshold instead of removing
  it. Whatever number is picked, a file slower than it is still buried; and the number large enough
  to be safe for the slowest legal input is the number that makes a genuine death undetectable for
  that long. Erring long is already ADR 0028 §1's choice and it is *this defect's* precondition,
  not its cure.
- **An uncapped heartbeat** — rejected in §4. A wedged-but-alive process beats forever, its job
  never returns to the queue, and the attempts budget — the durable bound the worker module docstring
  says exists precisely for deaths that run no handler — stops applying to the one failure mode a
  heartbeat introduces.
- **Stop bumping `attempts` in `claim`; bump it only when an attempt actually fails** — rejected
  for the reason the worker module docstring already gives. The failure that most needs bounding is
  the one where the worker DIES mid-job and no in-process handler runs; a bump that lives in a
  failure handler is a bump that a crash skips, and a poison file is then re-handed out forever.
- **Reset `attempts` in `reclaim_expired`** — rejected, and `reclaim_expired`'s docstring already
  says why: it hands a poison file a fresh budget after every crash. It is the same
  "stop counting the machinery's spend" instinct as the counter above, in its most dangerous form.
- **A shorter poll / a dedicated reaper cadence** — not an answer to this at all; the reaper is not
  the thing that is wrong. Noted only because the reaper is where the symptom appears.

## Consequences

- `queue.py` gains a fifth writer, so
  `test_every_writer_refuses_a_connection_already_in_a_transaction` — whose docstring calls itself a
  census rather than a sample — gains a row. The ADR 0025 precondition is satisfied by
  construction here, since the beat opens its own idle connection.
- **A worker briefly holds TWO connections** while it ingests. Free on local Postgres for one user;
  it is a real number under a connection-limited V2 (ADR 0006), and per-beat rather than per-ingest
  is what keeps it brief.
- `ingestion-worker.md` §Failure modes gains an **ingest slower than its lease** row, and §Position
  is amended where it described the reaper as collecting a job "stuck in `processing` past
  timeout" — that description is now about a *silent* worker.
- The reaper's WARNING line gets *stronger*, not weaker. ADR 0028 §Consequences argued that on V1 a
  non-zero reap means a process died, resting on the single-threaded loop; that conclusion now
  holds against a second worker too, because a live run renews its own lease. A reaped job is one
  whose worker stopped beating — dead, or wedged past the cap.
- **A known interaction, named rather than fixed.** `served_backoff` bounds the backoff by
  `lease_seconds - elapsed` measured from the *claim*, so for a run whose lease has been renewed it
  now **understates** the lease actually remaining, and clamps the backoff further than it needs
  to. Behaviour is unchanged — neither of its two inputs is touched by the heartbeat, so the
  trigger set is identical to `main`'s — but the "backoff cut" WARNING that ADR 0028 §Consequences
  names as the evidence that would shorten the lease becomes a **less reliable** signal, because a
  slow file can now legitimately reach `elapsed > lease_seconds` and still hold a live lease.
  Teaching `served_backoff` about the renewal is a second decision and is deliberately not taken
  here; whoever reads that warning as lease evidence should read this bullet first.
- What would move the two numbers: the **interval**, a `heartbeat failed to renew` warning
  appearing in clusters rather than singly — that is the margin being spent. The **cap**, a
  `heartbeat stopped at its cap` warning on a file that was healthy, which says the cap is shorter
  than a legitimate ingest. Both report themselves, unlike ADR 0028's poll number.
