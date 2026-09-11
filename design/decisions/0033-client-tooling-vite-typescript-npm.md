# 0033. V1 client tooling — Vite, TypeScript, npm; the dev server proxies the API

- **Date:** 2026-09-11
- **Status:** accepted (decided by Nate, 2026-09-11; type generation, the proxy's shape and the
  version pins decided in build, #140)

## Context
ADR 0012 decided *what* the V1 client is — a minimal React SPA over five P0 surfaces — and nothing
about how it is built. No doc in `design/` carried a front-end tooling decision until this one;
#140 is the first front-end code in the repo, and HANDOFF.md's readiness gate asks for key
tradeoffs as ADRs. Two forces beyond the toolchain itself made choices necessary:

- **The API has no CORS.** A dev server on its own port is another origin, so without a proxy or
  CORS middleware in `src/gct/api/app.py` the browser cannot call the API at all.
- **The API's bodies already have one writer**: the pydantic models beside each router
  (`design/components/api.md` §Routes). A client needs types for them, and a hand-written
  TypeScript copy would be a second writer.

## Decision
**Toolchain** (Nate). The client lives in `web/`. Vite builds and serves it; React (ADR 0012);
TypeScript (the split ADR 0006 already names as standard); npm with a committed
`package-lock.json`, installed with `npm ci`; Vitest; ESLint (`typescript-eslint`'s type-checked
rules and `eslint-plugin-react-hooks`) and Prettier, lint and formatting kept separate.

**Dev-time origin** (Nate): **the Vite dev server proxies the API; no CORS middleware.** The page
and the API share the dev server's origin, and `src/gct/api` is unchanged. The rule, in
`web/dev-proxy.ts`:
- **The API's own paths**: `/classes`, `/files`, `/ask`, `/health` and anything under them,
  with no `/api` prefix, so a path in the client is the path api.md spells.
- **To `http://127.0.0.1:8000`**, uvicorn's default bind. It is an IPv4 literal because Node
  resolves `localhost` to `::1` first.
- **Overridable with `GCT_API_TARGET`**, to reach a second API on another port. The value must
  be an http(s) origin; anything else is refused when the config loads, never repaired.

`vite preview` inherits the same proxy.

**Types** (in build). The client's request and success types are **generated from the API's
published OpenAPI schema**, never hand-written:
- `web/openapi.json` is a committed snapshot of `app.openapi()` with prose keywords removed
  (`web/scripts/openapi_snapshot.py`), so editing a docstring does not change it.
- `web/src/api/schema.gen.ts` is generated from that snapshot by **`openapi-typescript`**. It is
  the one devDependency that is not part of setting up a tool listed above, added because
  generation is what keeps the types field-for-field with the models. It is a build-time tool;
  none of it ships.
- `npm run api:refresh` rewrites both files. Each derived copy has a pin that fails when it
  drifts: pytest checks the snapshot against the live app (`tests/gct/api/test_openapi_snapshot.py`),
  and Vitest checks the TS against the snapshot (`web/scripts/api-types.test.ts`).

The **error side is hand-written** from api.md's error-envelope section, because the published
schema misstates it. It advertises FastAPI's default 422 body and no other error status, while
every non-2xx the app produces is the envelope.

**Versions** (in build). Every tool is at its current release except TypeScript, held at
**5.9**. That is the newest release inside the peer ranges of typescript-eslint (`<6.1.0`) and
openapi-typescript (`^5.x`), and npm would install 6 or 7 with only a warning. **Node** is pinned
by `engines.node`, set to the intersection of every locked package's own range, and enforced by
`engine-strict=true` in `web/.npmrc`, so npm refuses an unsupported Node instead of warning.

**CI.** This ADR does not change `.github/workflows/ci.yml`. A Node job is approved as a separate
chore. Until it lands, the client's checks (`npm ci`, then `typecheck`, `lint`, `format:check`,
`test`, `build`) are a **local gate only**. A green `CI` check proves nothing about `web/` beyond
what pytest and ruff reach: the snapshot pin and the Python script it tests. CLAUDE.md's
*What "green" is worth* stays Python-only.

## Alternatives considered
- **CORS middleware in `src/gct/api/app.py`.** It grows the footprint into the API's composition
  root, and its allowed origins would need a V2 revisit when the frontend moves to its own host
  (ADR 0006 §Deployment).
- **An `/api` prefix with a proxy rewrite.** Every route would have two spellings (client vs
  api.md), and every future host would have to repeat the rewrite.
- **Hand-written response types.** A second writer of the routers' models, with nothing to catch
  the day they diverge.
- **Jest.** It needs its own TypeScript/ESM transform; Vitest reuses Vite's.
- **TypeScript 6 or 7.** Outside the peer ranges above.
- **`devEngines` or `.nvmrc` for the Node pin.** The npm that an older Node bundles ignores
  `devEngines`, and an older Node is the case a pin exists for. `.nvmrc` would be a second writer
  of the version.

## Consequences
- `web/` is self-contained: `web/.gitignore` covers `node_modules/` and `dist/`, and ruff and
  pytest do not descend into `web/node_modules`.
- A change to a router's models needs one more step: run `npm run api:refresh` and commit both
  files. CI's pytest names the command when that step is missed.
- The published schema's error responses are wrong (the 422 above), which is a defect in the
  API's OpenAPI and out of this ADR's scope. The client does not use the schema's error types.
- The proxy is dev-time only. How the SPA reaches the API once deployed belongs to the V2 deploy
  (ADR 0006 §Deployment), which will revisit this ADR's dev-time answer.
