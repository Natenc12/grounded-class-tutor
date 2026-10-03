# Grounded Class Tutor

A local desktop tutor for your own PDF and PowerPoint course material. Documents
are read on your laptop; selected evidence and your question are sent through
your authorized ChatGPT connection when you ask. Answers carry file/page citations
or report missing support.

The local desktop architecture is now the primary product direction (ADR 0033).
The native app saves classes, original documents, parsed pages and source indexes
in a local SQLite library. Ask across a class, one saved document, or up to five
selected pages; reopen cited passages and create a library backup. A standalone
installer remains a separate release milestone.

## Run the development app

Requires Python 3.10+, uv, and Node 22.12+.

```sh
uv sync --extra dev --locked
cd desktop
npm ci
npm start
```

Create a class and add a PDF/PPTX; local library actions work without signing in.
Connect your ChatGPT account when you want to ask, or try the synthetic sample. Existing plan/account eligibility and allowance apply. No API key,
Postgres, Supabase, server process, or embeddings service is needed. Generation
requires a network connection. To avoid additional credit spending, keep paid
credit use disabled in ChatGPT's usage controls.

This currently launches from a checkout; it is not yet a standalone installer.
The visual redesign is deferred. Classes and imported originals survive restarts
and account changes; current questions/answers are not saved as chat history.
Automatic search is keyword-based and can miss paraphrases; choose explicit pages
when you already know the relevant source. Scanned files still need extractable text.


## Your data

On macOS, the app retains its existing `Grounded Class Tutor Local Proof` folder
in Application Support so the connected account remains accessible. `library.sqlite3`
contains the local library; the separate `chatgpt` folder contains encrypted account
state. Backups include saved course documents and exclude account credentials.
Backups must use a new filename. To restore manually, close GCT and move the existing
`library.sqlite3` and any matching `-journal`, `-wal`, and `-shm` files together to a
recovery folder. Copy a verified backup into the now-clear `library.sqlite3` location;
leave the `chatgpt` folder untouched. An in-app restore flow is not included.
Deleting library material leaves the originally selected file untouched.

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
limits are in [the proof record](design/local-app-proof.md). The local library and
restart demonstration are recorded in [migration validation](design/local-refactor-validation.md).

The old hosted adapters are temporarily isolated behind the `legacy-hosted`
extra for migration compatibility. They are not the default product setup. Their
previous instructions and implementation are recoverable at Git revision
`0b004e7`. Do not delete the existing Postgres database, ignored course corpus,
or saved credentials while retiring that code.

The vendored Sign in with ChatGPT SDK has its own license and notices in
`desktop/vendor/`; this development app currently uses it for personal learning
and experimentation. Review those terms before changing distribution scope.
