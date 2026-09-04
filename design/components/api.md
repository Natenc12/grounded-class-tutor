# Component Spec — API adapter

**Phase:** 3 — Component Design  **Rigor:** Deep (ADR 0001)  **Criticality:** adapter (thin; the trust surface is below it)

> Convention (Phase-4): names the shape + contract; where a choice is locked it **points to an ADR**
> rather than restating the rationale. Governing ADRs: **0004** (API-first; single hardcoded user, no
> auth in V1; `owner_id` on every row), **0009** (the core is a callable library — never weld the
> question into the handler), **0010** (V1 local file staging), **0011** + PM-3 addendum (the worker
> is a SEPARATE OS process, never a task in the API's loop), **0016** (failure states are RETURNED,
> not raised), **0025** / **0027** (the idle-connection precondition, and its guard). Source:
> `design/roadmap.md` → Slice 3; `design/architecture.md` (thin adapter, no business logic).
>
> **Scope of this revision (issue #104):** only what the skeleton settles — the composition root.
> Only the `POST /classes` section at the bottom is still a **stub**, for #107 to fill; the
> composition root above decides no route-level behaviour, and the filled route sections each
> name the issue that owns theirs.

## Responsibility
Translate HTTP ⇄ core calls. A handler **validates, calls one library callable, and renders** —
nothing else. Every decision about grounding, scoping, status or refusal already has an owner in
`src/gct/`, and a copy in a handler would be a second writer for it (ADR 0009; `architecture.md`
§Component map). The seam is *library-callable vs. adapter*, not one endpoint per module: what the
API needs and the library lacks is added to the library (#106), never to a route.

What the adapter does own, and owns **once** (the composition root, `src/gct/api/`): the per-request
connection a library writer will accept, the one source of the V1 owner, the app-wide provider
singletons, the startup requirements, and the error envelope.

## Position & dependencies
- **Down — the core:** `gct.jobs.queue.enqueue`, `gct.ask.ask`, and #106's `create_class` /
  `get_file_status`. Each is called with a connection from this component's dependency and the
  owner from its dependency; none is re-implemented here.
- **Down — file staging (ADR 0010):** #105's stager, called by `POST /files` before `enqueue`.
- **Beside — the worker:** a separate process against the same Postgres (ADR 0011 PM-3). The API
  never runs ingest; it enqueues and reports status. `uvicorn gct.api.app:app` serves the API;
  `scripts/worker.py` is the worker; neither starts the other.
- **Consumers:** the Slice 4 SPA (ADR 0012) and the Slice 3 exit smoke (#109).

## Interface / contract — the composition root

### The connection contract (`gct.api.deps.get_conn`)
```
get_conn() -> Iterator[psycopg.Connection]      # a FastAPI yield-dependency; spelt `Conn` in routes
```
Every request gets **its own connection**, built by **`gct.db.connect()`** (so the pgvector type is
registered — `ask()` requires it), with **`autocommit = True`**, **closed in a `finally`** when the
request ends. Never pooled, never shared across requests in V1, and never overridden by a
FIXTURE — a test may substitute it to drive a negative arm, never for a positive one.

Why autocommit is the contract and not a preference: every library writer refuses a connection
already inside a transaction (`gct.db.require_idle`; ADR 0025, guarded per ADR 0027), and psycopg
opens the implicit transaction on a bare `SELECT`. The natural handler — an ownership read, then
`enqueue` — raises on its second line on any other connection. This is the same wiring every
working caller already has (`scripts/worker.py`, `scripts/ask_smoke.py`, `scripts/ingest_smoke.py`).

Routes are spelt with the `Annotated` aliases `Conn`, `OwnerId`, `Embedder`, `Generator` from
`gct.api.deps` — `def handler(conn: Conn, owner: OwnerId, ...)` — not `= Depends(...)` defaults.

### The owner (`gct.api.deps.owner_id`)
```
owner_id() -> str                                # spelt `OwnerId` in routes
```
Returns **`gct.config.V1_OWNER_ID`** — the single hardcoded V1 user (ADR 0004), and the **one**
place the API reads it. A route never takes an owner from the request (body, header, query): an
unauthenticated client choosing whose rows it sees is the F12 leak. Every scoped query a route
issues filters **`owner_id AND class_id`** (F6/F12), with the owner from this dependency. V3
replaces this one function with the authenticated principal; no route changes.

`scripts/ask_smoke.py` defaults `--owner` to the same constant, so the API answers over the corpus
the smoke ingested. A second literal anywhere is a second writer of this value.

### Provider singletons (`gct.api.deps.get_embedder` / `get_generator`)
```
get_embedder(request) -> Embeddings              # spelt `Embedder`
get_generator(request) -> Generation             # spelt `Generator`
```
Built **once, at startup, in the app's lifespan**, stored on `app.state`. `ask()`'s collaborators
are keyword-only and are never constructed per request. The real embedder constructs itself from
`gct.config.ACTIVE_EMBEDDING_MODEL_ID` (ADR 0018 — never a hardcoded model id).

`create_app(*, embedder=None, generator=None)` accepts substitutes. Tests pass fakes; a process
that passes neither gets the OpenAI providers (ADR 0005/0007 defaults).

### Startup requirements (`gct.api.app.create_app`)
- **`OPENAI_API_KEY` must be set** when a real provider is to be built. Checked in the lifespan,
  **before** any client is constructed; a missing key refuses startup with a message naming the
  remedy. `load_settings()` defaults the key to `""`; whether the OpenAI client refuses that at
  construction or only on the first paid call has varied by SDK version, and the startup check
  makes the refusal ours - naming `.env` - either way.
- With **both** providers injected, no key is required — the requirement belongs to constructing
  the real provider, so the bypass is by construction rather than by flag. This is how the api test
  suite runs in CI against an empty key.
- `import gct.api.app` never needs a key: `app = create_app()` builds the app, and the check runs
  when the server starts.
- The API does **not** read a second settings layer. Config lives in `gct.config`, as #105 also
  requires of the stager.

### The error envelope (`gct.api.errors`, `gct.api.schemas.ErrorEnvelope`)
Every non-2xx response body is exactly:
```
{ "error": { "kind": str, "message": str, "detail": any | null } }
```
- `kind` — a stable machine token a client switches on. Framework-level kinds this component
  emits: `validation` (422, `detail` = pydantic's per-field list), `http` (the framework's own
  404/405), `internal` (500 — the exception text is **not** echoed; it belongs in the server log).
  Routes mint their own domain kinds.
- `message` — for a human; names the remedy where one exists.
- Routes raise **`ApiError(status_code, kind, message, detail=None)`**; the status is the raiser's
  choice. **Which status a given failure maps to is each route issue's decision**, not this spec's.
- `kind`/`message` is the vocabulary `GrounderError` already uses (`grounder.md` §Interface),
  which is why `POST /ask` forwards a Grounder `ERROR`'s own `kind` into this envelope rather than
  minting a second one — see that route's section for the `kind` → status split and the one
  message it substitutes. **A refusal is never an envelope:** the four *grounding* states are 200
  bodies (ADR 0016), and only the fifth, transport-level `ERROR`, leaves this way.

### The skeleton's own route
```
GET /health  ->  200 {"status": "ok"}
```
Reads `select 1` through the request connection, so a green probe means a connection was built,
reached Postgres, and closed — not merely that the process is up.

## Failure modes
| Condition | Behaviour | Owner |
|---|---|---|
| Handler reads then calls a writer on a non-autocommit connection | `require_idle` raises → 500 envelope, **nothing published** | prevented by `get_conn`; pinned in both directions by `tests/gct/api/test_skeleton.py` |
| `OPENAI_API_KEY` empty, real providers requested | startup refused, remedy named | `create_app` lifespan |
| Uncaught exception in a handler | 500 `internal` envelope; traceback to the server log only | `errors.install` |
| Unknown path / method | 404 / 405 `http` envelope | `errors.install` |
| Request fails validation | 422 `validation` envelope, field list in `detail` | `errors.install` |

## Invariants
- One connection per request, autocommit, closed on exit, and no test FIXTURE overrides it —
  *The connection contract* above owns that rule and its one exception; this line does not
  restate them.
- One source for the owner; no route reads an owner from the request. Every scoped query filters
  `owner_id AND class_id`.
- Providers are per-app singletons built at startup; never per request.
- One error body shape; status mapping lives with the route that raises.
- No business logic in a handler: validate → call one library callable → render.
- The worker is a separate process (ADR 0011 PM-3); nothing in the API's loop ingests.

## Testing contract (`tests/gct/api/conftest.py`)
The TestClient fixture **does not inject the `db` fixture into `get_conn`** — `db` is not
autocommit, so that test fails `require_idle` while production works. The app builds its own
connection per request as shipped; the FIXTURE overrides only `owner_id`, with `db`'s unique
per-test owner, so every row a handler writes lands under an owner `db`'s teardown already
deletes. Read-back of anything WRITTEN goes through `db_other` (CLAUDE.md). Providers are
stubs; no api test takes a `live_*` fixture or constructs a real client.

---

## Routes — owned by their issues

> Each section below is filled by the issue named. Request/response models live beside the router
> (`src/gct/api/routers/<name>.py`), never in `schemas.py`. Status mappings are that issue's decision.

### `POST /classes` — **#107** *(stub)*
Router mounted at `/classes`. Calls #106's `create_class(conn, *, owner_id, name)`. Request/response
models and the failure → status map: **to be written by #107.**

### `POST /files`, `GET /files/{file_id}` — **#110**
Router mounted at `/files`; models and status map live beside it in
`src/gct/api/routers/files.py`, which owns every sentence this section does not restate.

`POST /files` takes multipart `file` + form `class_id`. It parses `class_id` ONCE at the top —
Python's uuid parser accepts spellings Postgres's `::uuid` cast refuses — then checks ownership
with `class_exists` BEFORE `stage(...)`, so a refusal leaves nothing on disk, and finishes with
`enqueue(conn, path=, owner_id=, class_id=)`. **202 Accepted** `{file_id, filename}`: accepted,
not created, because nothing has been parsed or indexed yet and `file_id` is what the student
polls. Refusals are `400` (`bad_class_id`, `bad_filename`), `413` (`too_large`), `404`
(`class_not_found`).

`GET /files/{file_id}` renders `get_file_status` as **200** `{filename, status, failed_reason,
message}` for every status *including* `failed` — no `file_id`, since the caller supplied it.
`message` is the route's rendering of the stored pair: one sentence naming what happened and what
to do next, which is the slice's Exit criterion. A file that could not be
read is a true answer about the student's materials, not a broken request. `400 bad_file_id`,
`404 file_not_found`.

**404, never 403, on both routes.** Another owner's file and an unknown one are the same response
byte for byte, so ids cannot be probed by watching which error comes back; `get_file_status`
returns `None` for both by construction. The class check is the route's own work rather than the
database's: `files.class_id`'s foreign key carries no owner predicate, so without it an upload
naming a stranger's class would be accepted and filed there.

Every fact the router copies is pinned to its source by a test rather than trusted — the reason
and status sets read off the CHECK constraints (`migrations/0001_init.sql`,
`0003_failed_reason_too_long.sql`), `too_long`'s bound off `gct.config.MAX_INGEST_WORDS`
(ADR 0029), and the file types the `unsupported` advice names off `parse_file`'s own dispatch.

### `POST /ask` — **#108**
Router mounted at `/ask`; the models, the status map and the message-substitution map live
beside it in `src/gct/api/routers/ask.py`, which owns every sentence this section does not
restate.

`POST /ask` takes JSON `{class_id, question}`. It parses `class_id` ONCE at the top for the same
reason `POST /files` does — `retrieve` binds it raw into a `::uuid` cast, so a spelling Python
accepts and Postgres refuses would abort a request that named a real class (#121 reports the
identical shape in `enqueue`) — then checks ownership with `class_exists` BEFORE `ask(conn,
question, owner_id, class_id, embedder=, generator=)`, using the singletons above.

**All four grounding states are 200.** `GROUNDED`, `PARTIAL`, `REFUSAL` and `INTEGRITY_FLAGGED`
render as `{state, answer_prose, citations, coverage, integrity}` — the HTTP face of ADR 0016's
*error ≠ refusal*, and the same rule `GET /files/{file_id}` follows for a `failed` row. A refusal
is a true answer about the student's materials; a 4xx would say the request was wrong when it was
fine. **`ERROR` is the exception and splits by `error.kind`:** `provider_transient` → `503` (the
one kind where retrying can work, and the Grounder has already spent its budget),
`embedding_mismatch` → `500` (ADR 0018 — the class needs re-indexing by an operator),
`provider_terminal` → `500`. An unrecognised kind defaults to `500`, and the map is pinned to the
`ERROR_KIND_*` constants of both producing modules by a test, so the default is a safety net and
never the guard. ERROR leaves as the shared `ErrorEnvelope` rather than a 200 body, because
`errors.py` makes every non-2xx the adapter produces that shape; `ask()` still *returns* it, and
choosing the HTTP rendering is the route's decision, not the ADR's.

**The error `message` is the library's, except where the library did not write it.** `kind` is
always the library's token, adopted verbatim. `provider_terminal`'s message is built from a raw
provider exception (`grounder/answer.py`), so the route substitutes its own sentence rather than
forwarding vendor text — the same thing `errors._unhandled` refuses to do — and an unknown kind is
substituted for the same reason. **A substituted sentence is written to the server log**, at the
site that substitutes it and only there: `ApiError` is handled, so nothing re-raises for uvicorn
to log the way an uncaught exception does, and the route's own sentence tells the operator to
look in that log. The two forwarding kinds are not logged — the client was already told.
**Rejected requests** - never a refusal, which is a 200 - are
`400` (`bad_class_id`), `404` (`class_not_found`), `422` (blank, missing, or over-long
`question`, through the shared validation envelope).

**404, never 403**, and byte-identical for a foreign class and a nonexistent one — `class_exists`
returns one `False` for both by construction (F12). The check is not a formality: a missing class
falling through to `ask()` retrieves nothing, which `answer()` renders as *"no course material was
retrieved for this question"* — a statement about a corpus that never existed.

**Not on the wire, by decision.** No `retrieved`: it exists for the eval runner's recall@k
(ADR 0023, `AskResult`'s docstring), while the HTTP client renders citations, and shipping it
would make the top-k an observable part of this contract. No `k` request field: `DEFAULT_K` has
one home and no product meaning for a caller. No conversation id or history — single-shot
(ADR 0009). No `error` field on a success body, since ERROR is never a 2xx.

**The broad catch this route needs is the app's, not its own.** `ask()` converts two exception
types and propagates everything else, so raw psycopg and terminal provider errors do reach this
boundary; `errors._unhandled` renders them as a 500 `internal` with the text going to the log. A
route-local `except Exception` would be a second writer for that rendering, so there is none, and
a test asserts both the behaviour and its absence from the source.

## Open / deferred (out of this spec)
- Connection pooling — V2, with the Supabase move (ADR 0006).
- Authentication — V3 (ADR 0004); replaces `owner_id` with the authenticated principal.
- A CLI for `scripts/worker.py` — #109.
