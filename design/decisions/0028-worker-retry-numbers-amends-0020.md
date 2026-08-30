# 0028. The worker's four retry numbers, the reaper's cadence, and what a DB blip is — amending ADR 0020

- **Date:** 2026-08-29
- **Status:** accepted — amends **ADR 0020** (its §1 classification of a DB blip as *transient*,
  and its §1 reference to "the ADR 0011 budget", which named an ADR that holds no budget; §1's
  terminal/bad-input half, §2's all-or-nothing replace and §3's index-write-only boundary all
  stand unchanged)

> §§1–3 ratify numbers and rules the code already ran on; **§4 is the amendment**; **§5 is a new
> decision** — the reaper's cadence, which ADR 0011's addendum located on the poll loop without
> fixing how often. §5 is the authority `run` and its tests cite, so it is named here rather than
> left to be found.

## Context
ADR 0011 chose the substrate — a DB-backed `jobs` table claimed by an in-process poll worker —
and **deliberately deferred the numbers**: its §Consequences hands "visibility timeout / lease,
idempotent retry" to Phase 3. ADR 0020 §1 then wrote "retry with backoff up to **the ADR 0011
budget**", and `ingestion-worker.md` §4 and `data-model.md`'s `jobs.attempts` row copied that
phrase. Three documents therefore cited a budget that no document held. Issue #71 names this
exactly: *"a spec pointing at an ADR for a fact it does not hold; picking numbers without
closing it leaves the next reader following a citation to nothing."*

