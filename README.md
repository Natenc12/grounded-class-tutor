# Grounded Class Tutor

A local desktop tutor for your own PDF and PowerPoint course material. Documents
are read on your laptop; selected evidence and your question are sent through
your authorized ChatGPT connection when you ask. Answers carry file/page citations
or report missing support.

The local desktop architecture is now the primary product direction (ADR 0033).
The native app saves classes, original documents, parsed pages and source indexes
in a local SQLite library. Ask across a class, one saved document, or up to five
selected pages; reopen cited passages and create a library backup. A personal
macOS Apple Silicon app bundle is available through the reproducible packaging
command below. Signed public distribution remains a separate milestone.

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

These development commands launch from the checkout.
The visual redesign is deferred. Classes and imported originals survive restarts
and account changes; current questions/answers are not saved as chat history.
Keyword search is available immediately. With the optional local model installed,
use **Prepare semantic search** for a class to combine meaning-based and keyword
matching. Preparation runs on your laptop, needs no account and can be cancelled.
Add/remove documents, then prepare the class again; keyword search remains available
until a complete current index is ready. Choose explicit pages when you already know
the relevant source. Scanned files still need extractable text.

The packaged Mac preview includes the free model. To enable it in the source app,
use Python 3.11+ (3.13.14 was tested) and download only the pinned public MiniLM files:

```sh
uv sync --python 3.13.14 --extra dev --extra semantic --locked
uv run --no-sync python eval/local-embeddings/runtime/download_assets.py --candidate minilm --assets-root desktop/build/models
```

These downloads and inference have no service charge. The app never downloads a
model during a question or falls back to a paid embedding API. The Python 3.10
development setup continues to use keyword search.

## Build the personal Mac preview

On an Apple Silicon Mac, after installing the development prerequisites:

```sh
cd desktop
npm ci
npm run package:mac
```

This creates a self-contained `.app` and ZIP in `desktop/dist`. The resulting app
includes Python and its runtime dependencies; running it does not require the
checkout or developer tools. It preserves the existing library and account-storage
location. macOS may ask for Keychain access to the saved connection on first launch.
GCT restores the saved ChatGPT connection when it opens. If macOS temporarily
blocks access, **Retry saved connection** retries the existing credentials without
starting browser sign-in. A changed preview build can still require fresh macOS
permission; see [saved-connection recovery](design/session-recovery-validation.md).

This is an ad-hoc signed personal preview, without Developer ID notarization or
automatic updates. The packaging checks relocate the app and exercise its shipped
runtime in an isolated environment on the build Mac; they are not a clean-machine
certification. See [packaging and installation details](desktop/PACKAGING.md).

## Your data

On macOS, the app retains its existing `Grounded Class Tutor Local Proof` folder
in Application Support so the connected account remains accessible. `library.sqlite3`
contains the local library; `library.embeddings.sqlite3` is its optional rebuildable
semantic index; the separate `chatgpt` folder contains encrypted account
state. Backups include saved course documents and exclude account credentials and
derived vectors; prepare semantic search again after restoring.
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
The [quality bench](eval/local-quality/README.md) separates source retrieval from
reviewed answer quality; [observed evidence](design/local-quality-validation.md)
records both successes and known search limits.
The [local embedding comparison](design/local-embedding-validation.md) records the
measured model choice, remaining misses, resource costs and reproducible controls.

## Design and migration

Start with [the architecture guide](design/START-HERE.md),
[the local architecture decision](design/decisions/0033-local-desktop-primary.md),
and [the local roadmap](design/roadmap.md). Historical account proof and its
limits are in [the proof record](design/local-app-proof.md). The local library and
restart demonstration are recorded in [migration validation](design/local-refactor-validation.md).

The hosted server, worker, Postgres migrations, paid provider adapters and separate
web client have been retired. Their code, tests and setup instructions remain
recoverable at Git revision `0b004e7`. Existing Postgres data, local `.env` files,
ignored course material and saved credentials are preserved; this refactor does not
automatically import that old database. Import the original PDFs/PPTX files through
the desktop app to build the local library.

The vendored Sign in with ChatGPT SDK has its own license and notices in
`desktop/vendor/`; this development app currently uses it for personal learning
and experimentation. Review those terms before changing distribution scope.
