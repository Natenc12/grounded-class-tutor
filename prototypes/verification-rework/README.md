# Verification rework: prototypes (reference only)

These files back the verification rework issues. They were produced in scratch copies of the repo
at commit `7dfb514` on 2026-11-19. **Nothing here is meant to be merged as-is.** Each patch applies
to `7dfb514` with `git apply`; treat it as a worked example and write the real change fresh.

Everything was run with free stand-in models and scratch databases. No paid model was called.

| Folder | What it is |
|---|---|
| `connection/` | `a1` the one-line autocommit change. `d1` the full cleanup with one `transaction()` helper (+38/−332, suite green). `trap.py` rebuilds the silent-save trap five ways. `ruff-ban.toml` the lint ban on `psycopg.connect`. |
| `schema/` | `b1` cross-column CHECK constraints as a migration only. `b2` adds "publish clears the failure reason" (suite green). `b3` adds composite foreign keys (`ON UPDATE CASCADE`). `races.py` reverts each historical fix and shows the constraint catching it. |
| `types/` | `c1` the answer result as one type per state. `c2` typed ids on the ask path. `probes/` illegal results the checker should reject. `typemut-results.json` type checker vs the planted bugs. |
| `quotes/` | `01` the verbatim-quote verifier. `02` wiring it into the grounder behind a flag. |
| `replay/` | `03` record/replay wrapper for the model providers. |
| `wire/` | `04` a fake OpenAI server test that exercises the real provider code for free. |
| `retrieval/` | `hnsw_probe.py` shows filtered search returning zero rows when the index path is forced. |
| `audit/` | The planted-bug audit: `generate_bugs.py` picks the bugs, `mut2.py` runs them, `sqlmut.py` is the hand-written SQL-guard set (first batch of 16), `planAB.json` is the 200 bugs, `kills.json` is every failing test per bug, `score.py` scores any slice of the suite from `kills.json`. |
| `tests/` | `prop/` three property tests (chunker, request-body limits, queue state machine). `dbperworker.py` the pytest plugin for one database per parallel worker. `jobs-residue.json` the jobs tests a state machine cannot replace. |

Known rough edges, so nobody copies them:
- `tests/prop/test_prop_limits.py` imports a private helper and `test_prop_jobs.py` patches worker internals. The real versions must go through public functions only.
- `connection/d1` leaves about ten comment mentions of `require_idle` and ten `autocommit = True` lines behind.
- `audit/generate_bugs.py` addresses bugs by byte offset, so the sample only reproduces on `7dfb514`.
- `wire/04` uses hand-written response bodies, not recorded ones.
