# 0027. `index_file` should guard its own caller precondition — amending ADR 0025

- **Date:** 2026-08-07
- **Status:** accepted 2026-08-11 (#75) — amends **ADR 0025** (its "documented and asserted by the
  callers, not enforced inside `index_file`" decision, and the *Guard inside `index_file`*
  rejection; the precondition itself, the autocommit fix, and the atomicity/publication distinction
  all stand unchanged). Adoption came in two steps, both 2026-08-11: `src/gct/jobs/queue.py`
  guarded its five writers first (*§Adopted early in the queue module*), then #75 accepted the
  `index_file` half. The guard lives in `gct.db.require_idle`, called by every writer that wraps
  its work in `conn.transaction()`.

## Context

ADR 0025 named the failure exactly, measured it, and chose documentation over enforcement. This ADR
does not dispute the diagnosis. It disputes one empirical claim in the rejection, on evidence that
did not exist in 2026-07.

**What ADR 0025 rejected, and why.** Its *Alternatives considered* calls the guard "the most
attractive option, and it would have caught this on the first run", then rejects it on blast radius:

> the guard's correctness depends on *statement order within the caller*, not on caller intent, so it
> fires on benign code. The test suite's own `seed_class` fixture calls `index_file` directly on the
> shared `db` connection, and whether that connection is `IDLE` or `INTRANS` at that moment depends
> on whether the test happened to run a statement first. A guard that a test passes or fails based on
> an unrelated earlier `SELECT` teaches nothing and would be silenced rather than heeded.

That argument has two halves. The first — a guard fires on statement order, not intent — is a true
statement about mechanism and is not in dispute. The second — **that it therefore fires on *benign*
code** — is an empirical prediction. It was never measured, and it is now false.

**Measurement (2026-08-07).** The rejected guard was implemented verbatim as a probe (raise unless
`conn.info.transaction_status` is `IDLE`) and the full suite run against it:

```
7 failed, 289 passed

FAILED tests/scripts/test_ask_smoke.py::TestConvergeCorpus::test_ingests_every_file_once_on_a_cold_class
FAILED tests/scripts/test_ask_smoke.py::TestConvergeCorpus::test_second_run_ingests_nothing_and_embeds_nothing
FAILED tests/scripts/test_ask_smoke.py::TestConvergeCorpus::test_the_chunk_window_reaches_ingest
FAILED tests/scripts/test_ask_smoke.py::TestConvergeCorpus::test_duplicate_ready_rows_warn_and_do_not_re_ingest
FAILED tests/scripts/test_ask_smoke.py::TestConvergeCorpus::test_partial_convergence_counts_only_files_ingested_this_run
FAILED tests/scripts/test_ask_smoke.py::TestSetupValidityGuards::test_indexed_pages_reads_back_the_pair_retrieval_compares
FAILED tests/scripts/test_ask_smoke.py::TestSetupValidityGuards::test_indexed_pages_is_scoped_to_this_owner_and_class
```

Two things about that list, and the second is the argument.

**`seed_class` is not on it.** The fixture ADR 0025 named as the counter-example does not trip the
guard: the `db` fixture commits its seeded `classes` row, so the connection is `IDLE` when `_seed`
first runs, and each `index_file` commits and returns it to `IDLE`. The concrete evidence the
rejection rested on has expired. Kept here rather than quietly dropped, because "the named example
no longer holds" is the kind of fact a future reader of ADR 0025 would otherwise have to rediscover.

**None of the seven is benign.** The guard fires *iff* the connection is not `IDLE`, and a
non-`IDLE` connection is precisely the state in which `with conn.transaction():` issues a SAVEPOINT
and publishes nothing (ADR 0025 §Context, measured there). So every test on that list was already
running ingest in savepoint mode. The guard did not misfire seven times; it found seven more
instances of the bug it exists to catch. "Fires on benign code" predicts false positives, and the
measurement produced none.

