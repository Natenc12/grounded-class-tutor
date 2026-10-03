# Grounded Class Tutor agent workflow

Read `CLAUDE.md` and `design/START-HERE.md` before implementation.

Nathan expects a coordinated fleet for GCT requests. The lead decomposes work,
assigns explicit file ownership, integrates results, and obtains independent
review. Use architecture, implementation, adversarial testing, and governance
roles as appropriate; rotate roles within the available agent slots. Reviewers
must not solely approve changes they authored. The lead remains responsible for
resolving disagreements and completing the requested result.

Work in this original repository. Do not create duplicate checkouts or worktrees.
Only the lead changes branches, commits, pushes, opens PRs, or merges. Keep PRs
coherent, review their final commit, and require current CI without admin bypass.
Use bounded review rounds tied to concrete findings, rather than endless polling.

Test proposals before calling them improvements. For retrieval or performance
changes, state the comparison and acceptance criteria before inspecting results,
keep an unchanged baseline, report per-case regressions and resource costs, and
use independent held-out cases before choosing a default. Preserve unsuccessful
experiments; a benchmark result is not a factual-answer accuracy claim.

No money may be spent. Use local computation and free public downloads; do not
use paid APIs, credits, hosted inference, paid runners, storage overages, purchases
or new subscriptions. The local embedding study makes no ChatGPT generation calls.
When a proposed operation's cost cannot be established as zero, continue with
independent free work instead of initiating that operation.

The product is a local desktop tutor under ADR 0033. Preserve course files,
existing account credentials, and the old local database during migration.
Library operations are local; generation uses the user's authorized ChatGPT
connection. Never introduce paid API or embedding fallbacks. Visual redesign is
separate from required functional controls.
