# Grounded Class Tutor

A local desktop tutor for your own PDF and PowerPoint course material. Documents
are read on your laptop; selected evidence and your question are sent through
your authorized ChatGPT connection when you ask. Answers carry file/page citations
or report missing support.

The local desktop architecture is now the primary product direction (ADR 0033).
The current native preview proves account sign-in, bounded parsing, selected-page
questions, citations and refusal. Durable classes, class-wide retrieval and a
standalone installer are separate acceptance milestones in the local roadmap.

## Run the development app

Requires Python 3.10+, uv, and Node 22.12+.

```sh
uv sync --extra dev --locked
cd desktop
npm ci
npm start
```

Connect your ChatGPT account in the app, then select a PDF/PPTX or the synthetic
sample. Existing plan/account eligibility and allowance apply. No API key,
Postgres, Supabase, server process, or embeddings service is needed. Generation
requires a network connection. To avoid additional credit spending, keep paid
credit use disabled in ChatGPT's usage controls.

This currently launches from a checkout; it is not yet a standalone installer.
The UI redesign is deferred while the runtime and local library are completed.

## Check the local product

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
cd desktop
npm test
```

The default Python tests exercise the local core without hosted dependencies.
Desktop tests use the real Python bridge and synthetic/mock account traffic;
CI does not make live model requests.

## Design and migration

Start with [the architecture guide](design/START-HERE.md),
[the local architecture decision](design/decisions/0033-local-desktop-primary.md),
and [the local roadmap](design/roadmap.md). Historical account proof and its
limits are in [the proof record](design/local-app-proof.md).

The old hosted adapters are temporarily isolated behind the `legacy-hosted`
extra for migration compatibility. They are not the default product setup. Their
previous instructions and implementation are recoverable at Git revision
`0b004e7`. Do not delete the existing Postgres database, ignored course corpus,
or saved credentials while retiring that code.

The vendored Sign in with ChatGPT SDK has its own license and notices in
`desktop/vendor/`; this development app currently uses it for personal learning
and experimentation. Review those terms before changing distribution scope.
