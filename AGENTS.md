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

The product is a local desktop tutor under ADR 0033. Preserve course files,
existing account credentials, and the old local database during migration.
Library operations are local; generation uses the user's authorized ChatGPT
connection. Never introduce paid API or embedding fallbacks. Visual redesign is
separate from required functional controls.