Slice 2 has since shipped the machinery those numbers govern — `jobs.attempts` and
`jobs.leased_until` (#70), the poll loop (#71 PR 1), the retryable/terminal split and the
durable budget (#71 PR 2) — with all four numbers as module constants in `gct/jobs/worker.py`
carrying a comment that they are provisional until this ADR. This is that ADR: the numbers stop
being provisional, and the citation stops dangling.

**Nothing here was measured when these numbers were chosen.** No file had been ingested
through the worker against a real provider with timing recorded. Every number below is chosen
to be *safe if wrong* in the direction that costs least, and each says which direction that is.
Ratifying an unmeasured number is not the same as claiming it is right — it is naming the
current answer so that the next reader argues with a decision instead of guessing at a constant.

> **Amended 2026-08-30 (#43/#82):** a first throughput measurement now exists — see
> `eval/FINDINGS.md` → *2026-08-30 — the first ingest throughput measurement*, which stays the
> writer of the figures. **It moves none of the four numbers below**, and is recorded here only
> so this section stops asserting that no measurement exists. One file at the ingest ceiling is
> a data point, not the evidence §Consequences names for re-choosing a number.

## Decision

### 1. The four numbers

| number | value | constant | safe-if-wrong direction |
|---|---|---|---|
| **lease** | 15 min | `DEFAULT_LEASE_SECONDS` | errs **long**. Too short reclaims a job a healthy worker is still ingesting and pays the embedding bill twice — **once a SECOND worker exists**, which `claim`'s `SKIP LOCKED` is built for even though V1 deploys one (§Alternatives considered → *A shorter lease* says what a short lease costs the single-worker case, which is much less). Too long only delays reclaim after a worker dies. |
| **retry budget** | 5 attempts that run | `DEFAULT_MAX_ATTEMPTS` | errs **generous**. Too few gives up on a provider having a bad minute and shows the student `transient_exhausted` for a file that was fine; too many only spends more time and more embed calls on a file that was going to fail anyway. Counted in `jobs.attempts`, so a crash costs an attempt exactly as a caught 429 does. |
| **backoff** | `min(2 · 2^(n-1), 60)` s | `BACKOFF_BASE_SECONDS` / `BACKOFF_MAX_SECONDS` | errs **short**. Too short re-asks a provider that is still busy — it wastes an attempt but breaks nothing; too long burns lease the attempt needed (§2) and, uncapped, stops being a delay at all. |
| **poll** | 2 s | `DEFAULT_POLL_SECONDS` | errs **fast**. Too fast costs idle queries, which are free against local Postgres for one user; too slow is latency the student feels on every upload with nothing to say why. |

**The budget is five attempts that RUN, and a sixth claim always happens on a job doomed by
TRANSIENT failures.** (A terminal failure gets one claim and no budget at all — ADR 0020 §1's terminal half,
which this ADR leaves standing.)
`process_one` checks `job.attempts > max_attempts` against the post-bump value `claim` returns,
so attempts 1–5 execute the pipeline and the sixth claim exists only to write
`files.status='failed'` + `failed_reason='transient_exhausted'`. That claim is not a wasted
retry — it is the *only* way the file can be buried, because `queue.py` may not touch `files`
and burying requires holding the lease. It buys no embed. Pinned by
`test_the_budget_runs_out_and_the_file_fails_transient_exhausted`, which asserts
`len(embedder.calls) == max_attempts` alongside `attempts == max_attempts + 1`.

### 2. The backoff is bounded by the lease it is served under
The worker sleeps **while holding the lease** — deliberately, because that is what holds every
*other* worker off the row rather than merely delaying this one's next poll. A delay that could
outlast the lease would let the reaper hand the job to a second worker mid-wait: two runs, one
file, two embedding bills, which is the exact double-charge the lease exists to prevent.

`BACKOFF_MAX_SECONDS` sits far under `DEFAULT_LEASE_SECONDS`, but that is a coincidence of the
defaults — `lease_seconds` is a parameter and the curve is module constants, so they cannot see
each other. The rule is therefore **enforced, not asserted**: `served_backoff` bounds the wait
by the *remaining* lease, halved to absorb the claim→measure gap, the `release` write and clock
skew against the server whose clock stamped the lease. Measured at `lease_seconds=5`, where the
unclamped curve slept 16 s under a 5 s lease.

**A cut backoff is logged, and the log line is evidence against this ADR's lease number.** It
means an attempt ate a lease that was meant to cover it. Silently clamping would swallow the
one measurement that would move the number.

### 3. Head-of-line blocking is an accepted V1 cost
Serving the backoff in-process blocks the worker, so one flaky file delays every other queued
file by up to `BACKOFF_MAX_SECONDS`. That is affordable **only** because V1 is one worker
serving one user with a cap measured in seconds. Worker concurrency is what makes it stop being
affordable — at which point the delay wants to live in the database, as a `visible_after` column
the claim filters on, rather than in a sleeping process.

### 4. A DB blip is NOT transient in V1 — it takes the crash path
This is the amendment. ADR 0020 §1 lists "DB blip" among the retryable class alongside provider
429 / timeout / 5xx. The worker does not implement it that way, and should not: **nothing in the
corpus classifies psycopg errors**, and an `except` broad enough to catch a connection blip
would absorb every programming error alongside it — a `TypeError` in the pipeline would land in
front of a student as `transient_exhausted`, which is precisely the wrong-cause failure ADR
0020 §1's own "actionable reason" requirement forbids.

So a DB error propagates and the worker crashes, deliberately. That is safe because §2 and §3
stand unchanged: the run committed nothing, so there is zero cleanup. The lease expires, the
reaper requeues the job (§5), and the durable `attempts` budget bounds the resulting crash loop
rather than a handler doing it.

The revisit condition is **a psycopg error taxonomy** — something that can tell "the connection
dropped" from "this code is wrong" — not a wider `except`.

### 5. The reaper runs on every poll tick
ADR 0011's addendum says the reaper runs on the worker's poll loop; this fixes the cadence at
*every* iteration, before the claim, rather than only on an idle tick. Reaping only when the
queue looks empty would make reclaim latency **unbounded under sustained load**: a job stranded
by a crash would sit in `processing` behind every queued file, with the student's status frozen
and no reason shown. The saving is nil — one indexed UPDATE against
`jobs_state_lease_idx (state, leased_until)`, matching zero rows on a normal tick — so the
optimization costs a guarantee and buys nothing.

Reaping before the claim is what lets a job stranded by the previous crash be picked up by the
*same* tick rather than the next one.

**This cadence is safe because V1 has one single-threaded worker**, which holds a lease only
between `claim` and its settle verb — both inside `process_one`, as is the backoff sleep. The
reaper therefore only ever runs at an instant when this worker holds nothing. That is a property
of the topology, not of the reaper, and it is the first thing to re-examine under concurrency.

## Alternatives considered
- **Measure first, ratify later** — rejected. The numbers are already running in shipped code;
  leaving them formally un-ratified is what produced the dangling citation this ADR closes, and
  a constant nobody has decided on is harder to argue with than one that has been written down
  wrong. The logging in §2 is what turns the next run into evidence.
- **A shorter lease** (say 60 s, "long enough for a small file") — rejected. **On a second
  worker** the failure it causes is invisible and expensive: the reaper reclaims a job a
  healthy worker is still embedding, and the all-or-nothing replace means the duplicate run
  *corrupts nothing* and *fails no test* — it just bills OpenAI twice. Erring long makes that
  failure a delay instead. On today's single worker the cost is smaller and different: a lease
  short enough to bite shows up as a **cut backoff** (§2), never as a double bill, because the
  reaper cannot run while `process_one` holds the loop.
- **Jitter on the backoff** — rejected for V1. Jitter de-synchronises a fleet retrying in
  lockstep; one worker has nothing to de-synchronise from, so it would add a moving part with no
  failure to prevent. Revisit with concurrency.
- **A `visible_after` column instead of the in-process sleep** — the right answer under
  concurrency (§3), rejected now: it adds a migration, a claim-filter change and a second notion
  of "when is this job runnable" to buy back a head-of-line delay that one user cannot feel.
- **Catching psycopg errors as transient**, the literal reading of ADR 0020 §1 — rejected in §4.
- **Reaping on a timer** (every N seconds, independent of the poll) — rejected: a second cadence
  to reason about and to configure, for a query whose cost is already nil at the poll cadence.

## Consequences
- `ingestion-worker.md` §4 and §Failure modes, and `data-model.md`'s `jobs.attempts` row, now
  cite **this** ADR for the budget. ADR 0020 §1's own "ADR 0011 budget" phrase is closed by this
  ADR's status-line back-pointer, which is the sanctioned repair — ADRs are not edited in place.
- `ingestion-worker.md` §Failure modes gains a **DB connection error** row, because §4 above is a
  taxonomy decision and that table is the taxonomy's spec-side writer (ADR 0020 §Consequences:
  "feeds `components/ingestion-worker.md`").
- The four constants in `gct/jobs/worker.py` stop being provisional and cite this ADR. Their
  *values* are unchanged by ratification — this ADR moves no number.
- **A stopped worker hands its job back; only a death that runs no handler still pays the
  15 minutes.** This bullet originally recorded the opposite — that stopping the worker
  mid-ingest left the job `processing` under a lease up to 15 minutes ahead, unreachable by the
  reaper's `leased_until < now()`, and that the fix was named but not built. Issue #82 built it:
  `process_one` wraps everything after the claim in a guard that `release`s the in-flight job and
  **re-raises**, and `scripts/worker.py` routes SIGTERM onto the same unwind. A Ctrl-C, a `kill`,
  or an unclassified exception therefore leaves the job claimable at once. Written here rather
  than in a new ADR because **no decision in this one moved**: §1's lease is unchanged, §4's
  "unclassified exceptions crash the worker" stance survives a guard that only adds a cleanup
  write on the way out, and §5's reaper is unchanged and still the backstop. What stays uncovered
  is the class that runs no handler at all — `SIGKILL`, an OOM kill, a power cut — which still
  pays the full lease. Two consequences worth naming: a job released this way can be one whose
  `index_file` already committed, so **`ready` is reachable under a *released* job** exactly as
  the next bullet says it is under a reaped one — the duplicate embed that costs is the same one
  the reaper paid fifteen minutes later, not a new class of cost, which is why the release is
  unconditional rather than branching on whether the pipeline returned; and a shutdown spends one
  `attempts` exactly as a crash or a caught 429 does (§1), so the durable budget bounds a restart
  loop with no special case to invent.
- What would move each number, and where the evidence comes from:
  - **lease** — a completion log whose `elapsed` approaches `lease_seconds`, or a **"backoff
    cut" warning**. NOT the reaper log, and §5 is why: this worker never reaps its own
    in-flight job — not merely while its lease is live, which is vacuous (`leased_until <
    now()` matches no live lease, for anyone), but even once that lease has EXPIRED, because
    the reap and the job run on one thread and `process_one` holds it. `complete`/`fail`/
    `release` then guard on state and token but never on `leased_until`,
    so a single worker that overruns its lease still finishes normally and leaves the reaper
    nothing to find. On V1 a non-zero reap means a process DIED — it cannot mean a healthy run
    outran its lease. What it does argue is the opposite of "too short": the reap can only land
    once the whole lease has elapsed, so every reaper line measures how long the JOB sat
    unclaimable after a crash. Not necessarily how long the student waited, because the reap
    says nothing about `files.status` — it is a single `update jobs`. All four legal statuses
    are reachable under a reaped job, depending on where the run died: `queued` before
    `process_one`'s status write, `processing` after it, `failed` inside `_bury`'s two-write
    window, and **`ready`** if the crash landed between `index_file`'s commit and `complete` —
    the case the `status <> 'ready'` guards in `process_one` and `_bury` exist for. In that
    last one the student waited for nothing: the file was queryable the whole time. That is the
    shutdown bullet above, arriving as a log line — and since #82, a reaper line of that shape
    can no longer be a Ctrl-C or a `kill`, because those hand the job back themselves. It now
    means a death that ran no handler.
  - **budget** — a file that exhausts it on genuine 429s rather than on crashes.
  - **backoff** — the same "backoff cut" warning, which says the curve did not fit its lease.
  - **poll** — nothing in-process, by construction: the cost of a slow poll is latency a student
    feels and the worker cannot see. Its evidence is a report, not a log line.

  Three of the four therefore report themselves. **The poll number does not**, which is worth
  knowing before trusting silence as confirmation that 2 s is right.
