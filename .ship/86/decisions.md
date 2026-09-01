# Delegated decisions — `/ship 86`

The gate settled one question with Nate (yes: add the "a `ready` file carries no failure reason"
invariant to `design/components/ingestion-worker.md`). Everything below was delegated to the build,
each at the moment `.ship/86/ship-plan.json` named for it. One commit acts on all of them:
`fix(worker): guard _bury's files write with the job lease (#86)`.

---

## D1 — Guard shape: the issue's `EXISTS`, not `leased_until > now()`

**When:** while writing the SQL.
**Choice:** the issue's measured form, verbatim —

```sql
and exists (
    select 1 from jobs
    where job_id = %(job_id)s::uuid
      and state = 'processing'
      and lease_token = %(lease_token)s::uuid
)
```

**Reasoning.** These are `_settle`'s two conditions verbatim, which is the point rather than a
coincidence: the defect is that the two halves of one function disagreed about who owned the job —
`fail` refused a dead lease and the `files` write went through anyway. Matching `fail` exactly is
what makes a lost-lease bury write *nothing* rather than half of an event whose two writes "always
travel together".

I looked for evidence against the `EXISTS` form and found none. The alternative the gate warned
about is worse in a way I verified by mutation rather than argued: adding `leased_until > now()`
makes `test_a_bury_still_holding_its_claim_writes_both_halves_past_the_lease_deadline` go red,
because an ingest that outruns its lease with no reaper behind it still settles its job through
`fail` (whose guard never reads the clock). A files half that refused that call would leave the row
`processing` under a terminally `failed` job — the exact stranding `_bury`'s files-before-jobs
ordering exists to avoid, reintroduced by the guard meant to make it safer.

**Sub-decision, recorded because it crosses a stated convention.** The module docstring said
`queue.py` "owns every statement that touches" `jobs`. The `EXISTS` reads that table from
`worker.py`. Rather than break the rule silently, I narrowed it to what it actually protects —
`queue.py` owns every statement that **writes** `jobs` — and named the one read, in the module
docstring and in `_bury`'s. The alternative (ask `queue.py` first, then write) is a check-then-act
with a reclaim-sized gap in the middle; inside the UPDATE the two are one statement.

**Known likely-equivalent mutant, flagged for the verifier:** dropping `job_id = %(job_id)s::uuid`
from the `EXISTS`. `claim` mints a fresh uuid4 per claim, so a token identifies at most one job and
the remaining two conditions already select it. I did not write a test for it; a differential search
is the honest way to close it, not a hand-argued equivalence.

---

## D2 — The reproduction ships as BOTH: a committed script *and* a test

**When:** right before committing evidence.
**Choice:** `evidence/issue_86_zombie_bury_repro.py` stays and is committed; the interleaving is
*also* driven by `test_a_zombie_whose_lease_expired_writes_nothing_to_files`.

**Reasoning.** They answer different questions and neither substitutes. The test is what CI runs and
what goes red if the guard is deleted (verified — see D5). The script prints the five-step
interleaving on a connection none of the writers hold, so "step 4 is the defect" stays *readable*
rather than inferred from one failing assertion — and it resolves for someone who arrives after this
worktree is gone. The script's own docstring now names the test, so the two are not two writers for
one fact: the test owns the assertion, the script owns the narration. It exits 0 when the guard is
present, so it doubles as a check on either side of the fix.

---

## D3 — Test placement: extend `tests/gct/jobs/test_worker.py`

**When:** when writing the first test.
**Choice:** a new commented section at the end of the existing module, not a new file.

**Reasoning.** The file is already organized as titled sections per issue (`# --- Shutdown (issue
#82)`, `# --- The claim clears the previous attempt's `failed_reason` (issue #24)`), and this work
is the other half of the #24 section's own subject — the two guards together are what make
`('ready', <a reason>)` unreachable. A separate module would put the two halves of one invariant in
two places. The new tests also reuse `_backdate_lease`, `_file_row`, `write_pdf`, `tick` and
`FakeEmbeddings`, all local to this module by a deliberate choice its docstring records; a new file
would have to duplicate them (pytest does not expose a conftest sideways).

---

## D4 — Docstring rewrite scope: three sites, because the stale claim is stated in three

**When:** after the fix was green.
**Choice:** rewrote `_bury`'s guard paragraphs, `process_one`'s *WHAT IT DOES NOT CLOSE* block, the
module docstring's `jobs`-ownership line, and the stale sentence in
`test_the_claim_does_not_clear_a_reason_on_a_file_that_is_already_ready`'s docstring.

**Reasoning.** CLAUDE.md's drift rule: a comment that spells out its target expires exactly like a
copy does, and #86 was named as an *open* residual in four places. Fixing `_bury` and leaving
`process_one`'s block would have left the codebase asserting, in a comment a reader has every reason
to trust, that the path is still open — which is the `_to_score` failure that rule was written from.
The census was `grep -rn '#86' src/ tests/ design/ scripts/`; all hits are now current.

What I did **not** rewrite: `_bury`'s ordering paragraph and its `failed_reason` taxonomy paragraph
are untouched — this fix does not revise them, and rewording them would churn the diff past what a
reviewer can check against the issue.

---

## D5 — Yes, pin the guard-shape edge with its own test — two of them

**When:** after the primary test was green.
**Choice:** three tests, one per condition the guard can get wrong.

| test | what it pins | mutation that kills it |
|---|---|---|
| `test_a_zombie_whose_lease_expired_writes_nothing_to_files` | the interleaving; the guard exists at all | delete the `EXISTS`; drop `lease_token = ...` |
| `test_a_bury_still_holding_its_claim_writes_both_halves_past_the_lease_deadline` | the guard is ownership, **not** the clock | add `leased_until > now()` |
| `test_a_bury_on_a_job_that_is_no_longer_processing_writes_nothing` | the `state` half, which the token half cannot stand in for | drop `state = 'processing'` |

**Reasoning.** The gate flagged the strictness fork as a real risk, and a fork that only lives in a
docstring is one refactor from being "simplified" back. `leased_until > now()` reads like a *better*
guard — it is the one a reviewer would suggest — so the argument against it needs to be a red test,
not a paragraph. The third exists because `complete`/`fail` deliberately leave the winning run's
`lease_token` on the settled row, so a token match alone is not ownership; that is `_settle`'s own
argument, and the state half was otherwise unpinned.

All four mutations above were **run**, each on the restored file, and each went red on exactly one
test (the others stayed green, so no test is doing two jobs). Phase 3's derived-guard run should
reproduce this independently — this is a build-time sanity check, not a substitute for it.

---

## Not decided here, carried nowhere

No unexpected fork arose that anything un-started reads, so nothing was escalated and nothing is
parked. The one convention edge (D1's `jobs` read from `worker.py`) is a narrowing of a rule this
module owns, in a file this ticket already touches — not a new contract for anyone else.
