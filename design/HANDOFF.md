# Local app implementation handoff

Read `AGENTS.md`, `CLAUDE.md`, ADR 0033 and the local architecture before work.
The original repository is the only active checkout; only the lead changes Git state.

For each request, the lead coordinates architecture, implementation, independent
review and governance. Agree on data/wire contracts before parallel implementation.
Give each builder exclusive files; do not increase parallelism by making competing
copies of the application. Rotate reviewers when a reviewer authors a repair.

Deliver coherent PRs: architecture/core defaults, durable library, retrieval,
desktop integration, then hosted retirement. Each PR has exact-head review evidence,
current meaningful tests, and normal protected merge. A review loop stops when
concrete findings are resolved; it does not keep generating speculative work.

Use the issue board for current ownership and completion. Existing hosted issues
must be reconciled explicitly into retained requirements or superseded work; never
run the old Slice 4 projection blindly against the new architecture. Preserved
history at `0b004e7` is useful context, not the current task frontier.

Default tests must run without server dependencies. Temporarily retained hosted
tests require an explicit compatibility install and collection switch. Prove new
local invariants directly: fresh-process durability, class isolation, atomic import,
source identity, bounded retrieval, complete response acceptance, credential-safe
shutdown and original-file preservation. Inherited hosted test counts are not
proof of these behaviors.

Keep the existing encrypted account path and local data. Do not inspect/log tokens,
auto-import the old Postgres database, delete the private corpus, or run paid smoke
checks. Generation uses the user's authorized ChatGPT connection only when requested.
Packaging and any distribution remain distinct from a successful development run.