Those seven pass today because they read back through the same connection, which sees its own
uncommitted work — the property ADR 0025 §Consequences already identifies ("this class of bug is
invisible to the existing tests, by construction"). Their *conclusions* are mostly unaffected: they
assert convergence counting and scope filtering, not durability. They are not all false greens. They
are all in the hazardous state.

**Why revisit now: documentation has been measured, and it failed.** Issue #69 added a
caller-supplied `file_id`, and the review of that change found the precondition broken **twice** in
tests written *against* it:

- `test_ingest_file_uses_a_caller_supplied_file_id` — seeded a row and never committed. Four
  assertions passed on rows no other connection could see, then the teardown rollback discarded
  them. The test asserted publication and could not observe it.
- `test_ingest_file_still_mints_a_file_id_when_none_is_supplied` — counted rows before ingesting. A
  bare `SELECT` opens the implicit transaction exactly as a write does, so this test published
  nothing either. It contains no write at all, which is why nobody looked at it when its sibling was
  fixed.

At the moment those were written, the rule was stated in four places: ADR 0025 itself (including a
worked example of the bare-`SELECT` case), `index_file`'s docstring, `components/ingestion-worker.md`,
and issue #71 — which opens with a section headed *"The highest-risk item in the slice — ADR 0025's
precondition"* and says outright that the only defense is a second-connection test. The first of the
two broken tests was written in the same commit series as the docstring stating the rule.

Documentation was not merely insufficient here; it was abundant, specific, measured, and adjacent.
Nine instances now exist in a codebase where every writer had been told. ADR 0025's own preference
for autocommit over a per-call-site commit was argued on exactly this ground — that a fix "leaves the
mechanism live for the next writer who forgets one." That reasoning applies to the mechanism itself.

## Decision

**Proposed:** `index_file` raises if it is handed a connection that is not `IDLE`, instead of
silently degrading to a savepoint.

The precondition, the autocommit remedy, and everything ADR 0025 says about atomicity surviving while
publication does not are unchanged. Only *who enforces it* changes: from "documented and asserted by
the callers" to "documented, and checked at the boundary it protects."

The seven tests are fixed the way `scripts/ask_smoke.py` already fixes itself — `autocommit = True`
at wiring, or an explicit commit before ingest. That is not incidental cleanup: seven tests currently
exercise a mode the production script deliberately avoids, so they are not testing what the script
does.

**This ADR does not authorize the code change.** It records the case for reversing a decision that is
on the record, so the reversal happens in the open. Nothing in `src/` changes until this is accepted.

## Adopted early in the queue module (2026-08-11)

`src/gct/jobs/queue.py` (#70) added five new writers with this precondition, and the review of that
PR asked the question this ADR had not: **what does the guard cost in a module that is not
`index_file`?** ADR 0025's rejection rests entirely on blast radius — a guard "fires on benign
code" — and that is an empirical claim about a specific set of callers. It had been measured once,
against one module.

Measured against `queue.py`, guard live on all five writers (`enqueue`, `claim`, `complete`, `fail`,
`reclaim_expired`):

```
pytest -m "not live"  ->  313 passed, 0 failed
```

**Zero firings.** Not "seven, none benign" — none at all. Every existing caller already satisfies the
precondition, so in this module the blast-radius objection is not outweighed, it is *empty*. The
guard was separately confirmed live rather than vacuous: after a single bare `SELECT`, all five
writers raise.

The guard is therefore adopted **for `queue.py` only**, and this section is its decision record.
Three things follow, and the third is the point:

- **This is not the `index_file` decision.** That one still costs the seven `test_ask_smoke.py` edits
  catalogued above, and it stays open on #75. Adopting here does not pre-empt it.
- **It is evidence for it.** #75 now gets to argue the expensive half with a working precedent and a
  measurement, instead of arguing the pattern and the cost together.
- **The asymmetry is itself informative.** `index_file`'s seven firings were never a sign that the
  guard is wrong; they were seven callers already in the hazardous state. A module whose callers were
  written *after* the rule was understood fires zero times. That is the strongest available argument
  that the guard's cost is a one-off migration of existing call sites, not an ongoing tax.

Where ADR 0025's reasoning is untouched: it declined the guard for `index_file`, whose callers include
the `seed_class` fixture and every test in `test_index.py`. Nothing here re-opens that; it records
that the same objection, tested elsewhere, came back empty.

## Alternatives considered

- **Leave ADR 0025 as it stands.** Defensible: the guard genuinely cannot distinguish a careless
  caller from a deliberate one, and #71 already carries the warning where the risk is highest. The
  count is what argues against it — nine instances, four documents, one week, including two written
  by the reviewer of the very change that documented the rule.
- **Guard in `ingest_file` only, leaving `index_file` open.** Cheaper, and covers the pipeline entry
  point most callers use. Rejected: `seed_class` and every test in `test_index.py` call `index_file`
  directly, so the seam that actually loses publication would stay unguarded — the guard belongs at
  the transaction it protects, not one layer up.
- **A test-only tripwire** (autouse fixture asserting `IDLE` before ingest). Catches the recurrence
  in the suite without touching `src/`. Rejected as the primary: the failure it must prevent is a
  *production* worker (#71) holding its lease across ingest, and a test-only guard is silent there.
  Worth doing as well as, not instead of.
- **Warn instead of raise.** Rejected on the same ground ADR 0025 raises against a guard that gets
  "silenced rather than heeded" — a warning in a passing test is exactly the thing nobody reads. If
  the seven current call sites are genuinely in the wrong state, a raise is what says so.

## Consequences

- ADR 0025's status line gains a back-pointer if accepted; its *Alternatives considered* entry for
  the guard should be read as superseded, and the `seed_class` example noted as no longer holding.
- Seven `test_ask_smoke.py` tests need one line each. They stop exercising a mode the script under
  test does not use.
- #71's *"the only defense is a test"* framing becomes "the defense is a guard, and a test." The
  `db_other` fixture added by #69 is what that test uses.
- The guard cannot see a caller that is `IDLE` at the call and wrong later, and it cannot make a
  savepoint publish. It converts a silent wrong answer into a loud one, which is the whole claim.
