# Feature map

What the product does, seen from outside. A student puts course files into a class and asks
questions; every answer is cited to file and page, or honestly refused.

There is no user interface yet. A student's actions are HTTP calls. The loop is:
create a class, upload a file, wait until it is ready, ask.

`Costs money: yes` means reaching the feature with real providers calls a paid API.

## Student features

### Create a class
- Reach: `POST /classes`
- Costs money: no
- Code: `src/gct/api/routers/classes.py`
- Tests: `tests/gct/api/test_classes_router.py`

Send a name, get back a `class_id`. Everything else is scoped to a class.

### Upload course material
- Reach: `POST /files`
- Costs money: no
- Code: `src/gct/api/routers/files.py`
- Tests: `tests/gct/api/test_files_router.py`

Send a PDF or PowerPoint deck and a `class_id`, get back a `file_id` straight away with status 202.
The file is only stored and queued here. Nothing is read until the worker picks it up, so an
upload that "does nothing" usually means no worker is running.

### Check whether a file is ready
- Reach: `GET /files/{file_id}`
- Costs money: no
- Code: `src/gct/api/routers/files.py`
- Tests: `tests/gct/api/test_files_router.py`

Returns the file's status and a plain-language message.

- Statuses: `queued`, `processing`, `ready`, `failed`
- Failure reasons: `unparseable`, `protected`, `unsupported`, `empty`, `too_long`, `transient_exhausted`

A file can be asked about only once it is `ready`. A `failed` file names its reason and what the
student should do about it.

### Ask a question
- Reach: `POST /ask`
- Costs money: yes
- Code: `src/gct/api/routers/ask.py`
- Tests: `tests/gct/api/test_ask_router.py`

Send a `class_id` and a question. The answer text carries `[S1]`-style labels, and each label
resolves to a file and a page or slide.

- Answer states: `GROUNDED`, `PARTIAL`, `REFUSAL`, `INTEGRITY_FLAGGED`, `ERROR`

A refusal is a normal, successful response: the materials do not cover the question. Only `ERROR`
leaves as an HTTP error. `INTEGRITY_FLAGGED` means the answer failed a citation check and must
not be shown as a verified one.

### Health check
- Reach: `GET /health`
- Costs money: no
- Code: `src/gct/api/routers/health.py`
- Tests: `tests/gct/api/test_skeleton.py`

Says the API process is up. It says nothing about the worker or the database.

## Operator features

### Process uploads
- Reach: `scripts/worker.py`
- Costs money: yes
- Code: `src/gct/jobs/worker.py`
- Tests: `tests/gct/jobs/test_worker.py`

A separate process that takes queued files and reads, splits, embeds and indexes them. Embedding
is the paid step. Without it running, files stay `queued` for ever.

### Set up the database
- Reach: `scripts/migrate.py`
- Costs money: no
- Code: `scripts/migrate.py`
- Tests: none

Applies `migrations/*.sql` in order. Safe to run twice.

### Prove the foundation is wired
- Reach: `scripts/smoke_slice0.py`
- Costs money: yes
- Code: `scripts/smoke_slice0.py`
- Tests: none

Checks the tables exist and that the real embedding and answering providers respond. No
product logic is involved.

### Prove asking works on real course files
- Reach: `scripts/ask_smoke.py`
- Costs money: yes
- Code: `scripts/ask_smoke.py`
- Tests: `tests/scripts/test_ask_smoke.py`

Runs the question suite in `eval/questions.jsonl` against the owner's own corpus, which is not in
the repository. It cannot run anywhere that corpus is absent.

### Prove uploading works end to end
- Reach: `scripts/ingest_smoke.py`
- Costs money: yes
- Code: `scripts/ingest_smoke.py`
- Tests: `tests/scripts/test_ingest_smoke.py`

Drives real files through the queue and the worker and checks what was stored.

### Prove the whole loop over HTTP
- Reach: `scripts/http_smoke.py`
- Costs money: yes
- Code: `scripts/http_smoke.py`
- Tests: `tests/scripts/test_http_smoke.py`

Starts the API and the worker itself, then uploads and asks over HTTP. With `--launch-only` it
only proves the two processes start, and that form is free. Both forms refuse a database that
already holds files.

### Generate a throwaway corpus
- Reach: `scripts/ci_corpus.py`
- Costs money: no
- Code: `scripts/ci_corpus.py`
- Tests: `tests/scripts/test_http_smoke.py`

Writes a small PDF and deck so the upload check can run without the owner's files.

### Set up a cloud container
- Reach: `scripts/cloud-bootstrap.sh`
- Costs money: no
- Code: `scripts/cloud-bootstrap.sh`
- Tests: none

Creates Postgres, the database and `.env` inside a Linux container. Refuses to run on a laptop.

## Web client
- Screens: none

`web/` holds a typed client for the API and an empty page. No feature can be reached through it.
