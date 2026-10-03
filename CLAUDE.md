# Grounded Class Tutor

GCT is a local desktop study tutor. It answers from the user's course materials
with file/page citations, or reports insufficient support. Citation formatting
checks do not establish factual entailment.

## Source of truth

Start with `design/START-HERE.md`. `design/architecture.md`, `data-model.md`, and
ADR 0033 define the current local architecture. Older hosted-stack ADRs and
component diagrams are historical where ADR 0033 supersedes them. The GitHub
issue board owns implementation assignments and completion; do not infer them
from old Slice 4 labels or the historical launch plan.

## Runtime and development

- Electron owns UI coordination, file dialogs, encrypted ChatGPT credentials,
  model requests, and cancellation.
- The Python library owns bounded parsing, chunking, local persistence/search,
  and the Grounder's five states.
- SQLite and app-owned original bytes are the durable local-library target.
- No Postgres, HTTP server, `.env`, API key, or hosted embeddings are prerequisites
  for the normal local install. The transitional `legacy-hosted` extra exists only
  to keep the old adapters testable until their explicit retirement.

```sh
uv sync --extra dev --locked
uv run pytest
uv run ruff check
uv run ruff format --check
cd desktop
npm ci
npm test
npm start
```

See `design/local-app-proof.md` for the observed subscription proof and remaining
release limits. A checkout-launched app is not a self-contained installer.

## Invariants

- Keep parser, chunker, Grounder and evaluation reusable without hosted imports.
- Preserve source identity and exact physical page/slide numbers through citations.
- Scope every library lookup to the selected class; filenames are not identity.
- Publish an imported original, metadata and its full index atomically.
- Originals selected by the user must never be modified or deleted.
- The local OS user's library survives ChatGPT sign-out and account changes.
- Credentials stay in Electron's encrypted account store, never Python, renderer,
  database backups, logs, or Git. Retain the existing app-data path during migration.
- Partial or cancelled model responses cannot be accepted as completed answers.
  Account changes invalidate pending results; shutdown waits for credential saves.
- Provider failure is an error, not evidence that the course material lacks an answer.
- Search ranking is not calibrated support confidence. Evaluate source retrieval
  separately from generated claims; no paid embeddings fallback.
- Preserve the old database, `.env`, and ignored course corpus when retiring code.
- Change accepted design decisions through a new ADR with matching back-pointers.

## Delivery

Follow `AGENTS.md`: use a fleet, separate builder/reviewer ownership, one Git
integrator, and this original checkout. Publish coherent PRs with exact-head review
records and current CI; never force-push main or bypass protection. Routine checks
must not spend API money. The explicit paid workflow is transitional historical
compatibility only and requires separate user authorization to dispatch.
